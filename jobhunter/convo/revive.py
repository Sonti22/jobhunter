"""Оживление диалогов, застрявших в NEEDS_HUMAN.

Защёлка NEEDS_HUMAN защищает от того, чтобы автоматика перебивала человека:
пока владелец не ответил, plan_reply молчит. Но снимать защёлку в системе
не умел никто. Карточка истекала через сутки (expire_stale ставит
decision='expired' и гасит кнопки), статус заявки оставался прежним — и
тред умирал навсегда. На момент написания так лежало семь диалогов из
тринадцати ответивших: рекрутёр написал, владелец не успел нажать кнопку,
всё замерло.

Здесь исправление ровно этого: заявку, где карточка истекла без решения,
переклассифицируем заново (LLM к этому времени обычно уже доступна) и, если
интент безопасный, снимаем защёлку и кладём входящее в PendingReply.

Отправляет по-прежнему существующий drain_pending → handle_message →
send_reply. Второго пути к Telethon не появляется: два клиента на одной
сессии означают AuthKeyDuplicatedError и потерю доступа ко всей переписке.

Опасные интенты (деньги, оффер, слоты) сюда не попадают никогда — им
заводится новая карточка со свежим сроком.

    python -m jobhunter.convo.revive --list
    python -m jobhunter.convo.revive
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..config import get_settings
from ..db import session_scope
from ..models import (
    Application,
    Message,
    OwnerRequest,
    OwnerRequestKind,
    PendingReply,
    Status,
    utcnow,
)
from ..textutil import norm_hash
from . import classify as C

# Интенты, которые остаются за владельцем при любых настройках. Решение
# владельца, закреплено тестом: деньги, оффер и предложенное время — не то,
# что автомат вправе взять на себя.
NEVER_AUTO = {C.MONEY, C.OFFER, C.SLOT_PROPOSED}


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def stale(hours: int | None = None, limit: int = 10) -> list:
    """Заявки, где карточка истекла без решения и тред замер."""
    s = get_settings()
    hours = hours if hours is not None else s.revive_after_hours
    edge = _now() - timedelta(hours=hours)
    out = []
    with session_scope() as sess:
        apps = sess.scalars(
            select(Application)
            .where(Application.status == Status.NEEDS_HUMAN.value,
                   Application.revive_attempts < s.revive_max_attempts)
            .order_by(Application.score.desc())).all()
        for a in apps:
            card = sess.scalars(
                select(OwnerRequest)
                .where(OwnerRequest.application_id == a.id,
                       OwnerRequest.kind == OwnerRequestKind.NEEDS_HUMAN.value)
                .order_by(OwnerRequest.id.desc()).limit(1)).first()
            # Возраст считаем по карточке, а не по updated_at заявки:
            # массовые правки статусов делают updated_at бесполезным.
            if card is None or card.created_at > edge:
                continue
            if card.decision not in ("", "expired"):
                continue                       # владелец решил — не лезем
            last_in = sess.scalars(
                select(Message)
                .where(Message.application_id == a.id, Message.direction == "in")
                .order_by(Message.id.desc()).limit(1)).first()
            if last_in is None:
                continue
            # Пришло новое сообщение после карточки — тред живёт сам,
            # его разберёт обычный inbox.
            if last_in.received_at and last_in.received_at > card.created_at:
                continue
            out.append(a.id)
            if len(out) >= limit:
                break
    return out


def revive_one(app_id: int) -> str:
    """Одна попытка вернуть диалог в оборот. Возвращает короткий итог."""
    from .verify import verify_intent

    with session_scope() as sess:
        app = sess.get(Application, app_id)
        if app is None or app.status != Status.NEEDS_HUMAN.value:
            return "не подходит"
        last_in = sess.scalars(
            select(Message)
            .where(Message.application_id == app_id, Message.direction == "in")
            .order_by(Message.id.desc()).limit(1)).first()
        if last_in is None:
            return "нет входящих"
        text = last_in.body or ""
        history = [(m.direction, m.body or "") for m in sess.scalars(
            select(Message).where(Message.application_id == app_id)
            .order_by(Message.id.desc()).limit(6)).all()][::-1]
        app.revive_attempts = (app.revive_attempts or 0) + 1
        app.revived_at = utcnow()
        attempts = app.revive_attempts

    intent = C.classify(text)
    # Второй ярус зовём только там, где regex не уверен: лишний вызов LLM
    # на каждое оживление — деньги на ветер.
    if intent.label == C.UNKNOWN or intent.confidence < C.CONFIDENCE_MIN:
        v = verify_intent(text, history, intent.label)
        if v is not None and v.confidence >= 0.7:
            intent = C.Intent(v.label, v.confidence, "llm")

    if intent.label in NEVER_AUTO:
        return "остаётся владельцу: %s" % intent.label

    # Подтверждённый отказ закрываем: висящий в NEEDS_HUMAN отказ занимает
    # место в воронке и врёт статистике «ждут решения». Порог тот же
    # двойной, что и в основном движке, — закрытие необратимо.
    if (intent.label == C.REJECTION
            and intent.confidence >= C.REJECTION_CLOSE_MIN):
        with session_scope() as sess:
            app = sess.get(Application, app_id)
            if app.advance(Status.REJECTED_BY_EMPLOYER,
                           reason="отказ подтверждён при оживлении"):
                app.needs_human_reason = ""
                return "закрыто как отказ"
        return "отказ, но путь статуса закрыт"
    if intent.label not in C.AUTO_OK or intent.confidence < C.CONFIDENCE_MIN:
        return "по-прежнему неясно: %s (%.2f)" % (intent.label,
                                                  intent.confidence)

    with session_scope() as sess:
        app = sess.get(Application, app_id)
        # Снимаем защёлку ДО постановки в очередь: иначе plan_reply при
        # разборе очереди снова промолчит из-за статуса NEEDS_HUMAN.
        if not app.advance(Status.IN_DIALOGUE,
                           reason="оживление: %s" % intent.label):
            return "путь статуса закрыт"
        app.needs_human_reason = ""
        h = norm_hash(text)
        dup = sess.scalars(
            select(PendingReply)
            .where(PendingReply.application_id == app_id,
                   PendingReply.body_hash == h,
                   PendingReply.processed_at.is_(None))).first()
        if dup is None:
            sess.add(PendingReply(application_id=app_id,
                                  incoming_text=text, body_hash=h))
    return "оживлено (%s, попытка %d)" % (intent.label, attempts)


def run(limit: int = 10) -> dict:
    """Шаг автопилота."""
    if not get_settings().revive_enabled:
        return {"skipped": "revive_enabled=false"}
    ids = stale(limit=limit)
    stats = {"stale": len(ids), "revived": 0, "left_to_owner": 0}
    for app_id in ids:
        res = revive_one(app_id)
        if res.startswith("оживлено"):
            stats["revived"] += 1
        else:
            stats["left_to_owner"] += 1
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Оживление застрявших диалогов")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()

    ids = stale(limit=args.limit)
    if args.list:
        print("застрявших: %d" % len(ids))
        with session_scope() as sess:
            for app_id in ids:
                a = sess.get(Application, app_id)
                m = sess.scalars(
                    select(Message)
                    .where(Message.application_id == app_id,
                           Message.direction == "in")
                    .order_by(Message.id.desc()).limit(1)).first()
                print("  #%-6d %s | %s" % (a.id, (a.needs_human_reason or "?")[:40],
                                           (m.body or "")[:46].replace("\n", " ")))
        return 0

    print(run(args.limit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
