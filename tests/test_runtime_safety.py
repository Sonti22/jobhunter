"""Real database races and simulated crashes; no external sends or sessions."""
import asyncio
import os
import smtplib
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from types import SimpleNamespace

import pytest
from sqlalchemy import select


@pytest.fixture
def db(tmp_path, monkeypatch):
    from jobhunter import db as dbmod
    from jobhunter.config import get_settings
    monkeypatch.setenv("DB_PATH", str(tmp_path / "runtime.db"))
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("SMTP_USER", "owner@example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "fake")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake:test")
    monkeypatch.setenv("BOT_ALLOWED_USER_IDS", "1")
    get_settings.cache_clear()
    dbmod._engine = dbmod._Session = None
    dbmod.get_engine()
    yield dbmod
    dbmod._engine.dispose()
    dbmod._engine = dbmod._Session = None
    get_settings.cache_clear()


def make_request(db, *, status="REPLIED", email=False, decision="say"):
    from jobhunter.models import Application, Job, OwnerRequest, utcnow
    with db.session_scope() as sess:
        job = Job(external_uuid="test-job", title="Python engineer",
                  description_raw="We are hiring a Python engineer. Responsibilities: backend.",
                  contact_kind="email" if email else "user_handle",
                  contact_handle="test_recruiter", contact_url="mailto:hr@company.org" if email else "",
                  posted_at=int(datetime.now(timezone.utc).timestamp()))
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, status=status, gate_passed=True,
                          score=80, message_body="Hello, Python engineer here.")
        sess.add(app)
        sess.flush()
        req = OwnerRequest(application_id=app.id, decision=decision, decision_arg="Hello",
                           answered_at=utcnow())
        sess.add(req)
        sess.flush()
        return app.id, req.id


def test_decision_lease_is_exclusive_even_with_parallel_workers(db):
    from jobhunter import decisions
    _, req = make_request(db)
    barrier = threading.Barrier(2)
    def claim():
        barrier.wait(timeout=5)
        return decisions._lease(req)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sum(r is not None for r in results) == 1
    assert req not in decisions.pending()


def test_decision_lease_recovers_before_dispatch(db):
    from jobhunter import decisions
    from jobhunter.models import OwnerRequest, utcnow
    _, req = make_request(db)
    assert decisions._lease(req)
    with db.session_scope() as sess:
        sess.get(OwnerRequest, req).next_try_at = utcnow() - timedelta(seconds=1)
    assert req in decisions.pending()
    assert decisions._lease(req)["attempts"] == 2


@pytest.mark.parametrize("decision", ["", "expired"])
def test_undecided_or_expired_request_cannot_be_leased(db, decision):
    from jobhunter import decisions
    _, req = make_request(db, decision=decision)
    assert decisions._lease(req) is None


def test_crash_after_dispatch_never_auto_repeats(db, monkeypatch):
    from jobhunter import decisions
    from jobhunter.dashboard import attention
    from jobhunter.models import OwnerRequest, utcnow
    _, req = make_request(db)
    calls = []
    class ProcessCrash(BaseException):
        pass
    async def accepted_then_crashed(*args, **kwargs):
        calls.append(1)
        raise ProcessCrash()
    monkeypatch.setattr("jobhunter.convo.send.send_reply", accepted_then_crashed)
    with pytest.raises(ProcessCrash):
        asyncio.run(decisions.apply_one(None, req))
    with db.session_scope() as sess:
        row = sess.get(OwnerRequest, req)
        assert row.apply_error == decisions.DELIVERY_UNCONFIRMED and row.applied_at is None
        row.next_try_at = utcnow() - timedelta(hours=1)
    assert req not in decisions.pending()
    assert asyncio.run(decisions.apply_one(None, req)) == "уже исполнено"
    assert calls == [1]
    item = attention()["items"][0]
    assert not item["can_open_card"] and "не подтверждена" in item["reason"]


def test_stop_does_not_book_interview_or_consume_attempt(db, monkeypatch):
    from jobhunter import decisions
    from jobhunter.models import OwnerRequest
    _, req = make_request(db, decision="ok")
    monkeypatch.setattr("jobhunter.convo.send.can_reply", lambda sess: (False, "manual-only"))
    monkeypatch.setattr("jobhunter.schedule.book.confirm", lambda *a: pytest.fail("No calendar write"))
    assert asyncio.run(decisions.apply_one(None, req)) == "stop:manual-only"
    with db.session_scope() as sess:
        row = sess.get(OwnerRequest, req)
        assert row.attempts == 0 and row.applied_at is None and row.next_try_at


def test_decision_dry_run_has_no_effect(db, monkeypatch):
    from jobhunter import decisions
    from jobhunter.models import OwnerRequest
    _, req = make_request(db, decision="ok")
    monkeypatch.setattr("jobhunter.schedule.book.confirm", lambda *a: pytest.fail("No calendar write"))
    assert asyncio.run(decisions.apply_one(None, req, dry=True)) == "dry"
    with db.session_scope() as sess:
        row = sess.get(OwnerRequest, req)
        assert row.attempts == 0 and row.applied_at is None and row.next_try_at is None


