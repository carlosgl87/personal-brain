"""Consultas de lectura deterministas: sin LLM ni SQL generado por usuarios."""
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sqlalchemy import BigInteger, and_, cast, exists, func, or_, select
from sqlalchemy.orm import aliased, selectinload

from app.models import Area, Company, Decision, ProcessingRun, Project, Source, Task
from app.services.normalization import normalize

HELP = """Preguntas con evidencia:
 /ask texto de la pregunta
 /pregunta texto de la pregunta
 Las preguntas con signos de interrogacion usan Claude.

Gestionar tareas (UUID mostrado en /pendientes):
 /completar UUID
 /reabrir UUID
 /fecha UUID AAAA-MM-DD HH:MM (Lima)
 /responsable UUID Nombre completo
 /proyecto UUID Nombre o alias

Puedes consultar:
 /pendientes SIMA
 /pendientes Catusita
 /pendientes con Ana
 /pendientes SIMA con Ana
 /decisiones SIMA
 /resumen SIMA
 /estado SIMA
 /pendientes (todos, incluidos los que no tienen proyecto)

También: ¿Qué tengo pendiente de SIMA? o ¿Qué decidimos sobre SIMA?
Usa /nota seguido del texto para guardar una nota que parezca consulta.
Los nombres y aliases deben coincidir con el catálogo; no se crean proyectos."""


@dataclass(frozen=True)
class Query:
    kind: str
    target: str = ""
    owner: str = ""


def task_query(target):
    if target.startswith("con "):
        return Query("tasks", owner=target[4:].strip())
    scope, separator, owner = target.partition(" con ")
    return Query("tasks", scope.strip(), owner.strip() if separator else "")


def parse_query(text: str) -> Query | None:
    raw = text.strip()
    if re.match(r"^/nota(?:@[a-z0-9_]+)?(?:\s|$)", raw, re.I):
        return None
    normalized = normalize(raw)
    if raw.startswith("/"):
        command, _, arguments = raw.partition(" ")
        command = command.casefold().split("@")[0]
        target = normalize(arguments)
        if command == "/pendientes":
            return task_query(target)
        if command in {"/decisiones", "/resumen", "/estado"}:
            return Query({"/decisiones": "decisions", "/resumen": "summary", "/estado": "status"}[command], target)
        return Query("help")
    patterns = [
        ("tasks", r"^(?:que tengo|que tenemos|que hay) pendientes?(?: (?:de|en|para) (.+))?$"),
        ("owner", r"^que tengo pendientes? con (.+)$"),
        ("decisions", r"^que (?:decidimos|se decidio|decisiones hay)(?: (?:sobre|de|en) (.+))?$"),
        ("summary", r"^(?:resumen|ultimas notas|que paso en las ultimas reuniones|que paso)(?: (?:de|en|sobre) (.+))?$"),
        ("status", r"^cual es el estado(?: actual)? (?:del proyecto|de) (.+)$"),
    ]
    for kind, pattern in patterns:
        match = re.fullmatch(pattern, normalized)
        if match:
            target = match.group(1) or ""
            if kind == "owner":
                return Query("tasks", owner=target)
            return task_query(target) if kind == "tasks" else Query(kind, target)
    # Una pregunta no reconocida recibe ayuda, no extracción de tareas.
    if raw.startswith("¿") or raw.endswith("?"):
        return Query("help")
    return None


@dataclass
class Scope:
    project_ids: list | None
    label: str
    error: str | None = None


def resolve_scope(session, target: str) -> Scope:
    if not target:
        return Scope(None, "Todos los proyectos y notas sin proyecto")
    kind = None
    for prefix in ("proyecto", "empresa", "area"):
        if target.startswith(prefix + " "):
            kind, target = prefix, target[len(prefix) + 1:]
            break
    projects = session.scalars(select(Project).options(selectinload(Project.aliases))).all()
    companies = session.scalars(select(Company)).all()
    areas = session.scalars(select(Area)).all()
    matches = []
    if kind in {None, "proyecto"}:
        for p in projects:
            names = [p.name, p.slug, *(a.alias for a in p.aliases)]
            if target in {normalize(name) for name in names}:
                matches.append(Scope([p.id], "Proyecto: " + p.name))
    if kind in {None, "empresa"}:
        for company in companies:
            if target in {normalize(company.name), normalize(company.slug)}:
                matches.append(Scope([p.id for p in projects if p.company_id == company.id], "Empresa: " + company.name))
    if kind in {None, "area"}:
        for area in areas:
            if target in {normalize(area.name), normalize(area.slug)}:
                matches.append(Scope([p.id for p in projects if p.area_id == area.id], "Área: " + area.name))
    if not matches:
        return Scope([], "", "No encontré ese nombre o alias. Usa el nombre completo de un proyecto, empresa o área.")
    if len(matches) > 1:
        choices = "\n".join(match.label for match in matches[:10])
        return Scope([], "", "El nombre es ambiguo:\n" + choices + "\nPrecisa el nombre o usa proyecto, empresa o area antes del nombre.")
    return matches[0]


