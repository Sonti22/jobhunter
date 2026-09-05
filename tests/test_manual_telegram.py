"""Manual hand-off: no recruiter transport, no invented delivery, no duplicates."""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select


@pytest.fixture
def db(tmp_path, monkeypatch):
    from jobhunter import db as dbmod
    from jobhunter.config import get_settings
    monkeypatch.setenv("DB_PATH", str(tmp_path / "manual.db"))
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake:test")
    monkeypatch.setenv("BOT_ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("BASE_CV_PATH", "")
    monkeypatch.setenv("CV_OUT", str(tmp_path / "cv"))
    get_settings.cache_clear()
    dbmod._engine = dbmod._Session = None
    dbmod.get_engine()
    yield dbmod
    dbmod._engine.dispose()
    dbmod._engine = dbmod._Session = None
    get_settings.cache_clear()


def make_app(db, *, handle="recruiter_test", **values):
    from jobhunter.models import Application, Employer, Job, utcnow
    with db.session_scope() as sess:
        emp = sess.scalar(select(Employer).where(Employer.handle_norm == handle.lower()))
        if not emp:
            emp = Employer(handle_norm=handle.lower())
            sess.add(emp)
        job = Job(external_uuid="tg:test_channel/" + str(uuid.uuid4().int),
                  title="Python engineer", source="careered", contact_kind="user_handle",
                  contact_handle=handle, posted_at=int(utcnow().timestamp()),
                  description_raw="We are hiring a Python engineer. Responsibilities: backend.")
        sess.add(job)
        sess.flush()
        fields = dict(status="APPROVED", gate_passed=True, message_body="Hello, Python engineer here.",
                      score=80)
        fields.update(values)
        app = Application(job_id=job.id, employer_id=emp.id, **fields)
        sess.add(app)
        sess.flush()
        return app.id


def test_issue_reserves_and_notifies_only_owner(db):
    from jobhunter import manual_telegram as mt
    from jobhunter.models import Application, BotOutbox, SendLog
    from jobhunter.outreach import eligibility, sender
    aid = make_app(db)
    result = mt.issue()
    assert result["ids"] == [aid]
    assert mt.issue()["issued"] == 0
    with db.session_scope() as sess:
        app = sess.get(Application, aid)
        assert app.outcome == mt.READY and app.sent_at is None and app.send_attempts == 0
        assert eligibility.check(app, None).code == "manual_owner"
        assert sess.scalar(select(func.count(SendLog.id))) == 0
        notifications = sess.scalars(select(BotOutbox)).all()
        assert len(notifications) == 2
        assert all(n.chat_id == 0 for n in notifications)
        assert "Hello, Python engineer here." in notifications[-1].text
    assert sender.pick_batch(10) == []
    row = mt.listing()["items"][0]
    assert row["vacancy_url"].startswith("https://t.me/test_channel/")


def test_batch_cap_and_handle_dedup_without_employer(db):
    from jobhunter import manual_telegram as mt
    from jobhunter.models import Application
    a = make_app(db)
    b = make_app(db, handle="RECRUITER_TEST")
    with db.session_scope() as sess:
        sess.get(Application, a).employer_id = None
        sess.get(Application, b).employer_id = None
    assert mt.issue()["issued"] == 1
    for row in mt.listing()["items"]:
        mt.mark(row["id"], "skip")
    for i in range(8):
        make_app(db, handle=f"recruiter_{i}")
    assert mt.issue(limit=100)["issued"] == 5


@pytest.mark.parametrize("changes", [
    {"status": "SEND_FAILED_AMBIGUOUS"}, {"send_attempts": 1},
    {"status": "PENDING_APPROVAL"}, {"gate_passed": False},
    {"message_body": ""}, {"message_body": "x" * 3000},
    {"review_note": "needs checking"}, {"outcome": "applied"},
])
def test_unsafe_or_unready_application_not_issued(db, changes):
    from jobhunter.manual_telegram import issue
    make_app(db, **changes)
    assert issue()["issued"] == 0


@pytest.mark.parametrize("problem", ["stale", "closed", "candidate", "dnc", "replied", "cooldown",
                                      "email", "channel", "invalid_handle", "bot_handle"])
def test_shared_safety_filters_remain_active(db, problem):
    from jobhunter.manual_telegram import issue
    from jobhunter.models import Application, Employer, Job, utcnow
    aid = make_app(db)
    with db.session_scope() as sess:
        app = sess.get(Application, aid)
        job = sess.get(Job, app.job_id)
        emp = sess.get(Employer, app.employer_id)
        if problem == "stale":
            job.posted_at = int((utcnow() - timedelta(days=90)).timestamp())
        elif problem == "closed":
            job.is_closed = True
        elif problem == "candidate":
            job.title = "Python Developer #резюме"
            job.description_raw = "Ищу работу Python разработчиком. #резюме"
        elif problem == "dnc":
            emp.do_not_contact = True
        elif problem == "replied":
            app.first_reply_at = utcnow()
        elif problem == "cooldown":
            emp.last_contacted_at = utcnow()
        elif problem in ("email", "channel"):
            job.contact_kind = problem
        elif problem == "invalid_handle":
            job.contact_handle = "bad/link"
        elif problem == "bot_handle":
            job.contact_handle = "recruiting_bot"
    assert issue()["issued"] == 0


def test_previous_attempt_for_same_handle_excludes_other_application(db):
    from jobhunter.manual_telegram import issue
    make_app(db, send_attempts=1, status="SEND_FAILED_AMBIGUOUS")
    make_app(db)
    assert issue()["issued"] == 0


def test_mark_records_owner_report_not_api_delivery(db):
    from jobhunter import manual_telegram as mt
    from jobhunter.convo.send import reply_target_problem
    from jobhunter.models import Application, Employer, Message, SendLog
    from jobhunter.outreach.followup import due
    aid = make_app(db)
    mt.issue()
    assert mt.mark(aid, "sent")[0]
    assert not mt.mark(aid, "sent")[0]
    with db.session_scope() as sess:
        app = sess.get(Application, aid)
        assert app.status == "AWAITING_REPLY" and app.outcome == mt.SENT
        assert app.sent_at == app.applied_at
        assert app.telegram_msg_id is None and app.send_channel == "telegram_manual"
        assert sess.scalar(select(func.count(SendLog.id))) == 0
        assert sess.scalar(select(func.count(Message.id))) == 0
        assert sess.get(Employer, app.employer_id).total_messages_sent == 1
        assert "вручную" in reply_target_problem(sess, app)
        app.sent_at -= timedelta(days=5)
    with db.session_scope() as sess:
        assert due(sess) == []
    assert mt.listing()["sent"] == 1


@pytest.mark.parametrize("action", ["skip", "failed"])
def test_skip_or_failure_does_not_forge_delivery_or_requeue(db, action):
    from jobhunter import manual_telegram as mt
    from jobhunter.models import Application
    aid = make_app(db)
    mt.issue()
    assert mt.mark(aid, action)[0]
    with db.session_scope() as sess:
        app = sess.get(Application, aid)
        assert app.status == "WITHDRAWN" and app.sent_at is None
    assert mt.issue()["issued"] == 0


def test_stale_card_hides_text_and_changed_handle_cannot_be_marked(db):
    from jobhunter import manual_telegram as mt
    from jobhunter.models import Application, Job
    aid = make_app(db)
    mt.issue()
    with db.session_scope() as sess:
        app = sess.get(Application, aid)
        sess.get(Job, app.job_id).contact_handle = "new_recruiter"
    row = mt.get_card(aid)
    assert row["problem"] and "НЕ ОТПРАВЛЯЙ" in mt.card(row)
    assert not mt.mark(aid, "sent")[0]
    assert not any("text" == b.get("callback_data", "").split(":")[-1]
                   for r in mt.keyboard(row)["inline_keyboard"] for b in r)


def test_ats_buttons_cannot_clear_manual_ownership(db):
    from jobhunter import manual_telegram as mt
    from jobhunter.manual_apply import mark
    from jobhunter.models import Application
    aid = make_app(db)
    mt.issue()
    mark(aid, "applied")
    with db.session_scope() as sess:
        assert sess.get(Application, aid).outcome == mt.READY


def test_changed_status_can_be_removed_without_losing_existing_dialogue(db):
    from jobhunter import manual_telegram as mt
    from jobhunter.models import Application
    aid = make_app(db)
    mt.issue()
    with db.session_scope() as sess:
        sess.get(Application, aid).status = "REPLIED"
    assert not mt.mark(aid, "sent")[0]
    assert mt.mark(aid, "failed")[0]
    assert mt.listing()["ready"] == 0
    with db.session_scope() as sess:
        assert sess.get(Application, aid).status == "REPLIED"


def test_concurrent_issue_has_one_batch_and_no_duplicate_notifications(db):
    from jobhunter import manual_telegram as mt
    make_app(db)
    barrier = threading.Barrier(2)
    def issue():
        barrier.wait(timeout=5)
        return mt.issue()["issued"]
    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(lambda _: issue(), range(2))) == 1


