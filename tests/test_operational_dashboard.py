"""Регрессии из аудита: реальные границы партий, статусы и рабочие связи."""
import asyncio
import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "audit.db"))
    monkeypatch.setenv("MAX_VACANCY_AGE_DAYS", "7")
    monkeypatch.setenv("LLM_ENABLED", "false")
    import jobhunter.db as db
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    db._engine = None
    db._Session = None
    yield db
    if db._engine is not None:
        db._engine.dispose()
    db._engine = None
    db._Session = None
    get_settings.cache_clear()


def make_app(db, *, days=1, content="Вакансия Python developer, @hr_company",
             followup=False, employer=False, status="APPROVED"):
    from jobhunter.models import Application, ContactKind, Employer, Job
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with db.session_scope() as sess:
        emp = Employer(handle_norm="hr_company", last_contacted_at=now-timedelta(days=4)) if employer else None
        if emp:
            sess.add(emp)
            sess.flush()
        job = Job(external_uuid=str(random.random()), source="tg:demo", title="Python developer",
                  description_raw=content, posted_at=int((now.replace(tzinfo=timezone.utc)-timedelta(days=days)).timestamp()),
                  contact_kind=ContactKind.USER_HANDLE.value, contact_handle="hr_company")
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, employer_id=emp.id if emp else None, status=status,
                          score=80, gate_passed=True, message_body="Здравствуйте! Python developer.",
                          sent_at=now-timedelta(days=4) if followup else None,
                          followup_body="Вакансия ещё актуальна?" if followup else "")
        sess.add(app)
        sess.flush()
        return app.id


@pytest.mark.parametrize("text", [
    "#резюме #devops Обо мне: 7 лет опыта. Ожидания по зарплате от 300000. Рассматриваю вакансии. @hr_candidate",
    "#резюме Senior SRE. Мой опыт: Kubernetes. Требования к работодателю: удалённая работа. @hr_candidate",
    "#cv DevOps. Looking for a job. My requirements: remote position, salary 5000 USD. @candidate",
])
def test_candidate_describing_requirements_is_not_vacancy(text):
    from jobhunter.ingest.tgchannels import _is_vacancy
    assert not _is_vacancy(text)


def test_contradictory_resume_heading_does_not_trigger_outreach():
    from jobhunter.ingest.tgchannels import _is_vacancy
    assert not _is_vacancy("#резюме #devops Ищем DevOps-инженера для нашей команды. "
                       "Требования: Python, Kubernetes. Удалённо, зарплата 300000. @hr_company")


def test_media_post_does_not_shift_vacancy_identity(monkeypatch):
    from jobhunter.ingest import tgchannels as tg
    monkeypatch.setattr(tg, "_verified_channels", lambda: [])
    monkeypatch.setattr(tg, "_discovered", lambda: [])
    page = ('<div data-post="demo/1"><time datetime="2026-09-01T00:00:00Z"></time></div>'
            '<div data-post="demo/2"><div class="tgme_widget_message_text">'
            'Вакансия <b>Python developer</b>, @hr_company</div>'
            '<time datetime="2026-09-05T00:00:00Z"></time></div>')
    with tg.TelegramChannelSource(channels=["demo"], throttle=0) as src:
        monkeypatch.setattr(src, "_fetch", lambda *args: page)
        jobs = list(src.iter_jobs(pages_per_channel=1))
    assert len(jobs) == 1
    assert jobs[0].external_uuid == "tg:demo/2"
    assert jobs[0].posted_at == int(datetime(2026, 9, 5, tzinfo=timezone.utc).timestamp())
    assert "Python developer" in jobs[0].content


def test_old_approved_job_is_excluded(isolated_db):
    from jobhunter.outreach.sender import pick_batch
    make_app(isolated_db, days=20)
    assert pick_batch(10) == []


