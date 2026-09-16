"""Повтор на английском для HN-компаний, получивших русское письмо."""
import random
import smtplib
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import select


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "resend.db"))
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("KILL_SWITCH_PATH", str(tmp_path / "STOP"))
    monkeypatch.setenv("SMTP_USER", "suren@gmail.com")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "x")
    monkeypatch.setenv("CV_OUT", str(tmp_path / "cv"))
    import jobhunter.db as dbmod
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    dbmod._engine = None
    dbmod._Session = None
    from jobhunter.outreach import policy, resend_en
    monkeypatch.setattr(policy, "within_send_window", lambda *a: True)
    monkeypatch.setattr(resend_en.time, "sleep", lambda *a: None)
    yield dbmod
    if dbmod._engine is not None:
        dbmod._engine.dispose()
    dbmod._engine = None
    dbmod._Session = None
    get_settings.cache_clear()


def _app(db, addr="enghiring@senzing.com", lang="ru", **kw):
    from jobhunter.models import Application, ContactKind, Job, utcnow
    with db.session_scope() as sess:
        job = Job(external_uuid=str(random.random()), source=kw.pop("source", "hn"),
                  title="Platform Engineer", company_name="Senzing",
                  description_raw=kw.pop("body", "Senzing | Platform Engineer | Remote (USA)\n"
                                                  "We're hiring. Python, PostgreSQL, Kubernetes."),
                  contact_kind=ContactKind.EMAIL.value, contact_url=addr)
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, status=kw.pop("status", "AWAITING_REPLY"), score=70,
                          gate_passed=True, cv_lang=lang, message_body="По вакансии",
                          sent_at=utcnow() - timedelta(days=21), **kw)
        sess.add(app)
        sess.flush()
        return app.id


def _prepared(db, app_id):
    from jobhunter.models import Application
    from jobhunter.outreach import resend_en
    with db.session_scope() as sess:
        resend_en._save(sess.get(Application, app_id),
                        body=resend_en.OPENER + "\n\nRegarding the Platform Engineer role.",
                        cv_path="")


def test_candidates_only_russian_unanswered_foreign(db):
    from jobhunter.models import utcnow
    from jobhunter.outreach import resend_en
    good = _app(db)
    _app(db, lang="en")                                    # письмо уже было английским
    _app(db, first_reply_at=utcnow())                      # ответили
    _app(db, addr="hr@company.ru")                         # русский домен
    _app(db, source="wwr")                                 # не HN
    _app(db, status="REJECTED_BY_EMPLOYER")
    _app(db, body="Location: Brazil\nRemote: Yes\nWilling to relocate: No\n"
                  "Technologies: Node\nRésumé/CV: https://x.io")  # соискатель
    with db.session_scope() as sess:
        assert resend_en.candidates(sess) == [good]


class FakeSMTP:
    def __init__(self, fail=None):
        self.sent, self.fail = [], fail

    def send_message(self, msg):
        if self.fail:
            raise self.fail
        self.sent.append(msg)


def _smtp(monkeypatch, server):
    import jobhunter.outreach.mailer as mailer

    @contextmanager
    def session():
        yield server
    monkeypatch.setattr(mailer, "smtp_session", session)


def test_send_is_english_with_honest_opener_and_counts_to_daily_cap(db, monkeypatch):
    from jobhunter.models import Application, Message, SendLog
    from jobhunter.outreach import policy, resend_en
    app_id = _app(db)
    _prepared(db, app_id)
    server = FakeSMTP()
    _smtp(monkeypatch, server)
    assert resend_en.send()["sent"] == 1
    msg = server.sent[0]
    assert msg["Subject"].startswith("Application:")
    assert msg["In-Reply-To"] == "<jobhunter-%d-initial@gmail.com>" % app_id
    assert "went out in Russian by mistake" in msg.get_body().get_content()
    with db.session_scope() as sess:
        assert resend_en._state(sess.get(Application, app_id))["sent_at"]
        assert sess.scalar(select(SendLog)).result == "ok"
        assert sess.scalar(select(Message)).email_message_id.startswith("<jobhunter-%d-resend-en@" % app_id)
        assert policy.email_sent_today(sess) == 1
        assert resend_en.candidates(sess) == []
    assert resend_en.send()["sent"] == 0                    # второй раз не шлём


def test_ambiguous_failure_is_never_retried(db, monkeypatch):
    from jobhunter.models import Application
    from jobhunter.outreach import resend_en
    app_id = _app(db)
    _prepared(db, app_id)
    _smtp(monkeypatch, FakeSMTP(fail=smtplib.SMTPDataError(451, b"lost")))
    assert resend_en.send()["errors"] == 1
    with db.session_scope() as sess:
        assert resend_en._state(sess.get(Application, app_id))["ambiguous"] == "SMTPDataError"
        assert resend_en.candidates(sess) == []


def test_never_reached_server_is_retried_later(db, monkeypatch):
    import socket

    from jobhunter.models import Application
    from jobhunter.outreach import resend_en
    app_id = _app(db)
    _prepared(db, app_id)
    _smtp(monkeypatch, FakeSMTP(fail=socket.gaierror(-3, "name resolution")))
    resend_en.send()
    with db.session_scope() as sess:
        assert not resend_en._state(sess.get(Application, app_id)).get("attempt_at")
        assert resend_en.candidates(sess) == [app_id]


def test_daily_limit_is_respected(db, monkeypatch):
    from jobhunter.outreach import resend_en
    for i in range(6):
        _prepared(db, _app(db, addr="hr%d@acme%d.com" % (i, i)))
    server = FakeSMTP()
    _smtp(monkeypatch, server)
    assert resend_en.send()["sent"] == resend_en.DAILY
