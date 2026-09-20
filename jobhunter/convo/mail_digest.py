"""Сводка почты для владельца: «что там в ящике, какие дела».

Владелец попросил, чтобы бот сообщал о состоянии почты целиком, а не только
о письмах рекрутёров. Дайджест собирается из ОДНИХ ЗАГОЛОВКОВ (From/Subject/
Date по BODY.PEEK, ящик открыт readonly): тела не скачиваются, в базу ничего
не пишется, флаг «прочитано» не ставится. Это сознательная граница — бот
рассказывает владельцу о его почте, но не хранит её.

Письма делятся на «по откликам» (адрес отправителя знаком системе — есть
такой Employer) и «прочее». Первые обрабатываются отдельным конвейером
convo/inbox_email; здесь они только считаются, чтобы сводка была полной.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..db import session_scope
from ..models import Employer
from . import imapbox

MAX_LINES = 12          # больше в одно сообщение бота не влезает читаемо

_ADDR = re.compile(r"<([^>]+)>")


def _sender(raw_from: str) -> tuple:
    """(имя, адрес) из заголовка From."""
    raw = (raw_from or "").strip()
    m = _ADDR.search(raw)
    if m:
        addr = m.group(1).strip().lower()
        name = raw[:m.start()].strip().strip('"')
        return name or addr, addr
    return raw, raw.lower()


def collect(hours: int = 24, limit: int = 60) -> dict:
    """Заголовки непрочитанных писем за последние `hours` часов."""
    conn = imapbox.connect()
    try:
        if getattr(conn, "is_gmail_api", False):
            uids = conn.unseen_uids(hours, limit)
        else:
            typ, _ = conn.select("INBOX", readonly=True)
            if typ != "OK":
                return {"error": "IMAX SELECT failed"}
            since = (datetime.now(timezone.utc) - timedelta(hours=hours))
            typ, data = conn.uid("SEARCH", None,
                                 "UNSEEN SINCE %s" % since.strftime("%d-%b-%Y"))
            if typ != "OK":
                return {"error": "SEARCH failed"}
            uids = [int(u) for u in (data[0] or b"").split()][-limit:]
        headers = imapbox.fetch_headers(conn, uids) if uids else []
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    known = set()
    with session_scope() as sess:
        for e in sess.scalars(select(Employer.handle_norm)).all():
            if "@" in (e or ""):
                known.add(e.lower())

    job_mail, other = [], []
    for _uid, h in headers:                 # fetch_headers отдаёт (uid, dict)
        name, addr = _sender(h.get("from", ""))
        item = {"from": name[:40], "addr": addr,
                "subject": (h.get("subject") or "(без темы)")[:70]}
        (job_mail if addr in known else other).append(item)
    return {"unseen": len(headers), "job": job_mail, "other": other,
            "hours": hours}


def render(d: dict) -> str:
    """Текст сводки для бота."""
    if d.get("error"):
        return "📬 Почта: не удалось проверить (%s)" % d["error"]
    if not d.get("unseen"):
        return ("📬 Почта: непрочитанных за последние %d ч нет — всё "
                "разобрано." % d.get("hours", 24))
    lines = ["📬 Почта за %d ч: непрочитанных %d"
             % (d.get("hours", 24), d["unseen"])]
    if d["job"]:
        lines.append("")
        lines.append("ПО ОТКЛИКАМ (%d) — карточки придут отдельно:" % len(d["job"]))
        for it in d["job"][:MAX_LINES // 2]:
            lines.append("  • %s — %s" % (it["from"], it["subject"]))
    if d["other"]:
        lines.append("")
        lines.append("ПРОЧЕЕ (%d):" % len(d["other"]))
        for it in d["other"][:MAX_LINES]:
            lines.append("  • %s — %s" % (it["from"], it["subject"]))
        rest = len(d["other"]) - MAX_LINES
        if rest > 0:
            lines.append("  … и ещё %d" % rest)
    return "\n".join(lines)


def run(hours: int = 24) -> str:
    return render(collect(hours=hours))
