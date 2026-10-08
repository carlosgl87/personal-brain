"""Save first; apply one interpretation; answer only after commit."""
from uuid import UUID
from app.schemas.action_plan import InterpretedQuery
from app.services.telegram_ingestion import ingest_update
from app.services.processing import process_source, SourceNotProcessable
from app.services.claude import ExtractionError
from app.services.reasoning import answer_reasoning
from app.services.memory import maybe_index
from app.models import Project, Source


def respond_to_plan(session, result, settings):
    if result.get("schema_version") == "action-plan-v2":
        return respond_v2(session, result, settings)
    with session.begin():
        project = session.get(Project, UUID(result["project_id"])) if result.get("project_id") else None
        label = project.name if project else "sin proyecto identificado"
    parts = [] if result.get("interaction") == "query" else ["Nota guardada: " + label + "."]
    parts.extend("✅ Completada: " + title for title in result.get("completed_titles", []))
    parts.extend("📌 Nueva tarea: " + title for title in result.get("new_task_titles", []))
    parts.extend(result.get("ambiguities", []))
    if result.get("query"):
        query = InterpretedQuery.model_validate(result["query"])
        parts.append(answer_reasoning(session, query.question, result["source_id"], settings,
                                      interpreted_plan=query.retrieval))
    return "\n".join(parts)


def handle_message(session, update, user_id, settings):
    saved = ingest_update(session, update, user_id, interpret=True)
    if saved["status"] not in {"saved", "duplicate"}:
        return saved
    if saved["status"] == "duplicate":
        with session.begin():
            original = session.get(Source, UUID(saved["source_id"]))
            legacy = original.raw_metadata.get("processing_schema") not in {"action-plan-v1", "action-plan-v2"}
            old_query = original.source_type == "telegram_query"
            text = original.raw_content
        if legacy:
            answer = answer_reasoning(session, text, saved["source_id"], settings) if old_query else "Nota ya guardada; se conserva su procesamiento histórico."
            return {"status": "answered", "source_id": saved["source_id"], "answer": answer}
    try:
        result = process_source(session, UUID(saved["source_id"]), settings)
    except (ExtractionError, SourceNotProcessable, ValueError):
        return {"status": "answered", "source_id": saved["source_id"],
                "answer": "Mensaje guardado. No se aplicaron acciones; no pude completar la interpretación. Puedes reintentar."}
    answer = respond_to_plan(session, result, settings)
    if result.get("action_plan") and result.get("interaction") != "query":
        maybe_index(session, UUID(saved["source_id"]), settings)
    return {"status": "answered", "source_id": saved["source_id"], "answer": answer}


def respond_v2(session, result, settings):
    from app.schemas.action_plan import ScopedQuery
    from app.services.message_interpreter import PROMPT_VERSION_V2
    groups, clarifications = {}, []
    for item in result.get("execution", {}).get("items", []):
        if item["status"] in {"applied", "completed", "already_applied"}:
            pid = item.get("project_id")
            prefix = {"tasks": "Nueva tarea: ", "completed_task": "Completada: ",
                      "updates": "Hecho: ", "decisions": "Decision: "}[item["type"]]
            groups.setdefault(pid, []).append(prefix + item["title"])
            if item.get("reason") == "unresolved_project":
                clarifications.append("Precisa el proyecto de «" + item["title"] + "».")
        elif item["type"] == "completed_task" and item.get("reason") not in {"not_performed", "duplicate_in_plan"}:
            choices = [item["title"], *(a["title"] for a in item.get("alternatives", []))]
            clarifications.append("No complete «" + item["title"] + "». Confirma el pendiente: " + "; ".join(choices))
    with session.begin():
        labels = {}
        for pid in groups:
            project = session.get(Project, UUID(pid)) if pid else None
            labels[pid] = project.name if project else "Sin proyecto identificado"
    parts = [labels[pid] + "\n" + "\n".join(items) for pid, items in groups.items()]
    parts.extend(dict.fromkeys(clarifications))
    if result.get("query"):
        query = ScopedQuery.model_validate(result["query"])
        if query.ambiguities:
            parts.append("Precisa el alcance de la consulta: " + "; ".join(a.message for a in query.ambiguities))
        else:
            parts.append(answer_reasoning(session, query.question, result["source_id"], settings,
                                          interpreted_plan=query.retrieval, interpreter_version=PROMPT_VERSION_V2))
    return "\n\n".join(parts) or "Nota guardada."
