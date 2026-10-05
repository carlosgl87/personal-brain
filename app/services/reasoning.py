"""Recuperación permitida, acotada y trazable; no interpreta SQL."""
import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import Area, Company, Decision, Project, ReasoningRun, Source, Task
from app.services.embeddings import EmbeddingError, embedding_ready
from app.services.memory import MemoryError, semantic_search
from app.services.normalization import normalize
from app.services.queries import Scope, current_derived, resolve_scope, scoped, source_statement, task_statement, decision_statement
from app.services.reasoning_llm import PLANNER_VERSION, ReasoningError, plan_question, synthesize

MAX_CHUNKS = 8


def reasoning_question(text):
    raw = text.strip()
    if raw.startswith("/"):
        name, _, question = raw.partition(" ")
        if name.casefold().split("@")[0] in {"/ask", "/pregunta"}:
            return question.strip()
        return None
    return raw if raw.startswith("¿") or raw.endswith("?") else None


def catalog_context(session):
    projects = session.scalars(select(Project).options(selectinload(Project.aliases))).all()
    companies = session.scalars(select(Company)).all()
    areas = session.scalars(select(Area)).all()
    return {"projects": [{"id": str(p.id), "name": p.name, "aliases": [a.alias for a in p.aliases],
                         "status": p.status, "company_id": str(p.company_id) if p.company_id else None,
                         "area_id": str(p.area_id)} for p in projects],
            "companies": [{"id": str(c.id), "name": c.name} for c in companies],
            "areas": [{"id": str(a.id), "name": a.name} for a in areas]}


def plan_scope(session, plan):
    if plan.scope_type in {None, "global"}:
        return Scope(None, "Todos los proyectos y notas sin proyecto")
    kind = {"project": "proyecto", "company": "empresa", "area": "area"}[plan.scope_type]
    return resolve_scope(session, kind + " " + normalize(plan.scope_value))


def date_filter(statement, column, plan):
    if plan.date_from:
        statement = statement.where(column >= plan.date_from)
    if plan.date_to:
        statement = statement.where(column <= plan.date_to)
    return statement


def clipped(text, limit):
    text = text or ""
    return {"text": text[:limit], "truncated": len(text) > limit}


def retrieve(session, plan, settings):
    scope = plan_scope(session, plan)
    if scope.error:
        raise MemoryError(scope.error)
    context = {"scope": scope.label, "tasks": [], "decisions": [], "recent_sources": [],
               "chunks": [], "warnings": [], "projects": []}
    projects = session.scalars(scoped(select(Project), Project.id, scope).order_by(Project.name).limit(51)).all()
    context["projects"] = [{"id": str(p.id), "name": p.name, "catalog_status": p.status} for p in projects[:50]]
    if len(projects) > 50:
        context["warnings"].append("Catálogo limitado a 50 proyectos.")
    if plan.include_tasks:
        if plan.include_completed_tasks:
            statement = select(Task, Project.name).outerjoin(Project, Project.id == Task.project_id).outerjoin(
                Source, Source.id == Task.source_id).where(current_derived(Task))
            statement = scoped(statement, Task.project_id, scope)
        else:
            statement = task_statement(scope)
        rows = session.execute(date_filter(statement, Task.due_at, plan).order_by(
            Task.due_at.asc().nulls_last(), Task.id).limit(21)).all()
        for task, name in rows[:20]:
            context["tasks"].append({"task_id": str(task.id), "source_id": str(task.source_id) if task.source_id else None,
                "run_id": str(task.processing_run_id) if task.processing_run_id else None,
                "title": task.title, "description": clipped(task.description, 500), "owner": task.owner_text,
                "status": task.status, "due_at": task.due_at.isoformat() if task.due_at else None,
                "project": name, "completed_at": task.completed_at.isoformat() if task.completed_at else None})
        if len(rows) > 20:
            context["warnings"].append("Solo se recuperaron 20 tareas; hay más.")
    if plan.include_decisions:
        statement = date_filter(decision_statement(scope), Decision.decided_at, plan)
        rows = session.execute(statement.order_by(Decision.created_at.desc(), Decision.id).limit(21)).all()
        for decision, name in rows[:20]:
            context["decisions"].append({"decision_id": str(decision.id), "source_id": str(decision.source_id) if decision.source_id else None,
                "run_id": str(decision.processing_run_id) if decision.processing_run_id else None,
                "text": clipped(decision.decision_text, 1500), "project": name,
                "decided_at": decision.decided_at.isoformat() if decision.decided_at else None})
        if len(rows) > 20:
            context["warnings"].append("Solo se recuperaron 20 decisiones; hay más.")
    if plan.include_recent_sources:
        statement = date_filter(source_statement(scope), Source.received_at, plan)
        rows = session.execute(statement.order_by(Source.received_at.desc(), Source.id).limit(plan.recent_sources_limit + 1)).all()
        for source, run in rows[:plan.recent_sources_limit]:
            context["recent_sources"].append({"source_id": str(source.id), "received_at": source.received_at.isoformat(),
                "run_id": str(run.id) if run else None, "source_type": source.source_type,
                "summary": clipped(run.result.get("summary", "") if run else "", 1500),
                "excerpt": clipped(source.raw_content, 2000)})
        if len(rows) > plan.recent_sources_limit:
            context["warnings"].append("Las notas recientes están limitadas por el plan.")
    if plan.semantic_queries:
        if not embedding_ready(settings):
            context["warnings"].append("Búsqueda semántica no disponible: configura embeddings y ejecuta backfill.")
        else:
            found = {}
            try:
                for question in plan.semantic_queries:
                    for item in semantic_search(session, question, settings, project_ids=scope.project_ids,
                                date_from=plan.date_from, date_to=plan.date_to, limit=MAX_CHUNKS):
                        previous = found.get(item["chunk_id"])
                        if previous is None or item["distance"] < previous["distance"]:
                            found[item["chunk_id"]] = item
                context["chunks"] = sorted(found.values(), key=lambda x: (x["distance"], x["chunk_id"]))[:MAX_CHUNKS]
                if not context["chunks"]:
                    context["warnings"].append("No se encontraron chunks indexados para el alcance y modelo activos.")
            except EmbeddingError:
                context["warnings"].append("Falló la búsqueda semántica; la respuesta usa únicamente evidencia estructurada y reciente.")
    context["warnings"].append("Recuperación acotada; no implica cobertura completa de toda la historia.")
    if len(json.dumps(context, ensure_ascii=False)) > 130000:
        raise MemoryError("El contexto excedió el límite seguro. Acota la pregunta a un proyecto.")
    return context


