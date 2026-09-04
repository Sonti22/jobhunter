"""Повторная классификация входящих, по которым LLM промолчала.

Второй ярус классификации (verify.py) при любом сбое возвращает None, и до
появления этого модуля такой ответ был окончательным: сообщение навсегда
оставалось с пустой llm_label. За час, когда все пять провайдеров разом
отдавали 429, так умерло восемь диалогов из тринадцати — рекрутёры
написали, система пометила «не понял» и замолчала.

Здесь только переклассификация: модуль уточняет метку и ничего не
отправляет. Решение «ответить» принимает revive.py, и это разделение
намеренное — recheck можно гонять на обычном пуле, не занимая
однопоточную очередь Telethon.

    python -m jobhunter.convo.recheck --list
    python -m jobhunter.convo.recheck
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy import select

from ..db import session_scope
from ..models import Application, Message, Status
from .engine import LLM_MAX_ATTEMPTS, _remember_llm_verdict

# Статусы, где ответ ещё имеет смысл. Закрытые и отправленные-без-ответа
# сюда не входят: уточнять метку у мёртвой заявки незачем.
LIVE = (Status.SENT.value, Status.AWAITING_REPLY.value,
        Status.FOLLOWED_UP.value, Status.REPLIED.value,
        Status.IN_DIALOGUE.value, Status.NEEDS_HUMAN.value,
        Status.INTERVIEW_PROPOSED.value)


def due(limit: int = 20) -> list:
    """Сообщения, которым пора повторить классификацию."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    out = []
    with session_scope() as sess:
        rows = sess.scalars(
            select(Message)
            .where(Message.direction == "in",
                   Message.llm_label == "",
                   Message.llm_attempts < LLM_MAX_ATTEMPTS,
                   Message.llm_next_try_at.is_not(None),
                   Message.llm_next_try_at <= now)
            .order_by(Message.id.desc()).limit(limit * 3)).all()
        for m in rows:
            app = sess.get(Application, m.application_id)
            if app is None or app.status not in LIVE:
                continue
            out.append(m.id)
            if len(out) >= limit:
                break
    return out


def recheck_one(msg_id: int) -> str:
    """Повторный вызов второго яруса. Возвращает короткий итог."""
    from .verify import verify_intent

    with session_scope() as sess:
        msg = sess.get(Message, msg_id)
        if msg is None:
            return "нет сообщения"
        app_id = msg.application_id
        text = msg.body or ""
        regex_label = msg.classifier_label or "unknown"
        history = [(x.direction, x.body or "") for x in sess.scalars(
            select(Message).where(Message.application_id == app_id)
            .order_by(Message.id.desc()).limit(6)).all()][::-1]

    v = verify_intent(text, history, regex_label)
    _remember_llm_verdict(app_id, v, error="повтор: модель не ответила")
    if v is None:
        return "повтор не удался"
    return "%s (%.2f)" % (v.label, v.confidence)


def run(limit: int = 20) -> dict:
    """Шаг автопилота."""
    ids = due(limit)
    stats = {"due": len(ids), "resolved": 0, "still_silent": 0}
    for mid in ids:
        res = recheck_one(mid)
        if res.startswith("повтор не удался") or res == "нет сообщения":
            stats["still_silent"] += 1
        else:
            stats["resolved"] += 1
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Повторная классификация входящих")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    if args.list:
        ids = due(args.limit)
        print("ждут повтора: %d" % len(ids))
        with session_scope() as sess:
            for mid in ids:
                m = sess.get(Message, mid)
                print("  #%-6d попыток %d, следующая %s: %s"
                      % (m.id, m.llm_attempts,
                         m.llm_next_try_at.strftime("%d.%m %H:%M")
                         if m.llm_next_try_at else "—",
                         (m.body or "")[:50].replace("\n", " ")))
        return 0

    print(run(args.limit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
