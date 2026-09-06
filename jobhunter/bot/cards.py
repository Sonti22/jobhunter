"""Карточки на решение: клавиатура вместо команд и разбор нажатий.

callback_data у Telegram ограничен 64 байтами, поэтому в кнопке едут только
идентификаторы: `d:<req_id>:<действие>:<аргумент>`. Слоты уже лежат в
OwnerRequest.payload_json — второй раз их передавать незачем, да и нельзя.

Свободный текст в callback_data не кладём принципиально: данные из кнопки
приходят от клиента и доверять им как источнику адресата или содержания
сообщения нельзя. Адресат всегда берётся из БД по req_id.
"""
from __future__ import annotations

from ..config import get_settings
from ..convo.slots import fmt
from ..models import OwnerRequestKind

CB_MAX = 64


def cb(*parts) -> str:
    """Собрать callback_data и убедиться, что она влезает в лимит."""
    data = ":".join(str(p) for p in parts)
    if len(data.encode("utf-8")) > CB_MAX:
        raise ValueError("callback_data длиннее %d байт: %r" % (CB_MAX, data))
    return data


def parse_cb(data: str) -> dict:
    """Разбор нажатия. {kind: d|s|k|q, ...} либо {} если формат чужой."""
    parts = (data or "").split(":")
    if not parts:
        return {}
    head = parts[0]
    if head == "w" and 3 <= len(parts) <= 4:
        return {"kind": "work", "action": parts[1], "arg": parts[2],
                "extra": parts[3] if len(parts) == 4 else ""}
    if head == "d" and len(parts) >= 3:
        return {"kind": "decision", "req_id": int(parts[1]) if parts[1].isdigit() else 0,
                "action": parts[2], "arg": parts[3] if len(parts) > 3 else ""}
    if head == "s" and len(parts) >= 2:
        return {"kind": "screen", "screen": parts[1]}
    if head == "k" and len(parts) >= 2:
        return {"kind": "killswitch", "on": parts[1] == "on"}
    if head == "q" and len(parts) >= 2:
        return {"kind": "queue", "action": parts[1]}
    if head == "m" and len(parts) >= 3:
        return {"kind": "manual",
                "app_id": int(parts[1]) if parts[1].isdigit() else 0,
                "action": parts[2]}
    if head == "t" and len(parts) == 3 and parts[1].isdigit():
        return {"kind": "manual_telegram", "app_id": int(parts[1]), "action": parts[2]}
    return {}


def _kb(rows: list) -> dict:
    return {"inline_keyboard": rows}


def keyboard_for(req) -> dict:
    """Клавиатура под карточку. req — OwnerRequest (или похожий объект)."""
    s = get_settings()
    rid = req.id
    payload = dict(getattr(req, "payload_json", None) or {})

    if payload.get("manual_reply"):
        aid = req.application_id
        return _kb([[{"text": "Открыть диалог и черновик", "callback_data": cb("s", f"work_task_{aid}")}],
                    [{"text": "Ответил вручную", "callback_data": cb("w", "manual", aid, rid)}]])

    if req.kind == OwnerRequestKind.SLOT_CONFIRM.value:
        rows = []
        for i, slot in enumerate(payload.get("slots", [])[:4]):
            # «В пятницу» без часа парсится в слот с has_time=False и
            # временем-заглушкой. Кнопка «✅ пт 10:00» подтверждала бы час,
            # который рекрутёр НЕ называл, — вместо неё кнопка уточнения:
            # тот же день, но через ввод времени владельцем.
            if not slot.get("has_time", True):
                day = slot.get("raw", "вариант %d" % (i + 1))[:36]
                rows.append([{"text": "🕒 %s — уточнить час" % day,
                              "callback_data": cb("d", rid, "time")}])
                continue
            try:
                from datetime import datetime
                when = fmt(datetime.fromisoformat(slot["utc"]),
                           slot.get("tz") or s.owner_tz)
            except Exception:
                when = slot.get("raw", "вариант %d" % (i + 1))[:40]
            rows.append([{"text": "✅ %s" % when[:48],
                          "callback_data": cb("d", rid, "ok", i)}])
        rows.append([{"text": "🕒 Другое время",
                      "callback_data": cb("d", rid, "time")},
                     {"text": "❌ Не подходит",
                      "callback_data": cb("d", rid, "no")}])
        rows.append([{"text": "⏭ Пропустить",
                      "callback_data": cb("d", rid, "skip")}])
        return _kb(rows)

    # NEEDS_HUMAN: кнопка отправки черновика есть только если черновик есть.
    rows = []
    if payload.get("draft"):
        rows.append([{"text": "📨 Отправить черновик",
                      "callback_data": cb("d", rid, "send")}])
    rows.append([{"text": "✍️ Свой текст", "callback_data": cb("d", rid, "say")},
                 {"text": "⏭ Пропустить", "callback_data": cb("d", rid, "skip")}])
    # Спорный отказ: текстовая подсказка «/close N» из бот-карточки
    # вырезается strip_hints, и без кнопки подтвердить отказ из бота нечем.
    if "отказ" in str(payload.get("reason", "")).lower():
        rows.append([{"text": "🚫 Подтвердить отказ — закрыть",
                      "callback_data": cb("d", rid, "close")}])
    return _kb(rows)


def confirm_keyboard(req_id: int, action: str) -> dict:
    """Второй шаг для свободного текста.

    Сообщение рекрутёру отозвать нельзя, а опечатка в пульте — обычное дело,
    поэтому между «написал» и «ушло» стоит явное подтверждение.
    """
    return _kb([[{"text": "📤 Отправить", "callback_data": cb("d", req_id, "yes" + action)},
                 {"text": "✖️ Отмена", "callback_data": cb("d", req_id, "cancel")}]])


def strip_hints(question: str) -> str:
    """Убрать из текста карточки хвост с текстовыми командами.

    В «Избранном» подсказка «/ok 12 · /time 12 …» — единственный способ
    ответить. В боте на её месте кнопки, и дублировать команды значит
    предлагать владельцу два разных способа сделать одно и то же.
    """
    lines = [ln for ln in (question or "").split("\n")
             if not ln.strip().startswith("/")]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def manual_keyboard(app_id: int, url: str = "") -> dict:
    """Кнопки под вакансией из очереди ручных откликов.

    Ссылка отдельной кнопкой, а не в тексте: в мобильном Telegram по кнопке
    попасть пальцем проще, чем по ссылке внутри абзаца, а очередь листают
    именно с телефона.
    """
    rows = []
    if url:
        rows.append([{"text": "🔗 Открыть вакансию", "url": url}])
    rows.append([{"text": "✅ Откликнулся", "callback_data": cb("m", app_id, "applied")},
                 {"text": "🚫 Не подходит", "callback_data": cb("m", app_id, "not_fit")}])
    rows.append([{"text": "📋 Письмо", "callback_data": cb("m", app_id, "letter")},
                 {"text": "📝 Анкета", "callback_data": cb("m", app_id, "form")}])
    rows.append([{"text": "🕒 Потом", "callback_data": cb("m", app_id, "snoozed")}])
    return _kb(rows)
