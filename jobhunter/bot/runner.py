"""Главный цикл бота: получить апдейты → выполнить план → слить очередь.

Единственное место в пакете, где происходит сеть и хранится состояние между
итерациями. Всё остальное (разбор, экраны, клавиатуры) — чистые функции.

Telethon здесь не импортируется и не может быть импортирован: сессия одна, и
второй MTProto-клиент на ней означает разлогин аккаунта. Это проверяется
тестом tests/test_bot_no_telethon.py, а не только договорённостью.
"""
from __future__ import annotations

import logging
import sys
import threading
import time

from .. import health
from ..config import get_settings
from . import api, outbox, screens, state

log = logging.getLogger("bot")

# Как часто поток проверяет очередь уведомлений.
DELIVER_TICK = 0.7

CONFLICT_SLEEP = 30.0       # второй поллер — ждём, пока он уйдёт
TASK_TICK = 0.3             # опрос очереди задач: нажатие ждут прямо сейчас


def _setup_logging() -> None:
    from pathlib import Path
    s = get_settings()
    logdir = Path(s.log_dir)
    logdir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(logdir / "bot.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    logging.basicConfig(level=logging.INFO, handlers=[handler, console])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _apply(actions: list, http) -> None:
    """Исполнить план, составленный handlers."""
    from . import handlers  # noqa: F401  (для симметрии импортов в тестах)

    # Часики гасим ПЕРВЫМ делом. Порядок в плане может быть любым, а
    # ожидание на кнопке владелец видит сразу: пока идёт отправка длинного
    # текста или перерисовка экрана, у него крутится индикатор.
    actions = sorted(actions, key=lambda a: 0 if a.get("do") == "answer" else 1)
    for act in actions:
        do = act.get("do")
        try:
            if do == "task":
                # Тяжёлая работа (сеть к ATS, IMAP, генерация письма) —
                # в очередь в БД, разберёт отдельный поток задач: главный
                # цикл не должен стоять дольше одного вызова к Telegram, а
                # нажатие владельца обязано пережить рестарт (offset уже
                # подтверждён, Telegram повтора не пришлёт).
                state.task_push(act)
                continue
            if do == "answer":
                api.answer_callback_query(act["cb_id"], act.get("text", ""),
                                          http=http)
            elif do == "send":
                api.send_message(act["chat_id"], act["text"],
                                 act.get("markup"), http=http)
            elif do == "edit":
                api.edit_message_text(act["chat_id"], act["msg_id"],
                                      act["text"], act.get("markup"), http=http)
            elif do == "screen":
                _show_screen(act, http)
        except api.TokenRevoked:
            raise
        except Exception as e:
            log.warning("действие %s не выполнено: %s: %s",
                        do, type(e).__name__, str(e)[:120])


def _show_screen(act: dict, http) -> None:
    """Экран: кнопки правят на месте, команды шлют свежий вниз.

    Модель «один экран» правила существующее сообщение всегда — и это
    выглядело как зависание: владелец пишет /stats внизу чата, а бот
    обновляет пульт, уехавший за день на сотню сообщений вверх. Ответ
    приходил за полсекунды, но там, куда никто не смотрит; жалоба «долго
    показывает статистику» была ровно об этом.

    Теперь msg_id есть только у нажатий кнопок (callback несёт сообщение,
    на котором нажали) — они обновляются на месте, взгляд владельца уже
    там. Команда идёт без msg_id — ей отправляется новый экран вниз, а у
    старого гаснут кнопки, чтобы в чате не жило два живых пульта.
    """
    text, markup = screens.render(act.get("name", "main"))
    chat_id = act["chat_id"]
    msg_id = act.get("msg_id")
    if msg_id:
        try:
            api.edit_message_text(chat_id, msg_id, text, markup, http=http)
            state.set_screen(chat_id, msg_id)
            return
        except Exception:
            pass          # сообщение слишком старое или удалено — шлём новое

    st = state.get()
    old_id = st["screen_msg_id"] if st["screen_chat_id"] == chat_id else 0
    res = api.send_message(chat_id, text, markup, http=http)
    if res.get("message_id"):
        state.set_screen(chat_id, res["message_id"])
        if old_id and old_id != res["message_id"]:
            try:
                api.edit_message_text(
                    chat_id, old_id, "⬇️ Пульт переехал вниз",
                    {"inline_keyboard": []}, http=http)
            except Exception:
                pass      # старое сообщение могло быть удалено — не страшно


def run_forever() -> int:
    from . import handlers

    s = get_settings()
    if not s.telegram_bot_token:
        log.error("TELEGRAM_BOT_TOKEN не задан — боту нечем работать")
        return 2
    if not s.bot_owner_ids:
        # Fail-closed: бот управляет отправкой от лица владельца, поэтому
        # без явного белого списка он не выполняет ничего.
        log.error("BOT_ALLOWED_USER_IDS не задан — бот не будет ничего делать")

    http = api._client()
    try:
        # Контейнер стартует раньше DNS docker-сети: первые секунды getMe
        # падает с «Temporary failure in name resolution», процесс выходил,
        # и Docker поднимал бота по 3-4 раза на каждый деплой. Ждём сеть до
        # двух минут — это старт, а не рабочий цикл.
        me = None
        for attempt in range(24):
            try:
                me = api.get_me(http=http)
                break
            except api.TokenRevoked:
                raise
            except Exception as e:                          # noqa: BLE001
                log.warning("getMe при старте: %s (попытка %d/24), жду сеть",
                            type(e).__name__, attempt + 1)
                time.sleep(5)
        if me is None:
            log.error("сеть так и не появилась — выхожу, Docker перезапустит")
            return 3
        log.info("бот @%s (id %s), владельцы: %s",
                 me.get("username"), me.get("id"),
                 ", ".join(str(i) for i in sorted(s.bot_owner_ids)) or "НЕ ЗАДАНЫ")
        # С установленным вебхуком getUpdates всегда возвращает 409.
        api.delete_webhook(http=http)
        api.set_my_commands(screens.COMMANDS, http=http)
    except api.TokenRevoked:
        log.error("токен отозван или неверен — проверь TELEGRAM_BOT_TOKEN")
        return 2

    # Доставка уведомлений — отдельным потоком. В одном цикле с опросом
    # она ждала окончания long-poll: карточка, поставленная автопилотом
    # сразу после начала опроса, лежала до двадцати пяти секунд, хотя
    # отправить её можно было мгновенно.
    stop = threading.Event()

    def _task_loop():
        # Отдельный поток, не поток доставки: /mail при висящем IMAP держит
        # соединение до 30 с, и карточки с уведомлениями всё это время
        # стояли бы за ним в очереди.
        import httpx

        from . import handlers as _h
        own = httpx.Client(timeout=30, trust_env=False)
        while not stop.is_set():
            act = state.task_pop()
            if act is None:
                stop.wait(TASK_TICK)
                continue
            try:
                text = _h.run_task(act)
                api.send_message(act["chat_id"], text, http=own)
                state.task_done(act.get("_task_id"))
            except api.TokenRevoked:
                state.task_failed(act.get("_task_id"), "TokenRevoked")
                return
            except Exception as e:                          # noqa: BLE001
                log.warning("задача %s: %s: %s", act.get("task"),
                            type(e).__name__, str(e)[:120])
                state.task_failed(act.get("_task_id"),
                                  "%s: %s" % (type(e).__name__, str(e)[:400]))
        own.close()

    def _deliver_loop():
        import httpx
        own = httpx.Client(timeout=30, trust_env=False)
        while not stop.is_set():
            try:
                from . import watch
                watch.check()                     # автопилот встал или поднялся — скажем владельцу
                n = outbox.drain(http=own)
                if n:
                    log.info("доставлено уведомлений: %d", n)
            except api.TokenRevoked:
                log.error("токен отозван — доставка остановлена")
                return
            except Exception as e:                          # noqa: BLE001
                log.warning("очередь уведомлений: %s: %s",
                            type(e).__name__, str(e)[:120])
            stop.wait(DELIVER_TICK)
        own.close()

    threading.Thread(target=_deliver_loop, name="outbox",
                     daemon=True).start()
    threading.Thread(target=_task_loop, name="tasks",
                     daemon=True).start()

    log.info("бот запущен, слушаю обновления")
    while True:
        health.beat("bot")
        try:
            updates = api.get_updates(state.get()["offset"], http=http)
        except api.Conflict:
            log.error("параллельный getUpdates: где-то работает второй бот "
                      "(контейнер + запуск с хоста?). Жду %.0f с", CONFLICT_SLEEP)
            time.sleep(CONFLICT_SLEEP)
            continue
        except api.TokenRevoked:
            log.error("токен отозван — останавливаюсь")
            return 2
        except Exception as e:
            log.warning("опрос не удался: %s: %s", type(e).__name__, str(e)[:120])
            time.sleep(1)
            continue

        # Пачка нажатий обрабатывается с двумя ускорениями.
        #
        # 1. Планы собираются на всю пачку сразу, и повторные переходы по
        #    экранам схлопываются: владелец, быстро тыкающий «Статистика →
        #    Воронка → Очередь», ждал три полных цикла «рендер + сеть», а
        #    видел только последний экран. Теперь рисуется только он,
        #    остальным нажатиям гаснут часики — и всё.
        # 2. Замер на каждое нажатие пишется в лог: «долго» перестаёт быть
        #    ощущением, у него появляется цифра.
        plans = []
        for upd in updates:
            uid = upd.get("update_id", 0)
            if state.seen(uid):
                continue          # Telegram переотправил уже обработанное
            try:
                plans.append((uid, handlers.handle(upd)))
            except Exception:
                log.exception("апдейт %s не разобран", uid)
            finally:
                # Двигаем offset после разбора: падение посередине даст
                # повтор, а не потерю. Повтор безопасен — решения ставятся
                # атомарным claim.
                state.set_offset(uid)

        screen_at = [i for i, (_, acts) in enumerate(plans)
                     if any(a.get("do") == "screen" for a in acts)]
        last_screen = screen_at[-1] if screen_at else -1

        for i, (uid, acts) in enumerate(plans):
            if i != last_screen and i in screen_at:
                # Устаревший переход: экран всё равно будет перерисован
                # более поздним нажатием — оставляем только гашение часиков.
                acts = [a for a in acts if a.get("do") == "answer"]
            t0 = time.monotonic()
            try:
                _apply(acts, http)
            except api.TokenRevoked:
                return 2
            except Exception:
                log.exception("апдейт %s не обработан", uid)
            ms = (time.monotonic() - t0) * 1000
            if ms > 1500:
                log.warning("медленная обработка апдейта %s: %.0f мс", uid, ms)

        # Пустой long-poll и так означает «25 секунд тишины» — пауза
        # сверху была чистым мёртвым временем (7.4%).


def main() -> int:
    _setup_logging()
    try:
        return run_forever()
    except KeyboardInterrupt:
        log.info("бот остановлен")
        return 0


if __name__ == "__main__":
    sys.exit(main())
