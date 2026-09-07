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


def complete_reason(app_id: int, reason: str, actor_id: int) -> tuple[bool, str]:
    """Optional reason after a one-tap skip; do not repeat the queue action."""
    from .config import get_settings
    from .models import Application
    if isinstance(actor_id, bool) or actor_id not in get_settings().bot_owner_ids:
        return False, "Причина доступна только владельцу"
    if reason not in REASONS:
        return False, "Неизвестная причина"
    with session_scope() as sess:
        sess.connection().exec_driver_sql("BEGIN IMMEDIATE")
        app = sess.get(Application, app_id)
        row = sess.scalar(select(ApplicationFeedback).where(
            ApplicationFeedback.action_key == f"manual-skip:{app_id}"))
        if not app or app.outcome != "manual_tg_skipped" or row is None:
            return False, "Нет пропуска, к которому можно добавить причину"
        if row.reason != "unspecified" and row.reason != reason:
            return False, "Причина уже сохранена"
        row.reason, row.actor_id = reason, actor_id
        return True, "Причина сохранена: " + REASONS[reason]
