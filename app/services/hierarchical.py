"""Small committed stages; source locks serialize concurrent paid calls."""
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import (Decision, DecisionEvidence, ProcessingRun, ProcessingRunPart,
                        Project, Source, SourceChunk, Task, TaskEvidence, TaskChange, ExtractionGeneration, GenerationPart)
from app.services.claude import ExtractionError
from app.services.hierarchical_llm import (CONSOLIDATION_PROMPT_VERSION, PART_PROMPT_VERSION,
                                           consolidate, extract_part)
from app.services.memory import ensure_source_chunks
from app.services.project_matching import allows_content_project, match_project
from app.services.project_memory_events import mark_dirty
from app.services.source_revisions import revision_projects


def source_snapshot(source):
    return SimpleNamespace(**{key: getattr(source, key) for key in
        ("id", "raw_content", "raw_metadata", "received_at", "source_type", "primary_project_id")})


def part_snapshot(part):
    return SimpleNamespace(**{key: getattr(part, key) for key in
        ("id", "source_chunk_id", "part_index", "result")},
        base_part_id=getattr(part, "base_part_id", part.id), generation_id=getattr(part, "generation_id", None))


def checked_source(session, source_id, force):
    from app.services.processing import SourceNotFound, SourceNotProcessable
    source = session.scalar(select(Source).where(Source.id == source_id).with_for_update()
                            .execution_options(populate_existing=True))
    if source is None:
        raise SourceNotFound("Fuente no encontrada.")
    revision_projects(session, source)
    if source.source_type == "telegram_query":
        raise SourceNotProcessable("Las consultas no se procesan como notas.")
    if force:
        # Task commands lock these same rows; manual edits cannot slip in during consolidation.
        session.scalars(select(Task).where(Task.source_id == source_id).with_for_update()).all()
    if force and session.scalar(select(TaskChange.id).join(Task, Task.id == TaskChange.task_id)
                                .where(Task.source_id == source_id).limit(1)) is not None:
        raise SourceNotProcessable("La fuente tiene tareas editadas manualmente; reprocesamiento bloqueado.")
    return source


def concurrent_result(session, source, previous_run_id):
    from app.services.processing import run_result
    if source.latest_processing_run_id != previous_run_id:
        return run_result(session.get(ProcessingRun, source.latest_processing_run_id), "already_processed")
    return None


def stage_status(source, status):
    # During a reprocess, previous successful extraction remains current and usable.
    if source.latest_processing_run_id is None:
        source.processing_status = status


