"""Утренняя пачка ручных откликов в бот.

4111 вакансий без прямого контакта — это не список, который можно «разобрать»
за один присест, и не то, что стоит вываливать целиком. Поэтому каждое утро
уходит небольшая пачка лучших по соответствию, отдельными сообщениями с
кнопками: открыть, откликнулся, не подходит, потом.

Почему отдельными сообщениями, а не одним списком: решение принимается по
каждой вакансии, и кнопки должны стоять рядом с ней. Одно сообщение со
списком из десяти пунктов потребовало бы нумерации и команд — то есть ровно
того, от чего мы ушли, заменив команды кнопками.

Почему не OwnerRequest: карточка решения обещает, что автопилот его
исполнит. Здесь исполнять нечего — владелец сам открывает форму и заполняет
её руками, а система только запоминает отметку.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import notify
from .manual_apply import listing

log = logging.getLogger("manual_batch")

# Сколько вакансий в утренней пачке. Поднято 8 → 15 после того, как к
# карточке добавилась кнопка «Письмо»: с готовым текстом отклик занимает
# ~30 секунд, и пачка из 15 разбирается быстрее, чем прежние 8 без текста.
BATCH_SIZE = 15


def _card(row: dict) -> str:
    lines = ["🖐 %s" % (row["title"] or "вакансия")[:70]]
    if row["company"]:
        lines.append(row["company"][:60])
    meta = ["скор %.0f" % row["score"]]
    if row["salary"]:
        meta.append(row["salary"][:40])
    src = (row["source"] or "").split(":")[0]
    if src:
        meta.append(src)
    lines.append(" · ".join(meta))
    if row["cv_path"]:
        lines.append("Резюме готово: http://127.0.0.1:8765/cv/%d" % row["id"])
    return "\n".join(lines)


def run(limit: int = BATCH_SIZE, now: datetime | None = None) -> dict:
    """Ставит пачку в очередь бота. Дедуп — по дню и заявке."""
    from .bot.cards import manual_keyboard

    if not notify.enabled():
        return {"sent": 0, "reason": "бот не настроен"}

    now = now or datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    rows = listing(limit)
    if not rows:
        return {"sent": 0, "reason": "очередь пуста"}

    notify.push("manual_batch",
                "🖐 Ручные отклики на сегодня: %d вакансий.\n"
                "Открой, откликнись через форму и отметь кнопкой." % len(rows),
                dedup="manual_head:%s" % day)
    sent = 0
    for i, row in enumerate(rows, 1):
        # Номер в пачке — дешёвая механика завершения: «12 из 15» тянет
        # дожать три оставшихся сильнее, чем безымянный поток карточек.
        notify.push("manual_item",
                    "[%d/%d] %s" % (i, len(rows), _card(row)),
                    dedup="manual:%d" % row["id"],
                    markup=manual_keyboard(row["id"], row["url"]))
        sent += 1
    log.info("ручные отклики: пачка из %d", sent)
    return {"sent": sent}
