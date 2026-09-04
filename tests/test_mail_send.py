"""Маршрутизация ответа по каналу и отправка письма в тред.

Ключевое утверждение набора: заявка с почтовым контактом уходит по SMTP и
НЕ трогает Telethon, а телеграмная — наоборот. До этой правки владелец
физически не мог ответить на письмо: любая его команда возвращала
«нет хендла», потому что send_reply умел только MTProto.
"""
import asyncio
import os
import uuid

import pytest


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "mail.db")
    os.environ["SMTP_USER"] = "suren6pro@gmail.com"
    os.environ["SMTP_APP_PASSWORD"] = "test-pass"
    os.environ["SMTP_FROM_NAME"] = "Suren Hakobyan"
    os.environ["LLM_ENABLED"] = "false"
    os.environ["GCAL_ENABLED"] = "false"
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    import jobhunter.db as dbmod
    dbmod._engine = None
    dbmod._Session = None
    yield dbmod
    get_settings.cache_clear()


def _app(db, *, kind, handle="", url="", status=None):
    from jobhunter.models import Application, Job, Status
    with db.session_scope() as sess:
        job = Job(external_uuid=str(uuid.uuid4()), source="test",
                  title="Python Backend", company_name="Acme",
                  contact_kind=kind, contact_handle=handle, contact_url=url)
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, score=70,
                          status=(status or Status.AWAITING_REPLY.value))
        sess.add(app)
        sess.flush()
        return app.id


class FakeSMTP:
    """Подмена smtp_session: письма собираются в список, сеть не трогается."""

    sent = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture()
def smtp(monkeypatch):
    from jobhunter.outreach import mailer
    FakeSMTP.sent = []
    monkeypatch.setattr(mailer, "smtp_session", lambda: FakeSMTP())
    return FakeSMTP


# ── маршрутизация ──────────────────────────────────────────────────────

def test_email_application_goes_by_smtp(db, smtp):
    from jobhunter.convo.send import send_reply
    from jobhunter.models import ContactKind

    app_id = _app(db, kind=ContactKind.EMAIL.value, url="hr@acme.ru")

    telethon_touched = []

    class Boom:
        def __getattr__(self, name):
            telethon_touched.append(name)
            raise AssertionError("Telethon не должен участвовать в почте")

    res = asyncio.run(send_reply(Boom(), app_id, "Добрый день, готов обсудить."))
    assert res == "ok", res
    assert not telethon_touched
    assert len(smtp.sent) == 1
    assert smtp.sent[0]["To"] == "hr@acme.ru"


def test_telegram_application_does_not_use_smtp(db, smtp):
    """Обратная проверка: телеграмная заявка не должна уходить письмом."""
    from jobhunter.convo.send import send_reply
    from jobhunter.models import ContactKind

    app_id = _app(db, kind=ContactKind.USER_HANDLE.value, handle="hr_acme")
    res = asyncio.run(send_reply(None, app_id, "текст", dry=True))
    assert res == "ok"
    assert smtp.sent == []


def test_missing_contact_keeps_old_message(db, smtp):
    """Текст ответа не меняем — на него опираются существующие тесты."""
    from jobhunter.convo.send import send_reply
    from jobhunter.models import ContactKind

    app_id = _app(db, kind=ContactKind.UNKNOWN.value)
    res = asyncio.run(send_reply(None, app_id, "текст"))
    assert res == "skipped:нет хендла"


def test_reply_goes_to_address_that_wrote_us(db, smtp):
    """Рекрутёр ответил с личного ящика — продолжаем разговор там."""
    from jobhunter.convo.send import send_reply
    from jobhunter.models import Application, ContactKind

    app_id = _app(db, kind=ContactKind.EMAIL.value, url="hr@acme.ru")
    with db.session_scope() as sess:
        sess.get(Application, app_id).email_peer = "maria.personal@gmail.com"

    asyncio.run(send_reply(None, app_id, "спасибо за ответ"))
    assert smtp.sent[-1]["To"] == "maria.personal@gmail.com"


