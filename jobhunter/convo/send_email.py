"""Ответ рекрутёру по почте — зеркало convo/send.py для SMTP.

Контракт возврата тот же ('ok' | 'skipped:…' | 'stop:…'), поэтому вызывающий
код — и цикл входящих, и исполнение решений владельца — не знает, каким
транспортом ушёл ответ.

Клиента Telethon в сигнатуре нет намеренно: почте он не нужен, а лишний
параметр рано или поздно превращается в случайную зависимость.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from ..config import get_settings
from ..db import session_scope
from ..models import Application, Job, Message, SendLog
from ..outreach import policy
from . import route
from .send import record_outbound

log = logging.getLogger("send_email")


def _thread_headers(app_id: int) -> tuple:
    """(in_reply_to, references, тема ответа) для попадания в тот же тред."""
    with session_scope() as sess:
        app = sess.get(Application, app_id)
        job = sess.get(Job, app.job_id) if app else None
        last_in = sess.scalars(
            select(Message)
            .where(Message.application_id == app_id,
                   Message.direction == "in",
                   Message.email_message_id != "")
            .order_by(Message.id.desc()).limit(1)).first()
        parent = last_in.email_message_id if last_in else ""
        subject = (last_in.email_subject if last_in else "") or ""
        if not parent:
            last_out = sess.scalars(
                select(Message)
                .where(Message.application_id == app_id,
                       Message.direction == "out",
                       Message.email_message_id != "")
                .order_by(Message.id.desc()).limit(1)).first()
            parent = last_out.email_message_id if last_out else ""
            subject = subject or (last_out.email_subject if last_out else "")
        refs = list(app.email_thread_refs or []) if app else []
        if not subject and job:
            from ..outreach.mailer import _subject
            subject = _subject(job, app.cv_lang or "ru")
    low = subject.lower()
    if not low.startswith(("re:", "ответ:")):
        subject = "Re: " + subject if subject else "Re: отклик"
    return parent, refs, subject


def _send_blocking(msg) -> None:
    from ..outreach.mailer import smtp_session
    with smtp_session() as server:
        server.send_message(msg)


async def send_reply_email(app_id: int, text: str, attach_cv: bool = False,
                           is_auto: bool = True, dry: bool = False) -> str:
    """'ok' | 'skipped:<причина>' | 'stop:<причина>'."""
    from ..outreach.mailer import build_message

    s = get_settings()
    if not (text or "").strip():
        return "skipped:пустой текст"
    if policy.kill_switch_active():
        return "stop:стоп-кран"
    if is_auto and not s.email_auto_reply_enabled:
        return "skipped:автоответы по почте выключены"

    with session_scope() as sess:
        app = sess.get(Application, app_id)
        if not app:
            return "skipped:заявка %d не найдена" % app_id
        job = sess.get(Job, app.job_id)
        channel, addr = route.channel_for(app, job)
        cv_path = app.cv_path if attach_cv else ""
        last_out = app.last_outbound_at
    if channel != route.EMAIL or "@" not in addr:
        return "skipped:нет адреса"

    # Защита от петли: два автоответчика, переписывающиеся друг с другом,
    # сходятся к этому интервалу на итерацию, а лимит ответов на тред
    # обрывает их окончательно.
    if is_auto and last_out:
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if now - last_out < timedelta(minutes=s.email_reply_min_gap_min):
            return "skipped:слишком частый ответ"

    parent, refs, subject = _thread_headers(app_id)
    if dry:
        print("      [dry-run] письмо %s | тема: %s | %d симв.%s"
              % (addr, subject[:48], len(text), ", + резюме" if cv_path else ""))
        return "ok"

    msg = build_message(to=addr, subject=subject, body=text, cv_path=cv_path,
                        app_id=app_id, in_reply_to=parent, references=refs,
                        auto=is_auto)
    try:
        # smtplib блокирующий, а цикл входящих асинхронный: без выноса в
        # поток таймаут SMTP в 30 секунд заморозил бы весь проход по почте.
        await asyncio.to_thread(_send_blocking, msg)
    except Exception as e:
        with session_scope() as sess:
            sess.add(SendLog(application_id=app_id, result="error",
                             error_class=type(e).__name__, peer_id=addr))
        log.warning("письмо %s не ушло: %s: %s", addr, type(e).__name__,
                    str(e)[:120])
        return "skipped:%s" % type(e).__name__

    record_outbound(app_id, channel="email", peer=addr, text=text,
                    is_auto=is_auto, cv_path=cv_path,
                    email_message_id=msg.get("Message-ID", ""))
    return "ok"
