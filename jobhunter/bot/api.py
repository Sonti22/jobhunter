"""Клиент Bot API поверх httpx.

Отдельная библиотека (aiogram, python-telegram-bot) здесь не нужна: один чат,
десяток экранов, пять методов API. Зато httpx уже в зависимостях, и код
остаётся читаемым без фреймворка с собственным жизненным циклом.

Синхронный клиент, как в llm.py: цикл один, запросы идут последовательно,
асинхронность не даёт ничего, кроме лишней сложности.

Разбор кодов ответа — половина ценности этого модуля:
  429 — Telegram сам говорит, сколько ждать, в retry_after. Ждём и повторяем.
  409 — где-то работает второй getUpdates. Повторять бесполезно: пока второй
        поллер жив, конфликт не рассосётся. Пишем в лог и делаем паузу.
  401 — токен отозван. Это не сбой сети, а конец: сообщаем и выходим.
  5xx — на стороне Telegram. Экспоненциальная пауза.
"""
from __future__ import annotations

import json
import logging
import time

import httpx

from ..config import get_settings

log = logging.getLogger("bot.api")

BASE = "https://api.telegram.org/bot%s/%s"
# Long polling: Telegram держит соединение до timeout секунд. Клиентский
# таймаут обязан быть больше, иначе httpx рвёт нормальный запрос и порождает
# поток 409-х.
POLL_TIMEOUT = 25
# Таймауты по назначению. Одним общим числом (было 40 секунд) любой
# сетевой глюк превращался в «бот завис»: владелец жал кнопку и ждал, пока
# истечёт весь запас. Обычному вызову хватает секунд; долгим правом ждать
# обладает только long-poll getUpdates.
HTTP_TIMEOUT = 12.0        # обычные вызовы: send/edit/answer
CONNECT_TIMEOUT = 5.0      # на установку соединения — всегда
MAX_RETRIES = 3


class TokenRevoked(RuntimeError):
    """401 от Telegram: токен недействителен, работать дальше нечем."""


class Conflict(RuntimeError):
    """409: параллельный getUpdates. Второй поллер надо найти и убрать."""


def _client() -> httpx.Client:
    # trust_env=False: системный socks-прокси в окружении ломает httpx, а
    # api.telegram.org открывается напрямую.
    return httpx.Client(
        timeout=httpx.Timeout(HTTP_TIMEOUT, connect=CONNECT_TIMEOUT),
        trust_env=False)


def call(method: str, _http: httpx.Client | None = None, _files: dict | None = None,
         **params):
    """Вызов метода Bot API. Возвращает result или бросает исключение."""
    token = get_settings().telegram_bot_token
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")

    url = BASE % (token, method)
    # _read_timeout — переопределение для long-poll: getUpdates легально
    # молчит дольше обычного лимита, остальным такое право не положено.
    read_override = params.pop("_read_timeout", None)
    req_timeout = (httpx.Timeout(read_override, connect=CONNECT_TIMEOUT)
                   if read_override else None)
    own = _http is None
    http = _http or _client()
    try:
        # attempt counts only failures that justify the bounded retry budget.
        # A broken keep-alive and Telegram's explicit 429 wait are recovery
        # signals, not failed API attempts, but both still have a guard against
        # an endless loop when the service stays unhealthy.
        attempt = 0
        protocol_retries = 0
        rate_retries = 0
        while attempt < MAX_RETRIES:
            try:
                request = ({"data": {k: json.dumps(v) if isinstance(v, (dict, list, bool))
                                      else str(v) for k, v in params.items() if v is not None},
                            "files": _files} if _files else {"json": params})
                if req_timeout:
                    request["timeout"] = req_timeout
                r = http.post(url, **request)
            except httpx.RemoteProtocolError:
                # Сервер закрыл keep-alive из пула — норма протокола, а
                # не сбой. Раньше лечилось сном 2 с (43 раза за вечер =
                # полминуты глухоты в час); правильный ответ — немедленный
                # повтор. Он не расходует сетевую попытку, но постоянный
                # обрыв всё равно получает конечный предохранитель.
                protocol_retries += 1
                if protocol_retries > MAX_RETRIES * 3:
                    raise
                log.debug("%s: keep-alive закрыт, повтор", method)
                continue
            except httpx.TransportError as e:
                attempt += 1
                if attempt >= MAX_RETRIES:
                    raise
                log.warning("%s: сеть недоступна (%s), попытка %d",
                            method, type(e).__name__, attempt)
                time.sleep(0.25 * (2 ** (attempt - 1)))
                continue

            if r.status_code == 200:
                return r.json().get("result")
            if r.status_code == 401:
                raise TokenRevoked("токен отозван или неверен")
            if r.status_code == 409:
                raise Conflict("параллельный getUpdates")
            if r.status_code == 429:
                rate_retries += 1
                wait = 1
                try:
                    wait = int(r.json().get("parameters", {})
                               .get("retry_after", 1))
                except Exception:
                    pass
                # Потолок обязателен: retry_after приходит от сервера и
                # без ограничения морозил главный цикл на произвольное время.
                wait = min(wait, 15)
                # 429 не расходует сетевую попытку, но не может зациклить
                # процесс навсегда при неисправной стороне сервера.
                if rate_retries > MAX_RETRIES:
                    raise RuntimeError(
                        "%s: лимит запросов (429), попытки исчерпаны" % method)
                log.warning("%s: лимит, ждём %d с", method, wait)
                time.sleep(wait + 1)
                continue
            if r.status_code >= 500:
                attempt += 1
                if attempt >= MAX_RETRIES:
                    r.raise_for_status()
                time.sleep(2 ** attempt)
                continue

            # 400 и прочее — ошибка в наших параметрах, повтор не поможет.
            desc = ""
            try:
                desc = r.json().get("description", "")
            except Exception:
                desc = r.text[:200]
            raise RuntimeError("%s: HTTP %d %s" % (method, r.status_code, desc))
        raise RuntimeError("%s: исчерпаны попытки" % method)
    finally:
        if own:
            http.close()