def test_concurrent_mark_counts_once(db):
    from jobhunter import manual_telegram as mt
    aid = make_app(db)
    mt.issue()
    barrier = threading.Barrier(2)
    def mark():
        barrier.wait(timeout=5)
        return mt.mark(aid, "sent")[0]
    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(lambda _: mark(), range(2))) == 1


def test_bot_command_authorization_copy_and_two_step_confirmation(db):
    from jobhunter import manual_telegram as mt
    from jobhunter.bot.handlers import handle
    aid = make_app(db)
    def command(user):
        return {"message": {"text": "/outreach", "from": {"id": user}, "chat": {"id": user}}}
    assert handle(command(99)) == []
    assert mt.listing()["ready"] == 0
    acts = handle(command(1))
    assert any("PDF" in a.get("text", "") for a in acts)
    from jobhunter.models import BotOutbox
    with db.session_scope() as sess:
        assert sess.scalar(select(BotOutbox).where(BotOutbox.kind == "manual_tg_step"))
    def callback(action):
        return {"callback_query": {"id": "cb", "from": {"id": 1},
                "data": f"t:{aid}:{action}", "message": {"message_id": 1, "chat": {"id": 1}}}}
    copied = handle(callback("text"))
    assert copied[-1]["text"] == "Hello, Python engineer here."
    handle(callback("confirm"))
    assert mt.listing()["sent"] == 0
    handle(callback("sent"))
    assert mt.listing()["sent"] == 1


