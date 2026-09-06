"""Цикл переписки: входящие → классификация → автоответ или карточка владельцу.

Один проход process_inbox() делает четыре вещи, в этом порядке:

  1. Читает новые входящие от рекрутёров по всем живым заявкам и пишет их
     в журнал messages. Источник правды о «новом» — Application.last_inbound_msg_id,
     а не флаг unread: непрочитанность сбивается чтением с телефона.
  2. Для каждого входящего решает судьбу через plan_reply():
     рутина (резюме / «когда созвон» / вежливое «спасибо») — автоответ шаблоном;
     время интервью — парсинг слотов и карточка владельцу;
     техвопрос / деньги / оффер / непонятное — NEEDS_HUMAN, черновик от LLM
     и карточка владельцу. Автоматика НИКОГДА не отвечает на эскалированное.
  3. Разносит карточки в «Избранное» и читает команды владельца (/ok, /send…).
  4. Закрывает просроченные карточки.

Отказ рекрутёра — терминал: заявка закрывается, письмо вежливости не шлём
(нечего выжимать из «мы выбрали другого кандидата»).
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..config import get_settings
from ..db import session_scope
from ..models import (
    Application,
    Employer,
    Job,
    Message,
    OwnerRequestKind,
    PendingReply,
    Status,
    utcnow,
)
from ..textutil import norm_hash
from . import classify as C
from . import route
from .reply import plan_reply, reply_delay_seconds, within_reply_window
from .send import send_reply
from .slots import parse_slots

# Статусы, в которых есть смысл слушать входящие.
LIVE = {Status.SENT.value, Status.AWAITING_REPLY.value, Status.FOLLOWED_UP.value,
        Status.FOLLOWUP_PENDING_APPROVAL.value,
        Status.REPLIED.value, Status.IN_DIALOGUE.value, Status.NEEDS_HUMAN.value,
        Status.INTERVIEW_PROPOSED.value, Status.INTERVIEW_CONFIRMED.value}


# ─────────────────────────────────────────────────── чтение входящих ──

async def fetch_incoming(client, progress: dict | None = None) -> list:
    """Новые входящие по всем живым заявкам.

    Возвращает [(app_id, [(msg_id, text, received_utc), ...]), ...].
    Идём по своим заявкам, а не по всем диалогам аккаунта: бот не должен
    даже читать переписки, не относящиеся к поиску работы.
    """
    with session_scope() as sess:
        rows = sess.scalars(
            select(Application).where(
                Application.status.in_(LIVE) |
                ((Application.status == Status.APPROVED.value) &
                 Application.sent_at.is_not(None)))).all()
        targets = []
        for a in rows:
            job = sess.get(Job, a.job_id)
            h = (job.contact_handle or "").lstrip("@")
            if h:
                targets.append((a.id, h, int(a.last_inbound_msg_id or 0)))

    out = []
    progress = progress if progress is not None else {}
    progress.update(dialogs=len(targets), scanned=0, limited_dialogs=0, errors=0,
                    cursors={}, remaining=0)
    for app_id, handle, last_id in targets:
        try:
            batch = []
            fetched, cursor = 0, last_id
            async for m in client.iter_messages(handle, limit=200, min_id=last_id, reverse=True):
                fetched += 1
                cursor = max(cursor, m.id)
                if m.out:
                    continue
                body = (m.message or "").strip()
                if not body and getattr(m, "media", None):
                    body = "[Входящее вложение без текста. Требуется просмотр владельцем в Telegram.]"
                if not body:
                    continue
                received = m.date or datetime.now(timezone.utc)
                batch.append((m.id, body, received))
            progress["scanned"] += fetched
            progress["cursors"][app_id] = cursor
            if fetched >= 200:
                progress["limited_dialogs"] += 1
                progress["remaining"] = None
            if batch:
                batch.sort(key=lambda x: x[0])          # от старых к новым
                out.append((app_id, batch))
        except Exception as e:
            progress["errors"] += 1
            progress["remaining"] = None
            # Обрыв на одном диалоге не должен останавливать остальные.
            print("      входящие @%s: %s" % (handle, type(e).__name__))
        await asyncio.sleep(random.uniform(1.5, 4.0))    # не частим по API
    return out


def store_incoming(app_id: int, batch: list, channel: str = "telegram") -> list:
    """Пишет входящие в журнал, двигает водяной знак. Возвращает новые id.

    Элемент batch — (id, текст, дата) либо (id, текст, дата, доп. поля).
    Четвёртый элемент необязателен: телеграмный цикл его не передаёт, и
    трёхэлементные кортежи должны продолжать работать как раньше.
    """
    from .. import notify

    new_ids = []
    with session_scope() as sess:
        app = sess.get(Application, app_id)
        for item in batch:
            msg_id, text, received = item[0], item[1], item[2]
            extra = dict(item[3]) if len(item) > 3 else {}
            if channel == "telegram":
                if msg_id <= (app.last_inbound_msg_id or 0):
                    continue
                chan_fields = {"telegram_msg_id": msg_id}
            else:
                # Дедуп по Message-ID: он уникален по стандарту и переживает
                # сброс водяного знака при смене uidvalidity, когда часть
                # писем неизбежно приезжает повторно.
                mid = extra.get("email_message_id", "")
                if mid and sess.scalars(
                        select(Message.id)
                        .where(Message.email_message_id == mid)
                        .limit(1)).first():
                    continue
                chan_fields = dict(extra, email_uid=msg_id)
            intent = C.classify(text)
            sess.add(Message(application_id=app_id, direction="in",
                             body=text[:4000], body_hash=norm_hash(text),
                             received_at=received.astimezone(timezone.utc)
                                                 .replace(tzinfo=None),
                             classifier_label=intent.label,
                             classifier_confidence=intent.confidence,
                             processing_pending=True,
                             **chan_fields))
            if channel == "telegram":
                app.last_inbound_msg_id = msg_id
            if not app.first_reply_at:
                app.first_reply_at = utcnow()
            # Поле читают три места (напоминания, рассылка, отправка), но до
            # сих пор его никто не заполнял — защита «не холодить того, кто
            # нам уже писал» была мертва.
            app.last_inbound_at = utcnow()
            if extra.get("email_from"):
                app.email_peer = extra["email_from"]
            new_ids.append(msg_id)

        if new_ids:
            if app.employer_id:
                emp = sess.get(Employer, app.employer_id)
                if emp:
                    emp.last_inbound_at = utcnow()
            if app.status in (Status.SENT.value, Status.AWAITING_REPLY.value,
                              Status.FOLLOWED_UP.value,
                              Status.FOLLOWUP_PENDING_APPROVAL.value) or (
                                  app.status == Status.APPROVED.value and app.sent_at):
                app.advance(Status.REPLIED)
            # Человек ответил — напоминание отменяется. Иначе подготовленное
            # «напоминаю о своём отклике» уйдёт тому, кто только что написал,
            # и выставит систему невнимательной ровно в тот момент, когда
            # разговор наконец начался.
            app.followup_body = ""
            app.followup_due_at = None
            job = sess.get(Job, app.job_id)
            notify.push("reply_received",
                        "💬 Ответил %s по «%s»\n\n%s"
                        % (route.peer_label(app, job),
                           (job.title or job.tag or "")[:60],
                           batch[-1][1].strip()[:400]),
                        dedup="reply:%d:%s" % (app_id, new_ids[-1]), sess=sess)
    return new_ids


def finish_incoming(app_id: int, ids: list, *, channel="telegram", error="") -> None:
    """Не переотправлять автоматически после неопределённого сбоя обработки."""
    field = Message.telegram_msg_id if channel == "telegram" else Message.email_uid
    with session_scope() as sess:
        for msg in sess.scalars(select(Message).where(
                Message.application_id == app_id, Message.direction == "in", field.in_(ids))):
            msg.processing_pending = bool(error)
            msg.processing_error = error[:200]


# ───────────────────────────────────────────────── обработка одного ──

# Пауза перед повторной попыткой классификации, по числу неудач.
LLM_BACKOFF_MIN = (15, 60, 240, 720)
LLM_MAX_ATTEMPTS = 4


def _remember_llm_verdict(app_id: int, v, error: str = "") -> None:
    """Записывает второе мнение LLM в последнее входящее сообщение.

    v is None означает «модель не ответила», а не «мнения нет». Раньше
    такой случай молча выходил, и сообщение оставалось с пустой меткой
    навсегда: за час, когда все провайдеры отдавали 429, так умерло восемь
    диалогов. Теперь неудача планирует повтор с растущей паузой.
    """
    try:
        with session_scope() as sess:
            msg = sess.scalars(
                select(Message).where(Message.application_id == app_id,
                                      Message.direction == "in")
                .order_by(Message.id.desc()).limit(1)).first()
            if msg is None:
                return
            if v is None:
                msg.llm_attempts = (msg.llm_attempts or 0) + 1
                msg.llm_error = (error or "нет ответа")[:120]
                if msg.llm_attempts < LLM_MAX_ATTEMPTS:
                    idx = min(msg.llm_attempts - 1, len(LLM_BACKOFF_MIN) - 1)
                    msg.llm_next_try_at = (
                        datetime.now(timezone.utc).replace(tzinfo=None)
                        + timedelta(minutes=LLM_BACKOFF_MIN[idx]))
                else:
                    msg.llm_next_try_at = None
                return
            msg.llm_label = v.label
            msg.llm_confidence = v.confidence
            msg.llm_next_try_at = None
            msg.llm_error = ""
    except Exception:                                    # noqa: BLE001
        pass


def _record_business_result(sess, app_id: int, text: str, kind: str, intent) -> None:
    """Tie an observed intent to its saved inbound message, including on retries."""
    from ..results import record_event

    message = sess.scalar(select(Message).where(
        Message.application_id == app_id, Message.direction == "in",
        (Message.body_hash == norm_hash(text)) | (Message.body == text[:4000]),
    ).order_by(Message.id.desc()).limit(1))
    if message is not None:
        record_event(sess, app_id, kind, "classifier", "message:%d:%s" % (message.id, kind),
                     occurred_at=message.received_at,
                     details={"message_id": message.id, "label": intent.label,
                              "confidence": intent.confidence, "evidence": "inbound_intent"})


async def handle_message(client, app_id: int, text: str,
                         dry: bool = False) -> str:
    """Решает судьбу одного входящего. Возвращает краткий итог для лога."""
    from .. import owner
    from .draft import draft_reply

    with session_scope() as sess:
        app = sess.get(Application, app_id)
        job = sess.get(Job, app.job_id)
        intent = C.classify(text)
        title = job.title or job.tag or ""
        jd_text = job.description_raw or ""
        history = [(m.direction, m.body) for m in sess.scalars(
            select(Message).where(Message.application_id == app_id)
            .order_by(Message.id.desc()).limit(8)).all()][::-1]

    # Отказ. Закрытие терминально и необратимо, поэтому требует ДВОЙНОГО
    # сигнала: regex уверен (≥REJECTION_CLOSE_MIN) И LLM, прочитав историю
    # треда, согласна (≥0.8). Один regex уже ошибался на живом паттерне:
    # «к сожалению, в четверг не получится, давайте в пятницу» закрывал
    # заявку, по которой рекрутёр предлагал перенос. Любое сомнение — LLM
    # молчит, выключена, не согласна — карточка владельцу, не терминал:
    # день ожидания подтверждения дешевле потерянной сделки.
    if intent.label == C.REJECTION and intent.confidence >= C.CONFIDENCE_MIN:
        from .. import notify
        from .verify import verify_intent
        v = verify_intent(text, history, intent.label)
        _remember_llm_verdict(app_id, v)
        if (intent.confidence >= C.REJECTION_CLOSE_MIN
                and v is not None and v.label == C.REJECTION
                and v.confidence >= 0.8):
            with session_scope() as sess:
                app = sess.get(Application, app_id)
                job = sess.get(Job, app.job_id)
                app.advance(Status.REJECTED_BY_EMPLOYER, reason="отказ рекрутёра")
                if not dry:
                    _record_business_result(sess, app_id, text, "rejected", intent)
                # Раньше закрытие было молчаливым — владелец узнавал о нём
                # из статистики. Отказ — тоже событие.
                notify.push("rejection_closed",
                            "🚫 Отказ по «%s» (%s)\n\n%s"
                            % ((job.title or job.tag or "")[:60],
                               job.company_name or "—", text.strip()[:300]),
                            dedup="reject:%d" % app_id, sess=sess)
            return "отказ (подтверждён LLM), заявка закрыта"
        reason = "похоже на отказ, но я не уверен — /close закроет"
        if v is not None and v.label != C.REJECTION:
            reason = "regex видит отказ, LLM — «%s»" % v.label
        with session_scope() as sess:
            app = sess.get(Application, app_id)
            job = sess.get(Job, app.job_id)
            if app.status != Status.NEEDS_HUMAN.value:
                app.advance(Status.NEEDS_HUMAN)
            app.needs_human_reason = reason[:200]
            if not owner.open_request_for(sess, app_id,
                                          OwnerRequestKind.NEEDS_HUMAN.value):
                owner.create_human_request(sess, app, job, text, reason)
        return "возможный отказ → карточка владельцу (%s)" % reason

    # «Не понял» — единственная метка, где LLM может ПОВЫСИТЬ автоматику:
    # если она с историей треда уверенно узнала рутину или слоты, идём по
    # соответствующей ветке. Опасные метки (деньги, оффер, техвопрос) так
    # получить нельзя — они и без того эскалация, а рутина после этого всё
    # равно проходит все гейты автоответа. Сбой LLM — остаёмся на unknown.
    if intent.label == C.UNKNOWN:
        from .verify import verify_intent
        v = verify_intent(text, history, intent.label)
        _remember_llm_verdict(app_id, v)
        if v is not None and v.confidence >= 0.75 \
                and v.label in (C.ASK_CV, C.ASK_CALL, C.ACK, C.SLOT_PROPOSED):
            intent = C.Intent(v.label, v.confidence, "llm:" + v.reason[:50])

    # Record the observed business signal before any reply or state-machine
    # path. A proposed slot is interest; only an explicit booking proves that
    # an interview was scheduled. Dry previews never append result events.
    if not dry:
        from ..results import classifier_kind
        kind = classifier_kind(intent.label, intent.confidence)
        if kind is not None:
            with session_scope() as sess:
                _record_business_result(sess, app_id, text, kind, intent)

    # Предложение времени — слоты + карточка владельцу. Автоответа нет:
    # подтверждение слота и есть назначение встречи, а его делает человек.
    if intent.label == C.SLOT_PROPOSED:
        slots = parse_slots(text)
        # Черновик подтверждения — чтобы /ok не был одобрением вслепую.
        # Раньше подсказка slot_proposed в draft.py была мёртвым кодом: эта
        # ветка выходила до генерации. Вызов ДО транзакции: LLM думает до
        # 30 секунд, держать под ней запись в SQLite нельзя.
        slot_draft = draft_reply(title, jd_text, text, history,
                                 intent="slot_proposed").text if slots else ""
        with session_scope() as sess:
            app = sess.get(Application, app_id)
            job = sess.get(Job, app.job_id)
            app.advance(Status.INTERVIEW_PROPOSED)
            if not owner.open_request_for(sess, app_id,
                                          OwnerRequestKind.SLOT_CONFIRM.value):
                if slots:
                    owner.create_slot_request(sess, app, job, slots, text,
                                              draft_text=slot_draft)
                else:
                    owner.create_human_request(
                        sess, app, job, text,
                        "предлагают время, но я не разобрал его")
        return "слоты → карточка владельцу (%d вариантов)" % len(slots)

    with session_scope() as sess:
        app = sess.get(Application, app_id)
        plan = plan_reply(app, text, intent=intent, history=history)

    if plan.should_reply:
        if not within_reply_window():
            # Водяной знак входящих уже сдвинут — просто выйти означало бы
            # потерять ответ навсегда. Кладём ВХОДЯЩИЙ текст в очередь;
            # утренний проход подаст его в handle_message заново и решение
            # перепримется целиком, по свежему состоянию заявки.
            h = norm_hash(text)
            with session_scope() as sess:
                dup = sess.scalars(
                    select(PendingReply)
                    .where(PendingReply.application_id == app_id,
                           PendingReply.body_hash == h,
                           PendingReply.processed_at.is_(None))).first()
                if dup is None:
                    sess.add(PendingReply(application_id=app_id,
                                          incoming_text=text, body_hash=h))
            return "автоответ отложен до утра (в очереди)"
        # Живой текст вместо шаблона — если LLM включена и её вариант прошёл
        # все проверки (качество, дословная строка слотов, гейт правды).
        # Любой сбой откатывает на шаблон из plan: тот выдумать ничего
        # не может, и автоответ уходит всегда.
        out_text, out_src = plan.text, "шаблон"
        if get_settings().llm_auto_reply_enabled or plan.needs_draft:
            from .draft import draft_routine_reply
            d = draft_routine_reply(plan.intent, title, jd_text, text,
                                    history, slots_line=plan.slots_line)
            if d.ok:
                out_text, out_src = d.text, "llm"

        if plan.needs_draft and out_src != "llm":
            # Шаблона для этого интента нет: техвопрос и «о себе» пишутся
            # только по фактам профиля. Не смогли написать — зовём владельца,
            # а не отправляем заглушку.
            return await _escalate(client, app_id, text, history, title,
                                   jd_text, plan.intent,
                                   "черновик не получился", dry)

        if plan.needs_review:
            # Самопроверка обязательна и должна ПРОЙТИ. В письмах она
            # fail-open (сбой LLM не должен останавливать рассылку), здесь
            # наоборот: непроверенный ответ живому человеку хуже, чем
            # карточка владельцу.
            from ..tailor.review import review
            verdict = review(out_text, role=title, jd_text=jd_text)
            if not (verdict.checked and verdict.ok):
                return await _escalate(client, app_id, text, history, title,
                                       jd_text, plan.intent,
                                       "самопроверка не пройдена: %s"
                                       % (verdict.reason or "нет проверки"),
                                       dry)
        if not dry:                      # человеческая пауза — только в бою
            await asyncio.sleep(reply_delay_seconds())
        res = await send_reply(client, app_id, out_text,
                               attach_cv=plan.attach_cv, dry=dry)
        if res == "ok" or dry:
            with session_scope() as sess:
                app = sess.get(Application, app_id)
                app.advance(Status.IN_DIALOGUE)
                if plan.intent == "tech_question":
                    app.auto_tech_replies_count = (
                        app.auto_tech_replies_count or 0) + 1
            return "автоответ (%s, %s): %s" % (plan.intent, out_src, res)
        return "автоответ не ушёл: %s" % res

    # Эскалация: черновик от LLM (может быть пустым) + карточка.
    if plan.escalate:
        return await _escalate(client, app_id, text, history, title, jd_text,
                               plan.intent, plan.reason, dry)
    return "без действий (%s)" % plan.reason


async def _escalate(client, app_id: int, text: str, history: list,
                    title: str, jd_text: str, intent: str, reason: str,
                    dry: bool = False) -> str:
    """Отдать тред владельцу: статус, причина и карточка с черновиком.

    Единственная точка эскалации. Смелый режим зовёт её же, когда черновик
    не написался или не прошёл самопроверку: у техвопроса нет шаблонного
    отката, и молча отправить «что-нибудь» нельзя.
    """
    from .. import owner
    from .draft import draft_reply

    d = draft_reply(title, jd_text, text, history, intent=intent)
    with session_scope() as sess:
        app = sess.get(Application, app_id)
        job = sess.get(Job, app.job_id)
        if app.status != Status.NEEDS_HUMAN.value:
            app.advance(Status.NEEDS_HUMAN)
        app.needs_human_reason = (reason or "")[:200]
        if not owner.open_request_for(sess, app_id,
                                      OwnerRequestKind.NEEDS_HUMAN.value):
            owner.create_human_request(sess, app, job, text, reason,
                                       d.text, d.problem)
    return "эскалация (%s), черновик: %s" % (
        reason, "есть" if d.text else "нет — " + (d.problem or "?"))


# ────────────────────────────────────────────────────── полный проход ──

async def drain_pending(client, dry: bool = False,
                        fresh_by_app: dict | None = None) -> int:
    """Отдаёт ночную очередь в обычную обработку. Возвращает число решений.

    fresh_by_app — тексты, пришедшие по тем же заявкам в ЭТОМ проходе:
    ночной и утренний текст склеиваются в одно решение, иначе рекрутёр
    получит два ответа подряд. Просроченные записи не автоотвечаются —
    контекст устарел, их судьбу решает владелец по карточке.
    """
    from .. import owner
    from ..config import get_settings

    fresh_by_app = fresh_by_app or {}
    max_age = timedelta(hours=get_settings().night_queue_max_age_hours)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    with session_scope() as sess:
        rows = [(r.id, r.application_id, r.incoming_text, r.created_at)
                for r in sess.scalars(
                    select(PendingReply)
                    .where(PendingReply.processed_at.is_(None))
                    .order_by(PendingReply.id)).all()]

    done = 0
    seen_apps = set()
    for _row_id, app_id, text, created in rows:
        if app_id in seen_apps:
            # вторая ночная пачка той же заявки уже склеена первой
            continue
        seen_apps.add(app_id)
        merged = text
        extra = fresh_by_app.pop(app_id, "")
        if extra:
            merged = text + "\n" + extra

        if created and now - created > max_age:
            with session_scope() as sess:
                app = sess.get(Application, app_id)
                job = sess.get(Job, app.job_id) if app else None
                if app is not None and job is not None:
                    if app.status != Status.NEEDS_HUMAN.value:
                        app.advance(Status.NEEDS_HUMAN)
                    if not owner.open_request_for(
                            sess, app_id, OwnerRequestKind.NEEDS_HUMAN.value):
                        owner.create_human_request(
                            sess, app, job, merged,
                            "ответ ждал в ночной очереди слишком долго")
            verdict = "просрочено → карточка владельцу"
        else:
            verdict = await handle_message(client, app_id, merged, dry=dry)

        with session_scope() as sess:
            # Помечаем обработанными ВСЕ строки заявки: решение принято по
            # склейке, и оставить хвост значило бы ответить на него второй раз.
            for r in sess.scalars(
                    select(PendingReply)
                    .where(PendingReply.application_id == app_id,
                           PendingReply.processed_at.is_(None))).all():
                r.processed_at = utcnow()
                r.last_error = "" if "не ушёл" not in verdict else verdict[:180]
        print("   #%d (ночная очередь): %s" % (app_id, verdict))
        done += 1
    return done


async def process_inbox(client, dry: bool = False) -> dict:
    """Один проход цикла переписки. Возвращает счётчики для лога."""
    from .. import owner

    stats = {"incoming": 0, "auto": 0, "escalated": 0, "closed": 0,
             "cards": 0, "commands": 0, "expired": 0, "deferred": 0}

    progress: dict = {}
    incoming = await fetch_incoming(client, progress)
    # Сначала сохраняем ВСЕ тексты. Даже сбой ночной очереди/классификации
    # не оставит их только в оперативной памяти.
    fresh_incoming = []
    for app_id, batch in incoming:
        ids = store_incoming(app_id, batch)
        fresh_incoming.append((app_id, [row for row in batch if row[0] in ids]))
        stats["incoming"] += len(ids)
    incoming = fresh_incoming

    # Ночная очередь — ДО обработки свежих: если по заявке есть и ночной
    # текст, и утренний, они склеиваются в одно решение внутри drain, и
    # свежий текст ниже пропускается.
    drained_apps = set()
    if within_reply_window():
        fresh_map = {}
        for app_id, batch in incoming:
            fresh_map[app_id] = "\n".join(t for _, t, _ in batch)
        before = set(fresh_map)
        stats["deferred"] = await drain_pending(client, dry=dry,
                                                fresh_by_app=fresh_map)
        drained_apps = before - set(fresh_map)   # склеенные с ночными

    for app_id, batch in incoming:
        new_ids = [row[0] for row in batch]
        if app_id in drained_apps:
            # текст уже вошёл в решение по ночной очереди
            finish_incoming(app_id, new_ids)
            continue
        by_id = {mid: (mid, txt, ts) for mid, txt, ts in batch}
        fresh = [by_id[i] for i in new_ids if i in by_id]
        if not fresh:
            continue
        # Отвечаем на СКЛЕЙКУ новых сообщений одним решением: рекрутёры
        # часто пишут тремя сообщениями подряд, и три ответа на них — спам.
        merged = "\n".join(t for _, t, _ in fresh)
        try:
            verdict = await handle_message(client, app_id, merged, dry=dry)
        except Exception as exc:
            finish_incoming(app_id, new_ids, error=type(exc).__name__)
            progress["errors"] += 1
            continue
        finish_incoming(app_id, new_ids, error=verdict if "не ушёл" in verdict else "")
        print("   #%d: %s" % (app_id, verdict))
        if verdict.startswith("автоответ ("):
            stats["auto"] += 1
        elif verdict.startswith(("эскалация", "слоты")):
            stats["escalated"] += 1
        elif verdict.startswith("отказ"):
            stats["closed"] += 1

    stats["expired"] = owner.expire_stale()
    stats["cards"] = await owner.push_pending(client, dry=dry)

    # Команды в «Избранном» читаем только когда оно назначено каналом
    # владельца. В режиме бота водяной знак всё равно двигаем: иначе при
    # возврате на «Избранное» разом исполнятся все накопившиеся команды.
    if get_settings().owner_channel in ("saved", "both"):
        for cmd in await owner.poll_commands(client):
            answer = await owner.apply_command(client, cmd, dry=dry)
            stats["commands"] += 1
            if not dry:
                await owner.notify(client, answer)
            else:
                print("      [dry-run] ответ владельцу: %s" % answer.split("\n")[0])
    else:
        await owner.advance_watermark(client)
    if not dry:
        # Двигаем и через обработанные медиа/исходящие, только после обработки
        # текстов; исключение выше оставляет старый курсор для повтора.
        with session_scope() as sess:
            for app_id, cursor in progress.get("cursors", {}).items():
                app = sess.get(Application, app_id)
                if app:
                    app.last_inbound_msg_id = max(app.last_inbound_msg_id or 0, cursor)
        from ..observability import record
        detail = {k: v for k, v in progress.items() if k != "cursors"}
        detail.update(incoming=stats["incoming"])
        record("telegram_inbox", "partial" if progress.get("errors") or
               progress.get("limited_dialogs") else "ok", details=detail)
    return stats


async def run(dry: bool = False) -> dict:
    """Самостоятельный запуск с собственным клиентом (для крона и CLI)."""
    s = get_settings()
    from telethon import TelegramClient
    if not (s.tg_api_id and s.telegram_api_hash):
        return {"error": "нет API-ключей Telegram"}
    client = TelegramClient(s.telegram_session_path, s.tg_api_id,
                            s.telegram_api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return {"error": "сессия не авторизована (python tg_login.py)"}
        return await process_inbox(client, dry=dry)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    import sys
    dry = "--dry" in sys.argv
    out = asyncio.run(run(dry=dry))
    print(out)
