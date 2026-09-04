"""Доставка уведомлений из очереди в Telegram.

Очередь наполняет автопилот (jobhunter/notify.py), забирает бот — он
единственный, у кого есть токен. Побочная польза такой развязки: если бот
лежал ночью, утром владелец получает всё пропущенное, а не теряет.
"""
from __future__ import annotations

import logging
import time

from .. import notify
from ..config import get_settings
from . import api

log = logging.getLogger("bot.outbox")

# Пауза между сообщениями. Telegram для приватного чата допускает около
# сообщения в секунду в среднем и терпит короткие всплески; 1.1 секунды
# растягивали утреннюю пачку из пятнадцати карточек на сорок секунд —
# владелец успевал закрыть чат. 0.35 держится в пределах лимита и
# доставляет ту же пачку за пять секунд.
PAUSE = 0.35


def drain(http=None, limit: int = 20) -> int:
    """Отправить накопившееся. Возвращает число доставленных сообщений."""
    # Берём короткий lease до сетевого вызова. Это не делает Telegram
    # exactly-once (краш после accepted всё ещё принципиально неоднозначен),
    # но убирает параллельную доставку из двух bot-потоков/процессов.
    rows = notify.claim_pending(limit)
    if not rows:
        return 0

    owners = sorted(get_settings().bot_owner_ids)
    if not owners:
        return 0

    sent = 0
    last = len(rows) - 1
    for idx, row in enumerate(rows):
        targets = [row["chat_id"]] if row["chat_id"] else owners
        ok, msg_id, chat_used, err = False, None, None, ""
        for chat_id in targets:
            try:
                if row["target_msg_id"]:
                    api.edit_message_text(chat_id, row["target_msg_id"],
                                          row["text"],
                                          row["markup"] or {"inline_keyboard": []},
                                          http=http)
                    msg_id, chat_used = row["target_msg_id"], chat_id
                else:
                    res = api.send_message(chat_id, row["text"],
                                           row["markup"] or None, http=http)
                    msg_id, chat_used = res.get("message_id"), chat_id
                ok = True
            except api.TokenRevoked:
                raise
            except Exception as e:
                err = "%s: %s" % (type(e).__name__, str(e)[:120])
                log.warning("уведомление #%d не ушло: %s", row["id"], err)
        # Пауза только МЕЖДУ сообщениями: после последнего она задерживала
        # возврат на ровном месте.
        if idx < last:
            time.sleep(PAUSE)

        if ok:
            # Рестарт между send и mark_sent даёт дубль уведомления через
            # lease — осознанный выбор at-least-once: обратный порядок
            # (отметить до отправки) терял бы уведомление насовсем, а дубль
            # владелец переживёт. Не «чинить» перестановкой строк.
            notify.mark_sent(row["id"], msg_id, chat_used)
            sent += 1
        elif _not_started_yet(err):
            # Telegram запрещает боту писать первым, пока владелец не нажал
            # /start. Это не сбой доставки, а ожидание: попытку не считаем,
            # иначе накопившиеся за день уведомления сгорят по лимиту
            # ретраев ещё до того, как диалог откроют.
            #
            # Но ожидание не бесконечно: «ждущие» строки — старейшие в
            # выборке, и они вечно занимали бы всю голову очереди, а при
            # открытии чата хлынул бы шторм недельного старья. Устаревшее
            # закрываем — актуальное состояние покажут экраны бота.
            if _too_old(row):
                notify.mark_failed(row["id"], "не доставлено за %d ч: %s"
                                   % (WAIT_TTL_HOURS, err))
            else:
                log.info("уведомление #%d ждёт: владелец ещё не нажал /start",
                         row["id"])
        else:
            notify.mark_failed(row["id"], err)
    return sent


WAIT_TTL_HOURS = 48


def _too_old(row: dict) -> bool:
    from datetime import datetime, timedelta, timezone
    created = row.get("created_at")
    if not created:
        return False
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now - created > timedelta(hours=WAIT_TTL_HOURS)


def _not_started_yet(err: str) -> bool:
    low = (err or "").lower()
    return ("can't initiate conversation" in low
            or "bot was blocked" in low
            or "chat not found" in low)
