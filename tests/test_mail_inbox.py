"""Чтение почты: приватность, привязка, защита от петель.

Главный тест набора — `test_stranger_mail_never_downloaded`. Владелец
разрешил боту видеть весь ящик, но видеть и хранить — разное. Тело
постороннего письма не должно скачиваться вовсе, и это утверждение
проверяется, а не декларируется в комментарии.
"""
import asyncio
import os
import uuid

import pytest


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "inbox.db")
    os.environ["SMTP_USER"] = "suren6pro@gmail.com"
    os.environ["SMTP_APP_PASSWORD"] = "test-pass"
    os.environ["LLM_ENABLED"] = "false"
    os.environ["GCAL_ENABLED"] = "false"
    os.environ["OWNER_CHANNEL"] = "saved"
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    import jobhunter.db as dbmod
    dbmod._engine = None
    dbmod._Session = None
    yield dbmod
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def no_smtp(monkeypatch):
    """Ни один тест не имеет права выйти в сеть.

    Без этой заглушки автоответ на входящее письмо честно пытается
    подключиться к smtp.gmail.com с таймаутом в 30 секунд — набор тестов
    зависает, а в худшем случае реальному человеку уходит письмо.
    """
    from jobhunter.outreach import mailer

    sent = []

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setattr(mailer, "smtp_session", lambda: _Session())

    # Автоответ выдерживает человеческую паузу в 40-480 секунд. В бою это
    # правильно, в тестах — зависание набора.
    from jobhunter.convo import engine
    monkeypatch.setattr(engine, "reply_delay_seconds", lambda *a, **k: 0)
    return sent


@pytest.fixture(autouse=True)
def clean_apps(db):
    """Каждый тест начинает с пустого набора заявок.

    Иначе заявки предыдущих тестов остаются живыми, привязка находит лишних
    кандидатов, и падение выглядит как ошибка правил, а не как утечка
    состояния между тестами.
    """
    from sqlalchemy import delete

    from jobhunter.models import (
        Application,
        BotOutbox,
        CampaignState,
        Employer,
        Job,
        Message,
        OwnerRequest,
        SendLog,
    )
    with db.session_scope() as sess:
        # Порядок важен: сначала то, что ссылается на заявки.
        for model in (Message, SendLog, OwnerRequest, Application, Job,
                      Employer, BotOutbox):
            sess.execute(delete(model))
        # Водяной знак тоже общий: без сброса письма следующего теста с
        # меньшими UID считаются уже прочитанными.
        st = sess.get(CampaignState, 1)
        if st is not None:
            st.imap_last_uid = 0
            st.imap_uidvalidity = 0
    yield


@pytest.fixture()
def app_id(db):
    from jobhunter.models import Application, ContactKind, Employer, Job, Status
    with db.session_scope() as sess:
        emp = Employer(handle_norm="hr@acme.ru", handle_kind="email")
        sess.add(emp)
        sess.flush()
        job = Job(external_uuid=str(uuid.uuid4()), source="test",
                  title="Python Backend", company_name="Acme",
                  contact_kind=ContactKind.EMAIL.value,
                  contact_url="hr@acme.ru")
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, employer_id=emp.id, score=70,
                          status=Status.AWAITING_REPLY.value)
        sess.add(app)
        sess.flush()
        return app.id


class FakeIMAP:
    """Ящик из заранее записанных писем. Считает, у скольких брали тело."""

    def __init__(self, messages):
        self.messages = messages          # uid -> (headers_bytes, body_bytes)
        self.commands = []
        self.body_fetches = []
        self.readonly = None

    def login(self, user, password):
        return ("OK", [b"ok"])

    def select(self, folder, readonly=False):
        self.readonly = readonly
        return ("OK", [b"1"])

    def status(self, folder, what):
        return ("OK", [b'"INBOX" (UIDVALIDITY 42)'])

    def uid(self, cmd, *args):
        """FETCH умеет пачки «1,2,3» — как настоящий IMAP."""
        self.commands.append(cmd.upper())
        if cmd.upper() == "SEARCH":
            return ("OK", [" ".join(str(u) for u in self.messages).encode()])
        what = args[1]
        assert "BODY.PEEK" in what, "чтение обязано быть без снятия флагов"
        headers_only = "HEADER.FIELDS" in what
        out = []
        for token in str(args[0]).split(","):
            uid = int(token)
            if uid not in self.messages:
                continue
            if headers_only:
                raw = self.messages[uid][0]
            else:
                self.body_fetches.append(uid)
                raw = self.messages[uid][1]
            out.append((b"1 (UID %d {%d}" % (uid, len(raw)), raw))
            out.append(b")")
        return ("OK", out)

    def logout(self):
        return ("BYE", [b"ok"])