def trace_context(context):
    result = {key: value for key, value in context.items() if key not in {"chunks", "recent_sources"}}
    result["chunks"] = [{key: value for key, value in chunk.items() if key != "content"} |
                       {"content_sha256": hashlib.sha256(chunk["content"].encode()).hexdigest()}
                       for chunk in context["chunks"]]
    result["recent_sources"] = [{"source_id": s["source_id"], "run_id": s["run_id"],
                                 "received_at": s["received_at"],
                                 "excerpt_chars": len(s["excerpt"]["text"]),
                                 "excerpt_sha256": hashlib.sha256(s["excerpt"]["text"].encode()).hexdigest()}
                                for s in context["recent_sources"]]
    return result


def answer_reasoning(session, question, question_source_id, settings):
    if not question.strip():
        return "Usa /ask seguido de una pregunta."
    if len(question) > 3000:
        return "La pregunta admite hasta 3000 caracteres; acótala."
    try:
        with session.begin():
            source = session.scalar(select(Source).where(Source.id == UUID(str(question_source_id))).with_for_update())
            if source is None or source.source_type != "telegram_query":
                raise ReasoningError("La pregunta no está guardada como consulta.")
            cached = session.scalar(select(ReasoningRun).where(ReasoningRun.question_source_id == source.id).order_by(
                ReasoningRun.created_at.desc(), ReasoningRun.id).limit(1))
            if cached is not None:
                return cached.answer
            key, model = settings.llm_credentials()
            now = datetime.now(timezone.utc)
            plan = plan_question(question, catalog_context(session), now, settings)
            context = retrieve(session, plan, settings)
            if not any(context[k] for k in ("tasks", "decisions", "recent_sources", "chunks")):
                answer = "No encontré evidencia suficiente en el alcance consultado. Puede haber fuentes todavía sin indexar."
            else:
                answer = synthesize(question, context, settings)
                if any("semántica" in warning or "embeddings" in warning or "indexados" in warning for warning in context["warnings"]):
                    answer += "\n\nCobertura semántica incompleta; revisa configuración e indexación."
            session.add(ReasoningRun(id=uuid4(), question_source_id=source.id, provider="anthropic",
                model=model, planner_version=PLANNER_VERSION, plan=plan.model_dump(mode="json"),
                retrieved_context=trace_context(context), answer=answer))
            return answer
    except MemoryError as exc:
        return str(exc)
    except (ReasoningError, ValueError):
        return "No se pudo completar el razonamiento. Revisa configuración y disponibilidad; la pregunta se conserva. Reenvíala para reintentar."
