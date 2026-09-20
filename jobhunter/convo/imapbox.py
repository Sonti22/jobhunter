"""Чтение почтового ящика по IMAP: соединение, водяной знак, выборка.

Два правила, которые здесь не обсуждаются:

1. **Ящик открывается только на чтение.** `SELECT` в режиме readonly плюс
   `BODY.PEEK[]` во всех выборках означает, что процесс физически не может
   изменить ни один флаг. Владелец читает эту же почту с телефона, и
   пометить ему непрочитанное как прочитанное — быстрый способ, чтобы
   систему возненавидели. Обратное тоже верно: его чтение не влияет на нас,
   потому что источник правды о «новом» — UID, а не флаг \\Seen.

2. **Тело письма скачивается только после привязки к заявке.** Проход
   двухфазный: сначала заголовки всех новых писем, потом тела — лишь для
   тех, кого удалось связать с откликом. Владелец разрешил боту видеть весь
   ящик, но видеть и хранить — разное. Личная переписка и банковские
   уведомления не должны даже загружаться, и это выражено формой кода, а не
   обещанием в комментарии.

    python -m jobhunter.convo.imapbox --probe    # соединение и счётчики
"""
from __future__ import annotations

import email
import email.utils
import imaplib
import logging
import re
import socket
import ssl
import sys
import time
from datetime import date, datetime, timedelta, timezone
from email import policy as email_policy

from ..config import get_settings
from ..db import session_scope

log = logging.getLogger("imapbox")

# Заголовки, которых хватает и для привязки, и для предфильтров.
HEADER_FIELDS = ("MESSAGE-ID IN-REPLY-TO REFERENCES FROM TO CC SUBJECT DATE "
                 "DELIVERED-TO X-ORIGINAL-TO LIST-ID LIST-UNSUBSCRIBE "
                 "AUTO-SUBMITTED PRECEDENCE X-AUTOREPLY RETURN-PATH")

_UIDVALIDITY = re.compile(rb"UIDVALIDITY\s+(\d+)", re.I)


class MailboxError(RuntimeError):
    """Ящик недоступен: сеть, пароль отозван, Gmail просит вход через веб."""


def _state() -> tuple:
    with session_scope() as sess:
        # Строку кампании создаёт ТОЛЬКО policy.get_state: голый
        # CampaignState(id=1) получал дефолт потолка 15 вместо настроенных
        # 30, и кто первым успел создать строку — тот и задал квоту навсегда.
        from ..outreach.policy import get_state
        st = get_state(sess)
        return int(st.imap_uidvalidity or 0), int(st.imap_last_uid or 0)


def _resume_ts() -> int:
    """С какого момента (unix) читать ящик после смены транспорта.

    Последний УСПЕШНЫЙ проход прежнего транспорта (минус час на запас): всё до него уже
    разобрано, и повторный разбор недельной давности слал бы владельцу те же уведомления
    заново (сухой прогон 20.09: 12 писем «неоднозначная привязка»). Прохода не было или он
    не дошёл до конца — от последнего сохранённого входящего (минус сутки).
    """
    from sqlalchemy import func, select

    from ..models import Message, RuntimeState
    with session_scope() as sess:
        st = sess.get(RuntimeState, "gmail_ok")
        if st is not None and st.finished_at:
            return int(st.finished_at.replace(tzinfo=timezone.utc).timestamp()) - 3600
        ts = sess.scalar(select(func.max(Message.received_at)).where(
            Message.direction == "in", Message.email_message_id != ""))
    return int(ts.replace(tzinfo=timezone.utc).timestamp()) - 86400 if ts else 0


def _save_state(uidvalidity: int, last_uid: int) -> None:
    with session_scope() as sess:
        from ..outreach.policy import get_state
        st = get_state(sess)
        st.imap_uidvalidity = uidvalidity
        st.imap_last_uid = last_uid