def _mail(*, frm, subject, body, message_id="<x@acme.ru>", extra="",
          charset="utf-8"):
    """Письмо как его отдаёт IMAP: заголовки отдельно, целиком отдельно.

    charset=None воспроизводит письмо без объявленной кодировки — такие
    приходят, и на них кириллица разъезжается, если читать их наивно.
    """
    ctype = ("Content-Type: text/plain; charset=\"%s\"\r\n" % charset
             if charset else "")
    headers = ("Message-ID: %s\r\nFrom: %s\r\nTo: suren6pro@gmail.com\r\n"
               "Subject: %s\r\nDate: Tue, 26 Aug 2026 10:12:00 +0300\r\n%s%s\r\n"
               % (message_id, frm, subject, ctype, extra)).encode()
    full = headers + ("\r\n" + body).encode("utf-8")
    return headers, full


@pytest.fixture()
def imap(monkeypatch):
    """Подменяет соединение; тест сам кладёт письма в ящик."""
    from jobhunter.convo import imapbox

    holder = {}

    def _make(messages):
        fake = FakeIMAP(messages)
        holder["fake"] = fake
        monkeypatch.setattr(imapbox, "connect", lambda: fake)
        return fake

    holder["make"] = _make
    return holder


# ── приватность ────────────────────────────────────────────────────────

def test_stranger_mail_never_downloaded(db, app_id, imap):
    """Сто посторонних писем — ни одного скачанного тела и ни одной записи.

    Это главное свойство почтового цикла: чтение всего ящика нужно, чтобы
    найти ответы рекрутёров, а не чтобы сложить личную переписку в базу.
    """
    from sqlalchemy import select

    from jobhunter.convo.inbox_email import process
    from jobhunter.models import Message

    messages = {}
    for i in range(100):
        messages[1000 + i] = _mail(frm="friend%d@personal.ru" % i,
                                   subject="Привет, как дела",
                                   body="Личное письмо, банковская выписка",
                                   message_id="<p%d@personal.ru>" % i)
    fake = imap["make"](messages)

    stats = asyncio.run(process())
    assert stats["seen"] == 100
    assert stats["bodies"] == 0, "тело постороннего письма скачиваться не должно"
    assert stats["matched"] == 0
    assert fake.body_fetches == []
    assert fake.readonly is True, "ящик открывается только на чтение"

    with db.session_scope() as sess:
        assert sess.scalars(select(Message)).all() == []


def test_no_mailbox_mutating_commands(db, app_id, imap):
    """Ни STORE, ни COPY, ни MOVE, ни EXPUNGE — испортить ящик нечем."""
    from jobhunter.convo.inbox_email import process

    fake = imap["make"]({1: _mail(frm="x@y.ru", subject="тест", body="текст")})
    asyncio.run(process())
    assert not ({"STORE", "COPY", "MOVE", "EXPUNGE", "APPEND"}
                & set(fake.commands))


# ── привязка ───────────────────────────────────────────────────────────

def test_reply_matched_by_employer(db, app_id, imap):
    from sqlalchemy import select

    from jobhunter.convo.inbox_email import process
    from jobhunter.models import Message

    imap["make"]({7: _mail(frm="hr@acme.ru", subject="Re: Отклик: Python Backend",
                           body="Спасибо, ваше резюме получено.")})
    stats = asyncio.run(process())
    assert stats["matched"] == 1
    assert stats["by_rule"] == {"employer": 1}

    with db.session_scope() as sess:
        msgs = sess.scalars(select(Message).where(
            Message.application_id == app_id,
            Message.direction == "in")).all()
    assert len(msgs) == 1
    assert msgs[0].match_rule == "employer"
    assert msgs[0].email_from == "hr@acme.ru"


def test_reply_from_other_address_matched_by_message_id(db, app_id, imap):
    """Рекрутёр ответил с личного ящика — ловим по цепочке Message-ID."""
    from jobhunter.convo.inbox_email import process
    from jobhunter.models import Application, Message

    with db.session_scope() as sess:
        sess.add(Message(application_id=app_id, direction="out",
                         email_message_id="<ours@gmail.com>", body="отклик"))

    imap["make"]({9: _mail(frm="maria.personal@gmail.com", subject="Re: отклик",
                           body="Пишу с личной почты, вакансия актуальна.",
                           message_id="<m1@gmail.com>",
                           extra="References: <ours@gmail.com>")})
    stats = asyncio.run(process())
    assert stats["by_rule"] == {"msgid": 1}

    with db.session_scope() as sess:
        app = sess.get(Application, app_id)
    # Адрес запомнен: отвечать надо туда, откуда написали.
    assert app.email_peer == "maria.personal@gmail.com"


