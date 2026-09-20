"""Почта через Gmail API: отправка и чтение по HTTPS вместо SMTP/IMAP.

Причина: VPN владельца режет почтовые порты Gmail (465/587/993), и почта вставала на часы.
"""
import base64
import smtplib
import socket
from email.message import EmailMessage

import pytest

from jobhunter import googleauth
from jobhunter.convo import gmailapi, imapbox


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    monkeypatch.setattr(gmailapi, "META_PAUSE_S", 0)


class FakeExec:
    def __init__(self, result=None, error=None, log=None, tag=""):
        self.result, self.error, self.log, self.tag = result, error, log, tag

    def execute(self, num_retries=0):
        if self.log is not None:
            self.log.append((self.tag, num_retries))
        if self.error is not None:
            raise self.error
        return self.result


def http_error(status: int, reason: str = ""):
    import httplib2
    from googleapiclient.errors import HttpError

    resp = httplib2.Response({"status": str(status)})
    resp.reason = "test"
    body = ('{"error": {"errors": [{"reason": "%s"}]}}' % reason).encode()
    return HttpError(resp, body)


class Api:
    """Минимум клиента Gmail API: users().messages().send / list / get."""

    def __init__(self, store=None, send_error=None):
        self.store = store or {}                   # id -> {"ms", "headers", "raw"}
        self.send_error = send_error
        self.sent, self.calls, self.last_query = [], [], ""

    def users(self):
        return self

    def messages(self):
        return self

    def send(self, userId, body):
        self.sent.append(body["raw"])
        return FakeExec({"id": "sent1"}, self.send_error, self.calls, "send")

    def list(self, userId, q, maxResults, pageToken=None):
        self.last_query = q
        return FakeExec({"messages": [{"id": i} for i in self.store]}, None, self.calls, "list")

    def get(self, userId, id, format, metadataHeaders=None):
        m = self.store[id]
        if format == "raw":
            return FakeExec({"raw": base64.urlsafe_b64encode(m["raw"]).decode().rstrip("=")},
                            None, self.calls, "get-raw")
        return FakeExec({"internalDate": str(m["ms"]),
                         "payload": {"headers": [{"name": k, "value": v}
                                                 for k, v in m["headers"].items()]}},
                        None, self.calls, "get-meta")


def api(store=None, send_error=None):
    return Api(store, send_error)


def _msg():
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "me@gmail.com", "hr@acme.io", "Hello"
    m["Message-ID"] = "<jobhunter-1-initial@gmail.com>"
    m.set_content("Body")
    return m


# ───────────────────────── отправка ─────────────────────────

def test_gmail_replaces_message_id_so_the_real_one_is_written_back_to_the_letter(monkeypatch):
    """Живая проверка 20.09: Gmail API подменил наш Message-ID своим (по SMTP он сохранялся).
    Ответ рекрутёра ссылается на настоящий — без подстановки привязка по Message-ID сломалась бы."""
    monkeypatch.setattr(googleauth, "granted", lambda: set(googleauth.SCOPES))
    svc = api({"sent1": {"ms": 1, "raw": b"", "headers": {"Message-ID": "<CAHpQn@mail.gmail.com>"}}})
    msg = _msg()
    gmailapi.GmailSender(svc).send_message(msg)
    assert msg["Message-ID"] == "<CAHpQn@mail.gmail.com>"
    assert [t for t, _ in svc.calls] == ["send", "get-meta"]
    assert svc.calls[0] == ("send", 0)                 # отправка по-прежнему без автоповторов

    # чтение настоящего id не удалось — письмо уже ушло, ошибки быть не должно
    class NoRead(Api):
        def get(self, **kw):
            return FakeExec(error=http_error(500, "backendError"))
    lost = _msg()
    gmailapi.GmailSender(NoRead()).send_message(lost)
    assert lost["Message-ID"] == "<jobhunter-1-initial@gmail.com>"

    # без разрешения gmail.readonly настоящий id не читаем вовсе
    monkeypatch.setattr(googleauth, "granted", lambda: {googleauth.GMAIL_SEND})
    plain = api()
    kept = _msg()
    gmailapi.GmailSender(plain).send_message(kept)
    assert kept["Message-ID"] == "<jobhunter-1-initial@gmail.com>" and [t for t, _ in plain.calls] == ["send"]


def test_send_uses_the_raw_message_and_never_retries_on_its_own():
    svc = api()
    gmailapi.GmailSender(svc).send_message(_msg())
    raw = base64.urlsafe_b64decode(svc.sent[0] + "===")
    assert b"Message-ID: <jobhunter-1-initial@gmail.com>" in raw and b"Subject: Hello" in raw
    assert svc.calls == [("send", 0)]              # num_retries=0: повтор мог бы отправить письмо дважды


