"""Занятые интервалы владельца — чтобы не предлагать занятое время.

Дефект, который это закрывает: _slots() предлагал 11:00/16:00 ближайших
будней, не глядя ни на другие интервью, ни на календарь. Два рекрутёра,
получившие одинаковый слот, — это неявка на один из созвонов и потерянная
вакансия.

Два источника занятости, по убыванию надёжности:
  - interview_at_utc всех заявок в INTERVIEW_CONFIRMED и INTERVIEW_PROPOSED
    (предложенное время тоже лучше не пересекать: владелец может его
    подтвердить в любой момент);
  - Google Calendar free/busy — там живут события, о которых система не
    знает: личные дела, чужие встречи. Строго fail-open: календарь недоступен
    или медленный — работаем по данным из базы, как раньше. Ошибка сети не
    должна останавливать ответы рекрутёрам.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..config import get_settings
from ..db import session_scope
from ..models import Application, Status

_BUSY_STATUSES = (Status.INTERVIEW_CONFIRMED.value,
                  Status.INTERVIEW_PROPOSED.value)


def busy_intervals(days: int = 14) -> list:
    """[(start_utc, end_utc), ...] — наивные UTC-даты, как в базе."""
    s = get_settings()
    buf = timedelta(minutes=s.interview_busy_buffer_min)
    dur = timedelta(minutes=60)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    horizon = now + timedelta(days=days)

    out = []
    with session_scope() as sess:
        rows = sess.scalars(
            select(Application)
            .where(Application.status.in_(_BUSY_STATUSES),
                   Application.interview_at_utc.is_not(None))).all()
        for a in rows:
            start = a.interview_at_utc
            if start is None or start > horizon:
                continue
            d = timedelta(minutes=a.interview_duration_min) \
                if getattr(a, "interview_duration_min", None) else dur
            out.append((start - buf, start + d + buf))

    if s.gcal_freebusy_enabled:
        out.extend(_gcal_busy(now, horizon))
    return out


def _gcal_busy(start, end) -> list:
    """Занятость из Google Calendar. Любая ошибка — пустой список."""
    try:
        from ..schedule import gcal
        return gcal.freebusy(start, end)
    except Exception:                                    # noqa: BLE001
        return []


def is_free(slot_start, minutes: int = 60, busy: list | None = None) -> bool:
    """Свободен ли интервал [slot_start, slot_start+minutes)."""
    if busy is None:
        busy = busy_intervals()
    slot_end = slot_start + timedelta(minutes=minutes)
    for b_start, b_end in busy:
        if slot_start < b_end and b_start < slot_end:
            return False
    return True
