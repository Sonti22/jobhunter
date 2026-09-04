"""Отправка ответа в уже существующий диалог (тёплое сообщение).

Отличие от outreach/sender.py: там холодный первый контакт с квотой и
сессиями, здесь — ответ человеку, который написал сам. Риск для аккаунта
несопоставим, поэтому отдельный, более щедрый бакет (policy.WARM_REPLY_DAILY),
но те же предохранители: стоп-кран перед каждой отправкой, лок после
PeerFlood, разбор FloodWait как сигнала, а не задержки.
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone

from sqlalchemy import func, select

from ..config import get_settings
from ..db import session_scope
from ..models import Application, Employer, Job, Message, SendLog, utcnow
from ..outreach import policy
from ..outreach.resolver import HandleDead, NotAUser, resolve
from ..tailor.render import resolve_cv
from . import route


def warm_sent_today(sess) -> int:
    """Сколько ОТВЕТОВ ушло сегодня — по журналу сообщений.

    Холодные первые сообщения тоже пишутся в messages, но тёплый бакет
    должны расходовать только реплики в диалогах — фильтруем по признаку
    «рекрутёр уже отвечал» (first_reply_at). Иначе 30 холодных ТГ + 40
    писем съедают лимит ответов ещё до первого разговора.
    """
    start = (datetime.now(timezone.utc)
             .replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None))
    return int(sess.scalar(
        select(func.count(Message.id))
        .join(Application, Message.application_id == Application.id)
        .where(Message.direction == "out", Message.sent_at >= start,
               Application.first_reply_at.is_not(None))) or 0)


def can_reply(sess) -> tuple:
    """(можно, причина). Тёплые ответы не расходуют холодную квоту."""
    if policy.kill_switch_active():
        return False, "стоп-кран: %s" % get_settings().kill_switch.name
    st = policy.get_state(sess)
    if st.manual_only:
        return False, "ручной режим после двух PeerFlood"
    lk = policy.get_lock(sess)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if lk.locked_until and lk.locked_until > now and lk.scope == "all":
        return False, "лок до %s (%s)" % (lk.locked_until, lk.reason)
    n = warm_sent_today(sess)
    if n >= policy.WARM_REPLY_DAILY:
        return False, "дневной лимит ответов исчерпан (%d)" % n
    return True, "%d/%d за сегодня" % (n, policy.WARM_REPLY_DAILY)


async def send_reply(client, app_id: int, text: str, attach_cv: bool = False,
                     is_auto: bool = True, dry: bool = False,
                     rng: random.Random | None = None) -> str:
    """'ok' | 'skipped:<причина>' | 'stop:<причина>'."""
    rng = rng or random.Random()
    if not (text or "").strip():
        return "skipped:пустой текст"

    with session_scope() as sess:
        ok, why = can_reply(sess)
        if not ok:
            return "stop:%s" % why
        app = sess.get(Application, app_id)
        if not app:
            return "skipped:заявка %d не найдена" % app_id
        job = sess.get(Job, app.job_id)
        channel, peer = route.channel_for(app, job)
        cv_path = app.cv_path

    # Развилка по каналу стоит ПОСЛЕ can_reply: стоп-кран, ручной режим и
    # лимит вежливости обязаны действовать одинаково на оба транспорта.
    if channel == route.EMAIL:
        from .send_email import send_reply_email
        return await send_reply_email(app_id, text, attach_cv=attach_cv,
                                      is_auto=is_auto, dry=dry)
    if channel != route.TELEGRAM:
        return "skipped:нет хендла"
    handle = peer

    if dry:
        print("      [dry-run] ответ @%s (%d симв.%s)"
              % (handle, len(text), ", + резюме" if attach_cv else ""))
        return "ok"

    from telethon import errors

    with session_scope() as sess:
        try:
            peer = await resolve(client, sess, handle)
        except HandleDead:
            return "skipped:хендл мёртв"
        except NotAUser:
            return "skipped:не пользователь"

    try:
        async with client.action(peer.user_id, "typing"):
            await asyncio.sleep(policy.typing_seconds(text, rng))
    except Exception:
        pass

    try:
        cv_real = resolve_cv(cv_path) if attach_cv else ""
        if cv_real:
            sent = await client.send_file(peer.user_id, cv_real,
                                          caption=text[:1024], force_document=True)
        else:
            sent = await client.send_message(peer.user_id, text)
    except errors.FloodWaitError as e:
        with session_scope() as sess:
            policy.on_flood_wait(sess, int(e.seconds))
            sess.add(SendLog(application_id=app_id, result="flood",
                             error_class="FloodWaitError",
                             error_seconds=int(e.seconds), peer_id=handle))
        return "stop:FloodWait %ds" % e.seconds
    except errors.PeerFloodError as e:
        with session_scope() as sess:
            policy.on_peer_flood(sess, str(e)[:120])
            sess.add(SendLog(application_id=app_id, result="peerflood",
                             error_class="PeerFloodError", peer_id=handle))
        return "stop:PeerFloodError"
    except Exception as e:
        with session_scope() as sess:
            sess.add(SendLog(application_id=app_id, result="error",
                             error_class=type(e).__name__, peer_id=handle))
        return "skipped:%s" % type(e).__name__

    record_outbound(app_id, channel="telegram", peer="@" + handle, text=text,
                    is_auto=is_auto, cv_path=cv_real,
                    tg_msg_id=getattr(sent, "id", None))

    from ..outreach import folder
    await folder.add_to_folder(client, peer.user_id)
    return "ok"


def record_outbound(app_id: int, *, channel: str, peer: str, text: str,
                    is_auto: bool, cv_path: str = "",
                    tg_msg_id: int | None = None,
                    email_message_id: str = "") -> None:
    """Единственное место, где отправленный ответ попадает в БД и архив.

    Общее для Telegram и почты намеренно: если каждый транспорт ведёт учёт
    сам, то лимит вежливости, счётчики работодателя и архив на диске
    разъезжаются между каналами при первой же правке одного из них — и
    расхождение замечают через недели, когда цифры перестают сходиться.
    """
    from ..outreach import archive

    with session_scope() as sess:
        app = sess.get(Application, app_id)
        if not app:
            return
        job = sess.get(Job, app.job_id)
        job_title = (job.title or job.tag) if job else ""
        company = job.company_name if job else ""
        app.last_outbound_at = utcnow()
        if is_auto:
            app.auto_replies_count = (app.auto_replies_count or 0) + 1
        sess.add(Message(application_id=app_id, direction="out",
                         telegram_msg_id=tg_msg_id, body=text,
                         sent_at=utcnow(), is_auto=is_auto,
                         email_message_id=email_message_id))
        if email_message_id:
            refs = list(app.email_thread_refs or [])
            if email_message_id not in refs:
                refs.append(email_message_id)
            app.email_thread_refs = refs[-10:]
        sess.add(SendLog(application_id=app_id, result="ok", peer_id=peer))
        policy.register_sent(sess, cold=False)
        if app.employer_id:
            emp = sess.get(Employer, app.employer_id)
            if emp:
                emp.total_messages_sent += 1

    archive.record(app_id, channel, peer, text, job_title=job_title,
                   company=company, cv_path=cv_path,
                   kind="auto-reply" if is_auto else "owner-reply")