def test_followup_with_employer_bypasses_only_cold_cooldown(isolated_db):
    from jobhunter.models import Employer
    from jobhunter.outreach.sender import pick_batch
    app_id = make_app(isolated_db, followup=True, employer=True)
    assert [i["app_id"] for i in pick_batch(10)] == [app_id]
    with isolated_db.session_scope() as sess:
        sess.scalar(select(Employer)).do_not_contact = True
    assert pick_batch(10) == []


def test_reply_after_batch_selection_prevents_send(isolated_db):
    from jobhunter.models import Application
    from jobhunter.outreach import sender
    app_id = make_app(isolated_db)
    item = sender.pick_batch(10)[0]
    with isolated_db.session_scope() as sess:
        sess.get(Application, app_id).last_inbound_at = datetime.now(timezone.utc)
    result = asyncio.run(sender.send_one(None, item, random.Random(1), dry=False))
    assert result.startswith("skipped:")
    with isolated_db.session_scope() as sess:
        assert sess.get(Application, app_id).send_attempts == 0


def test_dry_send_does_not_create_delivery_evidence(isolated_db):
    from jobhunter.models import Application
    from jobhunter.outreach import sender
    app_id = make_app(isolated_db)
    assert asyncio.run(sender.send_one(None, sender.pick_batch(1)[0], random.Random(1), dry=True)) == "ok"
    with isolated_db.session_scope() as sess:
        app = sess.get(Application, app_id)
        assert app.send_attempts == 0
        assert app.telegram_random_id is None
        assert app.status == "APPROVED"


def test_imap_overflow_processes_oldest_first(monkeypatch):
    from jobhunter.convo import imapbox
    monkeypatch.setattr(imapbox, "get_settings", lambda: SimpleNamespace(imap_folder="INBOX", imap_max_fetch=2))
    monkeypatch.setattr(imapbox, "_state", lambda: (1, 10))
    monkeypatch.setattr(imapbox, "_uidvalidity", lambda *args: 1)
    conn = SimpleNamespace(select=lambda *args, **kw: ("OK", []),
                           uid=lambda *args: ("OK", [b"11 12 13 14 15"]))
    assert imapbox.new_uids(conn)[0] == [11, 12]
    assert conn._jobhunter_pending_count == 5


def test_missing_imap_header_does_not_advance_boundary(monkeypatch, isolated_db):
    from jobhunter.convo import imapbox, inbox_email
    from jobhunter.outreach.policy import get_state
    monkeypatch.setattr(imapbox, "connect", lambda: SimpleNamespace(logout=lambda: None))
    monkeypatch.setattr(imapbox, "new_uids", lambda conn: ([11, 12], 1, False))
    monkeypatch.setattr(imapbox, "fetch_headers", lambda *args: (_ for _ in ()).throw(imapbox.MailboxError("missing header")))
    with isolated_db.session_scope() as sess:
        get_state(sess).imap_last_uid = 10
    result = asyncio.run(inbox_email.process())
    assert result["error"]
    with isolated_db.session_scope() as sess:
        assert get_state(sess).imap_last_uid == 10


def test_fetch_headers_rejects_partial_reply():
    from jobhunter.convo import imapbox
    conn = SimpleNamespace(uid=lambda *args: ("OK", [(b'1 (UID 11 {3}', b'From: x@example.com\r\n')]))
    with pytest.raises(imapbox.MailboxError):
        imapbox.fetch_headers(conn, [11, 12])


def test_telegram_inbox_reads_from_oldest(isolated_db, monkeypatch):
    from jobhunter.convo import engine
    make_app(isolated_db, status="AWAITING_REPLY")
    class Client:
        async def iter_messages(self, handle, *, limit, min_id, reverse):
            assert reverse is True
            for mid in range(min_id+1, 31):
                yield SimpleNamespace(id=mid, out=False, message=str(mid), date=datetime.now(timezone.utc))
    async def no_sleep(*args):
        pass
    monkeypatch.setattr(engine.asyncio, "sleep", no_sleep)
    progress = {}
    result = asyncio.run(engine.fetch_incoming(Client(), progress))
    assert len(result[0][1]) == 30
    assert result[0][1][0][0] == 1
    assert progress["cursors"][result[0][0]] == 30