def test_web_get_is_read_only_and_post_issues_and_marks(db):
    from jobhunter import manual_telegram as mt
    from jobhunter.web.server import app
    aid = make_app(db, message_body="Hello <script>alert(1)</script>")
    client = TestClient(app)
    assert client.get("/manual-telegram").status_code == 200
    assert mt.listing()["ready"] == 0
    assert client.post("/manual-telegram/next", headers={"Origin": "https://evil.example"}).status_code == 403
    page = client.post("/manual-telegram/next")
    assert page.status_code == 200 and "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    assert "Hello <script>" not in page.text
    page = client.post("/manual-telegram/mark", data={"app_id": aid, "action": "sent"})
    assert page.status_code == 200 and mt.listing()["sent"] == 1


def test_username_link_opens_exact_draft_without_auto_send(db):
    from urllib.parse import parse_qs, urlparse

    from jobhunter import manual_telegram as mt
    text = "Здравствуйте! Python & SQL?\nЦена: 100% — обсудим."
    aid = make_app(db, message_body=text)
    mt.queue_current(1)
    row = mt.get_card(aid)
    buttons = [b for r in mt.keyboard(row)["inline_keyboard"] for b in r]
    link = next(b["url"] for b in buttons if "с текстом" in b["text"])
    parsed = urlparse(link)
    assert parsed.netloc == "t.me" and parsed.path == "/recruiter_test"
    assert parse_qs(parsed.query) == {"text": [text]}
    assert "127.0.0.1" not in mt.card(row)


def test_sent_automatically_queues_exactly_one_next_and_duplicate_does_not(db):
    from jobhunter import manual_telegram as mt
    from jobhunter.models import BotOutbox
    first = make_app(db, score=90)
    second = make_app(db, handle="second_recruiter", score=80)
    make_app(db, handle="third_recruiter", score=70)
    assert mt.queue_current(1) == first
    assert mt.listing()["ready"] == 1
    assert mt.mark(first, "sent", next_chat_id=1)[0]
    assert not mt.mark(first, "sent", next_chat_id=1)[0]
    assert [r["id"] for r in mt.listing()["items"]] == [second]
    with db.session_scope() as sess:
        rows = sess.scalars(select(BotOutbox).where(BotOutbox.kind == "manual_tg_step")).all()
        assert [r.markup_json["app_id"] for r in rows] == [first, second]


