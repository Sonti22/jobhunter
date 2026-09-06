"""Explicit skip feedback; reports only, never silently changes search rules."""
from collections import Counter

from sqlalchemy import select

from .db import session_scope
from .models import ApplicationFeedback

REASONS = {
    "role": "Роль", "stack": "Стек", "salary": "Зарплата", "format": "Формат",
    "company": "Компания", "other": "Другое", "unspecified": "Без причины",
}


def record(sess, app_id: int, reason: str = "unspecified", actor_id: int = 0) -> None:
    if reason not in REASONS:
        raise ValueError("Неизвестная причина пропуска")
    key = f"manual-skip:{app_id}"
    if not sess.scalar(select(ApplicationFeedback.id).where(ApplicationFeedback.action_key == key)):
        sess.add(ApplicationFeedback(application_id=app_id, reason=reason,
                                     actor_id=actor_id, action_key=key))


def summary() -> dict:
    with session_scope() as sess:
        counts = Counter(sess.scalars(select(ApplicationFeedback.reason)).all())
    return {"total": sum(counts.values()), "reasons": dict(counts),
            "note": "Причины записаны владельцем; настройки поиска автоматически не меняются."}
