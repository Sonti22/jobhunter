"""Состояние бота: offset апдейтов, текущий экран, ожидание ввода."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..db import session_scope
from ..models import BotState, BotTask, utcnow

# Сколько ждать свободного ввода после кнопки «Другое время» / «Свой текст».
# Без срока случайное сообщение через час уехало бы рекрутёру как ответ.
AWAIT_TTL_MIN = 10
TASK_LEASE_MIN = 15
TASK_MAX_ATTEMPTS = 5


def _row(sess) -> BotState:
    st = sess.get(BotState, 1)
    if st is None:
        st = BotState(id=1)
        sess.add(st)
        sess.flush()
    return st


def get() -> dict:
    with session_scope() as sess:
        st = _row(sess)
        return {"offset": st.updates_offset, "last_update_id": st.last_update_id,
                "screen_chat_id": st.screen_chat_id,
                "screen_msg_id": st.screen_msg_id,
                "awaiting_kind": st.awaiting_kind,
                "awaiting_req_id": st.awaiting_req_id,
                "awaiting_text": st.awaiting_text,
                "awaiting_until": st.awaiting_until}


def set_offset(update_id: int) -> None:
    """Подтвердить обработанный апдейт.

    Вызывается ПОСЛЕ обработки: падение посередине даёт повтор, а не потерю.
    Повтор безопасен — решения ставятся атомарным claim.
    """
    with session_scope() as sess:
        st = _row(sess)
        if update_id >= st.last_update_id:
            st.last_update_id = update_id
            st.updates_offset = update_id + 1


def seen(update_id: int) -> bool:
    """Апдейт уже обрабатывали (Telegram переотправил неподтверждённое)."""
    with session_scope() as sess:
        return update_id <= _row(sess).last_update_id


def set_screen(chat_id: int, msg_id: int) -> None:
    with session_scope() as sess:
        st = _row(sess)
        st.screen_chat_id = chat_id
        st.screen_msg_id = msg_id


def await_input(kind: str, req_id: int, text: str = "") -> None:
    """Ждать следующее сообщение владельца как аргумент решения."""
    with session_scope() as sess:
        st = _row(sess)
        st.awaiting_kind = kind
        st.awaiting_req_id = req_id
        st.awaiting_text = text or ""
        st.awaiting_until = (datetime.now(timezone.utc).replace(tzinfo=None)
                             + timedelta(minutes=AWAIT_TTL_MIN))


def take_awaited() -> dict | None:
    """Забрать ожидание, если оно ещё живо. Одноразово."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as sess:
        st = _row(sess)
        if not st.awaiting_kind:
            return None
        if st.awaiting_until and st.awaiting_until < now:
            st.awaiting_kind = ""
            st.awaiting_req_id = 0
            st.awaiting_text = ""
            return None
        out = {"kind": st.awaiting_kind, "req_id": st.awaiting_req_id,
               "text": st.awaiting_text}
        st.awaiting_kind = ""
        st.awaiting_req_id = 0
        st.awaiting_text = ""
        st.awaiting_until = None
        return out


def peek_awaited() -> dict | None:
    """Посмотреть ожидание, не забирая (для двухшагового подтверждения)."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as sess:
        st = _row(sess)
        if not st.awaiting_kind:
            return None
        if st.awaiting_until and st.awaiting_until < now:
            return None
        return {"kind": st.awaiting_kind, "req_id": st.awaiting_req_id,
                "text": st.awaiting_text}


def clear_awaited() -> None:
    with session_scope() as sess:
        st = _row(sess)
        st.awaiting_kind = ""
        st.awaiting_req_id = 0
        st.awaiting_text = ""
        st.awaiting_until = None


def task_push(act: dict) -> None:
    """Поставить тяжёлую задачу кнопки в очередь. Очередь в БД: нажатие
    владельца переживает рестарт бота, в отличие от списка в памяти."""
    with session_scope() as sess:
        sess.add(BotTask(chat_id=int(act.get("chat_id") or 0),
                         task=str(act.get("task") or ""),
                         payload_json={k: v for k, v in act.items()
                                       if k != "do"}))


def task_pop() -> dict | None:
    """Захватить старейшую задачу через lease.

    В отличие от старой версии, запись не удаляется до выполнения. Если бот
    упал, следующий процесс вернёт просроченный running в pending и повторит
    задачу; после пяти неудач она остаётся failed для ручной диагностики.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    edge = now - timedelta(minutes=TASK_LEASE_MIN)
    with session_scope() as sess:
        # Старые записи из версии до durable queue не должны застрять:
        # отсутствие status трактуется как pending благодаря SQL default.
        stale = sess.scalars(select(BotTask).where(
            BotTask.status == "running", BotTask.claimed_at < edge)).all()
        for old in stale:
            old.claimed_at = None
            # Задача, роняющая процесс, инкрементирует attempts при каждом
            # захвате, но task_failed после краша не вызывается. Без этой
            # ветки она после пятого краша вечно висела бы в pending —
            # невидимой для выборки (attempts >= MAX) и для счётчика failed.
            if old.attempts >= TASK_MAX_ATTEMPTS:
                old.status = "failed"
                old.finished_at = utcnow()
                old.last_error = (old.last_error
                                  or "процесс падал при исполнении")[:500]
            else:
                old.status = "pending"

        row = sess.scalars(select(BotTask).where(
            BotTask.status == "pending", BotTask.attempts < TASK_MAX_ATTEMPTS,
            ((BotTask.next_try_at.is_(None)) | (BotTask.next_try_at <= now)))
            .order_by(BotTask.id)).first()
        if row is None:
            return None
        act = dict(row.payload_json or {})
        act.setdefault("chat_id", row.chat_id)
        act.setdefault("task", row.task)
        row.status = "running"
        row.claimed_at = now
        row.attempts += 1
        act["_task_id"] = row.id
        return act


def task_done(task_id: int | None) -> None:
    """Зафиксировать успешно выполненную задачу, сохранив аудит."""
    if not task_id:
        return
    with session_scope() as sess:
        row = sess.get(BotTask, int(task_id))
        if row:
            row.status = "done"
            row.claimed_at = None
            row.finished_at = utcnow()


def task_failed(task_id: int | None, error: str) -> None:
    """Вернуть задачу на retry либо оставить в failed после лимита."""
    if not task_id:
        return
    with session_scope() as sess:
        row = sess.get(BotTask, int(task_id))
        if not row:
            return
        row.last_error = (error or "")[:500]
        row.claimed_at = None
        if row.attempts < TASK_MAX_ATTEMPTS:
            row.status = "pending"
            row.next_try_at = (datetime.now(timezone.utc).replace(tzinfo=None)
                               + timedelta(minutes=min(60, 2 ** row.attempts)))
        else:
            row.status = "failed"
            row.finished_at = utcnow()
