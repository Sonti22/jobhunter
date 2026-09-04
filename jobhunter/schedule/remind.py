"""Напоминания о предстоящих интервью — в бот владельцу.

Зачем отдельно от Google Calendar. Календарь уже шлёт свои напоминания за
сутки и за час, но они приходят туда, где владелец может их не увидеть:
уведомление Google легко утонуть среди прочих, а телефон бывает беззвучным.
Бот — то место, куда владелец и так смотрит по этой системе, и там же лежит
контекст: с кем разговор, по какой вакансии, где резюме, что подготовить.

Три касания, каждое ровно один раз (ключ дедупликации — заявка плюс вид):
  за сутки   — успеть подготовиться, посмотреть материалы
  за час     — собраться, проверить связь
  через 2 ч  — «как прошло?» и перевод заявки в следующий статус

Проверка идёт каждые полчаса, поэтому окна взяты с запасом: попасть точно в
минуту нельзя, а напомнить дважды — хуже, чем на двадцать минут раньше.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .. import notify
from ..config import get_settings
from ..convo.slots import fmt
from ..db import session_scope
from ..models import Application, Job, Status

log = logging.getLogger("remind")

# (вид, за сколько до встречи, ширина окна)
BEFORE = [
    ("iv24", timedelta(hours=24), timedelta(minutes=40)),
    ("iv1", timedelta(hours=1), timedelta(minutes=20)),
]
# Через сколько после начала спросить, как прошло.
AFTER = timedelta(hours=2)


def _prep_lines(job: Job, limit: int = 4) -> list:
    """Короткий план подготовки — тот же, что уходит в описание события."""
    try:
        from .ics import prep_plan
        return prep_plan(job)[:limit]
    except Exception:
        return []


def upcoming(now: datetime | None = None) -> list:
    """Что напомнить прямо сейчас: [(app_id, вид, текст)]."""
    s = get_settings()
    now = now or datetime.now(timezone.utc)
    out = []

    with session_scope() as sess:
        rows = sess.scalars(
            select(Application)
            .where(Application.status == Status.INTERVIEW_CONFIRMED.value,
                   Application.interview_at_utc.is_not(None))).all()
        for app in rows:
            when = app.interview_at_utc.replace(tzinfo=timezone.utc)
            job = sess.get(Job, app.job_id)
            title = (job.title or job.tag or "интервью") if job else "интервью"
            company = (job.company_name or "") if job else ""
            who = ("@" + job.contact_handle) if job and job.contact_handle else ""
            tz_name = app.interview_tz or s.owner_tz
            head = "%s%s" % (title, " · " + company if company else "")

            for kind, delta, window in BEFORE:
                target = when - delta
                if abs((now - target).total_seconds()) > window.total_seconds():
                    continue
                if now > when:
                    continue
                left = "завтра" if kind == "iv24" else "через час"
                lines = ["🔔 Интервью %s: %s" % (left, fmt(when, tz_name)),
                         head]
                if who:
                    lines.append(who)
                if app.gcal_link:
                    lines.append(app.gcal_link)
                if kind == "iv24":
                    prep = _prep_lines(job)
                    if prep:
                        lines += ["", "Подготовиться:"] + ["• " + x for x in prep]
                if app.cv_path:
                    from pathlib import Path
                    lines.append("Резюме: %s" % Path(app.cv_path).name)
                out.append((app.id, kind, "\n".join(lines)))

            after_at = when + AFTER
            if now >= after_at and (now - after_at) <= timedelta(hours=6):
                out.append((app.id, "ivdone",
                            "❓ Как прошло интервью — %s?\n%s\n\n"
                            "Ответь в чате, я обновлю статус заявки."
                            % (head, fmt(when, tz_name))))
    return out


def run(now: datetime | None = None) -> dict:
    """Ставит напоминания в очередь бота. Дедуп не даёт повторить касание."""
    sent = 0
    for app_id, kind, text in upcoming(now):
        before = len(notify.pending(200))
        notify.push(kind, text, dedup="%s:%d" % (kind, app_id))
        if len(notify.pending(200)) > before:
            sent += 1
    if sent:
        log.info("напоминаний о встречах: %d", sent)
    return {"reminders": sent}