def test_owner_dry_command_does_not_queue_real_decision(db):
    from jobhunter import owner
    from jobhunter.models import OwnerRequest
    app, req = make_request(db, decision="")
    result = asyncio.run(owner.apply_command(None, {"cmd": "say", "app_id": app, "arg": "Hi"}, dry=True))
    assert "dry" in result
    with db.session_scope() as sess:
        assert sess.get(OwnerRequest, req).decision == ""


@pytest.mark.parametrize("command", ["stop", "go"])
def test_owner_dry_command_does_not_toggle_stop(db, monkeypatch, command):
    from jobhunter import owner
    monkeypatch.setattr(owner, "set_kill_switch", lambda *a: pytest.fail("Must not toggle"))
    assert "dry" in asyncio.run(owner.apply_command(None, {"cmd": command}, dry=True))


def test_decisions_dry_queue_does_not_open_telegram(db, monkeypatch):
    from jobhunter import decisions
    make_request(db)
    monkeypatch.setattr("telethon.TelegramClient", lambda *a, **kw: pytest.fail("Must not open Telegram"))
    assert asyncio.run(decisions.run(dry=True))["dry_run"]


def test_recorded_answer_is_not_resent(db, monkeypatch):
    from jobhunter import decisions
    from jobhunter.models import Message, utcnow
    app, req = make_request(db)
    with db.session_scope() as sess:
        sess.add(Message(application_id=app, direction="out", sent_at=utcnow()+timedelta(seconds=1)))
    monkeypatch.setattr("jobhunter.convo.send.send_reply", lambda *a: pytest.fail("Duplicate"))
    assert asyncio.run(decisions.apply_one(None, req)) == "дубль предотвращён"


def test_outbox_claim_is_atomic_across_workers(db):
    from jobhunter import notify
    from jobhunter.models import BotOutbox
    with db.session_scope() as sess:
        sess.add_all([BotOutbox(text="test") for _ in range(30)])
    barrier = threading.Barrier(2)
    def claim():
        barrier.wait(timeout=5)
        return {r["id"] for r in notify.claim_pending(20)}
    with ThreadPoolExecutor(2) as pool:
        first, second = list(pool.map(lambda _: claim(), range(2)))
    assert not first & second and len(first | second) == 30
    assert not notify.claim_pending()


def test_outbox_recovers_lease_after_restart(db):
    from jobhunter import notify
    from jobhunter.models import BotOutbox, utcnow
    with db.session_scope() as sess:
        row = BotOutbox(text="test", claimed_at=utcnow()-timedelta(hours=1))
        sess.add(row)
        sess.flush()
        row_id = row.id
    assert [r["id"] for r in notify.claim_pending()] == [row_id]
    notify.mark_sent(row_id, 1, 1)
    assert not notify.claim_pending()


def test_outbox_no_owner_does_not_claim(db, monkeypatch):
    from jobhunter.bot import outbox
    monkeypatch.setattr(outbox, "get_settings", lambda: SimpleNamespace(bot_owner_ids=set()))
    monkeypatch.setattr(outbox.notify, "claim_pending", lambda *a: pytest.fail("Should not claim"))
    assert outbox.drain() == 0


@pytest.mark.parametrize("fail", [False, True])
def test_outbox_delivery_ack_or_retry(db, monkeypatch, fail):
    from jobhunter import notify
    from jobhunter.bot import outbox
    from jobhunter.models import BotOutbox
    notify.push("error", "test", chat_id=1)
    def send(*a, **kw):
        if fail:
            raise RuntimeError("temporary")
        return {"message_id": 42}
    monkeypatch.setattr(outbox.api, "send_message", send)
    assert outbox.drain() == (0 if fail else 1)
    with db.session_scope() as sess:
        row = sess.scalar(select(BotOutbox))
        assert bool(row.sent_at) != fail and row.claimed_at is None
        assert row.attempts == int(fail)


def test_os_lock_released_after_hard_process_exit(tmp_path):
    from jobhunter.locking import FileLock, LockBusy
    path = tmp_path / "worker.lock"
    code = ("import os,sys; from pathlib import Path; from jobhunter.locking import FileLock; "
            "lock=FileLock(Path(sys.argv[1])); lock.__enter__(); os._exit(0)")
    subprocess.run([sys.executable, "-c", code, str(path)], check=True, timeout=10)
    with FileLock(path):
        with pytest.raises(LockBusy), FileLock(path):
            pass
    with FileLock(path):
        pass


@pytest.mark.skipif(os.name != "nt", reason="Windows process API")
def test_windows_pid_check_never_sends_a_signal(monkeypatch):
    from jobhunter.outreach import sender
    monkeypatch.setattr(sender.os, "kill", lambda *a: pytest.fail("Must not signal on Windows"))
    assert sender._pid_alive(os.getpid())
    assert not sender._pid_alive(0)


