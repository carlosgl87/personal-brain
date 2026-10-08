"""Read-only bounded context from a previous generated task list in the same chat."""
from datetime import timedelta
from sqlalchemy import select, func
from app.models import ReasoningRun, Source
from app.services.queries import chat_identity

MAX_CONTEXT_TASKS = 100
MAX_CONTEXT_CHARS = 16000
RECENCY = timedelta(hours=6)


def conversation_identity(source):
    metadata = source.raw_metadata or {}
    message = metadata.get('message') or metadata.get('edited_message') or {}
    chat, user = message.get('chat', {}), message.get('from', {})
    if chat.get('type') != 'private' or chat.get('id') is None or user.get('id') is None:
        return None
    return str(chat['id']), str(user['id'])


def recent_task_response(session, source):
    identity = conversation_identity(source)
    if identity is None:
        return None
    user_identity = func.coalesce(Source.raw_metadata['message']['from']['id'].astext,
                                 Source.raw_metadata['edited_message']['from']['id'].astext)
    run = session.scalar(select(ReasoningRun).join(Source, Source.id == ReasoningRun.question_source_id).where(
        chat_identity(Source) == identity[0], user_identity == identity[1], Source.id != source.id,
        ReasoningRun.created_at >= source.received_at - RECENCY,
        ReasoningRun.created_at <= source.received_at,
        ReasoningRun.retrieved_context['shown_tasks'][0].is_not(None))
        .order_by(ReasoningRun.created_at.desc(), ReasoningRun.id.desc()).limit(1))
    if run is None:
        return None
    import json
    shown = run.retrieved_context.get('shown_tasks', [])
    selected, size = [], 0
    for task in shown[:MAX_CONTEXT_TASKS]:
        item_size = len(json.dumps(task, ensure_ascii=False))
        if size + item_size > MAX_CONTEXT_CHARS:
            break
        selected.append(dict(task))
        size += item_size
    return {'tasks': selected, 'listed_at': run.created_at.isoformat(),
        'response_source_id': str(run.question_source_id), 'complete': len(selected) == len(shown),
        'role': 'previous_display_not_current_state'}
