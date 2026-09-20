"""Почта через Gmail API: отправка и чтение по HTTPS (порт 443).

Зачем: VPN владельца режет почтовые порты Gmail (465, 587, 993), и почта вставала на часы
(16.09, 19.09). HTTPS на порт 443 он не трогает. SMTP и IMAP остаются запасным путём.

Режимы (MAIL_TRANSPORT):
  auto       Gmail API, если в токене есть нужное разрешение и он жив; иначе SMTP/IMAP;
  gmail_api  только Gmail API, без запасного пути (ошибка вместо тихого отката);
  smtp       как раньше: SMTP для отправки и IMAP для чтения.

Гарантии, которые нельзя ломать:
  * Чтение — только разрешение gmail.readonly: пометить, удалить или переместить письмо
    бот физически не может. Как и при IMAP, тела скачиваются только для опознанных писем.
  * Отправка никогда не повторяется сама (num_retries=0): повтор запроса после обрыва
    мог бы отправить письмо дважды. Что делать с ошибкой, решает mailer.
  * Ошибки переводятся в те же классы, что у smtplib, — классификаторы mailer
    («не принято», «повторить позже», «доставка неизвестна») работают без изменений.
"""
from __future__ import annotations

import base64
import email.header
import json
import logging
import smtplib
import socket
import time

from .. import googleauth
from ..config import get_settings

log = logging.getLogger("gmailapi")

# Маркер в CampaignState.imap_uidvalidity: «водяной знак хранит время письма, а не IMAP-UID».
VALIDITY = 4242
# Gmail отдаёт список писем с задержкой в секунды: новое письмо может стать видимым уже
# после того, как знак прошёл его время. Запрашиваем с запасом; повторы гасит дедуп по Message-ID.
OVERLAP_S = 900
MAX_LIST = 500
# Простая отправка через messages.send ограничена 5 МБ; резюме весит доли мегабайта.
MAX_RAW = 4_500_000

RATE_REASONS = {"ratelimitexceeded", "userratelimitexceeded", "dailylimitexceeded",
                "quotaexceeded", "backenderror"}
HEADER_NAMES = ["Message-ID", "In-Reply-To", "References", "From", "To", "Cc", "Subject",
                "Date", "Delivered-To", "X-Original-To", "List-Id", "List-Unsubscribe",
                "Auto-Submitted", "Precedence", "X-Autoreply", "Return-Path"]


def mode() -> str:
    value = (get_settings().mail_transport or "auto").strip().lower()
    return value if value in ("auto", "gmail_api", "smtp") else "auto"


def can_send() -> bool:
    """В токене есть разрешение на отправку и режим не «только SMTP»."""
    return mode() != "smtp" and googleauth.GMAIL_SEND in googleauth.granted()


def can_read() -> bool:
    return mode() != "smtp" and googleauth.GMAIL_READ in googleauth.granted()


def sending_configured() -> bool:
    """Почте есть чем отправлять: пароль приложения либо вход через Gmail API."""
    s = get_settings()
    return bool(s.smtp_user and (s.smtp_app_password or can_send()))


def reading_configured() -> bool:
    s = get_settings()
    return bool(s.smtp_user and (s.smtp_app_password or can_read()))


def _service(need: str):
    """Клиент Gmail API. Таймаут 30 с, без прокси из настроек Windows (как везде в проекте)."""
    import google_auth_httplib2
    import httplib2
    from googleapiclient.discovery import build
    creds = googleauth.credentials(need=(need,))
    http = google_auth_httplib2.AuthorizedHttp(
        creds, http=httplib2.Http(timeout=30, proxy_info=None))
    return build("gmail", "v1", http=http, cache_discovery=False)


def _reason(exc) -> str:
    """Причина из тела ответа Google: rateLimitExceeded, forbidden…"""
    try:
        body = json.loads(exc.content.decode("utf-8", "replace"))
        errors = (body.get("error") or {}).get("errors") or []
        return str((errors[0] or {}).get("reason", "")) if errors else \
            str((body.get("error") or {}).get("status", ""))
    except Exception:                                       # noqa: BLE001
        return ""


