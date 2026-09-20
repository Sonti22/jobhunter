"""Цикл входящих писем: IMAP → привязка → те же автоответы и карточки.

Отличие от телеграмного цикла в одном: там опрос идёт ПО ЗАЯВКАМ, здесь —
по ящику целиком, а привязка к заявке происходит уже после. Всё остальное
переиспользуется как есть: классификация, разбор слотов, план ответа,
карточки владельцу, статусы, черновики от LLM. Дублировать эту логику для
второго канала значило бы гарантировать, что однажды каналы разойдутся в
поведении.

    python -m jobhunter.convo.inbox_email --dry     # разбор без записи в БД
"""
from __future__ import annotations

import asyncio
import logging
import sys
from collections import Counter

from sqlalchemy import select

from ..config import get_settings
from ..db import session_scope
from ..models import Application, ContactKind, Job, Message, Status
from ..textutil import clean_email_body
from . import bounce, imapbox, mailmatch
from .engine import LIVE, finish_incoming, handle_message, store_incoming

log = logging.getLogger("inbox_mail")

# Отвечают и через три недели, когда заявка уже закрыта как «без ответа» —
# такое письмо принять надо.
LIVE_EMAIL = LIVE | {Status.NO_REPLY_CLOSED.value}
# По этим статусам письмо сохраняем, но автоматику не запускаем: разговор
# закончен, и любой ответ — дело владельца.
TERMINAL_NOTIFY = {Status.REJECTED_BY_EMPLOYER.value, Status.WITHDRAWN.value}


def build_context() -> mailmatch.MatchContext:
    """Снимок БД для привязки: один запрос на проход, дальше чистые функции."""
    ctx = mailmatch.MatchContext()
    with session_scope() as sess:
        rows = sess.execute(
            select(Application, Job)
            .join(Job, Application.job_id == Job.id)
            .where(Job.contact_kind == ContactKind.EMAIL.value)).all()
        live_ids = []
        for app, job in rows:
            addr = (job.contact_url or "").replace("mailto:", "").strip().lower()
            if (app.status in LIVE_EMAIL or app.status in TERMINAL_NOTIFY or
                    (app.status == Status.APPROVED.value and app.sent_at)):
                live_ids.append(app.id)
                if app.email_peer:
                    ctx.by_peer[app.email_peer.strip().lower()] = app.id
                if addr:
                    ctx.by_employer.setdefault(addr, []).append(app.id)
                    if "@" in addr:
                        ctx.known_domains.add(addr.split("@")[-1])
                    org = mailmatch.org_domain(addr)
                    if org:
                        ctx.by_domain.setdefault(org, []).append(app.id)
                ctx.subjects[app.id] = job.title or job.tag or ""
        if live_ids:
            for msg in sess.scalars(
                    select(Message)
                    .where(Message.application_id.in_(live_ids),
                           Message.email_message_id != "")).all():
                ctx.by_msgid[msg.email_message_id] = msg.application_id
    return ctx


def _parse_plus(text: str) -> int:
    from ..outreach.mailer import parse_reply_to
    return parse_reply_to(text)