def test_quoted_history_not_classified(db, app_id, imap):
    """Цитата нашего письма не должна превращаться в предложение времени."""
    from sqlalchemy import select

    from jobhunter.convo.inbox_email import process
    from jobhunter.models import Message

    body = ("Спасибо, посмотрю.\r\n\r\n"
            "вт, 26 авг. 2026 г. в 10:12, Suren <suren6pro@gmail.com> написал:\r\n"
            "> Мне удобно: пн 26.08 в 11:00 (UTC+3); ср 28.08 в 16:00.\r\n")
    imap["make"]({11: _mail(frm="hr@acme.ru", subject="Re: Отклик", body=body)})
    asyncio.run(process())

    with db.session_scope() as sess:
        msg = sess.scalars(select(Message).where(
            Message.application_id == app_id,
            Message.direction == "in").order_by(Message.id.desc())).first()
    assert "11:00" not in msg.body
    assert "написал:" not in msg.body
    assert msg.classifier_label != "slot_proposed"


def test_undeclared_charset_still_cleaned(db, app_id, imap):
    """Письмо без Content-Type не должно ломать очистку цитат.

    Без запасного декодирования кириллица приезжает символами замены,
    маркеры цитат не находятся, и время из цитаты доходит до классификатора.
    """
    from sqlalchemy import select

    from jobhunter.convo.inbox_email import process
    from jobhunter.models import Message

    body = ("Спасибо, посмотрю.\r\n\r\n"
            "вт, 26 авг. 2026 г. в 10:12, Suren <suren6pro@gmail.com> написал:\r\n"
            "> Мне удобно: пн 26.08 в 11:00 (UTC+3).\r\n")
    imap["make"]({12: _mail(frm="hr@acme.ru", subject="Re: Отклик",
                            body=body, charset=None)})
    asyncio.run(process())

    with db.session_scope() as sess:
        msg = sess.scalars(select(Message).where(
            Message.application_id == app_id,
            Message.direction == "in").order_by(Message.id.desc())).first()
    assert "Спасибо" in msg.body, "текст должен читаться, а не превращаться в мусор"
    assert "11:00" not in msg.body
    assert msg.classifier_label != "slot_proposed"


# ── автоматические письма ──────────────────────────────────────────────

@pytest.mark.parametrize("frm,subject,extra", [
    ("no-reply@hh.ru", "Новые вакансии", ""),
    ("hr@acme.ru", "Automatic reply: Out of office", ""),
    ("hr@acme.ru", "Ответ", "Auto-Submitted: auto-replied"),
    ("jobs@linkedin.com", "Дайджест", "List-Id: <jobs.linkedin.com>"),
])
def test_automated_mail_dropped(db, app_id, imap, frm, subject, extra):
    """Автоответчик и рассылки не должны запускать переписку с ботом."""
    from jobhunter.convo.inbox_email import process

    fake = imap["make"]({21: _mail(frm=frm, subject=subject,
                                   body="текст", extra=extra)})
    stats = asyncio.run(process())
    assert stats["matched"] == 0
    assert fake.body_fetches == [], "у автоматического письма тело не нужно"


def test_ambiguous_goes_to_owner(db, imap):
    """Две заявки в одну компанию — решает владелец, а не эвристика."""
    from jobhunter.convo.inbox_email import process
    from jobhunter.models import Application, ContactKind, Employer, Job, Status

    with db.session_scope() as sess:
        emp = Employer(handle_norm="hr@dup.ru", handle_kind="email")
        sess.add(emp)
        sess.flush()
        for title in ("Python Backend", "Go Developer"):
            job = Job(external_uuid=str(uuid.uuid4()), source="test",
                      title=title, contact_kind=ContactKind.EMAIL.value,
                      contact_url="hr@dup.ru")
            sess.add(job)
            sess.flush()
            sess.add(Application(job_id=job.id, employer_id=emp.id, score=70,
                                 status=Status.AWAITING_REPLY.value))

    imap["make"]({31: _mail(frm="hr@dup.ru", subject="Вопрос",
                            body="Уточните детали")})
    stats = asyncio.run(process())
    assert stats["ambiguous"] == 1
    assert stats["matched"] == 0