@pytest.mark.parametrize("status,reason,code", [
    (429, "rateLimitExceeded", 452), (403, "userRateLimitExceeded", 452), (403, "dailyLimitExceeded", 452),
    (401, "authError", 454), (403, "forbidden", 454),
    (400, "invalidArgument", 550), (404, "notFound", 550)])
def test_api_errors_map_to_smtp_classes_the_mailer_already_understands(status, reason, code):
    from jobhunter.outreach import mailer
    svc = api(send_error=http_error(status, reason))
    with pytest.raises(smtplib.SMTPResponseException) as err:
        gmailapi.GmailSender(svc).send_message(_msg())
    assert err.value.smtp_code == code
    assert mailer._smtp_delivery_ambiguous(err.value) is False      # письмо не принято — не «неизвестно»
    assert mailer._smtp_retryable(err.value) is (code in (452, 454))


def test_server_error_and_timeout_stay_ambiguous_but_missing_dns_is_safe_to_retry():
    from jobhunter.outreach import mailer
    for exc in (http_error(500, "backendError"), http_error(503), TimeoutError("timed out")):
        with pytest.raises(Exception) as err:
            gmailapi.GmailSender(api(send_error=exc)).send_message(_msg())
        assert mailer._smtp_delivery_ambiguous(err.value) is True   # мог принять — не повторяем вслепую

    class ServerNotFoundError(Exception):
        pass
    with pytest.raises(socket.gaierror) as err:
        gmailapi.GmailSender(api(send_error=ServerNotFoundError("Unable to find"))).send_message(_msg())
    assert mailer._smtp_delivery_ambiguous(err.value) is False and mailer._never_reached_server(err.value)


def test_message_over_the_api_limit_is_refused_locally():
    big = _msg()
    big.add_attachment(b"x" * 4_000_000, maintype="application", subtype="pdf", filename="cv.pdf")
    with pytest.raises(smtplib.SMTPResponseException) as err:
        gmailapi.GmailSender(api()).send_message(big)
    assert err.value.smtp_code == 552


# ─────────────────── выбор транспорта в mailer ───────────────────

@pytest.fixture()
def settings(monkeypatch, tmp_path):
    from jobhunter.config import get_settings

    def set_env(**env):
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(tmp_path / "token.json"))
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()
    yield set_env
    get_settings.cache_clear()


def test_mailer_prefers_gmail_api_and_falls_back_to_smtp_only_in_auto_mode(settings, monkeypatch):
    from jobhunter.outreach import mailer
    settings(SMTP_USER="me@gmail.com", SMTP_APP_PASSWORD="app-pass", MAIL_TRANSPORT="auto")
    monkeypatch.setattr(googleauth, "granted", lambda: set(googleauth.SCOPES))
    sender = gmailapi.GmailSender(api())
    monkeypatch.setattr(gmailapi, "_service", lambda need: sender._svc)
    assert isinstance(mailer._smtp_connect(), gmailapi.GmailSender)

    def dead(need):
        raise googleauth.GoogleUnavailable("токен отозван")
    monkeypatch.setattr(gmailapi, "_service", dead)
    assert gmailapi.open_sender() is None                            # письмо ещё не отправлялось — откат безопасен

    settings(MAIL_TRANSPORT="gmail_api")
    with pytest.raises(googleauth.GoogleUnavailable):
        gmailapi.open_sender()                                       # строгий режим не прячет ошибку входа

    settings(MAIL_TRANSPORT="smtp")
    assert gmailapi.open_sender() is None and gmailapi.open_mailbox() is None

    # нет разрешения в токене: auto — прежний путь, gmail_api — понятная ошибка
    monkeypatch.setattr(googleauth, "granted", lambda: {googleauth.CALENDAR})
    settings(MAIL_TRANSPORT="auto")
    assert gmailapi.open_sender() is None and gmailapi.open_mailbox() is None
    settings(MAIL_TRANSPORT="gmail_api")
    with pytest.raises(googleauth.GoogleUnavailable) as err:
        gmailapi.open_mailbox()
    assert "gmail.readonly" in str(err.value)


def test_gates_accept_gmail_api_without_an_app_password(settings, monkeypatch):
    settings(SMTP_USER="me@gmail.com", SMTP_APP_PASSWORD="", MAIL_TRANSPORT="auto")
    monkeypatch.setattr(googleauth, "granted", lambda: {googleauth.GMAIL_SEND})
    assert gmailapi.sending_configured() and not gmailapi.reading_configured()
    monkeypatch.setattr(googleauth, "granted", lambda: set())
    assert not gmailapi.sending_configured()
    settings(SMTP_APP_PASSWORD="app-pass")
    assert gmailapi.sending_configured() and gmailapi.reading_configured()


