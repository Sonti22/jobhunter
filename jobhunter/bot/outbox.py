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
    owners = sorted(get_settings().bot_owner_ids)
    if not owners:
        return 0
    rows = notify.claim_pending(limit)
    if not rows:
        return 0

    sent = 0
    last = len(rows) - 1
    for idx, row in enumerate(rows):
        # Rebuild manual cards at delivery time. A queued old draft must not
        # be presented as ready after the owner has already marked it or the
        # vacancy/contact has changed. Internal app_id metadata is not markup.
        manual_row = None
        if row["kind"] in ("manual_tg_step", "manual_tg_item", "manual_tg_document"):
            from .. import manual_telegram as manual_tg
            try:
                app_id = int(row["markup"].get("app_id") or 0)
            except (TypeError, ValueError):
                notify.cancel(row["id"], "некорректный номер ручной карточки")
                continue
            manual_row = manual_tg.get_card(app_id)
            if not manual_row:
                notify.cancel(row["id"], "ручная карточка уже обработана")
                continue
            if row["kind"] == "manual_tg_document" and manual_row["problem"]:
                notify.cancel(row["id"], "ручная карточка требует проверки")
                continue
        targets = [row["chat_id"]] if row["chat_id"] else owners
        ok, msg_id, chat_used, err = False, None, None, ""
        for chat_id in targets:
            try:
                if manual_row is not None:
                    if chat_id not in owners:
                        raise PermissionError("ручные карточки доступны только владельцу")
                    if row["kind"] == "manual_tg_document":
                        try:
                            filename, content = manual_tg.cv_document(manual_row["id"])
                        except (ValueError, OSError):
                            notify.push("manual_tg_cv_error",
                                        f"Не удалось приложить PDF к карточке #{manual_row['id']}. "
                                        "Резюме не отправлено. Попробуй «📎 Получить резюме» "
                                        "или сообщи об ошибке.", chat_id=chat_id,
                                        dedup=f"manual_tg_cv_error:{row['id']}:{chat_id}")
                            notify.cancel(row["id"], "PDF недоступен; владелец уведомлён")
                            break
                        res = api.send_document(chat_id, filename, content,
                            caption=f"📎 Резюме для #{manual_row['id']} · @{manual_row['handle']}\n"
                                    "Прикрепи этот PDF в переписке, если отправляешь резюме.", http=http)
                    else:
                        # Карточка = PDF с подписью и кнопками, одно сообщение:
                        # его же владелец пересылает рекрутёру. Нет PDF —
                        # текстовая карточка с теми же кнопками.
                        caption = manual_tg.card(manual_row)
                        kb = manual_tg.keyboard(manual_row)
                        doc = None
                        if not manual_row["problem"]:
                            try:
                                doc = manual_tg.cv_document(manual_row["id"])
                            except (ValueError, OSError) as e:
                                # Карточка уходит текстом, но молча — нельзя:
                                # владелец должен знать, что PDF к ней нет.
                                notify.push("manual_tg_cv_error",
                                            f"⚠️ К карточке #{manual_row['id']} не приложен PDF "
                                            f"({str(e)[:80]}). Резюме отправь вручную из cv_base.",
                                            chat_id=chat_id,
                                            dedup=f"manual_tg_cv_error:{row['id']}:{chat_id}")
                        if doc:
                            res = api.send_document(chat_id, doc[0], doc[1],
                                                    caption=caption, http=http,
                                                    markup=kb)
                        else:
                            res = api.send_message(chat_id, caption, kb, http=http)
                    msg_id, chat_used = res.get("message_id"), chat_id
                elif row["target_msg_id"]:
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
        elif _network_down(err):
            # Не доставили из-за сети — это не отказ Telegram, попытку не
            # считаем. Устаревшее закрываем по тому же сроку, что и ждущих.
            if _too_old(row):
                notify.mark_failed(row["id"], "не доставлено за %d ч: %s"
                                   % (WAIT_TTL_HOURS, err))
            else:
                notify.defer(row["id"], err)
        elif err:
            notify.mark_failed(row["id"], err)
    return sent


_NETWORK_MARKERS = (
    "connecterror", "connecttimeout", "readtimeout", "writetimeout",
    "pooltimeout", "remoteprotocolerror", "name resolution",
    "network is unreachable", "no route to host", "connection reset",
    "connection refused", "timed out", "http 502", "http 503", "http 504",
    "bad gateway", "service unavailable", "gateway timeout",
)


def _network_down(err: str) -> bool:
    """Сбой сети или шлюза Telegram — повторять можно и нужно."""
    low = (err or "").lower()
    return any(m in low for m in _NETWORK_MARKERS)


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