# ── тред и заголовки ───────────────────────────────────────────────────

def test_reply_joins_the_thread(db, smtp):
    from jobhunter.convo.send import send_reply
    from jobhunter.models import Application, ContactKind, Message

    app_id = _app(db, kind=ContactKind.EMAIL.value, url="hr@acme.ru")
    with db.session_scope() as sess:
        sess.add(Message(application_id=app_id, direction="in",
                         email_message_id="<incoming@acme.ru>",
                         email_subject="Отклик: Python Backend",
                         body="Расскажите про опыт"))
        sess.get(Application, app_id).email_thread_refs = ["<ours@gmail.com>"]

    asyncio.run(send_reply(None, app_id, "Опыт такой-то."))
    msg = smtp.sent[-1]
    assert msg["In-Reply-To"] == "<incoming@acme.ru>"
    assert "<ours@gmail.com>" in msg["References"]
    assert "<incoming@acme.ru>" in msg["References"]
    assert msg["Subject"].startswith("Re: ")
    assert msg["Message-ID"], "свой Message-ID обязателен для будущих ответов"


def test_auto_reply_marked_to_break_loops(db, smtp):
    """RFC 3834: корректный автоответчик не отвечает на auto-generated."""
    from jobhunter.convo.send import send_reply
    from jobhunter.models import ContactKind

    app_id = _app(db, kind=ContactKind.EMAIL.value, url="hr@acme.ru")
    asyncio.run(send_reply(None, app_id, "автоответ", is_auto=True))
    assert smtp.sent[-1]["Auto-Submitted"] == "auto-generated"

    asyncio.run(send_reply(None, app_id, "ручной ответ", is_auto=False))
    assert smtp.sent[-1]["Auto-Submitted"] is None


def test_frequent_auto_reply_suppressed(db, smtp):
    """Петля автоответов гасится паузой между собственными письмами."""
    from jobhunter.convo.send import send_reply
    from jobhunter.models import ContactKind

    app_id = _app(db, kind=ContactKind.EMAIL.value, url="hr@acme.ru")
    assert asyncio.run(send_reply(None, app_id, "первый", is_auto=True)) == "ok"
    res = asyncio.run(send_reply(None, app_id, "второй подряд", is_auto=True))
    assert res == "skipped:слишком частый ответ"
    # Ответ владельца ограничением не связан: он осознанный.
    assert asyncio.run(send_reply(None, app_id, "мой ответ", is_auto=False)) == "ok"


def test_outbound_recorded_once_for_both_channels(db, smtp):
    """Учёт ведётся общим кодом: письмо попадает и в журнал, и в архив."""
    from sqlalchemy import select

    from jobhunter.convo.send import send_reply
    from jobhunter.models import ContactKind, Message, SendLog

    app_id = _app(db, kind=ContactKind.EMAIL.value, url="hr@acme.ru")
    asyncio.run(send_reply(None, app_id, "текст ответа", is_auto=False))

    with db.session_scope() as sess:
        msgs = sess.scalars(select(Message).where(
            Message.application_id == app_id,
            Message.direction == "out")).all()
        logs = sess.scalars(select(SendLog).where(
            SendLog.application_id == app_id)).all()
    assert len(msgs) == 1
    assert msgs[0].email_message_id, "Message-ID должен сохраняться"
    assert logs and logs[-1].peer_id == "hr@acme.ru"


def test_kill_switch_stops_email(db, smtp, monkeypatch):
    from jobhunter.convo.send import send_reply
    from jobhunter.models import ContactKind
    from jobhunter.outreach import policy

    app_id = _app(db, kind=ContactKind.EMAIL.value, url="hr@acme.ru")
    monkeypatch.setattr(policy, "kill_switch_active", lambda: True)
    res = asyncio.run(send_reply(None, app_id, "текст"))
    assert res.startswith("stop:")
    assert smtp.sent == []