# ───────────────────────── чтение ─────────────────────────

def _mail(ms, subject, body=b"Hello there", mid="<m1@x>"):
    raw = (b"From: Anna <anna@acme.io>\r\nTo: me@gmail.com\r\nSubject: " + subject.encode()
           + b"\r\nMessage-ID: " + mid.encode()
           + b"\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n" + body)
    return {"ms": ms, "raw": raw,
            "headers": {"From": "Anna <anna@acme.io>", "Subject": subject, "Message-ID": mid,
                        "References": "<a@x>\r\n <b@x>"}}


def test_new_uids_watermark_overlap_and_transport_switch():
    store = {"g1": _mail(1_000_000_000_000, "one", mid="<1@x>"),
             "g2": _mail(1_000_000_300_000, "two", mid="<2@x>")}
    box = gmailapi.GmailMailbox(api(store))
    # первый проход: знак остался от IMAP → сброс и чтение по давности
    uids, validity, reset = box.new_uids((777, 5), 45, 200)
    assert reset is True and validity == gmailapi.VALIDITY and len(uids) == 2
    assert uids == sorted(uids) and uids[0] // 1000 == 1_000_000_000_000
    # обработали оба; знак — время последнего письма
    last = max(uids)
    box2 = gmailapi.GmailMailbox(api(store))
    again, _, reset2 = box2.new_uids((gmailapi.VALIDITY, last), 45, 200)
    assert reset2 is False
    assert again == [last] or last in again                          # нахлёст 15 минут: последнее письмо видно снова
    assert "after:%d" % (last // 1_000_000 - gmailapi.OVERLAP_S) in box2._svc.last_query
    # новое письмо позже знака попадает в выборку
    store["g3"] = _mail(1_000_000_900_000, "three", mid="<3@x>")
    box3 = gmailapi.GmailMailbox(api(store))
    fresh, _, _ = box3.new_uids((gmailapi.VALIDITY, last), 45, 200)
    assert fresh[-1] // 1000 == 1_000_000_900_000
    # ограничение пачки: самые старые первыми, знак пойдёт вперёд без пропусков
    limited, _, _ = gmailapi.GmailMailbox(api(store)).new_uids((0, 0), 45, 1)
    assert len(limited) == 1 and limited[0] // 1000 == 1_000_000_000_000


def test_headers_are_decoded_like_imap_and_the_body_arrives_only_on_request():
    store = {"g1": _mail(1_000_000_000_000, "=?utf-8?B?0J/RgNC40LLQtdGC?=",
                         body="Пришлите резюме".encode("utf-8"))}
    svc = api(store)
    box = gmailapi.GmailMailbox(svc)
    uids, _, _ = box.new_uids((0, 0), 45, 200)
    [(uid, headers)] = imapbox.fetch_headers(box, uids)             # тот же вызов, что и у IMAP
    assert headers["subject"] == "Привет" and headers["from"] == "Anna <anna@acme.io>"
    assert headers["references"] == "<a@x> <b@x>"                    # свёрнутые строки склеены
    assert [t for t, _ in svc.calls if t == "get-raw"] == []         # тела пока не качали
    text, is_html = imapbox.fetch_body(box, uid)
    assert text.strip() == "Пришлите резюме" and is_html is False
    assert [t for t, _ in svc.calls if t == "get-raw"] == ["get-raw"]


def test_unknown_uid_and_api_failure_become_mailbox_errors():
    box = gmailapi.GmailMailbox(api({}))
    with pytest.raises(imapbox.MailboxError):
        box.body(123)
    with pytest.raises(imapbox.MailboxError):
        box.headers([1, 2])

    class Broken(Api):
        def list(self, **kw):
            return FakeExec(error=http_error(500, "backendError"))
    b = Broken()
    with pytest.raises(imapbox.MailboxError) as err:
        gmailapi.GmailMailbox(b).new_uids((0, 0), 45, 200)
    assert "Gmail API" in str(err.value)

    class Offline(Api):
        def list(self, **kw):
            return FakeExec(error=socket.gaierror(-2, "no dns"))
    o = Offline()
    with pytest.raises(imapbox.MailboxError) as err:
        gmailapi.GmailMailbox(o).new_uids((0, 0), 45, 200)
    assert str(err.value).startswith("сеть:")                        # тот же путь уведомления «почта недоступна»


def test_imapbox_connect_prefers_the_api_and_reports_login_problems(settings, monkeypatch):
    settings(SMTP_USER="me@gmail.com", SMTP_APP_PASSWORD="", MAIL_TRANSPORT="auto")
    box = gmailapi.GmailMailbox(api())
    monkeypatch.setattr(gmailapi, "open_mailbox", lambda: box)
    assert imapbox.connect() is box

    def dead():
        raise googleauth.GoogleUnavailable("нужен повторный вход")
    monkeypatch.setattr(gmailapi, "open_mailbox", dead)
    with pytest.raises(imapbox.MailboxError) as err:
        imapbox.connect()
    assert "вход Google" in str(err.value)


def test_morning_digest_lists_unread_through_the_api(monkeypatch):
    from jobhunter.convo import mail_digest
    store = {"g1": _mail(2_000_000_000_000, "Interview?", mid="<u1@x>")}
    box = gmailapi.GmailMailbox(api(store))
    monkeypatch.setattr(imapbox, "connect", lambda: box)
    result = mail_digest.collect(hours=24)
    assert result["unseen"] == 1 and result["other"][0]["subject"] == "Interview?"
    assert "is:unread" in box._svc.last_query


def test_long_outage_keeps_the_oldest_letters_and_first_pass_starts_from_the_last_stored_one():
    """Живая проверка 20.09: список приходит от новых к старым; обрезка сверху теряла бы самые
    старые письма при долгом простое."""
    store = {("%016x" % (0x1a00000000000000 + i)): _mail(1_000_000_000_000 + i * 60_000, "n%d" % i,
                                                          mid="<n%d@x>" % i) for i in range(700)}
    ordered = dict(reversed(list(store.items())))                     # как отдаёт Gmail: новые первыми
    svc = api(ordered)
    box = gmailapi.GmailMailbox(svc)
    uids, _, _ = box.new_uids((0, 0), 45, 50)
    assert len(uids) == 50 and box._jobhunter_pending_count == 700
    assert uids[0] // 1000 == 1_000_000_000_000 and uids[-1] // 1000 == 1_000_000_000_000 + 49 * 60_000
    assert len([t for t, _ in svc.calls if t == "get-meta"]) == 50    # метаданные только на одну пачку
    # старт первого прохода — от последнего сохранённого входящего, а не за 45 дней
    import time
    now = int(time.time())
    b2 = gmailapi.GmailMailbox(api({}))
    b2.new_uids((0, 0), 45, 200, since_ts=now - 3 * 86400)
    assert abs(int(b2._svc.last_query.split("after:")[1]) - (now - 3 * 86400)) <= 2
    b3 = gmailapi.GmailMailbox(api({}))
    b3.new_uids((0, 0), 45, 200, since_ts=now - 400 * 86400)          # слишком давно — не глубже lookback
    assert abs(int(b3._svc.last_query.split("after:")[1]) - (now - 45 * 86400)) <= 2


def test_first_api_pass_resumes_from_the_last_successful_pass(monkeypatch, tmp_path):
    """Сухой прогон 20.09: разбор недельной давности слал бы владельцу те же 12 уведомлений заново.
    Всё до последнего успешного прохода прежнего транспорта уже разобрано."""
    from datetime import datetime, timedelta, timezone

    import jobhunter.db as dbmod
    from jobhunter.config import get_settings
    from jobhunter.models import Application, Job, Message
    from jobhunter.observability import record
    monkeypatch.setenv("DB_PATH", str(tmp_path / "resume.db"))
    get_settings.cache_clear()
    dbmod._engine = None
    dbmod._Session = None
    try:
        with dbmod.session_scope():
            pass                                                            # создать схему
        assert imapbox._resume_ts() == 0                                    # ни прохода, ни писем
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with dbmod.session_scope() as sess:
            job = Job(external_uuid="j", source="hn", title="Backend")
            sess.add(job)
            sess.flush()
            app = Application(job_id=job.id, status="AWAITING_REPLY")
            sess.add(app)
            sess.flush()
            sess.add(Message(application_id=app.id, direction="in", body="hi",
                             received_at=now - timedelta(days=4), email_message_id="<a@x>"))
        stamp = int((now - timedelta(days=4)).replace(tzinfo=timezone.utc).timestamp())
        assert imapbox._resume_ts() == stamp - 86400                        # нет прохода: от входящего минус сутки
        record("gmail", "ok", details={})
        assert abs(imapbox._resume_ts() - (int(datetime.now(timezone.utc).timestamp()) - 3600)) <= 5
        record("gmail", "partial", details={"remaining": 3})
        assert imapbox._resume_ts() == stamp - 86400                        # не дошёл до конца — глубже
    finally:
        if dbmod._engine is not None:
            dbmod._engine.dispose()
        dbmod._engine = None
        dbmod._Session = None
        get_settings.cache_clear()
