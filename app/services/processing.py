from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Decision, ProcessingRun, Project, Source, Task
from app.services.claude import ExtractionError, PROMPT_VERSION, extract
from app.services.project_matching import allows_content_project, match_source_project
from app.models.task_change import TaskChange
from app.services.project_memory_events import mark_dirty
from app.services.source_revisions import revision_projects
from app.services.transcription import transcribe_audio


class HierarchicalRequired(Exception):
    pass


class SourceNotFound(ValueError):
    pass


class SourceNotProcessable(ValueError):
    pass


def run_result(run, status="processed"):
    result = run.result
    response = {
        "status": status, "source_id": str(run.source_id), "run_id": str(run.id),
        "summary": result["summary"], "tasks_count": len(result["tasks"]),
        "decisions_count": len(result["decisions"]),
    }
    if result.get("schema_version") in {"action-plan-v1", "action-plan-v2"}:
        response.update(updates_count=len(result.get("updates", [])),
                        action_plan=True, query=result.get("query"), interaction=result["interaction"],
                        project_id=result.get("execution", {}).get("project_id"),
                        completed_titles=result.get("execution", {}).get("completed_titles", []),
                        new_task_titles=[t["title"] for t in result["tasks"]],
                        ambiguities=result.get("execution", {}).get("ambiguities", []))
    if result.get("schema_version") == "action-plan-v2":
        response.update(schema_version="action-plan-v2", execution=result.get("execution", {}),
                        ambiguities=result.get("ambiguities", []))
        applied = result.get("execution", {}).get("items", [])
        response["tasks_count"] = sum(i["type"] == "tasks" and i["status"] == "applied" for i in applied)
        response["decisions_count"] = sum(i["type"] == "decisions" and i["status"] == "applied" for i in applied)
        response["updates_count"] = sum(i["type"] == "updates" and i["status"] == "applied" for i in applied)
        response["new_task_titles"] = [i["title"] for i in applied if i["type"] == "tasks" and i["status"] == "applied"]
    return response


def process_source(session: Session, source_id: UUID, settings, force=False, refresh_parts=False, action_plan=False):
    settings.llm_credentials()
    if action_plan and not force:
        raise SourceNotProcessable("--action-plan requiere un reprocesamiento explícito de una fuente.")
    if refresh_parts and not force:
        raise SourceNotProcessable("--refresh-parts requiere --reprocess.")
    with session.begin():
        kind = session.scalar(select(Source.source_type).where(Source.id == source_id))
    if kind == "telegram_audio":
        transcript_id = transcribe_audio(session, source_id, settings)
        result = process_text_source(session, transcript_id, settings, force=force,
            **(({"refresh_parts": True} if refresh_parts else {}) | ({"action_plan": True} if action_plan else {})))
        result["audio_source_id"] = str(source_id)
        result["transcript_source_id"] = str(transcript_id)
        with session.begin():
            result["project_name"] = session.scalar(
                select(Project.name).join(Source, Source.primary_project_id == Project.id)
                .where(Source.id == transcript_id)
            )
        return result
    return process_text_source(session, source_id, settings, force=force,
        **(({"refresh_parts": True} if refresh_parts else {}) | ({"action_plan": True} if action_plan else {})))


