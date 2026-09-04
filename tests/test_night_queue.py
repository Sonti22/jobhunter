"""Ночная очередь: сообщение, пришедшее вне окна ответа, не теряется.

Найденный дефект: водяной знак входящих сдвигался в момент чтения, а
автоответ «откладывался» простым return — то есть навсегда. Рекрутёр,
написавший в 22:30, не получал ответа вообще, и по логу это выглядело
как «отложен».

Решение перепринимается утром заново, поэтому очередь хранит входящий
текст, а не сочинённый ночью ответ.
"""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "night.db")
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
        PendingReply,
        SendLog,
    )
    with db.session_scope() as sess:
        for model in (Message, SendLog, OwnerRequest, PendingReply,
                      Application, Job, Employer, BotOutbox):
            sess.execute(delete(model))
    yield


def _app(db, status=None):
    from jobhunter.models import Application, ContactKind, Job, Status
    with db.session_scope() as sess:
        job = Job(external_uuid=str(uuid.uuid4()), source="tg:test",
                  title="Python Backend", company_name="Acme",
                  contact_kind=ContactKind.USER_HANDLE.value,
                  contact_handle="hr_acme",
                  description_raw="Python, FastAPI")
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, score=80,
                          status=(status or Status.AWAITING_REPLY).value)
        sess.add(app)
        sess.flush()
        return app.id


def _pending(db):
    from sqlalchemy import select

    from jobhunter.models import PendingReply
    with db.session_scope() as sess:
        return [(r.application_id, r.incoming_text, r.processed_at)
                for r in sess.scalars(select(PendingReply)).all()]


def test_night_message_is_queued_not_lost(db, monkeypatch):
    from jobhunter.convo import engine

    monkeypatch.setattr("jobhunter.convo.engine.within_reply_window",
                        lambda: False)
    app_id = _app(db)
    res = asyncio.run(engine.handle_message(
        None, app_id, "Пришлите, пожалуйста, ваше резюме", dry=True))
    assert "в очереди" in res
    rows = _pending(db)
    assert len(rows) == 1 and rows[0][0] == app_id
    assert rows[0][2] is None, "не должно быть помечено обработанным"


def test_duplicate_night_text_not_requeued(db, monkeypatch):
    from jobhunter.convo import engine

    monkeypatch.setattr("jobhunter.convo.engine.within_reply_window",
                        lambda: False)
    app_id = _app(db)
    for _ in range(2):
        asyncio.run(engine.handle_message(
            None, app_id, "Пришлите резюме", dry=True))
    assert len(_pending(db)) == 1


def test_morning_drain_sends_reply(db, monkeypatch):
    from jobhunter.convo import engine
    from jobhunter.models import Application, Status

    monkeypatch.setattr("jobhunter.convo.engine.within_reply_window",
                        lambda: False)
    app_id = _app(db)
    asyncio.run(engine.handle_message(None, app_id, "Пришлите резюме", dry=True))

    monkeypatch.setattr("jobhunter.convo.engine.within_reply_window",
                        lambda: True)
    done = asyncio.run(engine.drain_pending(None, dry=True))
    assert done == 1
    rows = _pending(db)
    assert rows[0][2] is not None, "строка должна быть помечена обработанной"
    with db.session_scope() as sess:
        assert sess.get(Application, app_id).status == Status.IN_DIALOGUE.value


def test_drain_merges_with_fresh_morning_text(db, monkeypatch):
    """Ночной и утренний текст одной заявки — ОДНО решение, не два ответа."""
    from jobhunter.convo import engine

    monkeypatch.setattr("jobhunter.convo.engine.within_reply_window",
                        lambda: False)
    app_id = _app(db)
    asyncio.run(engine.handle_message(None, app_id, "Пришлите резюме", dry=True))

    monkeypatch.setattr("jobhunter.convo.engine.within_reply_window",
                        lambda: True)
    seen = []
    orig = engine.handle_message

    async def spy(client, aid, text, dry=False):
        seen.append(text)
        return await orig(client, aid, text, dry=dry)

    monkeypatch.setattr("jobhunter.convo.engine.handle_message", spy)
    fresh = {app_id: "Добрый день! Ждём резюме сегодня"}
    done = asyncio.run(engine.drain_pending(None, dry=True, fresh_by_app=fresh))
    assert done == 1
    assert len(seen) == 1
    assert "Пришлите резюме" in seen[0] and "Ждём резюме сегодня" in seen[0]
    assert app_id not in fresh, "заявка должна быть изъята из свежих"


def test_drain_respects_needs_human_latch(db, monkeypatch):
    """За ночь заявку эскалировали — утром автоматика молчит."""
    from jobhunter.convo import engine
    from jobhunter.models import Application, Status

    monkeypatch.setattr("jobhunter.convo.engine.within_reply_window",
                        lambda: False)
    app_id = _app(db)
    asyncio.run(engine.handle_message(None, app_id, "Пришлите резюме", dry=True))
    with db.session_scope() as sess:
        sess.get(Application, app_id).status = Status.NEEDS_HUMAN.value

    monkeypatch.setattr("jobhunter.convo.engine.within_reply_window",
                        lambda: True)
    sent = []

    async def no_send(client, aid, text, attach_cv=False, dry=False):
        sent.append(text)
        return "ok"

    monkeypatch.setattr("jobhunter.convo.engine.send_reply", no_send)
    asyncio.run(engine.drain_pending(None, dry=True))
    assert sent == [], "защёлка NEEDS_HUMAN должна пережить ночь"


def test_stale_entry_becomes_card_not_autoreply(db, monkeypatch):
    from sqlalchemy import select

    from jobhunter.convo import engine
    from jobhunter.models import OwnerRequest, PendingReply

    monkeypatch.setattr("jobhunter.convo.engine.within_reply_window",
                        lambda: False)
    app_id = _app(db)
    asyncio.run(engine.handle_message(None, app_id, "Пришлите резюме", dry=True))
    with db.session_scope() as sess:
        row = sess.scalars(select(PendingReply)).first()
        row.created_at = (datetime.now(timezone.utc).replace(tzinfo=None)
                          - timedelta(hours=30))

    monkeypatch.setattr("jobhunter.convo.engine.within_reply_window",
                        lambda: True)
    sent = []

    async def no_send(client, aid, text, attach_cv=False, dry=False):
        sent.append(text)
        return "ok"

    monkeypatch.setattr("jobhunter.convo.engine.send_reply", no_send)
    asyncio.run(engine.drain_pending(None, dry=True))
    assert sent == []
    with db.session_scope() as sess:
        assert sess.scalars(select(OwnerRequest)).first() is not None, \
            "просроченное — карточка владельцу"
