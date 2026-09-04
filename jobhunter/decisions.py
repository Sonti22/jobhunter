"""Очередь решений владельца: от нажатия кнопки до сообщения рекрутёру.

Зачем очередь, а не прямое действие. Кнопку нажимают в боте от BotFather, а
ответить рекрутёру можно только с личного аккаунта по MTProto. Сессия
Telethon — файл, который нельзя открывать двумя процессами, поэтому бот
физически не может отправить сообщение сам. Он лишь помечает карточку
решённой, а исполняет её процесс автопилота, владеющий сессией.

Две гарантии, на которых всё держится:

  claim()  — решение ставится атомарным UPDATE с условием decision = ''.
             Каналов ввода два (кнопка в боте и команда в «Избранном»), и
             обычная проверка «если ещё не решено — записать» пропускает оба:
             между чтением и записью успевает вклиниться второй. Тогда
             рекрутёр получает два сообщения на один вопрос.

  _lease() — взятие в работу тоже атомарно, с инкрементом attempts. Карточка,
             роняющая исполнитель, после трёх попыток выпадает из очереди и
             не блокирует остальные.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, update

from .config import get_settings
from .db import session_scope
from .models import Application, Message, OwnerRequest, Status, utcnow

log = logging.getLogger("decisions")

MAX_ATTEMPTS = 3
# Решения, которые требуют отправки рекрутёру. skip закрывает карточку без
# единого сообщения, поэтому исполнять его нечем.
NEEDS_SEND = {"ok", "time", "no", "send", "say"}


def claim(req_id: int, decision: str, arg: str = "", by: str = "") -> bool:
    """Поставить решение по карточке. False — кто-то успел раньше.

    Единственный способ записать decision. Условие живёт внутри UPDATE, а не
    в коде: под WAL второй писатель ждёт busy_timeout, затем видит непустое
    поле и получает rowcount == 0. Гонки нет по устройству СУБД, а не по
    договорённости между модулями.
    """
    with session_scope() as sess:
        res = sess.execute(
            update(OwnerRequest)
            .where(OwnerRequest.id == req_id, OwnerRequest.decision == "")
            .values(decision=decision, decision_arg=(arg or "")[:2000],
                    decided_by=(by or "")[:60], answered_at=utcnow()))
        return res.rowcount == 1


def pending(limit: int = 10) -> list:
    """Карточки с решением, которое ещё не исполнено."""
    with session_scope() as sess:
        rows = sess.scalars(
            select(OwnerRequest)
            .where(OwnerRequest.decision != "",
                   OwnerRequest.decision != "expired",
                   OwnerRequest.applied_at.is_(None),
                   OwnerRequest.attempts < MAX_ATTEMPTS)
            .order_by(OwnerRequest.id)
            .limit(limit)).all()
        return [r.id for r in rows]


def _lease(req_id: int) -> dict | None:
    """Взять карточку в работу. None — уже исполнена или исчерпаны попытки."""
    with session_scope() as sess:
        res = sess.execute(
            update(OwnerRequest)
            .where(OwnerRequest.id == req_id,
                   OwnerRequest.applied_at.is_(None),
                   OwnerRequest.attempts < MAX_ATTEMPTS)
            .values(attempts=OwnerRequest.attempts + 1))
        if res.rowcount != 1:
            return None
        r = sess.get(OwnerRequest, req_id)
        return {"id": r.id, "application_id": r.application_id,
                "kind": r.kind, "decision": r.decision,
                "decision_arg": r.decision_arg or "",
                "payload": dict(r.payload_json or {}),
                "owner_msg_id": r.owner_msg_id,
                "owner_chat_id": r.owner_chat_id,
                "answered_at": r.answered_at,
                "attempts": r.attempts}


def finish(req_id: int, ok: bool, note: str, error: str = "") -> None:
    """Закрыть карточку и сообщить владельцу результат."""
    from . import notify

    with session_scope() as sess:
        r = sess.get(OwnerRequest, req_id)
        if not r:
            return
        r.applied_at = utcnow()
        r.decision_note = (note or "")[:500]
        r.apply_error = (error or "")[:200]
        chat_id, msg_id = r.owner_chat_id, r.owner_msg_id
        question = r.question
    if chat_id and msg_id:
        head = question.split("\n")[0] if question else "#%d" % req_id
        mark = "✅" if ok else "⚠️"
        notify.push("card_result", "%s %s\n%s" % (mark, head, note),
                    chat_id=chat_id, target_msg_id=msg_id, req_id=req_id)


def _already_answered(app_id: int, since) -> bool:
    """Не ушло ли сообщение по этой заявке уже после принятия решения.

    Страховка от единственного сценария, который не закрывают claim и lease:
    процесс отправил сообщение рекрутёру и упал до записи applied_at. Повтор
    привёл бы ко второму сообщению.
    """
    if not since:
        return False
    with session_scope() as sess:
        row = sess.scalars(
            select(Message)
            .where(Message.application_id == app_id,
                   Message.direction == "out",
                   Message.sent_at > since)
            .limit(1)).first()
        return row is not None


async def apply_one(client, req_id: int, dry: bool = False) -> str:
    """Исполнить одно решение. Возвращает человекочитаемый итог."""
    from .convo.send import send_reply
    from .convo.slots import fmt, parse_owner_time
    from .owner import _confirm_text
    from .schedule import book

    lease = _lease(req_id)
    if lease is None:
        return "уже исполнено"

    app_id = lease["application_id"]
    decision = lease["decision"]
    arg = lease["decision_arg"]
    s = get_settings()

    if not app_id:
        finish(req_id, True, "карточка без заявки — закрыта")
        return "нет заявки"

    if decision == "skip":
        finish(req_id, True, "закрыто без действий")
        return "закрыто без действий"

    # Подтверждение отказа человеком. Автоматика сама закрывает заявку
    # только при двойном сигнале regex+LLM; во всех спорных случаях карточка
    # приходит сюда, и /close — единственный путь закрыть её терминально.
    if decision == "close":
        with session_scope() as sess:
            app = sess.get(Application, app_id)
            if app is not None and app.status != Status.REJECTED_BY_EMPLOYER.value:
                app.advance(Status.REJECTED_BY_EMPLOYER,
                            reason="отказ, подтверждён владельцем")
        finish(req_id, True, "заявка закрыта: отказ")
        return "заявка закрыта (отказ)"

    if decision in NEEDS_SEND and _already_answered(app_id, lease["answered_at"]):
        finish(req_id, True, "ответ уже был отправлен ранее")
        return "дубль предотвращён"

    # ── подтверждение времени интервью ──
    if decision in ("ok", "time"):
        slots = list(lease["payload"].get("slots", []))
        tz_name = s.owner_tz
        chosen = None
        if decision == "ok":
            idx = int(arg) if str(arg).isdigit() else 0
            if idx >= len(slots):
                finish(req_id, False, "варианта %d нет в карточке" % (idx + 1),
                       "bad_slot_index")
                return "нет такого варианта"
            chosen = datetime.fromisoformat(slots[idx]["utc"])
            tz_name = slots[idx].get("tz") or tz_name
        else:
            chosen = parse_owner_time(arg, tz_name)
            if not chosen:
                finish(req_id, False, "не разобрал время «%s»" % arg[:60],
                       "bad_time")
                return "не разобрал время"
        if chosen.tzinfo is None:
            chosen = chosen.replace(tzinfo=timezone.utc)

        note = book.confirm(app_id, chosen, tz_name)
        text = _confirm_text(chosen, tz_name, note.get("meet_link", ""))
        res = "dry" if dry else await send_reply(client, app_id, text,
                                                 is_auto=False, dry=dry)
        ok = res in ("ok", "dry")
        finish(req_id, ok, "интервью %s · рекрутёру: %s · календарь: %s"
               % (fmt(chosen, tz_name), res, note.get("calendar", "—")),
               "" if ok else res)
        return res

    # ── отказ от предложенного времени ──
    if decision == "no":
        text = ("Спасибо! К сожалению, в это время не получится. "
                "Подскажите, какие ещё варианты возможны — подстроюсь.")
        res = "dry" if dry else await send_reply(client, app_id, text,
                                                 is_auto=False, dry=dry)
        ok = res in ("ok", "dry")
        finish(req_id, ok, "отказ от слота отправлен (%s)" % res,
               "" if ok else res)
        return res

    # ── ответ рекрутёру: черновик или свой текст ──
    if decision in ("send", "say"):
        text = arg if decision == "say" else lease["payload"].get("draft", "")
        if not text.strip():
            finish(req_id, False, "пустой текст ответа", "empty_text")
            return "пустой текст"
        res = "dry" if dry else await send_reply(client, app_id, text,
                                                 is_auto=False, dry=dry)
        ok = res in ("ok", "dry")
        if ok:
            with session_scope() as sess:
                app = sess.get(Application, app_id)
                if app and app.status == Status.NEEDS_HUMAN.value:
                    app.advance(Status.IN_DIALOGUE)
                    app.needs_human_reason = ""
        finish(req_id, ok, "ответ отправлен (%s)" % res, "" if ok else res)
        return res

    finish(req_id, False, "неизвестное решение «%s»" % decision, "unknown")
    return "неизвестное решение"


async def run(limit: int = 10, dry: bool = False) -> dict:
    """Слить очередь решений. Точка входа для шага автопилота."""
    ids = pending(limit)
    stats = {"taken": len(ids), "done": 0, "failed": 0}
    if not ids:
        return stats

    s = get_settings()
    from pathlib import Path
    if not (s.tg_api_id and s.telegram_api_hash
            and Path(s.telegram_session_path).exists()):
        return dict(stats, error="Telegram не настроен")

    from telethon import TelegramClient
    client = TelegramClient(s.telegram_session_path, s.tg_api_id,
                            s.telegram_api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return dict(stats, error="сессия не авторизована")
        for req_id in ids:
            try:
                res = await apply_one(client, req_id, dry=dry)
            except Exception as e:
                log.exception("решение #%d не исполнено", req_id)
                finish(req_id, False, "ошибка исполнения: %s" % type(e).__name__,
                       type(e).__name__)
                stats["failed"] += 1
                continue
            if res in ("ok", "dry", "закрыто без действий", "дубль предотвращён"):
                stats["done"] += 1
            else:
                stats["failed"] += 1
    finally:
        await client.disconnect()
    return stats