def message_identity(source, key):
    return func.coalesce(
        source.raw_metadata["message"][key].astext,
        source.raw_metadata["edited_message"][key].astext,
    )


def chat_identity(source):
    return func.coalesce(
        source.raw_metadata["message"]["chat"]["id"].astext,
        source.raw_metadata["edited_message"]["chat"]["id"].astext,
    )


def revision_time(source):
    # update_id puede reiniciarse aleatoriamente tras una semana de inactividad.
    return cast(func.coalesce(
        source.raw_metadata["edited_message"]["edit_date"].astext,
        source.raw_metadata["message"]["date"].astext,
        source.raw_metadata["edited_message"]["date"].astext,
    ), BigInteger)


def current_source():
    """Oculta la edición anterior solo cuando la nueva edición fue procesada."""
    newer = aliased(Source, name="newer_source")
    superseded = exists(select(newer.id).where(
        newer.source_type.in_(["telegram_text", "audio_transcript"]),
        newer.latest_processing_run_id.is_not(None),
        chat_identity(newer) == chat_identity(Source),
        message_identity(newer, "message_id") == message_identity(Source, "message_id"),
        or_(
            revision_time(newer) > revision_time(Source),
            and_(
                revision_time(newer) == revision_time(Source),
                cast(newer.raw_metadata["update_id"].astext, BigInteger)
                > cast(Source.raw_metadata["update_id"].astext, BigInteger),
            ),
        ),
    ).correlate(Source))
    parent = aliased(Source, name="audio_parent")
    current_transcript = exists(select(parent.id).where(
        parent.id == Source.parent_source_id,
        parent.latest_transcript_source_id == Source.id,
    ).correlate(Source))
    return and_(
        Source.latest_transcript_source_id.is_(None),
        or_(Source.parent_source_id.is_(None), current_transcript),
        or_(Source.source_type.not_in(["telegram_text", "audio_transcript"]), ~superseded),
    )


def current_derived(model):
    return and_(
        or_(model.processing_run_id.is_(None),
            model.processing_run_id == Source.latest_processing_run_id),
        or_(Source.id.is_(None), and_(Source.source_type != "telegram_query", current_source())),
    )


def scoped(statement, column, scope):
    return statement if scope.project_ids is None else statement.where(column.in_(scope.project_ids))


def task_statement(scope):
    statement = select(Task, Project.name).outerjoin(Project, Project.id == Task.project_id).outerjoin(Source, Source.id == Task.source_id)
    statement = statement.where(current_derived(Task), Task.status.in_(["open", "in_progress"]), Task.completed_at.is_(None))
    return scoped(statement, Task.project_id, scope)


def decision_statement(scope):
    statement = select(Decision, Project.name).outerjoin(Project, Project.id == Decision.project_id).outerjoin(Source, Source.id == Decision.source_id)
    return scoped(statement.where(current_derived(Decision)), Decision.project_id, scope)


def source_statement(scope):
    statement = select(Source, ProcessingRun).outerjoin(ProcessingRun, ProcessingRun.id == Source.latest_processing_run_id)
    statement = statement.where(Source.source_type != "telegram_query", current_source())
    return scoped(statement, Source.primary_project_id, scope)


def brief(value, size=500):
    value = " ".join(str(value).split())
    return value[:size] + ("…" if len(value) > size else "")


def date_label(value):
    return value.astimezone(ZoneInfo("America/Lima")).strftime("%d/%m/%Y %H:%M Lima") if value else "sin fecha"


