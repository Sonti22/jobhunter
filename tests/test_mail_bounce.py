"""Отбивки и прогрев лимита почты.

Владелец поднял лимит до 80 писем в день с прогревом: +5 за каждый день без
отбивок от 40. Прогрев имеет смысл, только если отбивки видны — до этого
отчёты о недоставке отбрасывались как «адрес автоматики».
"""
import random
from datetime import datetime, timedelta, timezone

import pytest

from jobhunter.convo import bounce

GMAIL_FAILURE_HEADERS = {
    "from": "Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
    "subject": "Delivery Status Notification (Failure)",
    "return-path": "<>",
}
GMAIL_FAILURE_BODY = """Address not found

Your message wasn't delivered to jobs@deadco.com because the address couldn't be found, or is unable to receive mail.

Reporting-MTA: dns; googlemail.com
Final-Recipient: rfc822; jobs@deadco.com
Action: failed
Status: 5.1.1

From: Suren Hakobyan <suren@gmail.com>
To: jobs@deadco.com
Subject: Application: Backend Engineer
Message-ID: <jobhunter-%d-initial@gmail.com>
"""
DELAY_BODY = """Delivery incomplete

There was a temporary problem delivering your message to hr@slowco.com. Gmail will retry for 47 more hours.

Final-Recipient: rfc822; hr@slowco.com
Action: delayed
Status: 4.4.1
Message-ID: <jobhunter-%d-initial@gmail.com>
"""


def test_gmail_failure_is_a_permanent_bounce():
    assert bounce.is_bounce(GMAIL_FAILURE_HEADERS)
    b = bounce.parse(GMAIL_FAILURE_BODY % 42, GMAIL_FAILURE_HEADERS["subject"])
    assert (b.permanent, b.app_id, b.recipient) == (True, 42, "jobs@deadco.com")


def test_delay_is_not_a_bounce():
    b = bounce.parse(DELAY_BODY % 7, "Delivery Status Notification (Delay)")
    assert not b.permanent


def test_recruiter_email_is_not_a_bounce():
    assert not bounce.is_bounce({"from": "Anna <anna@acme.com>",
                                 "subject": "Re: Undelivered promises of our product"})


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bounce.db"))
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("KILL_SWITCH_PATH", str(tmp_path / "STOP"))
    monkeypatch.setenv("EMAIL_DAILY_LIMIT", "80")
    monkeypatch.setenv("EMAIL_WARMUP_SINCE", "2000-01-01")
    import jobhunter.db as dbmod
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    dbmod._engine = None
    dbmod._Session = None
    yield dbmod
    if dbmod._engine is not None:
        dbmod._engine.dispose()
    dbmod._engine = None
    dbmod._Session = None
    get_settings.cache_clear()


def _sent_app(db, addr="jobs@deadco.com"):
    from jobhunter.models import Application, ContactKind, Employer, Job, utcnow
    with db.session_scope() as sess:
        emp = Employer(handle_norm=addr, handle_kind=ContactKind.EMAIL.value)
        sess.add(emp)
        sess.flush()
        job = Job(external_uuid=str(random.random()), source="hn", title="Backend Engineer",
                  contact_kind=ContactKind.EMAIL.value, contact_url=addr)
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, employer_id=emp.id, status="AWAITING_REPLY",
                          score=80, gate_passed=True, sent_at=utcnow(),
                          followup_body="Hi! Following up")
        sess.add(app)
        sess.flush()
        return app.id


def _log(db, days_ago: int, result: str = "ok", n: int = 1):
    from jobhunter.models import SendLog
    at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_ago)
    with db.session_scope() as sess:
        for _ in range(n):
            sess.add(SendLog(result=result, peer_id="hr%d@acme.com" % days_ago, attempted_at=at))


def test_bounce_closes_application_and_blocks_address(db):
    from sqlalchemy import select

    from jobhunter.convo.inbox_email import record_bounce
    from jobhunter.models import Application, Employer, SendLog
    app_id = _sent_app(db)
    verdict = record_bounce(GMAIL_FAILURE_HEADERS, GMAIL_FAILURE_BODY % app_id)
    assert verdict.startswith("отбивка"), verdict
    with db.session_scope() as sess:
        app = sess.get(Application, app_id)
        assert app.status == "NO_REPLY_CLOSED" and app.followup_body == ""
        assert sess.get(Employer, app.employer_id).do_not_contact
        assert sess.scalar(select(SendLog).where(SendLog.result == "bounce")).peer_id == "jobs@deadco.com"
    assert record_bounce(GMAIL_FAILURE_HEADERS, GMAIL_FAILURE_BODY % app_id) == "уже учтено"


def test_bounce_found_by_recipient_when_message_id_is_missing(db):
    from jobhunter.convo.inbox_email import record_bounce
    app_id = _sent_app(db, addr="jobs@deadco.com")
    body = (GMAIL_FAILURE_BODY % 0).replace("<jobhunter-0-initial@gmail.com>", "<other@x>")
    assert record_bounce(GMAIL_FAILURE_HEADERS, body) == "отбивка: #%d jobs@deadco.com" % app_id


def test_warmup_grows_by_clean_days(db):
    from jobhunter.outreach import policy
    with db.session_scope() as sess:
        assert policy.email_daily_cap(sess) == 40
    for days_ago in (1, 2, 4):              # день 3 без писем серию не рвёт
        _log(db, days_ago)
    with db.session_scope() as sess:
        assert policy.email_clean_days(sess) == 3
        assert policy.email_daily_cap(sess) == 55


def test_bounce_resets_warmup(db):
    from jobhunter.outreach import policy
    for days_ago in (2, 3, 4):
        _log(db, days_ago)
    _log(db, 1)
    _log(db, 1, result="bounce")
    with db.session_scope() as sess:
        assert policy.email_clean_days(sess) == 0
        assert policy.email_daily_cap(sess) == 40


def test_warmup_never_exceeds_target(db):
    from jobhunter.outreach import policy
    for days_ago in range(1, 20):
        _log(db, days_ago)
    with db.session_scope() as sess:
        assert policy.email_daily_cap(sess) == 80


def test_many_bounces_today_stop_sending(db):
    from jobhunter.outreach import policy
    _log(db, 0, result="bounce", n=4)
    with db.session_scope() as sess:
        verdict = policy.can_send_email(sess)
        assert not verdict.allowed and "отбивок" in verdict.reason
        assert policy.email_sent_today(sess) == 0
