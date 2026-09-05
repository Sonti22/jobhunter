"""Автономность почтового канала.

Три дыры, найденные разведкой по живой системе:
  - ошибка SMTP глоталась логом, владелец не узнавал, что канал стоит;
  - почтовый follow-up навсегда застревал в FOLLOWUP_PENDING_APPROVAL —
    отправщик берёт только APPROVED, а экрана ручного аппрува нет;
  - после отправки follow-up срок следующего напоминания тут же назначался
    снова, и «напоминаю о себе» грозило уходить каждые пять дней.
"""
import os
import uuid

import pytest


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "auto.db")
    os.environ["TELEGRAM_BOT_TOKEN"] = "test:token"
    os.environ["BOT_ALLOWED_USER_IDS"] = "5875908057"
    os.environ["LLM_ENABLED"] = "false"
    os.environ["SMTP_USER"] = "owner@example.com"
    os.environ["SMTP_APP_PASSWORD"] = "x"
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

    from jobhunter.models import Application, Batch, BotOutbox, Employer, Job, Message, SendLog
    with db.session_scope() as sess:
        for model in (Message, SendLog, Application, Job, Employer,
                      BotOutbox, Batch):
            sess.execute(delete(model))
    yield


def _email_app(db, *, status, score=80, followup_body=""):
    from jobhunter.models import Application, ContactKind, Job
    with db.session_scope() as sess:
        job = Job(external_uuid=str(uuid.uuid4()), source="tg:test",
                  title="Python Backend", company_name="Acme",
                  contact_kind=ContactKind.EMAIL.value,
                  contact_url="mailto:hr@acme.io",
                  description_raw="Python, FastAPI")
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, score=score, status=status.value,
                          gate_passed=True, followup_body=followup_body,
                          message_body="Здравствуйте! Отклик.")
        sess.add(app)
        sess.flush()
        return app.id


def test_smtp_failure_notifies_owner(db, monkeypatch):
    """Падение SMTP — уведомление, а не строка в логе, которую никто не видит."""
    from jobhunter import autopilot, notify

    def boom(*a, **kw):
        raise ConnectionError("smtp.gmail.com timed out")

    monkeypatch.setattr("jobhunter.outreach.mailer.send_batch", boom)
    with pytest.raises(ConnectionError):
        autopilot.step_send_email()

    rows = [r for r in notify.pending(20) if r["kind"] == "error"]
    assert rows, "владелец не узнал, что почта стоит"
    assert "Почтовая отправка упала" in rows[0]["text"]


def test_email_followup_auto_approved(db):
    """Почтовый follow-up одобряется сам — вручную его одобрить негде."""
    from jobhunter import autopilot
    from jobhunter.models import Application, Status

    app_id = _email_app(db, status=Status.FOLLOWUP_PENDING_APPROVAL,
                        followup_body="Напоминаю о своём отклике.")
    autopilot.step_auto_approve()
    with db.session_scope() as sess:
        assert sess.get(Application, app_id).status == Status.APPROVED.value


def test_followup_approval_runs_without_fresh_queue(db):
    """Follow-up одобряется и в день без новых заявок.

    Ранний return при пустой основной очереди раньше срезал ветку follow-up —
    а копятся напоминания как раз в тихие дни.
    """
    from jobhunter import autopilot
    from jobhunter.models import Application, Status

    app_id = _email_app(db, status=Status.FOLLOWUP_PENDING_APPROVAL,
                        followup_body="Напоминаю о своём отклике.")
    # основная очередь пуста — PENDING_APPROVAL нет вовсе
    autopilot.step_auto_approve()
    with db.session_scope() as sess:
        assert sess.get(Application, app_id).status == Status.APPROVED.value


def test_telegram_followup_stays_manual(db):
    """Телеграмный follow-up квоту не тратит без ведома владельца."""
    from jobhunter import autopilot
    from jobhunter.models import Application, ContactKind, Job, Status

    app_id = _email_app(db, status=Status.FOLLOWUP_PENDING_APPROVAL,
                        followup_body="Напоминаю.")
    with db.session_scope() as sess:
        app = sess.get(Application, app_id)
        job = sess.get(Job, app.job_id)
        job.contact_kind = ContactKind.USER_HANDLE.value
        job.contact_handle = "hr_acme"
    autopilot.step_auto_approve()
    with db.session_scope() as sess:
        assert sess.get(Application, app_id).status \
            == Status.FOLLOWUP_PENDING_APPROVAL.value


def test_old_signature_still_binds(db, monkeypatch):
    """Ответ на письмо, подписанное старым секретом, привязывается и после смены.

    Первые письма подписаны фолбэком (api_hash), новые — MAIL_BIND_SECRET.
    Ответ на старое может прийти через недели после смены секрета.
    """

    from jobhunter.config import get_settings
    from jobhunter.outreach.mailer import parse_reply_to, reply_to_addr

    monkeypatch.setenv("TELEGRAM_API_HASH", "old-api-hash")
    # Пустая строка, а не delenv: настройки читают и .env файл, где секрет
    # уже задан, — env-переменная единственный способ его перекрыть.
    monkeypatch.setenv("MAIL_BIND_SECRET", "")
    get_settings.cache_clear()
    old_addr = reply_to_addr(42)          # подпись старым секретом

    monkeypatch.setenv("MAIL_BIND_SECRET", "new-secret-value")
    get_settings.cache_clear()
    new_addr = reply_to_addr(42)

    try:
        assert old_addr != new_addr, "секрет не сменился"
        assert parse_reply_to("ответ, в цитате %s" % old_addr) == 42
        assert parse_reply_to(new_addr) == 42
        # случайная подпись по-прежнему отвергается
        assert parse_reply_to("+jh42xabcdef@") == 0
    finally:
        get_settings.cache_clear()
