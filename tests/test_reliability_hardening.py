# -*- coding: utf-8 -*-
"""Регрессии для надёжности доставки, синхронизации и очередей."""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "hardening.db")
    os.environ["LLM_ENABLED"] = "false"
    os.environ["TELEGRAM_BOT_TOKEN"] = "test:token"
    os.environ["BOT_ALLOWED_USER_IDS"] = "1"
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

    from jobhunter.models import Application, BotOutbox, BotTask, Employer, Job, Message, SendLog
    with db.session_scope() as sess:
        for model in (Message, SendLog, Application, Job, Employer,
                      BotOutbox, BotTask):
            sess.execute(delete(model))


def _raw(company="Acme", external_uuid=None, content="Python FastAPI", handle="hr_acme"):
    from jobhunter.ingest.base import RawJob
    from jobhunter.models import ContactKind
    return RawJob(source="test", external_uuid=external_uuid or str(uuid.uuid4()),
                  title="Python Backend", company=company, content=content,
                  contact_kind=ContactKind.USER_HANDLE.value,
                  contact_handle=handle)


def _app(db, status, email="", handle="hr_acme"):
    from jobhunter.models import Application, ContactKind, Job
    with db.session_scope() as sess:
        job = Job(external_uuid=str(uuid.uuid4()), title="Python Backend",
                  company_name="Acme", description_raw="Python FastAPI",
                  contact_kind=(ContactKind.EMAIL.value if email
                                else ContactKind.USER_HANDLE.value),
                  contact_handle=handle,
                  contact_url=("mailto:" + email) if email else "")
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, status=status.value,
                          score=70, gate_passed=True)
        sess.add(app)
        sess.flush()
        return app.id


def test_same_description_different_companies_is_not_deduped(db):
    from jobhunter.ingest.base import save_jobs
    result = save_jobs([_raw("Acme", handle="acme_hr"),
                        _raw("Beta", handle="beta_hr")], verbose=False)
    assert result["new"] == 2


def test_existing_job_is_refreshed_and_reopened(db):
    from jobhunter.ingest.base import save_jobs
    from jobhunter.models import Job
    external = "test:%s" % uuid.uuid4()
    save_jobs([_raw(external_uuid=external, content="Python")], verbose=False)
    with db.session_scope() as sess:
        job = sess.query(Job).filter(Job.external_uuid == external).one()
        job.is_closed = True
        job.closed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    result = save_jobs([_raw(external_uuid=external, content="Python FastAPI Docker",
                             handle="new_hr")], verbose=False)
    assert result["updated"] == 1
    with db.session_scope() as sess:
        job = sess.query(Job).filter(Job.external_uuid == external).one()
        assert job.is_closed is False
        assert "FastAPI" in job.description_raw
        assert job.contact_handle == "new_hr"
        assert job.last_seen_at is not None


def test_attempted_stale_telegram_send_requires_manual_requeue(db):
    from jobhunter.models import Application, Job, Status
    from jobhunter.outreach.sender import reclaim_stale_sending, requeue_ambiguous
    with db.session_scope() as sess:
        job = Job(external_uuid=str(uuid.uuid4()), title="Python",
                  contact_handle="hr_acme")
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, status=Status.SENDING.value,
                          gate_passed=True, telegram_random_id=42,
                          send_idempotency_key="telegram:1:42",
                          sending_lease_until=datetime.now(timezone.utc)
                          .replace(tzinfo=None) - timedelta(minutes=5))
        sess.add(app)
        sess.flush()
        app_id = app.id
    assert reclaim_stale_sending() == 1
    with db.session_scope() as sess:
        assert sess.get(Application, app_id).status == Status.SEND_FAILED_AMBIGUOUS.value
    assert requeue_ambiguous(app_id)
    with db.session_scope() as sess:
        assert sess.get(Application, app_id).status == Status.APPROVED.value


def test_bot_task_is_retried_after_failure(db):
    from jobhunter.bot import state
    from jobhunter.models import BotTask
    state.task_push({"do": "task", "chat_id": 1, "task": "mail"})
    action = state.task_pop()
    assert action and action["_task_id"]
    state.task_failed(action["_task_id"], "temporary")
    with db.session_scope() as sess:
        row = sess.get(BotTask, action["_task_id"])
        assert row.status == "pending" and row.attempts == 1
        row.next_try_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
    retry = state.task_pop()
    assert retry and retry["_task_id"] == action["_task_id"]
    state.task_done(retry["_task_id"])


def test_notification_dedup_is_atomic_shape(db):
    from jobhunter import notify
    from jobhunter.models import BotOutbox
    notify.push("error", "first", dedup="same-key")
    notify.push("error", "second", dedup="same-key")
    with db.session_scope() as sess:
        rows = sess.query(BotOutbox).filter(BotOutbox.dedup_key == "same-key").all()
        assert len(rows) == 1 and rows[0].text == "first"


def test_llm_prompt_redacts_contact_data():
    from jobhunter.llm import redact_pii
    cleaned = redact_pii("Связь: suren@example.com, +7 (999) 123-45-67")
    assert "suren@example.com" not in cleaned
    assert "+7" not in cleaned
    assert "<EMAIL>" in cleaned and "<PHONE>" in cleaned


def test_raw_telegram_peer_uses_telethon_input_peer():
    from jobhunter.outreach.resolver import ResolvedPeer
    from jobhunter.outreach.sender import _telethon_input_peer

    peer = _telethon_input_peer(ResolvedPeer("hr_acme", 123, "456", "user"))
    assert peer.user_id == 123
    assert peer.access_hash == 456


def test_historical_sent_repair_returns_application_to_live(db):
    from jobhunter.models import Application, Status, utcnow
    from jobhunter.ops import repair_historical_sent

    app_id = _app(db, Status.APPROVED, handle="sent_before")
    with db.session_scope() as sess:
        sess.get(Application, app_id).sent_at = utcnow().replace(tzinfo=None)
    dry = repair_historical_sent(dry=True)
    assert dry["found"] == 1
    assert repair_historical_sent(dry=False)["repaired"] == 1
    with db.session_scope() as sess:
        assert sess.get(Application, app_id).status == Status.AWAITING_REPLY.value


def test_email_language_repair_only_resets_unsent(db):
    from jobhunter.models import Application, Job, Status
    from jobhunter.repair_queue import reset_email_language

    app_id = _app(db, Status.APPROVED, email="hr@english.io")
    with db.session_scope() as sess:
        app = sess.get(Application, app_id)
        app.cv_lang = "ru"
        sess.get(Job, app.job_id).title = "Backend Engineer"
        sess.get(Job, app.job_id).description_raw = "Python backend engineer, REST API"
    assert reset_email_language(dry=True)["найдено"] == 1
    assert reset_email_language(dry=False)["сброшено"] == 1
    with db.session_scope() as sess:
        assert sess.get(Application, app_id).status == Status.DISCOVERED.value