def test_existing_batch_is_not_lost_in_sequential_mode(db):
    from jobhunter import manual_telegram as mt
    first = make_app(db, score=90)
    second = make_app(db, handle="second_recruiter", score=80)
    mt.issue(deliver=False)
    assert mt.queue_current(1) == first
    assert mt.mark(first, "skip", next_chat_id=1)[0]
    assert mt.queue_current(1) == second
    assert mt.listing()["ready"] == 1


def test_delivery_failure_pauses_instead_of_prompting_more_sends(db):
    from jobhunter import manual_telegram as mt
    first = make_app(db, score=90)
    make_app(db, handle="second_recruiter", score=80)
    mt.queue_current(1)
    ok, note = mt.mark(first, "failed", next_chat_id=1)
    assert ok and "приостановлена" in note
    assert mt.listing()["ready"] == 0


def test_mark_and_next_notification_rollback_together(db, monkeypatch):
    from jobhunter import manual_telegram as mt
    first = make_app(db)
    mt.queue_current(1)
    def crash(*a, **kw):
        raise RuntimeError("simulated queue failure")
    monkeypatch.setattr(mt, "_queue_current", crash)
    with pytest.raises(RuntimeError):
        mt.mark(first, "sent", next_chat_id=1)
    assert mt.listing()["sent"] == 0 and mt.get_card(first)


def test_durable_continuation_survives_db_reopen(db):
    from jobhunter import manual_telegram as mt
    from jobhunter import notify
    first = make_app(db, score=90)
    second = make_app(db, handle="second_recruiter", score=80)
    mt.queue_current(1)
    mt.mark(first, "sent", next_chat_id=1)
    db._engine.dispose()
    db._engine = db._Session = None
    assert any(r["kind"] == "manual_tg_step" and r["markup"]["app_id"] == second
               for r in notify.pending(20))


def test_private_chat_only_even_if_group_sender_is_owner(db):
    from jobhunter import manual_telegram as mt
    from jobhunter.bot.handlers import handle
    first = make_app(db)
    with pytest.raises(PermissionError):
        mt.queue_current(99)
    acts = handle({"message": {"text": "/outreach", "from": {"id": 1},
                               "chat": {"id": -100}}})
    assert "личный" in acts[0]["text"] and mt.listing()["ready"] == 0
    mt.queue_current(1)
    assert not mt.mark(first, "sent", next_chat_id=-100)[0]


def test_explicit_resume_is_deduplicated_per_request(db):
    from jobhunter import manual_telegram as mt
    from jobhunter.models import BotOutbox
    make_app(db)
    mt.queue_current(1, "message:1")
    mt.queue_current(1, "message:1")
    mt.queue_current(1, "message:2")
    with db.session_scope() as sess:
        assert sess.scalar(select(func.count(BotOutbox.id)).where(BotOutbox.kind == "manual_tg_step")) == 2
    assert mt.listing()["ready"] == 1


def _pdf_app(db, tmp_path):
    from jobhunter import manual_telegram as mt
    folder = tmp_path / "cv"
    folder.mkdir(exist_ok=True)
    pdf = folder / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\nmanual test fixture")
    aid = make_app(db, cv_path=str(pdf))
    mt.queue_current(1)
    return aid, pdf


def test_outbox_delivers_card_and_real_pdf_only_to_owner(db, tmp_path, monkeypatch):
    from jobhunter.bot import outbox
    from jobhunter.models import BotOutbox, SendLog
    aid, pdf = _pdf_app(db, tmp_path)
    sent = []
    def message(chat_id, text, markup=None, http=None):
        sent.append(("message", chat_id, text))
        return {"message_id": 1}
    def document(chat_id, filename, content, caption="", http=None):
        sent.append(("document", chat_id, filename, content))
        return {"message_id": 2}
    monkeypatch.setattr(outbox.api, "send_message", message)
    monkeypatch.setattr(outbox.api, "send_document", document)
    monkeypatch.setattr(outbox.time, "sleep", lambda _: None)
    assert outbox.drain() == 2
    assert [r[0] for r in sent] == ["message", "document"]
    assert all(r[1] == 1 for r in sent)
    assert sent[1][3] == pdf.read_bytes()
    with db.session_scope() as sess:
        assert sess.scalar(select(func.count(SendLog.id))) == 0
        assert all(r.sent_at for r in sess.scalars(select(BotOutbox)))


