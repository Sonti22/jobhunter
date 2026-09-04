"""Слоты не пересекаются с занятым временем.

Дефект: _slots() предлагал 11:00/16:00 будней, не глядя ни на другие
интервью, ни на календарь. Два рекрутёра с одинаковым слотом — неявка на
один из созвонов.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "busy.db")
    os.environ["TELEGRAM_BOT_TOKEN"] = "test:token"
    os.environ["LLM_ENABLED"] = "false"
    # На хосте лежит настоящий google_token.json — без этого выключателя
    # busy_intervals() уходит в реальный Google API прямо из теста.
    os.environ["GCAL_FREEBUSY_ENABLED"] = "false"
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

    from jobhunter.models import Application, Job
    with db.session_scope() as sess:
        sess.execute(delete(Application))
        sess.execute(delete(Job))
    yield


def _interview(db, when_utc, status=None):
    from jobhunter.models import Application, Job, Status
    with db.session_scope() as sess:
        job = Job(external_uuid=str(uuid.uuid4()), source="tg:test",
                  title="X", description_raw="Python")
        sess.add(job)
        sess.flush()
        sess.add(Application(job_id=job.id, score=70,
                             status=(status or Status.INTERVIEW_CONFIRMED).value,
                             interview_at_utc=when_utc.replace(tzinfo=None),
                             interview_tz="Europe/Moscow"))


def _next_workday_slot(hour):
    """Ближайший будний слот hour:00 по Москве, как его строит _slots."""
    tz = ZoneInfo("Europe/Moscow")
    day = datetime.now(tz) + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day.replace(hour=hour, minute=0, second=0, microsecond=0)


def test_confirmed_interview_blocks_its_slot(db):
    from jobhunter.convo.busy import busy_intervals
    from jobhunter.convo.reply import _slots

    taken = _next_workday_slot(11)
    _interview(db, taken.astimezone(timezone.utc))

    slots = _slots(busy=busy_intervals())
    assert taken not in slots, "занятый слот не должен предлагаться"
    assert len(slots) == 3, "недостающие добираются из следующих дней"


def test_proposed_interview_also_blocks(db):
    """Предложенное время тоже занято: владелец может подтвердить его позже."""
    from jobhunter.convo.busy import busy_intervals
    from jobhunter.convo.reply import _slots
    from jobhunter.models import Status

    taken = _next_workday_slot(16)
    _interview(db, taken.astimezone(timezone.utc),
               status=Status.INTERVIEW_PROPOSED)
    slots = _slots(busy=busy_intervals())
    assert taken not in slots


def test_gcal_failure_is_fail_open(db, monkeypatch):
    """Календарь упал — слоты строятся по данным из базы, без исключений."""
    from jobhunter.config import get_settings
    from jobhunter.convo import busy

    monkeypatch.setenv("GCAL_FREEBUSY_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr("jobhunter.schedule.gcal.freebusy",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("сеть")))
    try:
        intervals = busy.busy_intervals()
    finally:
        monkeypatch.setenv("GCAL_FREEBUSY_ENABLED", "false")
        get_settings.cache_clear()
    assert intervals == []


def test_empty_busy_is_old_behaviour(db):
    from jobhunter.convo.reply import _slots

    a = _slots(busy=[])
    assert len(a) == 3
    assert all(s.hour in (11, 16) and s.weekday() < 5 for s in a)