# ────────────────────────────────────────────────────────── методы ──

def get_me(http=None) -> dict:
    return call("getMe", _http=http) or {}


def delete_webhook(http=None) -> None:
    """Обязательно при старте: с установленным вебхуком getUpdates отдаёт 409."""
    call("deleteWebhook", _http=http, drop_pending_updates=False)


def get_updates(offset: int, http=None, timeout: int = POLL_TIMEOUT) -> list:
    return call("getUpdates", _http=http, offset=offset or None,
                timeout=timeout,
                _read_timeout=timeout + 10,
                allowed_updates=["message", "callback_query"]) or []


def send_message(chat_id: int, text: str, markup: dict | None = None,
                 http=None) -> dict:
    params: dict = {"chat_id": chat_id, "text": text[:4096],
              "disable_web_page_preview": True}
    if markup:
        params["reply_markup"] = markup
    return call("sendMessage", _http=http, **params) or {}


def edit_message_text(chat_id: int, message_id: int, text: str,
                      markup: dict | None = None, http=None) -> dict:
    params: dict = {"chat_id": chat_id, "message_id": message_id,
              "text": text[:4096], "disable_web_page_preview": True}
    if markup is not None:
        params["reply_markup"] = markup
    try:
        return call("editMessageText", _http=http, **params) or {}
    except RuntimeError as e:
        # «message is not modified» — не ошибка: экран уже показывает это же.
        if "not modified" in str(e):
            return {}
        raise


def send_document(chat_id: int, filename: str, content: bytes, caption: str = "",
                  http=None) -> dict:
    """PDF upload to the configured owner only; never to a recruiter or group.

    Immutable bytes let the bounded HTTP retries resend a complete multipart
    body, not a file stream left at EOF by a failed attempt.
    https://core.telegram.org/bots/api#senddocument
    """
    if chat_id not in get_settings().bot_owner_ids:
        raise PermissionError("резюме можно отправлять только владельцу в личный чат")
    if (not content.startswith(b"%PDF-") or len(content) > 10 * 1024 * 1024
            or not filename.lower().endswith(".pdf") or "/" in filename or "\\" in filename):
        raise ValueError("некорректный PDF-файл")
    return call("sendDocument", _http=http, chat_id=chat_id, caption=caption[:1024],
                _read_timeout=30, _files={"document": (filename, content, "application/pdf")}) or {}


def answer_callback_query(cb_id: str, text: str = "", alert: bool = False,
                          http=None) -> None:
    call("answerCallbackQuery", _http=http, callback_query_id=cb_id,
         text=text[:200], show_alert=alert)


def set_my_commands(commands: list, http=None) -> None:
    call("setMyCommands", _http=http,
         commands=[{"command": c, "description": d} for c, d in commands])