def translate(exc: Exception) -> Exception:
    """Ошибка Gmail API → класс, который понимают классификаторы mailer.

    4xx-ответ означает «письмо не принято»; 452/454 (SMTP 4xx) mailer повторит через 15
    минут, 550 — нет. Всё, что могло случиться уже после приёма письма (ответ 5xx, обрыв,
    таймаут), остаётся как есть: mailer честно назовёт такую доставку неоднозначной.
    """
    from googleapiclient.errors import HttpError
    if isinstance(exc, HttpError):
        status = int(getattr(exc.resp, "status", 0) or 0)
        reason = _reason(exc)
        label = "Gmail API %d %s" % (status, reason or "")
        if status == 429 or (status == 403 and reason.lower() in RATE_REASONS):
            return smtplib.SMTPResponseException(452, label.strip())    # квота: повторить позже
        if status in (401, 403):
            return smtplib.SMTPResponseException(454, label.strip())    # вход: после повторного входа
        if 400 <= status < 500:
            return smtplib.SMTPResponseException(550, label.strip())    # письмо не принято
        return exc
    if type(exc).__name__ == "ServerNotFoundError" or isinstance(exc, socket.gaierror):
        # Имя не разрешилось ДО соединения — байты письма никуда не уходили.
        return socket.gaierror(-2, "gmail.googleapis.com не найден: %s" % str(exc)[:80])
    return exc


class GmailSender:
    """Отправка с тем же интерфейсом, что у smtplib.SMTP: send_message / quit / close."""

    def __init__(self, service):
        self._svc = service

    def send_message(self, msg, *args, **kwargs) -> dict:
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        if len(raw) > MAX_RAW:
            raise smtplib.SMTPResponseException(552, "письмо больше лимита Gmail API")
        try:
            self._svc.users().messages().send(userId="me", body={"raw": raw}).execute(num_retries=0)
        except Exception as e:                              # noqa: BLE001
            mapped = translate(e)
            if mapped is e:
                raise
            raise mapped from e
        return {}

    def quit(self) -> None:
        return None

    def close(self) -> None:
        return None


def open_sender():
    """Транспорт отправки или None, если письма идут по SMTP.

    В режиме auto недоступный Gmail API — не повод падать: письмо ещё не отправлялось,
    и откат на SMTP безопасен. В режиме gmail_api ошибка пробрасывается.
    """
    m = mode()
    if m == "smtp":
        return None
    if not can_send():
        # В токене нет разрешения на отправку (или токена нет вовсе): прежний путь SMTP.
        if m == "gmail_api":
            raise googleauth.GoogleUnavailable(
                "в токене нет разрешения gmail.send — нужен вход: python -m jobhunter.googleauth --login")
        return None
    try:
        return GmailSender(_service(googleauth.GMAIL_SEND))
    except Exception as e:                                  # noqa: BLE001
        if m == "gmail_api" or not get_settings().smtp_app_password:
            raise
        log.warning("Gmail API недоступен для отправки (%s) — письма пойдут по SMTP",
                    str(e)[:120])
        return None


def _decode(value: str) -> str:
    try:
        text = str(email.header.make_header(email.header.decode_header(value or "")))
    except Exception:                                       # noqa: BLE001
        text = value or ""
    return " ".join(text.split())


def pseudo_uid(internal_ms: int, gmail_id: str) -> int:
    """Числовой «UID» письма: время получения в мс и три цифры от id (различает письма одной мс).

    Водяной знак и дедуп рассчитаны на целые числа по возрастанию, а id письма в Gmail —
    строка. Время приёма растёт вместе с потоком почты, чего IMAP-UID и требует.
    """
    try:
        tail = int(gmail_id, 16) % 1000
    except ValueError:
        tail = 0
    return int(internal_ms) * 1000 + tail