def test_stopped_owner_decision_remains_pending(isolated_db):
    from jobhunter import decisions
    from jobhunter.models import OwnerRequest
    app_id = make_app(isolated_db, status="NEEDS_HUMAN")
    with isolated_db.session_scope() as sess:
        req = OwnerRequest(application_id=app_id, kind="needs_human", decision="send", attempts=1)
        sess.add(req)
        sess.flush()
        rid = req.id
    decisions.finish(rid, False, "not sent", "stop:manual mode")
    with isolated_db.session_scope() as sess:
        req = sess.get(OwnerRequest, rid)
        assert req.applied_at is None
        assert req.next_try_at is not None
        assert req.attempts == 0
    assert rid not in decisions.pending()


def test_expired_request_is_visible_in_attention(isolated_db):
    from jobhunter.dashboard import attention
    from jobhunter.models import OwnerRequest
    app_id = make_app(isolated_db, status="NEEDS_HUMAN")
    with isolated_db.session_scope() as sess:
        sess.add(OwnerRequest(application_id=app_id, decision="expired"))
    data = attention()
    assert data["total"] == 1
    assert "истекла" in data["items"][0]["reason"]


def test_operational_pages_and_apis(isolated_db):
    from fastapi.testclient import TestClient

    from jobhunter.web.server import app
    make_app(isolated_db, days=20)
    with TestClient(app) as client:
        for path in ("sending", "attention", "reading", "outcomes"):
            assert client.get("/"+path).status_code == 200
            assert client.get("/api/"+path).status_code == 200
        data = client.get("/api/sending").json()
        assert data["items"][0]["code"] == "stale"
        assert data["eligible"] == 0
        assert data["items"][0]["next_attempt"] is None
        assert client.get("/api/reading").json()["gmail"]["details"]["remaining"] is None


def test_email_approved_followup_keeps_inbox_context(isolated_db):
    from jobhunter.convo.inbox_email import build_context
    from jobhunter.models import Application, ContactKind, Job
    app_id = make_app(isolated_db, followup=True)
    with isolated_db.session_scope() as sess:
        job = sess.get(Job, sess.get(Application, app_id).job_id)
        job.contact_kind = ContactKind.EMAIL.value
        job.contact_url = "mailto:hr@example.com"
    assert build_context().by_employer["hr@example.com"] == [app_id]


def test_approved_followup_reply_cancels_sending(isolated_db):
    from jobhunter.convo.engine import store_incoming
    from jobhunter.models import Application
    app_id = make_app(isolated_db, followup=True)
    store_incoming(app_id, [(51, "Добрый день! Пришлите резюме", datetime.now(timezone.utc))])
    with isolated_db.session_scope() as sess:
        app = sess.get(Application, app_id)
        assert app.status == "REPLIED"
        assert app.followup_body == ""


def test_email_processing_crash_stays_visible_without_blind_resend(isolated_db, monkeypatch):
    from jobhunter.convo import inbox_email
    from jobhunter.dashboard import attention
    from jobhunter.models import Message
    app_id = make_app(isolated_db, status="AWAITING_REPLY")
    calls = []
    async def fail(*args, **kwargs):
        calls.append(True)
        raise RuntimeError("processing interrupted")
    monkeypatch.setattr(inbox_email, "handle_message", fail)
    headers = {"message-id": "<reply-51@example.com>", "from": "hr@example.com"}
    with pytest.raises(RuntimeError):
        asyncio.run(inbox_email._handle_one(app_id, 51, headers, "message_id", "Ответ"))
    assert asyncio.run(inbox_email._handle_one(app_id, 51, headers, "message_id", "Ответ")) == "уже было"
    assert len(calls) == 1
    with isolated_db.session_scope() as sess:
        msg = sess.scalar(select(Message).where(Message.direction == "in"))
        assert msg.processing_pending
        assert msg.processing_error == "RuntimeError"
    assert "разбор не завершён" in attention()["items"][0]["reason"]