def source_ref(source_id):
    return "Fuente: " + str(source_id) if source_id else "Registro manual sin fuente"


def answer_query(session, query: Query) -> str:
    if query.kind == "help":
        return HELP
    if query.kind in {"summary", "status"} and not query.target:
        return "Indica un proyecto, empresa o área.\n" + HELP
    scope = resolve_scope(session, query.target)
    if scope.error:
        return scope.error
    if query.kind == "tasks":
        statement = task_statement(scope)
        if query.owner:
            owners = session.scalars(statement.with_only_columns(Task.owner_text).distinct()).all()
            matching = [owner for owner in owners if owner and normalize(owner) == query.owner]
            if not matching:
                return scope.label + "\nNo encontré pendientes con ese responsable. Usa el nombre completo registrado."
            statement = statement.where(Task.owner_text.in_(matching))
        rows = session.execute(statement.order_by(Task.due_at.asc().nulls_last(), Task.created_at, Task.id).limit(21)).all()
        lines = [scope.label + (f" | Responsable: {query.owner}" if query.owner else ""), "Pendientes registrados:"]
        for task, name in rows[:20]:
            lines.append(f"• {brief(task.title)} [{name or 'sin proyecto'}]\n  Responsable: {brief(task.owner_text or 'sin asignar', 250)} | Fecha: {date_label(task.due_at)}\n  Tarea: {task.id}\n  {source_ref(task.source_id)}")
        if not rows:
            lines.append("No hay pendientes registrados en este alcance.")
        if len(rows) > 20:
            lines.append("Mostrando los primeros 20. Consulta un proyecto o responsable para acotar.")
        lines.append("Solo información ya estructurada; las notas sin procesar pueden contener otros pendientes.")
        return "\n".join(lines)
    if query.kind == "decisions":
        rows = session.execute(decision_statement(scope).order_by(Decision.created_at.desc(), Decision.id).limit(21)).all()
        lines = [scope.label, "Decisiones registradas:"]
        for decision, name in rows[:20]:
            lines.append(f"• {brief(decision.decision_text)} [{name or 'sin proyecto'}]\n  {source_ref(decision.source_id)}")
        if not rows:
            lines.append("No hay decisiones registradas en este alcance.")
        if len(rows) > 20:
            lines.append("Mostrando las últimas 20 decisiones; acota por proyecto.")
        return "\n".join(lines)
    if query.kind == "summary":
        rows = session.execute(source_statement(scope).order_by(Source.received_at.desc(), Source.id).limit(5)).all()
        lines = [scope.label, "Últimas 5 notas (resúmenes guardados):"]
        for source, run in rows:
            summary = run.result.get("summary", "") if run else "Sin extracción disponible; la fuente está guardada."
            lines.append(f"• {date_label(source.received_at)} — {brief(summary, 900)}\n  {source_ref(source.id)}")
        if not rows:
            lines.append("No hay notas registradas en este alcance.")
        return "\n".join(lines)
    if query.kind == "status":
        projects = session.scalars(scoped(select(Project), Project.id, scope).order_by(Project.name).limit(21)).all()
        tasks_count = session.scalar(task_statement(scope).with_only_columns(func.count(Task.id)).order_by(None))
        decisions_count = session.scalar(decision_statement(scope).with_only_columns(func.count(Decision.id)).order_by(None))
        lines = [scope.label, f"Pendientes registrados: {tasks_count or 0}", f"Decisiones registradas: {decisions_count or 0}"]
        lines.extend(f"• {p.name}: estado de catálogo = {p.status}" for p in projects[:20])
        if len(projects) > 20:
            lines.append("Mostrando los primeros 20 proyectos.")
        lines.append("El estado de catálogo no es una evaluación del avance. Usa /resumen para consultar las últimas notas.")
        return "\n".join(lines)
    return HELP


def answer_chunks(answer, limit=3500):
    # UTF-16 cuenta también los caracteres suplementarios como dos unidades.
    chunks, buffer, units = [], [], 0
    for char in answer:
        size = len(char.encode("utf-16-le")) // 2
        if units + size > limit:
            chunks.append("".join(buffer))
            buffer, units = [], 0
        buffer.append(char)
        units += size
    if buffer:
        chunks.append("".join(buffer))
    return chunks or ["No hay resultados."]
