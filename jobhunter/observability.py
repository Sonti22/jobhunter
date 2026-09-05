"""Наблюдаемость: факты последнего прохода без секретов и тел сообщений."""
from datetime import timezone

from .db import session_scope
from .models import RuntimeState, utcnow


def record(key: str, status: str, *, details: dict | None = None,
           error: str = "", next_run_at=None) -> None:
    if next_run_at and next_run_at.tzinfo:
        next_run_at = next_run_at.astimezone(timezone.utc).replace(tzinfo=None)
    with session_scope() as sess:
        row = sess.get(RuntimeState, key)
        if row is None:
            row = RuntimeState(key=key)
            sess.add(row)
        row.status = status
        row.error = error[:300]
        if status == "running":
            row.started_at = utcnow()
        elif status != "scheduled":
            row.finished_at = utcnow()
        row.next_run_at = next_run_at
        if details is not None:
            row.details_json = details