def process_text_source(session: Session, source_id: UUID, settings, force=False, refresh_parts=False, action_plan=False):
    if action_plan and not force:
        raise SourceNotProcessable("--action-plan requiere un reprocesamiento explícito de una fuente.")
    if refresh_parts and not force:
        raise SourceNotProcessable("--refresh-parts requiere --reprocess.")
    # Configuración inválida no modifica fuentes ni abre llamadas externas.
    settings.llm_credentials()
    try:
        with session.begin():
            source = session.scalar(
                select(Source).where(Source.id == source_id)
                .with_for_update().execution_options(populate_existing=True)
            )
            if source is None:
                raise SourceNotFound("Fuente no encontrada.")
            if source.source_type == "telegram_query":
                raise SourceNotProcessable("Las consultas no se procesan como notas.")
            if source.latest_processing_run_id is not None and not force:
                return run_result(session.get(ProcessingRun, source.latest_processing_run_id), "already_processed")
            if force and session.scalar(select(TaskChange.id).join(Task, Task.id == TaskChange.task_id).where(Task.source_id == source.id).limit(1)) is not None:
                raise SourceNotProcessable("La fuente tiene tareas editadas manualmente; el reprocesamiento esta bloqueado para conservar tus cambios.")
            if len(source.raw_content) > settings.extraction_max_chars:
                if settings.hierarchical_extraction_enabled:
                    raise HierarchicalRequired
                raise SourceNotProcessable("Extraccion jerarquica deshabilitada; fuente completa conservada.")
            affected = revision_projects(session, source)
            projects = session.scalars(select(Project).where(
                Project.status == "active", Project.archived_at.is_(None),
            ).options(selectinload(Project.aliases), selectinload(Project.area),
                      selectinload(Project.company))).all()
            if action_plan or source.raw_metadata.get("processing_schema") in {"action-plan-v1", "action-plan-v2"}:
                from app.services.action_context import assemble_context, assemble_v2_context
                from app.services.message_interpreter import interpret, PROMPT_VERSION as ACTION_PROMPT, PROMPT_VERSION_V2
                from app.services.action_execution import execute_plan
                try:
                    v2 = action_plan or source.raw_metadata.get("processing_schema") == "action-plan-v2"
                    context = (assemble_v2_context if v2 else assemble_context)(session, source, projects, settings)
                    plan = interpret(settings, context)
                except ValueError:
                    raise ExtractionError("Contexto demasiado grande; fuente conservada.") from None
                run = execute_plan(session, source, plan, context, settings, PROMPT_VERSION_V2 if v2 else ACTION_PROMPT)
                for previous_project in sorted(affected - {source.primary_project_id}, key=str):
                    mark_dirty(session, previous_project, settings, origin_key="run:" + str(run.id),
                               source_id=source.id, event_type="source_revision")
                return run_result(run)
            result = extract(settings, source, projects)
            # Una propuesta del LLM necesita también una coincidencia inequívoca del catálogo.
            matched = match_source_project(source, projects)
            project_id = source.primary_project_id
            if project_id is None and matched is not None and result.project_id == matched.id and allows_content_project(source):
                project_id = matched.id
            result.project_id = project_id
            _, model = settings.llm_credentials()
            run = ProcessingRun(
                id=uuid4(), source_id=source.id, provider="anthropic", model=model,
                prompt_version=PROMPT_VERSION, result=result.model_dump(mode="json"),
            )
            session.add(run)
            session.flush()
            for item in result.tasks:
                session.add(Task(
                    source_id=source.id, project_id=project_id, processing_run_id=run.id,
                    title=item.title, description=item.description, owner_text=item.owner_text,
                    due_at=item.due_at, status="open",
                ))
            for item in result.decisions:
                session.add(Decision(
                    source_id=source.id, project_id=project_id, processing_run_id=run.id,
                    decision_text=item.decision_text, decided_at=item.decided_at,
                ))
            source.primary_project_id = project_id
            source.latest_processing_run_id = run.id
            source.processing_status = "processed"
            source.processed_at = datetime.now(timezone.utc)
            affected.add(project_id)
            for affected_id in sorted(affected, key=str):
                mark_dirty(session, affected_id, settings, origin_key="run:" + str(run.id), source_id=source.id,
                    event_type="source_revision" if "edited_message" in source.raw_metadata else "processed_source")
            response = run_result(run)
        return response
    except HierarchicalRequired:
        from app.services.hierarchical import process_hierarchical
        return process_hierarchical(session, source_id, settings, force=force,
            **(({"refresh_parts": True} if refresh_parts else {}) | ({"action_plan": True} if action_plan else {})))
    except ExtractionError:
        # Mantiene el resultado previo si falla un reprocesamiento.
        with session.begin():
            source = session.scalar(select(Source).where(Source.id == source_id).with_for_update())
            if source is not None and source.latest_processing_run_id is None:
                source.processing_status = "failed"
        raise