def test_outbox_cancels_processed_card_without_faking_delivery(db, tmp_path, monkeypatch):
    from jobhunter import manual_telegram as mt
    from jobhunter.bot import outbox
    from jobhunter.models import BotOutbox
    aid, _ = _pdf_app(db, tmp_path)
    mt.mark(aid, "sent")
    monkeypatch.setattr(outbox.api, "send_message", lambda *a, **kw: pytest.fail("obsolete card sent"))
    monkeypatch.setattr(outbox.api, "send_document", lambda *a, **kw: pytest.fail("obsolete PDF sent"))
    assert outbox.drain() == 0
    with db.session_scope() as sess:
        rows = sess.scalars(select(BotOutbox)).all()
        assert all(r.sent_at is None and r.attempts == 5 for r in rows)


def test_missing_pdf_is_visible_without_sending_arbitrary_file(db, tmp_path, monkeypatch):
    from jobhunter.bot import outbox
    from jobhunter.models import BotOutbox
    aid, pdf = _pdf_app(db, tmp_path)
    pdf.unlink()
    monkeypatch.setattr(outbox.api, "send_message", lambda *a, **kw: {"message_id": 1})
    monkeypatch.setattr(outbox.api, "send_document", lambda *a, **kw: pytest.fail("missing PDF sent"))
    monkeypatch.setattr(outbox.time, "sleep", lambda _: None)
    assert outbox.drain() == 1
    with db.session_scope() as sess:
        cv = sess.scalar(select(BotOutbox).where(BotOutbox.kind == "manual_tg_document"))
        assert cv.sent_at is None and cv.attempts == 5
        assert sess.scalar(select(BotOutbox).where(BotOutbox.kind == "manual_tg_cv_error"))


@pytest.mark.parametrize("kind", ["outside", "not_pdf", "bad_header"])
def test_cv_path_and_content_guards(db, tmp_path, kind):
    from jobhunter import manual_telegram as mt
    from jobhunter.models import Application
    aid, pdf = _pdf_app(db, tmp_path)
    if kind == "outside":
        file = tmp_path / "secret.pdf"
        file.write_bytes(b"%PDF-sensitive")
    elif kind == "not_pdf":
        file = pdf.with_suffix(".txt")
        file.write_bytes(b"%PDF-sensitive")
    else:
        file = pdf
        file.write_bytes(b"token=not-a-PDF")
    with db.session_scope() as sess:
        sess.get(Application, aid).cv_path = str(file)
    with pytest.raises(ValueError):
        mt.cv_document(aid)


def test_api_document_is_multipart_and_retries_complete_bytes(db, monkeypatch):
    import httpx

    from jobhunter.bot import api
    attempts = []
    def response(request):
        assert "multipart/form-data" in request.headers["content-type"]
        attempts.append(request.read())
        if len(attempts) == 1:
            return httpx.Response(429, json={"parameters": {"retry_after": 1}})
        return httpx.Response(200, json={"result": {"message_id": 55}})
    monkeypatch.setattr(api.time, "sleep", lambda _: None)
    with httpx.Client(transport=httpx.MockTransport(response)) as http:
        result = api.send_document(1, "resume.pdf", b"%PDF-1.4\ncontent", http=http)
    assert result["message_id"] == 55 and len(attempts) == 2
    assert all(b"%PDF-1.4\ncontent" in body for body in attempts)


@pytest.mark.parametrize("chat_id,filename,content,exception", [
    (99, "resume.pdf", b"%PDF-1.4", PermissionError),
    (1, "../resume.pdf", b"%PDF-1.4", ValueError),
    (1, "resume.pdf", b"not PDF", ValueError),
])
def test_api_document_rejects_unsafe_input_before_network(db, monkeypatch, chat_id, filename, content, exception):
    from jobhunter.bot import api
    monkeypatch.setattr(api, "call", lambda *a, **kw: pytest.fail("network called"))
    with pytest.raises(exception):
        api.send_document(chat_id, filename, content)