async def process(dry: bool = False) -> dict:
    """Проход с наблюдаемым остатком; ошибка никогда не сдвигает границу."""
    from ..observability import record
    s = get_settings()
    stats = {"seen": 0, "matched": 0, "incoming": 0, "auto": 0, "escalated": 0,
             "closed": 0, "skipped": 0, "bodies": 0, "bounces": 0, "by_rule": Counter(),
             "ambiguous": 0, "processed": 0, "remaining": None,
             "folder": s.imap_folder, "lookback_days": s.inbox_lookback_days}
    if not dry:
        record("gmail", "running", details=dict(stats))
    try:
        result = await _process(dry, stats)
    except Exception as exc:
        stats["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:220])
        result = stats
    try:
        result["retried"] = await retry_stuck(dry=dry)
    except Exception as exc:                              # noqa: BLE001
        log.warning("повтор зависших писем: %s: %s", type(exc).__name__, str(exc)[:160])
    if not dry:
        status = "error" if result.get("error") else "partial" if result.get("remaining") else "ok"
        record("gmail", status, details=result, error=result.get("error", ""))
        if status == "ok":
            # Ключ «gmail» в начале каждого прохода получает статус running, поэтому по нему
            # нельзя узнать, когда проход последний раз дошёл до конца. Смена транспорта почты
            # (imapbox._resume_ts) стартует именно с этого момента.
            record("gmail_ok", "ok")
    return result


async def _process(dry: bool, stats: dict) -> dict:
    s = get_settings()
    if not s.imap_enabled:
        return dict(stats, error="IMAP выключен")

    try:
        conn = imapbox.connect()
    except imapbox.MailboxError as e:
        if str(e).startswith("сеть:") and not dry:
            _notify_network(str(e))
        return dict(stats, error=str(e))

    try:
        uids, validity, _reset = imapbox.new_uids(conn)
        stats["seen"] = len(uids)
        stats["available"] = getattr(conn, "_jobhunter_pending_count", len(uids))
        stats["remaining"] = stats["available"]
        if not uids:
            if not dry:
                imapbox.advance_watermark([], validity)
            return stats

        ctx = build_context()
        matched = []                      # (app_id, uid, headers, rule)
        bounces = []                      # (headers, тело отчёта о недоставке)
        for uid, headers in imapbox.fetch_headers(conn, uids):
            if bounce.is_bounce(headers):
                # Раньше отбрасывалось как «адрес автоматики» — и мёртвые
                # адреса продолжали числиться живыми заявками.
                body, _ = imapbox.fetch_body(conn, uid)
                stats["bodies"] += 1
                bounces.append((headers, body))
                continue
            cand = mailmatch.match_by_headers(headers, ctx, _parse_plus)
            if cand.drop_reason:
                stats["skipped"] += 1
                continue
            if cand.ambiguous:
                stats["ambiguous"] += 1
                if not dry:
                    _notify_ambiguous(headers, cand.ambiguous)
                continue
            if not cand.need_body:
                # Постороннее письмо: тело не скачивается вовсе. Именно так
                # выражается «в базу попадает только связанное с откликами».
                stats["skipped"] += 1
                continue

            body, is_html = imapbox.fetch_body(conn, uid)
            stats["bodies"] += 1
            if not cand.matched:
                # Последний шанс: plus-адрес в цитате пересланного письма.
                unresolved = cand.unresolved
                cand = mailmatch.match_by_body(body, ctx, _parse_plus)
                if not cand.matched:
                    if unresolved:
                        stats["ambiguous"] += 1
                        if not dry:
                            _notify_ambiguous(headers, unresolved, by_domain=True)
                    else:
                        stats["skipped"] += 1
                    continue
            text = clean_email_body(body, is_html=is_html)
            matched.append((cand.app_id, uid, headers, cand.rule, text))
            stats["matched"] += 1
            stats["by_rule"][cand.rule] += 1
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    for headers, body in bounces:
        verdict = record_bounce(headers, body, dry=dry)
        log.info("   отбивка %s: %s", (headers.get("subject", "") or "")[:60], verdict)
        if verdict.startswith("отбивка"):
            stats["bounces"] += 1

    for app_id, uid, headers, rule, text in matched:
        verdict = await _handle_one(app_id, uid, headers, rule, text, dry=dry)
        log.info("   #%s uid=%s (%s): %s", app_id, uid, rule, verdict)
        if verdict.startswith("автоответ"):
            stats["auto"] += 1
        elif verdict.startswith(("эскалация", "слоты")):
            stats["escalated"] += 1
        elif verdict.startswith("отказ"):
            stats["closed"] += 1
        stats["incoming"] += 1

    if not dry:
        imapbox.advance_watermark(uids, validity)
    stats["processed"] = len(uids)
    stats["last_uid"] = max(uids)
    stats["remaining"] = max(0, stats["available"] - len(uids))
    stats["by_rule"] = dict(stats["by_rule"])
    return stats


async def _handle_one(app_id: int, uid: int, headers: dict, rule: str,
                      text: str, dry: bool = False) -> str:
    """Сохранить письмо и, если уместно, пустить по общей логике ответов."""
    from ..textutil import clean_email_body  # noqa: F401  (док-ссылка)

    sender = mailmatch.addr_of(headers.get("from", ""))
    extra = {"email_message_id": mailmatch.MSGID_RE.search(
                 headers.get("message-id", "") or "").group(0)
             if mailmatch.MSGID_RE.search(headers.get("message-id", "") or "")
             else "",
             "email_in_reply_to": headers.get("in-reply-to", "")[:200],
             "email_from": sender,
             "email_subject": (headers.get("subject", "") or "")[:300],
             "match_rule": rule}

    if dry:
        return "dry: %s → заявка #%d" % (sender, app_id)

    received = imapbox.msg_date(headers)
    new_ids = store_incoming(app_id, [(uid, text or "(пустое письмо)",
                                       received, extra)], channel="email")
    if not new_ids:
        return "уже было"

    with session_scope() as sess:
        status = sess.get(Application, app_id).status
    if status in TERMINAL_NOTIFY:
        finish_incoming(app_id, new_ids, channel="email")
        return "сохранено, тред закрыт"
    if not text:
        # Письмо целиком из цитаты: факт ответа зафиксирован, а
        # классифицировать нечего — пустой текст даст ложный UNKNOWN.
        finish_incoming(app_id, new_ids, channel="email")
        return "сохранено, нового текста нет"

    # client=None: handle_message передаёт его только в send_reply, а тот для
    # почтовой заявки уходит в SMTP и клиента не касается.
    try:
        verdict = await handle_message(None, app_id, text, dry=dry)
    except Exception as exc:
        finish_incoming(app_id, new_ids, channel="email", error=type(exc).__name__)
        raise
    finish_incoming(app_id, new_ids, channel="email", error=verdict if "не ушёл" in verdict else "")
    return verdict


def _app_by_address(sess, addr: str) -> int:
    """Последняя отправленная заявка на этот адрес."""
    if not addr:
        return 0
    for app, job in sess.execute(
            select(Application, Job).join(Job, Application.job_id == Job.id)
            .where(Job.contact_kind == ContactKind.EMAIL.value,
                   Application.sent_at.is_not(None))
            .order_by(Application.sent_at.desc())).all():
        if (job.contact_url or "").replace("mailto:", "").strip().lower() == addr:
            return app.id
    return 0


def record_bounce(headers: dict, body: str, dry: bool = False) -> str:
    """Постоянная отбивка: адрес «не писать», заявка закрыта, владелец знает."""
    from .. import notify
    from ..models import Employer, SendLog, utcnow
    from ..outreach import policy

    b = bounce.parse(body, headers.get("subject", "") or "", _parse_plus)
    if not b.permanent:
        return "временная задержка доставки — сервер повторит сам"
    with session_scope() as sess:
        app_id = b.app_id or _app_by_address(sess, b.recipient)
        app = sess.get(Application, app_id) if app_id else None
        if app is None:
            return "отчёт не о наших письмах"
        if sess.scalar(select(SendLog.id).where(SendLog.application_id == app.id,
                                                SendLog.result == "bounce").limit(1)):
            return "уже учтено"
        job = sess.get(Job, app.job_id)
        addr = b.recipient or (job.contact_url or "").replace("mailto:", "").strip().lower()
        if dry:
            return "отбивка (dry): #%d %s" % (app.id, addr)
        reason = "отбивка: адрес %s не принимает почту" % addr
        sess.add(SendLog(application_id=app.id, result="bounce", error_class="DSN",
                         peer_id=addr, attempted_at=utcnow()))
        app.followup_body = ""
        if app.status in (Status.APPROVED.value, Status.SEND_FAILED.value):
            app.advance(Status.WITHDRAWN, reason=reason)
        else:
            app.advance(Status.NO_REPLY_CLOSED, reason=reason)
        emp = sess.get(Employer, app.employer_id) if app.employer_id else None
        if emp is not None:
            emp.do_not_contact = True
        notify.push("mail_bounce",
                    "📭 Письмо не доставлено: %s\n#%d · %s\nАдрес помечен «не писать»."
                    % (addr, app.id, (job.title or job.tag or "")[:60]),
                    dedup="bounce:%d" % app.id, sess=sess)
        sess.flush()
        today = policy.email_bounces_today(sess)
        if today > get_settings().email_bounce_stop:
            notify.push("error",
                        "🛑 Отбивок за сегодня: %d. Почтовая отправка остановлена до "
                        "завтра, прогрев лимита начнётся заново — это защита репутации "
                        "ящика в Gmail." % today,
                        dedup="bounce_stop:%s" % utcnow().date().isoformat(), sess=sess)
    return "отбивка: #%d %s" % (app_id, addr)


# Так помечались письма, чей автоответ заблокировала политика. Повтора у них
# не было: Astoria AI (next steps) и SearchAtlas (подать по ссылке) неделю
# лежали «в обработке». Сбои-исключения сюда не входят — их разбирает владелец.
STUCK_MARK = "автоответ не ушёл"


async def retry_stuck(dry: bool = False, limit: int = 10) -> int:
    """Прогнать зависшие ответы заново. Возвращает число заявок.

    Телеграм-ответы берём, только пока Telegram в ручном режиме: тогда повтор не
    отправляет ничего, а даёт владельцу карточку. Проверка 19.09: рекрутёр 8.09
    попросил резюме, автоответ упёрся в ручной режим, и сообщение 11 дней висело
    «в обработке» без карточки — повтор был только у почтовых вакансий.
    """
    from . import route
    from .send import can_reply

    with session_scope() as sess:
        kinds = [ContactKind.EMAIL.value]
        if not can_reply(sess, route.TELEGRAM)[0]:
            kinds.append(ContactKind.USER_HANDLE.value)
        rows = sess.execute(
            select(Message.id, Message.application_id, Message.body)
            .join(Application, Message.application_id == Application.id)
            .join(Job, Application.job_id == Job.id)
            .where(Message.direction == "in", Message.processing_pending.is_(True),
                   Message.processing_error.startswith(STUCK_MARK),
                   Job.contact_kind.in_(kinds))
            .order_by(Message.id)).all()
    by_app: dict = {}
    for msg_id, app_id, body in rows:
        by_app.setdefault(app_id, []).append((msg_id, body or ""))
    done = 0
    for app_id, items in list(by_app.items())[:limit]:
        text = "\n\n".join(body for _, body in items)
        if dry:
            log.info("   #%d: повтор %d зависших писем (dry)", app_id, len(items))
            done += 1
            continue
        verdict = await handle_message(None, app_id, text)
        with session_scope() as sess:
            for msg_id, _ in items:
                msg = sess.get(Message, msg_id)
                stuck = "не ушёл" in verdict
                msg.processing_pending = stuck
                msg.processing_error = verdict[:200] if stuck else ""
        log.info("   #%d: повтор зависших писем: %s", app_id, verdict)
        done += 1
    return done


def _notify_network(error: str) -> None:
    """Ящик недоступен по сети — владелец узнаёт сразу и с причиной.

    Раньше это была только строка WARNING в логе; за 12 дней так пропало 12%
    проходов, включая три часа подряд 14.09, и никто не заметил. Дедуп по
    дню: одна авария — одно сообщение.
    """
    from .. import notify
    from ..models import utcnow
    notify.push("error", "📪 Почта недоступна: %s\n%s" % (error[:160], imapbox.network_hint()),
                dedup="mail_net:%s" % utcnow().date().isoformat())


def _notify_ambiguous(headers: dict, apps: list, by_domain: bool = False) -> None:
    """Несколько заявок в одну компанию — решает владелец, а не эвристика.

    Гадать нельзя: цена ошибки — подтверждение интервью не по той вакансии.

    by_domain — совпал только домен компании. HR-платформа Сбера шлёт
    «Пройдите AI-интервью» по откликам владельца с hh.ru; к заявкам бота они
    не относятся, а 17.09 таких уведомлений пришло пять за день. Для домена —
    одно в сутки на отправителя и честная формулировка.
    """
    from .. import notify
    from ..models import utcnow
    sender = mailmatch.addr_of(headers.get("from", "")) or "?"
    subject = (headers.get("subject", "") or "")[:120]
    ids = ", ".join("#%d" % a for a in apps[:6])
    if by_domain:
        notify.push("mail_ambiguous",
                    "📧 Письмо от %s — с домена компании, куда бот откликался (%s), но по "
                    "теме к этим заявкам не относится. Возможно, это твой отклик с другого "
                    "сайта — посмотри в почте.\nТема: %s" % (sender, ids, subject),
                    dedup="ambig_domain:%s:%s" % (sender, utcnow().date().isoformat()))
        return
    notify.push("mail_ambiguous",
                "📧 Письмо от %s не удалось привязать однозначно.\n"
                "Тема: %s\nПодходят заявки: %s" % (sender, subject, ids),
                dedup="ambig:%s" % (headers.get("message-id", "") or "")[:120])


async def run(dry: bool = False) -> dict:
    return await process(dry=dry)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Входящие письма")
    ap.add_argument("--dry", action="store_true",
                    help="разобрать и показать, ничего не записывая")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    stats = asyncio.run(run(dry=args.dry))
    if stats.get("error"):
        print("Ошибка: %s" % stats["error"])
        return 2
    print("просмотрено %(seen)d, тел скачано %(bodies)d, привязано %(matched)d, "
          "пропущено %(skipped)d, неоднозначных %(ambiguous)d" % stats)
    if stats["by_rule"]:
        print("правила: %s" % ", ".join("%s=%d" % kv
                                        for kv in stats["by_rule"].items()))
    print("входящих %(incoming)d, автоответов %(auto)d, эскалаций %(escalated)d"
          % stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