def process_hierarchical(session, source_id, settings, force=False, refresh_parts=False):
    from app.services.processing import SourceNotProcessable, run_result
    _, model = settings.llm_credentials()
    if not settings.hierarchical_extraction_enabled:
        raise SourceNotProcessable("Extraccion jerarquica deshabilitada; fuente completa conservada.")
    generation_id = None
    try:
        with session.begin():
            source = checked_source(session, source_id, force)
            if source.latest_processing_run_id is not None and not force:
                return run_result(session.get(ProcessingRun, source.latest_processing_run_id), "already_processed")
            previous_run_id = source.latest_processing_run_id
            if refresh_parts and not force:
                raise SourceNotProcessable("--refresh-parts requiere --reprocess.")
            snapshot = source_snapshot(source)
            version = ensure_source_chunks(session, source, settings)
            chunks = session.scalars(select(SourceChunk).where(SourceChunk.source_id == source_id,
                SourceChunk.chunk_version == version).order_by(SourceChunk.chunk_index)).all()
            chunk_ids = [chunk.id for chunk in chunks]
            if refresh_parts:
                generation = session.scalar(select(ExtractionGeneration).where(
                    ExtractionGeneration.source_id == source_id, ExtractionGeneration.chunk_version == version,
                    ExtractionGeneration.model == model, ExtractionGeneration.prompt_version == PART_PROMPT_VERSION,
                    ExtractionGeneration.previous_run_id == previous_run_id,
                    ExtractionGeneration.status.in_(["pending", "failed"])).order_by(ExtractionGeneration.created_at.desc()).limit(1))
                if generation is None:
                    generation = ExtractionGeneration(id=uuid4(), source_id=source_id, chunk_version=version,
                        model=model, prompt_version=PART_PROMPT_VERSION, previous_run_id=previous_run_id, status="pending")
                    session.add(generation)
                    session.flush()
                generation_id = generation.id
            stage_status(source, "processing_parts")
        if not chunk_ids:
            raise SourceNotProcessable("Fuente sin chunks textuales; original conservado.")
        if len(chunk_ids) > settings.hierarchical_max_chunks:
            raise SourceNotProcessable("Fuente guardada y chunks generados, sin truncar: excede HIERARCHICAL_MAX_CHUNKS. Requiere otro tratamiento.")
        parts = []
        for index, chunk_id in enumerate(chunk_ids):
            with session.begin():
                source = checked_source(session, source_id, force)
                cached_run = concurrent_result(session, source, previous_run_id)
                if cached_run:
                    return cached_run
                part = session.scalar(select(ProcessingRunPart).where(
                    ProcessingRunPart.source_id == source_id, ProcessingRunPart.source_chunk_id == chunk_id,
                    ProcessingRunPart.chunk_version == version, ProcessingRunPart.model == model,
                    ProcessingRunPart.prompt_version == PART_PROMPT_VERSION))
                refreshed = None
                if generation_id is not None:
                    refreshed = session.scalar(select(GenerationPart).where(
                        GenerationPart.generation_id == generation_id, GenerationPart.source_chunk_id == chunk_id))
                elif part is not None:
                    refreshed = session.scalar(select(GenerationPart).join(ExtractionGeneration).where(
                        GenerationPart.base_part_id == part.id, ExtractionGeneration.status == "processed"
                    ).order_by(GenerationPart.created_at.desc()).limit(1))
                if (generation_id is not None and refreshed is None) or part is None:
                    chunk = session.scalar(select(SourceChunk).where(SourceChunk.id == chunk_id))
                    result = extract_part(settings, snapshot, chunk)
                    if part is None:
                        part = ProcessingRunPart(id=uuid4(), source_id=source_id, source_chunk_id=chunk_id,
                            chunk_version=version, part_index=index, provider="anthropic", model=model,
                            prompt_version=PART_PROMPT_VERSION, result=result)
                        session.add(part)
                        session.flush()
                    if generation_id is not None:
                        refreshed = GenerationPart(id=uuid4(), generation_id=generation_id, base_part_id=part.id,
                            source_chunk_id=chunk_id, part_index=index, result=result)
                        session.add(refreshed)
                        session.flush()
                parts.append(part_snapshot(refreshed or part))
        with session.begin():
            source = checked_source(session, source_id, force)
            stage_status(source, "parts_complete")
        with session.begin():
            source = checked_source(session, source_id, force)
            stage_status(source, "consolidating")
        # Final evidence and all definitive rows commit atomically.
        with session.begin():
            source = checked_source(session, source_id, force)
            cached_run = concurrent_result(session, source, previous_run_id)
            if cached_run:
                return cached_run
            affected = revision_projects(session, source)
            projects = session.scalars(select(Project).where(
                Project.status == "active", Project.archived_at.is_(None)).options(
                    selectinload(Project.aliases), selectinload(Project.area), selectinload(Project.company))).all()
            result, provenance = consolidate(settings, snapshot, parts, projects)
            matched = match_project(source.raw_content, projects)
            project_id = source.primary_project_id
            if project_id is None and matched is not None and result.project_id == matched.id and allows_content_project(source):
                project_id = matched.id
            result.project_id = project_id
            stored = result.model_dump(mode="json") | {
                "extraction_mode": "hierarchical", "chunk_version": version,
                "partial_prompt_version": PART_PROMPT_VERSION,
                "part_ids": [str(part.id) for part in parts],
                "generation_id": str(generation_id) if generation_id else None,
                "candidate_dispositions": provenance["dispositions"]}
            run = ProcessingRun(id=uuid4(), source_id=source_id, provider="anthropic", model=model,
                                prompt_version=CONSOLIDATION_PROMPT_VERSION, result=stored)
            session.add(run)
            session.flush()
            for kind, klass, evidence_class, fk in (
                ("tasks", Task, TaskEvidence, "task_id"),
                ("decisions", Decision, DecisionEvidence, "decision_id"),
            ):
                for index, item in enumerate(getattr(result, kind)):
                    values = item.model_dump(exclude={"evidence"})
                    if kind == "tasks":
                        values["status"] = "open"
                    row = klass(id=uuid4(), source_id=source_id, project_id=project_id,
                                processing_run_id=run.id, **values)
                    session.add(row)
                    session.flush()
                    for location in provenance[kind][index]:
                        session.add(evidence_class(id=uuid4(), **{fk: row.id},
                            source_chunk_id=UUID(location["source_chunk_id"]),
                            processing_run_part_id=UUID(location["processing_run_part_id"]),
                            generation_part_id=UUID(location["generation_part_id"]) if location.get("generation_part_id") else None,
                            evidence=location["evidence"], char_start=location["char_start"],
                            char_end=location["char_end"]))
            source.primary_project_id = project_id
            source.latest_processing_run_id = run.id
            source.processing_status = "processed"
            source.processed_at = datetime.now(timezone.utc)
            affected.add(project_id)
            for affected_id in sorted(affected, key=str):
                mark_dirty(session, affected_id, settings, origin_key="run:" + str(run.id), source_id=source_id,
                    event_type="source_revision" if "edited_message" in source.raw_metadata else "processed_source")
            if generation_id:
                generation = session.get(ExtractionGeneration, generation_id)
                generation.status = "processed"
                generation.completed_at = datetime.now(timezone.utc)
            return run_result(run)
    except (ExtractionError, SourceNotProcessable):
        with session.begin():
            source = session.scalar(select(Source).where(Source.id == source_id).with_for_update())
            if source is not None:
                stage_status(source, "failed")
            if generation_id:
                generation = session.get(ExtractionGeneration, generation_id)
                generation.status = "failed"
        raise
