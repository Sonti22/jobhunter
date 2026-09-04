"""Напоминания о встречах в бот.

Google Calendar шлёт свои напоминания, но они приходят туда, где их легко
пропустить. Бот — то место, куда владелец и так смотрит по этой системе, и
там же лежит контекст: с кем разговор, по какой вакансии, что подготовить.

Главное свойство — каждое касание ровно один раз. Напомнить дважды об одной
встрече хуже, чем не напомнить: на второй раз перестают читать.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "rem.db")
    os.environ["TELEGRAM_BOT_TOKEN"] = "test:token"
    os.environ["BOT_ALLOWED_USER_IDS"] = "5875908057"
    os.environ["LLM_ENABLED"] = "false"
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    import jobhunter.db as dbmod
    dbmod._engine = None
    dbmod._Session = None
    yield dbmod
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def clean(db):
    from sqlalchemy import delete

    from jobhunter.models import (
        Application,
        BotOutbox,
        Employer,
        Job,
        Message,
        OwnerRequest,
        SendLog,
    )
    with db.session_scope() as sess:
        for model in (Message, SendLog, OwnerRequest, Application, Job,
                      Employer, BotOutbox):
            sess.execute(delete(model))
    yield


def _interview(db, *, when, tz="Europe/Moscow", link="", cv=""):
    from jobhunter.models import Application, ContactKind, Job, Status
    with db.session_scope() as sess:
        job = Job(external_uuid=str(uuid.uuid4()), source="tg:test",
                  title="Python Backend", company_name="Acme",
                  contact_kind=ContactKind.USER_HANDLE.value,
                  contact_handle="hr_acme",
                  description_raw="Python, FastAPI, Docker, Kubernetes")
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, score=80,
                          status=Status.INTERVIEW_CONFIRMED.value,
                          interview_at_utc=when.replace(tzinfo=None),
                          interview_tz=tz, gcal_link=link, cv_path=cv)
        sess.add(app)
        sess.flush()
        return app.id


def test_day_before_reminder_has_context(db):
    """За сутки — со временем, контактом, ссылкой на календарь и подготовкой."""
    from jobhunter.schedule.remind import upcoming

    now = datetime.now(timezone.utc)
    _interview(db, when=now + timedelta(hours=24),
               link="https://calendar.google.com/event?eid=abc",
               cv="cv_base/Акопян_Сурен_Python.pdf")

    kinds = {k: t for _, k, t in upcoming(now)}
    assert "iv24" in kinds
    text = kinds["iv24"]
    assert "завтра" in text
    assert "Python Backend" in text and "Acme" in text
    assert "@hr_acme" in text
    assert "calendar.google.com" in text
    assert "Подготовиться" in text, "план подготовки — половина пользы"
    assert "Акопян" in text, "напомнить, каким резюме откликались"


def test_hour_before_is_short(db):
    """За час подготовку уже не читают — только время и с кем."""
    from jobhunter.schedule.remind import upcoming

    now = datetime.now(timezone.utc)
    _interview(db, when=now + timedelta(hours=1))

    kinds = {k: t for _, k, t in upcoming(now)}
    assert "iv1" in kinds
    assert "через час" in kinds["iv1"]
    assert "Подготовиться" not in kinds["iv1"]


def test_no_reminder_far_from_window(db):
    from jobhunter.schedule.remind import upcoming

    now = datetime.now(timezone.utc)
    _interview(db, when=now + timedelta(hours=8))
    assert upcoming(now) == []


def test_past_interview_asks_how_it_went(db):
    from jobhunter.schedule.remind import upcoming

    now = datetime.now(timezone.utc)
    _interview(db, when=now - timedelta(hours=3))

    kinds = {k for _, k, _ in upcoming(now)}
    assert kinds == {"ivdone"}


def test_each_touch_fires_once(db):
    """Повторный прогон не должен слать то же самое второй раз."""
    from jobhunter import notify
    from jobhunter.schedule.remind import run

    now = datetime.now(timezone.utc)
    _interview(db, when=now + timedelta(hours=24))

    assert run(now)["reminders"] == 1
    assert run(now)["reminders"] == 0, "дедуп не сработал — придёт дубль"
    rows = [r for r in notify.pending(50) if r["kind"] == "iv24"]
    assert len(rows) == 1


def test_reminder_time_in_owner_timezone(db):
    """Время показывается в поясе владельца, а не в UTC."""
    from jobhunter.schedule.remind import upcoming

    # Ровно сутки, без округления минут: округление вниз сдвигало точку
    # напоминания на случайные 0-59 минут назад, и тест падал в зависимости
    # от того, в какую минуту его запустили.
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    when = now + timedelta(hours=24)
    _interview(db, when=when)

    text = {k: t for _, k, t in upcoming(now)}["iv24"]
    from zoneinfo import ZoneInfo
    local_hour = when.astimezone(ZoneInfo("Europe/Moscow")).strftime("%H:%M")
    assert local_hour in text