def test_completed_incoming_has_no_pending_processing(isolated_db, monkeypatch):
    from jobhunter.convo import inbox_email
    from jobhunter.models import Message
    app_id = make_app(isolated_db, status="AWAITING_REPLY")
    async def handled(*args, **kwargs):
        return "эскалация: карточка"
    monkeypatch.setattr(inbox_email, "handle_message", handled)
    asyncio.run(inbox_email._handle_one(app_id, 51, {"message-id": "<done@example.com>"},
                                        "message_id", "Ответ"))
    with isolated_db.session_scope() as sess:
        assert not sess.scalar(select(Message).where(Message.direction == "in")).processing_pending


def test_imap_multiple_batches_have_no_uid_gaps(isolated_db, monkeypatch):
    from jobhunter.convo import imapbox
    monkeypatch.setattr(imapbox, "get_settings", lambda: SimpleNamespace(
        imap_folder="INBOX", imap_max_fetch=200, inbox_lookback_days=30))
    monkeypatch.setattr(imapbox, "_uidvalidity", lambda *args: 7)
    conn = SimpleNamespace(select=lambda *args, **kw: ("OK", []),
                           uid=lambda *args: ("OK", [b" ".join(str(i).encode() for i in range(1, 502))]))
    processed = []
    sizes = []
    for _ in range(4):
        uids, validity, _reset = imapbox.new_uids(conn)
        sizes.append(len(uids))
        processed.extend(uids)
        imapbox.advance_watermark(uids, validity)
    assert sizes == [200, 200, 101, 0]
    assert processed == list(range(1, 502))


def test_telegram_attachment_is_not_silently_discarded(isolated_db, monkeypatch):
    from jobhunter.convo import engine
    make_app(isolated_db, status="AWAITING_REPLY")
    class Client:
        async def iter_messages(self, *args, **kwargs):
            yield SimpleNamespace(id=3, out=False, message="", media=object(), date=datetime.now(timezone.utc))
    async def no_sleep(*args):
        pass
    monkeypatch.setattr(engine.asyncio, "sleep", no_sleep)
    incoming = asyncio.run(engine.fetch_incoming(Client()))
    assert "вложение" in incoming[0][1][0][1]


def test_schedule_state_survives_engine_reopen(isolated_db):
    from jobhunter.dashboard import next_attempts
    from jobhunter.models import RuntimeState
    from jobhunter.observability import record
    at = datetime.now(timezone.utc) + timedelta(hours=1)
    record("schedule:tg_more", "scheduled", next_run_at=at)
    record("sender:telegram", "partial", next_run_at=at)
    isolated_db._engine.dispose()
    isolated_db._engine = None
    isolated_db._Session = None
    assert not next_attempts()["telegram"]["estimated"]
    with isolated_db.session_scope() as sess:
        assert sess.get(RuntimeState, "sender:telegram").next_run_at == at.replace(tzinfo=None)


def test_outcome_categories_do_not_double_count(isolated_db):
    from jobhunter.dashboard import outcomes
    from jobhunter.models import Application, Message
    ids = [make_app(isolated_db, followup=True) for _ in range(4)]
    with isolated_db.session_scope() as sess:
        sess.get(Application, ids[0]).status = "OFFER"
        sess.get(Application, ids[1]).status = "REJECTED_BY_EMPLOYER"
        sess.get(Application, ids[2]).interview_at_utc = datetime.now(timezone.utc)
        for app_id in ids:
            sess.add(Message(application_id=app_id, direction="in", classifier_label="ask_cv"))
    data = outcomes()
    assert data["sent"] == 4
    assert sum(data["categories"].values()) == 4
    assert data["categories"]["offer"] == data["categories"]["interview"] == 1