# Обрывы, которые проходят сами: DNS не ответил, TLS-рукопожатие не
# уложилось, соединение сбросили. За 12 дней на этой машине так падал каждый
# восьмой проход почты (12%, 14.09 — три часа подряд); повтор через полминуты
# спасает короткие провалы, длинные всё равно ждут следующего крона.
_TRANSIENT = (socket.gaierror, TimeoutError, ConnectionError)
CONNECT_RETRIES = 3
CONNECT_BACKOFF = 20.0


def _transient(exc: OSError) -> bool:
    return isinstance(exc, _TRANSIENT) or "timed out" in str(exc).lower()


def connect(retries: int = CONNECT_RETRIES, backoff: float = CONNECT_BACKOFF):
    """Соединение с ящиком. Бросает MailboxError с внятной причиной.

    Сначала Gmail API (HTTPS, порт 443 — VPN его не режет), потом IMAP.
    """
    s = get_settings()
    from . import gmailapi
    try:
        box = gmailapi.open_mailbox()
    except gmailapi.googleauth.GoogleUnavailable as e:
        raise MailboxError("вход Google: %s" % str(e)[:200]) from None
    except Exception as e:                                  # noqa: BLE001
        raise MailboxError("сеть: %s: %s" % (type(e).__name__, str(e)[:120])) from None
    if box is not None:
        return box
    if not (s.smtp_user and s.smtp_app_password):
        raise MailboxError("нет SMTP_USER / SMTP_APP_PASSWORD")
    for attempt in range(1, max(1, retries) + 1):
        try:
            conn = imaplib.IMAP4_SSL(s.imap_host, s.imap_port,
                                     ssl_context=ssl.create_default_context(),
                                     timeout=30)
            conn.login(s.smtp_user, s.smtp_app_password)
            return conn
        except imaplib.IMAP4.error as e:
            # Gmail отвечает «[ALERT] Web login required» и подобным — текст
            # пробрасываем дословно, иначе диагностировать невозможно.
            raise MailboxError("вход не удался: %s" % str(e)[:200]) from None
        except OSError as e:
            if attempt < retries and _transient(e):
                log.warning("IMAP: %s: %s — повтор %d/%d через %ds", type(e).__name__,
                            str(e)[:80], attempt, retries - 1, int(backoff * attempt))
                time.sleep(backoff * attempt)
                continue
            raise MailboxError("сеть: %s: %s" % (type(e).__name__, str(e)[:120])) from None
    raise MailboxError("сеть: соединение не установлено")


