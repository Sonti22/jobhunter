"""Подтверждение интервью: БД → Google Calendar → .ics → план подготовки.

Точка входа одна — confirm(). Её вызывает owner.py, когда владелец ответил
/ok или /time. Прямых вызовов из автоматики нет и быть не должно: встреча
назначается только человеком.

Порядок важен: сначала запись в БД (это источник правды), затем календарь.
Если Google недоступен — интервью всё равно подтверждено, а событие уедет
при следующей синхронизации; обратный порядок оставил бы событие в
календаре без записи в базе.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from ..config import get_settings
from ..convo.slots import fmt
from ..db import session_scope
from ..models import Application, Job, Status, utcnow


def _description(job: Job, app: Application, prep: list) -> str:
    contact = ("@" + job.contact_handle) if job.contact_handle else (job.contact_url or "")
    lines = ["Вакансия: %s" % (job.title or job.tag or "—"),
             "Контакт: %s" % contact,
             "Источник: %s   Скор: %.0f" % (job.source, app.score)]
    if app.cv_path:
        lines.append("Отправленное резюме: %s" % app.cv_path)
    if prep:
        lines += ["", "Подготовка:"] + ["— " + x for x in prep]
    return "\n".join(lines)


def confirm(app_id: int, dt_utc: datetime, tz_name: str = "",
            duration_min: int | None = None) -> dict:
    """Записывает подтверждённое интервью и заводит событие в календаре.

    Возвращает {"calendar": человекочитаемый статус, "meet_link": ссылка}.
    """
    s = get_settings()
    tz_name = tz_name or s.owner_tz
    duration = duration_min or s.interview_duration_min
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)

    with session_scope() as sess:
        app = sess.get(Application, app_id)
        if not app:
            return {"calendar": "заявка не найдена", "meet_link": ""}
        job = sess.get(Job, app.job_id)
        if not app.advance(Status.INTERVIEW_CONFIRMED):
            return {"calendar": "статус %s не даёт подтвердить интервью"
                                % app.status, "meet_link": ""}
        app.interview_at_utc = dt_utc.astimezone(timezone.utc).replace(tzinfo=None)
        app.interview_tz = tz_name
        app.interview_duration_min = duration
        app.updated_at = utcnow()
        from .. import notify
        notify.push("interview_set",
                    "🗓 Интервью назначено: %s\n%s — @%s"
                    % (fmt(dt_utc, tz_name), (job.title or job.tag or "")[:60],
                       job.contact_handle or "?"),
                    dedup="interview:%d:%s" % (app_id, app.interview_at_utc),
                    sess=sess)
        title = job.title or job.tag or "Интервью"
        event_id = app.gcal_event_id
        from .ics import prep_plan
        prep = prep_plan(job)
        description = _description(job, app, prep)

    result = {"calendar": "", "meet_link": ""}
    try:
        from . import gcal
        ok, why = gcal.available()
        if not ok:
            result["calendar"] = "только .ics (%s)" % why
        else:
            ev = gcal.upsert_event("Интервью — %s" % title, dt_utc, duration,
                                   description, event_id, tz_name)
            with session_scope() as sess:
                app = sess.get(Application, app_id)
                app.gcal_event_id = ev.get("id", "")
                app.gcal_link = ev.get("link", "")
            result["calendar"] = ev.get("link") or "создано"
            result["meet_link"] = ev.get("meet", "")
    except Exception as e:
        result["calendar"] = "ошибка Google Calendar: %s" % type(e).__name__

    try:
        from .ics import build
        build()
    except Exception:
        pass

    result["when"] = fmt(dt_utc, tz_name)
    return result


def sync_calendar() -> dict:
    """Догоняет календарь: подтверждённые интервью без события в Google.

    Нужна, когда в момент подтверждения не было сети или авторизации.
    """
    from . import gcal
    ok, why = gcal.available()
    if not ok:
        return {"synced": 0, "reason": why}

    s = get_settings()
    todo = []
    with session_scope() as sess:
        rows = sess.scalars(
            select(Application)
            .where(Application.status == Status.INTERVIEW_CONFIRMED.value,
                   Application.interview_at_utc.is_not(None),
                   Application.gcal_event_id == "")).all()
        for a in rows:
            job = sess.get(Job, a.job_id)
            from .ics import prep_plan
            todo.append((a.id, job.title or job.tag or "Интервью",
                         a.interview_at_utc.replace(tzinfo=timezone.utc),
                         a.interview_duration_min or s.interview_duration_min,
                         _description(job, a, prep_plan(job)),
                         a.interview_tz or s.owner_tz))

    done = 0
    for app_id, title, dt, dur, desc, tz_name in todo:
        try:
            ev = gcal.upsert_event("Интервью — %s" % title, dt, dur, desc, "", tz_name)
        except Exception:
            continue
        with session_scope() as sess:
            app = sess.get(Application, app_id)
            app.gcal_event_id = ev.get("id", "")
            app.gcal_link = ev.get("link", "")
        done += 1
    return {"synced": done, "reason": ""}
