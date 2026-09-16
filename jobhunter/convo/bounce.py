"""Отбивки: письмо не доставлено — адрес мёртв, прогрев лимита не растёт.

Раньше отчёты о недоставке отбрасывались вместе с автоответчиками, и система
не знала, что часть писем ушла в пустоту. Для лимита с прогревом это
критично: рост 40 → 80 писем в день допустим только пока ящик не копит
отбивки, иначе Gmail сам урежет доставку всем письмам разом.

Функции здесь чистые: заголовки и текст на входе, разбор на выходе.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import mailmatch

_BOUNCE_FROM = re.compile(r"^(?:mailer-daemon|postmaster|mail-daemon)@", re.I)
_BOUNCE_SUBJECT = re.compile(
    r"delivery\s+status\s+notification|undeliver(?:able|ed)|"
    r"mail\s+delivery\s+(?:failed|failure|subsystem)|returned\s+mail|"
    r"delivery\s+(?:has\s+)?failed|failure\s+notice|не\s+доставлено|недоставленн", re.I)
# «Delivery Status Notification (Delay)» — сервер ещё пытается, адрес жив.
_TEMPORARY = re.compile(
    r"\(delay\)|delivery\s+(?:is\s+)?delayed|will\s+(?:keep\s+)?(?:retry|trying)|"
    r"temporar\w+\s+(?:problem|failure|error)|status:\s*4\.\d", re.I)
_PERMANENT = re.compile(
    r"status:\s*5\.\d|\b5\.\d\.\d{1,3}\b|\b55[0-4]\b|address\s+not\s+found|"
    r"wasn't\s+delivered|couldn't\s+be\s+delivered|does\s+not\s+exist|no\s+such\s+user|"
    r"user\s+unknown|recipient\s+(?:address\s+)?rejected|mailbox\s+(?:unavailable|not\s+found)|"
    r"\(failure\)", re.I)
_OUR_MSGID = re.compile(r"<jobhunter-(\d+)-(?:initial|followup)@", re.I)
_RECIPIENT = re.compile(
    r"(?:final|original)-recipient:\s*rfc822;\s*<?([^\s<>;]+@[^\s<>;]+?)>?\s*$|"
    r"(?:wasn't|couldn't\s+be)\s+delivered\s+to\s+<?([^\s<>]+@[^\s<>]+?)>?\s+because", re.I | re.M)


@dataclass
class Bounce:
    permanent: bool
    app_id: int = 0
    recipient: str = ""


def is_bounce(headers: dict) -> bool:
    """Отчёт почтового сервера о недоставке, а не письмо человека."""
    sender = mailmatch.addr_of(headers.get("from", ""))
    if sender and _BOUNCE_FROM.match(sender):
        return True
    empty_return = (headers.get("return-path") or "").strip() == "<>"
    return bool(empty_return and _BOUNCE_SUBJECT.search(headers.get("subject", "") or ""))


def parse(body: str, subject: str = "", parse_plus=None) -> Bounce:
    text = body or ""
    temporary = bool(_TEMPORARY.search(subject) or _TEMPORARY.search(text)) \
        and not re.search(r"status:\s*5\.", text, re.I)
    permanent = not temporary and bool(_PERMANENT.search(subject) or _PERMANENT.search(text))
    app_id = 0
    m = _OUR_MSGID.search(text)
    if m:
        app_id = int(m.group(1))
    elif parse_plus:
        app_id = parse_plus(text)
    rec = _RECIPIENT.search(text)
    recipient = (rec.group(1) or rec.group(2)).strip(".").lower() if rec else ""
    return Bounce(permanent=permanent, app_id=app_id, recipient=recipient)