def _tls_ok(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            with ssl.create_default_context().wrap_socket(sock, server_hostname=host):
                return True
    except OSError:
        return False


def network_hint(timeout: float = 6.0) -> str:
    """Отличает «интернета нет» от «туннель режет почтовые порты».

    16.09: TLS к www.google.com:443 проходил за 0.2 с и с хоста, и из
    контейнера, а к imap.gmail.com:993 и smtp.gmail.com:465/587 висел без
    ответа — весь трафик шёл через VPN (happ-xray), чей сервер не пропускает
    почтовые порты. В логе это выглядело как «сеть: timeout», и причину
    искали бы не там.
    """
    s = get_settings()
    from . import gmailapi
    if gmailapi.can_read():
        if _tls_ok("gmail.googleapis.com", 443, timeout):
            return "Сейчас Gmail API отвечает — обрыв был кратким."
        if _tls_ok("www.google.com", 443, timeout):
            return ("www.google.com отвечает, а gmail.googleapis.com — нет: проверь VPN и "
                    "прокси, они могут резать отдельные адреса Google.")
        return "Интернета нет вообще: не отвечает даже HTTPS."
    if _tls_ok(s.imap_host, s.imap_port, timeout):
        return "Сейчас ящик отвечает — обрыв был кратким."
    if _tls_ok("www.google.com", 443, timeout):
        return ("HTTPS работает, а почтовые порты Gmail (993/465/587) не отвечают: "
                "похоже, трафик идёт через VPN или прокси, который их не пропускает. "
                "Исключи imap.gmail.com и smtp.gmail.com из туннеля или выключи VPN "
                "на время работы бота — иначе почта не читается и не отправляется.")
    return "Интернета нет вообще: не отвечает даже HTTPS."


def _uidvalidity(conn, folder: str) -> int:
    typ, data = conn.status(folder, "(UIDVALIDITY)")
    if typ != "OK" or not data:
        return 0
    m = _UIDVALIDITY.search(data[0] if isinstance(data[0], bytes)
                            else str(data[0]).encode())
    return int(m.group(1)) if m else 0


def new_uids(conn, folder: str = "") -> tuple:
    """Номера новых писем. Возвращает (uids, uidvalidity, сброшен_ли_знак)."""
    s = get_settings()
    if _is_api(conn):
        return conn.new_uids(_state(), s.inbox_lookback_days, s.imap_max_fetch, _resume_ts())
    folder = folder or s.imap_folder
    # readonly=True — это команда EXAMINE: изменить флаги нельзя в принципе.
    typ, _ = conn.select(folder, readonly=True)
    if typ != "OK":
        raise MailboxError("папка %s недоступна" % folder)

    validity = _uidvalidity(conn, folder)
    if not validity:
        raise MailboxError("сервер не подтвердил UIDVALIDITY; граница чтения не изменена")
    saved_validity, last_uid = _state()
    reset = bool(saved_validity and validity and validity != saved_validity)
    if reset:
        # UID уникальны только внутри одного uidvalidity: после смены старые
        # номера указывают на другие письма, и продолжать с них — значит
        # молча пропустить всё, что пришло. Начинаем заново по дате.
        log.warning("uidvalidity сменился (%s → %s) — водяной знак сброшен",
                    saved_validity, validity)
        last_uid = 0

    if last_uid:
        typ, data = conn.uid("SEARCH", None, "UID", "%d:*" % (last_uid + 1))
    else:
        since = (date.today() - timedelta(days=s.inbox_lookback_days)
                 ).strftime("%d-%b-%Y")
        typ, data = conn.uid("SEARCH", None, "SINCE", since)
    if typ != "OK":
        raise MailboxError("не удалось получить список новых писем")
    if not data or not data[0]:
        conn._jobhunter_pending_count = 0
        return [], validity, reset

    # Диапазон «N:*» по стандарту возвращает как минимум последнее письмо
    # ящика, даже когда новых нет, — отсекаем сами.
    uids = sorted(int(x) for x in data[0].split() if int(x) > last_uid)
    conn._jobhunter_pending_count = len(uids)
    return uids[:max(1, s.imap_max_fetch)], validity, reset


def _parse(raw: bytes):
    """Разбор через policy.default: тема приходит уже раскодированной из
    =?UTF-8?B?...?=, а не строкой-абракадаброй."""
    return email.message_from_bytes(raw, policy=email_policy.default)


_UID_IN_RESPONSE = re.compile(rb"UID\s+(\d+)")

# Сколько писем запрашивать одной командой. По одному было бы 200 обращений
# к серверу за проход — минуты ожидания и лишняя нагрузка на Gmail.
FETCH_CHUNK = 50


def _is_api(conn) -> bool:
    """Соединение — это Gmail API, а не IMAP (см. gmailapi.py)."""
    return getattr(conn, "is_gmail_api", False) is True


def fetch_headers(conn, uids: list) -> list:
    """[(uid, dict заголовков)]. Тела не трогаются."""
    if _is_api(conn):
        return conn.headers(uids)
    out = []
    for start in range(0, len(uids), FETCH_CHUNK):
        chunk = uids[start:start + FETCH_CHUNK]
        typ, data = conn.uid("FETCH", ",".join(str(u) for u in chunk),
                             "(BODY.PEEK[HEADER.FIELDS (%s)])" % HEADER_FIELDS)
        if typ != "OK" or not data:
            raise MailboxError("не удалось загрузить заголовки писем; граница чтения сохранена")
        for part in data:
            if not (isinstance(part, tuple) and len(part) > 1):
                continue
            prefix, raw = part[0], part[1]
            m = _UID_IN_RESPONSE.search(prefix if isinstance(prefix, bytes)
                                        else str(prefix).encode())
            if not m or not raw:
                continue
            msg = _parse(raw)
            out.append((int(m.group(1)),
                        {k.lower(): str(v) for k, v in msg.items()}))
        missing = set(chunk) - {uid for uid, _ in out}
        if missing:
            raise MailboxError("неполная загрузка заголовков: %d писем; повтор следующим проходом"
                               % len(missing))
    return out


def fetch_body(conn, uid: int) -> tuple:
    """(текст, это_html) для одного письма. Зовётся только для опознанных."""
    if _is_api(conn):
        return conn.body(uid)
    typ, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
    if typ != "OK" or not data:
        raise MailboxError("не удалось загрузить тело письма UID %d" % uid)
    raw = next((part[1] for part in data
                if isinstance(part, tuple) and len(part) > 1), None)
    if not raw:
        raise MailboxError("пустой ответ загрузки письма UID %d" % uid)
    return body_of(raw)


def body_of(raw: bytes) -> tuple:
    """(текст, это_html) из сырых байтов письма — общий разбор для IMAP и Gmail API."""
    msg = _parse(raw)
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
    except Exception:
        part = None
    if part is None:
        return "", False
    try:
        content = part.get_content()
    except Exception:
        content = ""
    # Письмо без объявленной кодировки разбирается как us-ascii, и кириллица
    # превращается в символы замены. Тогда очистка цитат не находит своих
    # маркеров, процитированная история доезжает до классификатора, и он
    # видит в ней время — то есть ровно та поломка, ради которой писалась
    # очистка. Поэтому при виде «мусора» пробуем прочитать байты сами.
    if not content or content.count("�") > max(2, len(content) // 50):
        payload = part.get_payload(decode=True) or b""
        for enc in ("utf-8", "cp1251", "koi8-r"):
            try:
                decoded = payload.decode(enc)
            except UnicodeDecodeError:
                continue
            if decoded.count("�") == 0:
                content = decoded
                break
        else:
            content = payload.decode("utf-8", "replace") or content
    return content, part.get_content_subtype() == "html"


def msg_date(headers: dict) -> datetime:
    """Дата письма в UTC. Нет или битая — сейчас."""
    raw = headers.get("date", "")
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    if dt is None:
        return datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(
        tzinfo=timezone.utc)


def advance_watermark(uids: list, validity: int) -> None:
    """Двигает знак после обработки пачки.

    Именно после: падение посередине даст повтор, который погасится дедупом
    по Message-ID, а обратный порядок потерял бы письма навсегда.
    """
    if uids:
        _save_state(validity, max(uids))
    elif validity:
        saved, last = _state()
        if saved != validity:
            _save_state(validity, 0)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Проверка почтового ящика")
    ap.add_argument("--probe", action="store_true",
                    help="соединение и счётчики, без записи в БД")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    s = get_settings()
    try:
        conn = connect()
    except MailboxError as e:
        print("Ящик недоступен: %s" % e)
        return 2
    try:
        uids, validity, reset = new_uids(conn)
        print("папка: %s" % s.imap_folder)
        print("uidvalidity: %s%s" % (validity, " (СБРОШЕН)" if reset else ""))
        print("новых писем: %d" % len(uids))
        if args.probe and uids:
            heads = fetch_headers(conn, uids[-5:])
            print("тел скачано: 0")
            for uid, h in heads:
                print("  uid=%-8d from=%-38s subj=%s"
                      % (uid, h.get("from", "?")[:38],
                         (h.get("subject", "") or "")[:40]))
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