def test_mailer_serializes_whole_batches(db, monkeypatch):
    from jobhunter.outreach import mailer
    calls = []
    def run(*a, **kw):
        calls.append(1)
        assert mailer.send_batch(1, False) == 1
        return 0
    monkeypatch.setattr(mailer, "_send_batch", run)
    assert mailer.send_batch(1, False) == 0 and calls == [1]


@pytest.mark.parametrize("failure,status", [
    (None, "AWAITING_REPLY"), (TimeoutError("unknown delivery"), "SEND_FAILED_AMBIGUOUS"),
    (smtplib.SMTPResponseException(421, b"retry later"), "SEND_FAILED"),
])
def test_mail_batch_records_transport_outcome(db, monkeypatch, failure, status):
    from jobhunter.models import Application, Message
    from jobhunter.outreach import mailer, policy
    app_id, _ = make_request(db, status="APPROVED", email=True)
    calls = []
    class SMTP:
        def __init__(self, *a, **kw):
            pass
        def starttls(self, **kw):
            pass
        def login(self, *a):
            pass
        def send_message(self, msg):
            calls.append(msg)
            if failure:
                raise failure
        def quit(self):
            pass
    def message(**kwargs):
        msg = EmailMessage()
        msg["Message-ID"] = kwargs["message_id"]
        msg.set_content(kwargs["body"])
        return msg
    monkeypatch.setattr(mailer.smtplib, "SMTP", SMTP)
    monkeypatch.setattr(mailer, "build_message", message)
    monkeypatch.setattr(policy, "within_send_window", lambda *a: True)
    assert mailer.send_batch(1, False) == int(failure is not None)
    assert len(calls) == 1
    with db.session_scope() as sess:
        app = sess.get(Application, app_id)
        assert app.status == status
        outgoing = sess.scalar(select(Message))
        assert bool(outgoing.sent_at) == (failure is None)
    assert mailer.pick_batch(1) == []


def test_email_reply_rechecks_withdrawal_after_build(db, monkeypatch):
    from jobhunter.convo import send_email
    from jobhunter.models import Application
    app_id, _ = make_request(db, email=True)
    def message(**kw):
        with db.session_scope() as sess:
            sess.get(Application, app_id).status = "WITHDRAWN"
        return EmailMessage()
    monkeypatch.setattr("jobhunter.outreach.mailer.build_message", message)
    monkeypatch.setattr(send_email, "_send_blocking", lambda *a: pytest.fail("Withdrawn"))
    assert asyncio.run(send_email.send_reply_email(app_id, "Hello", is_auto=False)).startswith("skipped:")


@pytest.mark.parametrize("result,expected", [
    ({"ok": True}, "ok"), ({"error": "network"}, "error"),
    ({"sources": {"telegram": {"scan_errors": 1}}}, "error"),
    ({"blocked": "manual-only"}, "blocked"),
])
def test_scheduler_marks_only_success(tmp_path, monkeypatch, result, expected):
    from jobhunter import scheduled
    records = []
    monkeypatch.setattr(scheduled, "record", lambda *a, **kw: records.append(a))
    wrapped = scheduled.wrap_marked(lambda: result, "test", tmp_path)
    assert wrapped() == result
    assert records[-1] == ("task:test", expected)
    assert (tmp_path / "last_test.txt").exists() == (expected == "ok")
    assert not (tmp_path / "start_test.txt").exists()
    if expected == "ok":
        assert wrapped() is None


def test_scheduler_recovers_stale_marker_and_retries_exception(tmp_path, monkeypatch):
    from jobhunter import scheduled
    monkeypatch.setattr(scheduled, "record", lambda *a, **kw: None)
    (tmp_path / "start_test.txt").write_text(datetime.now().isoformat())
    calls = []
    def work():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("crash")
        return {"ok": True}
    wrapped = scheduled.wrap_marked(work, "test", tmp_path)
    with pytest.raises(RuntimeError):
        wrapped()
    assert not (tmp_path / "last_test.txt").exists()
    assert wrapped() == {"ok": True}


def test_scheduler_prevents_overlapping_execution(tmp_path, monkeypatch):
    from jobhunter import scheduled
    monkeypatch.setattr(scheduled, "record", lambda *a, **kw: None)
    calls = []
    def work():
        calls.append(1)
        assert wrapped()["blocked"]
        return {"ok": True}
    wrapped = scheduled.wrap_marked(work, "test", tmp_path)
    wrapped()
    assert calls == [1]


def test_legacy_reading_counter_is_unknown_not_zero(db):
    from jobhunter.dashboard import reading
    from jobhunter.models import TelegramChannelStat
    with db.session_scope() as sess:
        sess.add(TelegramChannelStat(username="legacy", last_posts=60, last_vacancies=20))
    assert reading()["channels"][0]["rejected"] is None
