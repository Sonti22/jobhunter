"""Уведомления владельцу: постановка в очередь.

Автопилот не знает токена бота и не должен: чем меньше процессов держат ключ,
дающий управление отправкой от лица владельца, тем меньше поверхность атаки.
Поэтому события кладутся в таблицу, а доставляет их бот — единственный, у
кого есть токен.

Два свойства, ради которых это отдельный модуль, а не пара строк на месте:

  sess — уведомление должно коммититься ТОЙ ЖЕ транзакцией, что и событие.
         policy.on_peer_flood() уже внутри транзакции: если писать
         уведомление отдельно, возможно понижение квоты без предупреждения
         владельцу или предупреждение о том, чего не случилось.

  dedup — PeerFlood проверяется каждые 20 минут, дневная сводка считается по
          расписанию, квота исчерпывается один раз за день. Без ключа
          дедупликации чат за сутки заполнился бы сотней одинаковых строк, и
          владелец перестал бы их читать — а значит, пропустил бы важное.
"""
from __future__ import annotations

from contextlib import nullcontext
from datetime import timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .config import get_settings
from .db import session_scope
from .models import BotOutbox, OwnerRequest, utcnow

# Виды уведомлений. Не enum: значение уходит в БД строкой, а список здесь —
# чтобы читающий код видел полный набор в одном месте.
KINDS = ("card", "card_result", "reply_received", "interview_set",
         "peerflood", "quota_done", "killswitch", "daily_summary", "error")
OUTBOX_LEASE_MIN = 10


def enabled() -> bool:
    """Есть ли смысл ставить уведомления: задан ли токен и получатель."""
    s = get_settings()
    return bool(s.telegram_bot_token and s.bot_owner_ids)


def push(kind: str, text: str, *, chat_id: int = 0, dedup: str = "",
         markup: dict | None = None, target_msg_id: int | None = None,
         req_id: int | None = None, sess=None) -> None:
    """Поставить уведомление в очередь.

    sess — открытая сессия вызывающего кода. Передавайте её, если событие уже
    происходит внутри транзакции: тогда уведомление и событие либо
    закоммитятся вместе, либо не случатся вовсе.
    """
    if not enabled():
        return

    ctx = nullcontext(sess) if sess is not None else session_scope()
    with ctx as s:
        values = dict(chat_id=chat_id, kind=kind, text=(text or "")[:3800],
                      markup_json=markup or {}, target_msg_id=target_msg_id,
                      owner_request_id=req_id, dedup_key=dedup[:200],
                      created_at=utcnow())
        if dedup:
            # Сравниваем в том же виде, в каком храним: ключ длиннее 200
            # символов усечённо писался, но полно сравнивался — и никогда
            # не совпадал, дедуп для длинных ключей молча не работал.
            dedup = dedup[:200]
            # INSERT OR IGNORE закрывает гонку между двумя автопилотами:
            # предварительный SELECT здесь был только оптимизацией, а не
            # гарантией. Уникальный partial index создаётся мигратором.
            s.execute(sqlite_insert(BotOutbox).values(**values)
                      .prefix_with("OR IGNORE"))
            return
        s.add(BotOutbox(**values))


def push_card(req: OwnerRequest, markup: dict | None = None,
              text: str = "", sess=None) -> None:
    """Карточка на решение — в бот.

    Уходит только когда бот назначен каналом владельца: иначе владелец увидит
    одну и ту же карточку дважды и решать её будет тоже дважды.

    text — текст для бота; пусто означает «как в карточке». Отдельный
    параметр нужен, потому что в боте из текста убирается хвост с командами:
    их заменяют кнопки.
    """
    if get_settings().owner_channel not in ("bot", "both"):
        return
    push("card", text or req.question, dedup="card:%d" % req.id, markup=markup,
         req_id=req.id, sess=sess)


def pending(limit: int = 20) -> list:
    """Неотправленные уведомления. Читает бот."""
    with session_scope() as sess:
        rows = sess.scalars(
            select(BotOutbox)
            .where(BotOutbox.sent_at.is_(None), BotOutbox.attempts < 5)
            .order_by(BotOutbox.id)
            .limit(limit)).all()
        return [_outbox_dict(r) for r in rows]


def _outbox_dict(r: BotOutbox) -> dict:
    return {"id": r.id, "chat_id": r.chat_id, "kind": r.kind,
            "text": r.text, "markup": dict(r.markup_json or {}),
            "target_msg_id": r.target_msg_id,
            "owner_request_id": r.owner_request_id,
            "attempts": r.attempts, "created_at": r.created_at,
            "claimed_at": r.claimed_at}


def claim_pending(limit: int = 20) -> list:
    """Забрать lease на уведомления, чтобы два bot-потока не дублировали их."""
    from datetime import datetime, timedelta
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    edge = now - timedelta(minutes=OUTBOX_LEASE_MIN)
    with session_scope() as sess:
        eligible = (
            BotOutbox.sent_at.is_(None), BotOutbox.attempts < 5,
            ((BotOutbox.claimed_at.is_(None)) | (BotOutbox.claimed_at < edge)))
        ids = (
            select(BotOutbox.id).where(*eligible)
            .order_by(BotOutbox.id).limit(max(0, limit)))
        # One statement chooses AND claims. SELECT followed by ORM writes
        # allowed two workers to both send the same notification.
        rows = sess.scalars(update(BotOutbox).where(BotOutbox.id.in_(ids), *eligible)
                            .values(claimed_at=now).returning(BotOutbox)).all()
        return [_outbox_dict(r) for r in sorted(rows, key=lambda row: row.id)]


def mark_sent(row_id: int, msg_id: int | None = None,
              chat_id: int | None = None) -> None:
    """Отметить доставленным. Для карточек запоминает, где они легли, —
    иначе результат исполнения будет некуда дописать."""
    with session_scope() as sess:
        row = sess.get(BotOutbox, row_id)
        if not row:
            return
        row.sent_at = utcnow()
        row.claimed_at = None
        if row.kind == "card" and row.owner_request_id and msg_id:
            req = sess.get(OwnerRequest, row.owner_request_id)
            if req:
                req.owner_msg_id = msg_id
                req.owner_chat_id = chat_id or row.chat_id
                req.channel = "bot"
                if req.sent_at is None:
                    req.sent_at = utcnow()


def mark_failed(row_id: int, error: str) -> None:
    with session_scope() as sess:
        row = sess.get(BotOutbox, row_id)
        if row:
            row.attempts += 1
            row.last_error = (error or "")[:200]
            row.claimed_at = None


def cancel(row_id: int, reason: str) -> None:
    """Cancel obsolete notification without claiming Telegram accepted it."""
    with session_scope() as sess:
        row = sess.get(BotOutbox, row_id)
        if row and row.sent_at is None:
            row.attempts = 5
            row.claimed_at = None
            row.last_error = ("cancelled: " + reason)[:200]
