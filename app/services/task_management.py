"""Ediciones explícitas autenticadas por Telegram; sin llamadas a IA."""
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.models import Source, Task
from app.models.task_change import TaskChange
from app.services.normalization import normalize
from app.services.queries import current_derived, resolve_scope
from app.services.project_memory_events import mark_dirty

COMMANDS = {"/completar", "/reabrir", "/fecha", "/responsable", "/proyecto"}
HELP = """Gestionar una tarea usando su UUID (aparece en /pendientes):
/completar UUID
/reabrir UUID
/fecha UUID 2026-10-09 17:00
/fecha UUID sin fecha
/responsable UUID Ana
/responsable UUID sin asignar
/proyecto UUID SIMA
/proyecto UUID sin proyecto
Las fechas usan America/Lima. /proyecto cambia solo esa tarea."""


@dataclass(frozen=True)
class TaskCommand:
    action: str
    task_id: UUID | None
    value: str = ""
    error: str | None = None


def parse_task_command(text):
    parts = text.strip().split(maxsplit=2)
    name = parts[0].casefold().split("@")[0] if parts else ""
    if name not in COMMANDS:
        return None
    try:
        task_id = UUID(parts[1])
    except (ValueError, IndexError):
        return TaskCommand(name[1:], None, error=HELP)
    value = parts[2].strip() if len(parts) == 3 else ""
    if (name in {"/completar", "/reabrir"} and value) or (name not in {"/completar", "/reabrir"} and not value):
        return TaskCommand(name[1:], task_id, error=HELP)
    return TaskCommand(name[1:], task_id, value)


def snapshot(task):
    return {
        "status": task.status, "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "owner_text": task.owner_text, "project_id": str(task.project_id) if task.project_id else None,
    }


def edit_task(session, command, command_source_id, settings=None):
    if command.error:
        return command.error
    with session.begin():
        # Una repetición del mismo update devuelve el resultado original sin volver a editar.
        receipt = session.scalar(select(Source).where(Source.id == command_source_id).with_for_update())
        if receipt is None or receipt.source_type != "telegram_query":
            return "No se encontró el comando guardado."
        previous = session.scalar(select(TaskChange).where(TaskChange.command_source_id == command_source_id))
        if previous is not None:
            return previous.answer
        source_id = session.scalar(select(Task.source_id).where(Task.id == command.task_id))
        if source_id is not None:
            session.scalar(select(Source).where(Source.id == source_id).with_for_update())
        task = session.scalar(select(Task).outerjoin(Source, Source.id == Task.source_id).where(
            Task.id == command.task_id, current_derived(Task),
        ).with_for_update(of=Task))
        if task is None:
            return "No encontré una tarea vigente con ese UUID. Consulta /pendientes."
        before = snapshot(task)
        if command.action == "completar":
            task.status = "completed"
            task.completed_at = task.completed_at or datetime.now(timezone.utc)
        elif command.action == "reabrir":
            task.status = "open"
            task.completed_at = None
        elif command.action == "fecha":
            if normalize(command.value) == "sin fecha":
                task.due_at = None
            else:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", command.value):
                    return "Usa /fecha UUID AAAA-MM-DD HH:MM (hora de Lima), o sin fecha."
                try:
                    task.due_at = datetime.strptime(command.value, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("America/Lima"))
                except ValueError:
                    return "La fecha no es válida. Usa AAAA-MM-DD HH:MM."
        elif command.action == "responsable":
            if len(command.value) > 250:
                return "El responsable admite hasta 250 caracteres."
            task.owner_text = None if normalize(command.value) == "sin asignar" else command.value
        elif command.action == "proyecto":
            if normalize(command.value) == "sin proyecto":
                task.project_id = None
            else:
                scope = resolve_scope(session, "proyecto " + normalize(command.value))
                if scope.error:
                    return scope.error
                task.project_id = scope.project_ids[0]
        else:
            return HELP
        after = snapshot(task)
        answer = "Tarea actualizada: " + str(task.id) + "\nAcción: " + command.action + "\n" + HELP.splitlines()[-1]
        change_id = uuid4()
        session.add(TaskChange(id=change_id, task_id=task.id, command_source_id=command_source_id,
                               action=command.action, before=before, after=after, answer=answer))
        if settings is not None and before != after:
            projects = {UUID(value) for value in (before["project_id"], after["project_id"]) if value}
            for project_id in sorted(projects, key=str):
                mark_dirty(session, project_id, settings, origin_key="task:" + str(change_id),
                           source_id=task.source_id, task_id=task.id, command_source_id=command_source_id,
                           event_type="task_change")
        return answer