class GmailMailbox:
    """Чтение ящика с тем же смыслом, что у imapbox: новые письма → заголовки → тело."""

    is_gmail_api = True

    def __init__(self, service):
        self._svc = service
        self._ids: dict = {}            # псевдо-UID → id письма в Gmail
        self._meta: dict = {}           # псевдо-UID → заголовки
        self._jobhunter_pending_count = 0

    def logout(self) -> None:
        return None

    # ── вызовы API: любая ошибка становится MailboxError ──

    def _exec(self, request, what: str):
        from googleapiclient.errors import HttpError

        from .imapbox import MailboxError
        try:
            return request.execute(num_retries=2)
        except HttpError as e:
            raise MailboxError("Gmail API (%s): HTTP %s %s" % (
                what, getattr(e.resp, "status", "?"), _reason(e))) from None
        except Exception as e:                              # noqa: BLE001
            raise MailboxError("сеть: %s: %s" % (type(e).__name__, str(e)[:120])) from None

    def _list(self, query: str) -> list:
        ids: list = []
        token = None
        while len(ids) < MAX_LIST:
            resp = self._exec(self._svc.users().messages().list(
                userId="me", q=query, maxResults=100, pageToken=token), "список писем")
            ids += [m["id"] for m in resp.get("messages") or []]
            token = resp.get("nextPageToken")
            if not token:
                break
        return ids

    def _load_meta(self, gmail_id: str):
        r = self._exec(self._svc.users().messages().get(
            userId="me", id=gmail_id, format="metadata", metadataHeaders=HEADER_NAMES),
            "заголовки письма")
        ms = int(r.get("internalDate") or 0)
        if not ms:
            return None
        uid = pseudo_uid(ms, gmail_id)
        headers = {}
        for h in (r.get("payload") or {}).get("headers", []):
            headers[str(h.get("name", "")).lower()] = _decode(h.get("value", ""))
        self._ids[uid], self._meta[uid] = gmail_id, headers
        return uid

    # ── интерфейс imapbox ──

    def new_uids(self, state: tuple, lookback_days: int, max_fetch: int) -> tuple:
        """(uids, validity, сброшен_ли_знак). Знак — время последнего обработанного письма."""
        saved_validity, last = state
        reset = bool(saved_validity and saved_validity != VALIDITY)
        if reset:
            # Знак остался от IMAP: там лежал UID, здесь — время. Начинаем заново по дате.
            log.warning("почта переведена на Gmail API — водяной знак IMAP сброшен")
            last = 0
        after = (last // 1_000_000 - OVERLAP_S) if last else int(time.time()) - lookback_days * 86400
        found = []
        for gmail_id in self._list("in:inbox after:%d" % max(0, after)):
            uid = self._load_meta(gmail_id)
            if uid is not None:
                found.append(uid)
        floor = last - OVERLAP_S * 1_000_000 if last else 0
        uids = sorted(u for u in found if u > floor)
        self._jobhunter_pending_count = len(uids)
        return uids[:max(1, max_fetch)], VALIDITY, reset

    def headers(self, uids: list) -> list:
        from .imapbox import MailboxError
        missing = [u for u in uids if u not in self._meta]
        if missing:
            raise MailboxError("неполная загрузка заголовков: %d писем; повтор следующим проходом"
                               % len(missing))
        return [(u, self._meta[u]) for u in uids]

    def body(self, uid: int) -> tuple:
        from .imapbox import MailboxError, body_of
        gmail_id = self._ids.get(uid)
        if not gmail_id:
            raise MailboxError("неизвестное письмо UID %d" % uid)
        r = self._exec(self._svc.users().messages().get(userId="me", id=gmail_id, format="raw"),
                       "тело письма")
        raw_b64 = r.get("raw") or ""
        if not raw_b64:
            raise MailboxError("пустой ответ загрузки письма UID %d" % uid)
        return body_of(base64.urlsafe_b64decode(raw_b64 + "=" * (-len(raw_b64) % 4)))

    def unseen_uids(self, hours: int, limit: int) -> list:
        """Непрочитанные письма за последние часы — для утренней сводки почты."""
        after = int(time.time()) - hours * 3600
        found = []
        for gmail_id in self._list("in:inbox is:unread after:%d" % after):
            uid = self._load_meta(gmail_id)
            if uid is not None:
                found.append(uid)
        return sorted(found)[-limit:]


def open_mailbox():
    """Чтение через Gmail API или None, если ящик читается по IMAP.

    В режиме auto при недоступном входе откат на IMAP возможен, только если задан пароль
    приложения; иначе ошибка входа пробрасывается — её нужно показать владельцу.
    """
    m = mode()
    if m == "smtp":
        return None
    if not can_read():
        if m == "gmail_api":
            raise googleauth.GoogleUnavailable(
                "в токене нет разрешения gmail.readonly — нужен вход: python -m jobhunter.googleauth --login")
        return None
    try:
        return GmailMailbox(_service(googleauth.GMAIL_READ))
    except Exception as e:                                  # noqa: BLE001
        if m == "gmail_api" or not get_settings().smtp_app_password:
            raise
        log.warning("Gmail API недоступен для чтения (%s) — ящик читается по IMAP", str(e)[:120])
        return None
