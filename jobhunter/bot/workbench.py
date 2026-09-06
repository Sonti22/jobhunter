"""Owner-only workbench: database views and explicit decisions, never transports."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from ..config import get_settings
from .cards import cb

PAGE = 5
EVENT_LABELS = {"interview_done": "Интервью прошло", "offer": "Получен оффер",
                "rejected": "Отказ"}
MILESTONE_LABELS = {"interested": "Сигнал интереса", "cv_requested": "Запрос CV",
                    "interview_scheduled": "Интервью назначено", **EVENT_LABELS}
SOURCE_LABELS = {"classifier": "сигнал классификатора", "owner": "подтверждение владельца",
                 "calendar": "согласованное интервью", "legacy": "исторические данные"}


def _button(text: str, screen: str) -> dict:
    return {"text": text, "callback_data": cb("s", screen)}


def _view(lines: list, rows: list) -> tuple:
    rows.append([_button("Нужно сделать", "work_tasks_0"), _button("Главная", "main")])
    return "\n".join(lines)[:4000], {"inline_keyboard": rows}


def _pages(rows: list, prefix: str, offset: int, total: int) -> None:
    nav = []
    if offset:
        nav.append(_button("← Назад", f"{prefix}_{max(0, offset - PAGE)}"))
    if offset + PAGE < total:
        nav.append(_button("Дальше →", f"{prefix}_{offset + PAGE}"))
    if nav:
        rows.append(nav)


def _time(value) -> str:
    if not value:
        return "неизвестно"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo(get_settings().owner_tz)).strftime("%d.%m %H:%M")
    except (AttributeError, ValueError, TypeError):
        return "неизвестно"


def tasks(offset: int = 0) -> tuple:
    from ..dashboard import attention
    data = attention(limit=PAGE, offset=offset)
    lines = [f"✋ НУЖНО СДЕЛАТЬ · {data['total']}", ""]
    rows = []
    for item in data["items"]:
        aid = item["id"]
        lines += [f"#{aid} · {item['title'][:110]}", item["reason"][:220], ""]
        rows.append([_button(f"Открыть #{aid}", f"work_task_{aid}")])
    if not data["total"]:
        lines.append("Незавершённых дел в учёте нет.")
    _pages(rows, "work_tasks", offset, data["total"])
    return _view(lines, rows)


def task(app_id: int) -> tuple:
    from ..db import session_scope
    from ..models import OwnerRequest
    from ..taskhub import detail
    from .cards import keyboard_for
    data = detail(app_id)
    if not data:
        return _view(["Диалог не найден или недоступен."], [])
    lines = [f"#{app_id} · {data.get('title', '')[:160]}",
             data.get("reason", data.get("status", "")), "",
             "Последнее входящее:", (data.get("incoming") or "Нет сохранённого текста")[:1100],
             "", "Черновик:", (data.get("draft") or "Нет готового черновика")[:1100]]
    rows = []
    rid = data.get("request_id")
    if data.get("can_review_match"):
        rows.append([{"text": "Почему подходит", "callback_data": cb("t", app_id, "why")},
                     {"text": "Проверил соответствие", "callback_data": cb("w", "match", app_id)}])
    if data.get("can_open_card"):
        rows.append([{"text": "Открыть / восстановить карточку",
                      "callback_data": cb("w", "open", app_id)}])
    if data.get("manual"):
        lines.append("Ручной диалог: бот рекрутёру ничего не отправляет.")
        handle = (data.get("handle") or "").lstrip("@")
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", handle):
            draft = data.get("draft") or ""
            rows.append([{"text": f"Открыть @{handle}", "url": "https://t.me/" + handle
                          + ("?" + urlencode({"text": draft}) if draft else "")}])
            rows.append([{"text": "Скопировать ник", "copy_text": {"text": "@" + handle}}])
        if data.get("draft"):
            rows.append([{"text": "Текст отдельно", "callback_data": cb("w", "draft", app_id)}])
        if rid and data.get("can_open_card"):
            rows.append([{"text": "Ответил вручную", "callback_data": cb("w", "manual", app_id, rid)}])
    elif rid:
        with session_scope() as sess:
            req = sess.get(OwnerRequest, rid)
            if req and not req.decision and (not req.expires_at or
                    req.expires_at > datetime.now(timezone.utc).replace(tzinfo=None)):
                rows += keyboard_for(req)["inline_keyboard"]
    if data.get("sent_at"):
        rows.append([{"text": label, "callback_data": cb("w", "event", app_id, kind)}
                     for kind, label in EVENT_LABELS.items()])
    rows.append([_button("История результатов", f"work_history_{app_id}")])
    return _view(lines, rows)


def tracks() -> tuple:
    from ..manual_telegram import TRACKS, selected_track
    selected = selected_track()
    rows = [[{"text": ("✓ " if key == selected else "") + label,
              "callback_data": cb("w", "track", key)}] for key, label in TRACKS.items()]
    rows.append([{"text": "Продолжить отправку вручную", "callback_data": "t:0:next"}])
    rows.append([_button("Причины пропуска", "work_feedback")])
    return _view(["🎯 НАПРАВЛЕНИЯ", "Выбрано: " + TRACKS[selected],
                  "Фильтр действует на новые карточки. Уже выданная остаётся текущей.",
                  "Порядок: соответствие → свежесть → идентификатор."], rows)


def feedback() -> tuple:
    from ..feedback import REASONS, summary
    data = summary()
    lines = [f"ПРИЧИНЫ ПРОПУСКА · {data['total']}"]
    lines += [f"{label}: {data['reasons'].get(key, 0)}" for key, label in REASONS.items()]
    lines += ["", data["note"]]
    return _view(lines, [])


def skip_keyboard(app_id: int) -> dict:
    from ..feedback import REASONS
    rows = [[{"text": label, "callback_data": cb("w", "skip", app_id, key)}]
            for key, label in REASONS.items()]
    rows.append([{"text": "Отмена", "callback_data": cb("t", app_id, "cancel")}])
    return {"inline_keyboard": rows}


def reading() -> tuple:
    from ..dashboard import reading as snapshot
    data = snapshot()
    lines = ["📖 ПОЛНОТА ЧТЕНИЯ", "Время — " + get_settings().owner_tz]
    for key, label in (("gmail", "Gmail"), ("telegram_inbox", "Рабочие Telegram-диалоги")):
        item = data.get(key, {})
        details = item.get("details") or {}
        remaining = details.get("remaining")
        lines += ["", label + ": " + str(item.get("status", "неизвестно")),
                  "Последний проход: " + _time(item.get("at")),
                  "Обработано: " + str(details.get("processed", details.get("seen", "неизвестно"))),
                  "Осталось загрузить: " + ("неизвестно" if remaining is None else str(remaining)),
                  "Сохранено, но разбор не завершён: " + str(item.get("pending_processing", 0))]
        if item.get("error"):
            lines.append("Ошибка: " + item["error"][:220])
    channels = data.get("channels", [])
    complete = sum(bool(c.get("history_complete")) for c in channels)
    lines += ["", f"Telegram-каналов в учёте: {len(channels)}",
              f"Полная история подтверждена: {complete}; остаток неизвестен: {len(channels)-complete}",
              "Публикаций за последние проходы: " + str(sum(c.get("posts", 0) or 0 for c in channels)),
              "Вакансий за последние проходы: " + str(sum(c.get("vacancies", 0) or 0 for c in channels)),
              "", data.get("scope_note", "Нулевой остаток не доказывает полноту старой истории.")]
    return _view(lines, [[_button("Обновить", "work_reading")]])


def sending(offset: int = 0) -> tuple:
    from ..dashboard import sending as snapshot
    data = snapshot(limit=PAGE, offset=offset)
    lines = [f"ПОЧЕМУ ЖДЁТ · {data['total']}", "Время — " + get_settings().owner_tz, ""]
    rows = []
    for item in data["items"]:
        lines += [f"#{item['id']} · {item['title'][:100]}", item["reason"][:260]]
        if item.get("retry_at"):
            lines.append("Не ранее: " + _time(item["retry_at"]))
        if item.get("next_attempt"):
            lines.append("Запуск по расписанию: " + _time(item["next_attempt"]))
        lines.append("")
        rows.append([_button(f"Заявка #{item['id']}", f"work_task_{item['id']}")])
    lines.append("Время запуска не является подтверждением доставки.")
    _pages(rows, "work_sending", offset, data["total"])
    return _view(lines, rows)


def results(track: str = "all", offset: int = 0) -> tuple:
    from ..manual_telegram import TRACKS
    from ..results import aggregate
    data = aggregate(track=track, offset=offset, limit=PAGE)
    lines = ["🎯 РЕЗУЛЬТАТЫ · 90 дней", "Направление: " + TRACKS.get(track, track),
             "Отправленных откликов в учёте: " + str(data.get("sent", 0))]
    labels = {"no_reply": "Без ответа", "interested": "Сигнал интереса",
              "cv_requested": "Запрос CV", "rejected": "Отказ", "interview": "Интервью",
              "offer": "Оффер", "other_reply": "Прочие ответы"}
    for key, label in labels.items():
        lines.append(f"{label}: {data.get('categories', {}).get(key, 0)}")
    stages = data.get("milestones", {})
    if stages:
        lines += ["", "Достигнутые этапы (сохраняются после отказа):"]
        lines += [f"{MILESTONE_LABELS.get(key, key)}: {value}" for key, value in stages.items()]
    delivery = data.get("delivery", {})
    lines += ["", "Отправка со слов владельца: " + str(delivery.get("owner_reported", 0)),
              "Принято транспортом: " + str(delivery.get("transport_confirmed", 0)),
              "Подтверждение неизвестно: " + str(delivery.get("unknown", 0))]
    if track == "all":
        lines += ["", "По направлениям: отправлено / интерес / состоявшиеся интервью / офферы"]
        for key in ("backend", "ml", "architect"):
            summary = data.get("by_track", {}).get(key, {})
            counts = summary.get("counts", {})
            lines.append(f"{TRACKS[key]}: {summary.get('sent', 0)} / {counts.get('interested', 0)} / "
                         f"{counts.get('interview_done', 0)} / {counts.get('offer', 0)}")
    lines += ["", "Сигнал классификатора не равен подтверждённому интервью или офферу.",
              "Ручная отметка не подтверждает доставку API и точную версию отправленного текста."]
    rows = [[_button(label, f"work_results_{key}_0")]
            for key, label in TRACKS.items() if key != "additional"]
    rows.append([_button("Качество источников и шаблонов", f"work_quality_{track}")])
    for item in data.get("applications", []):
        rows.append([_button(f"#{item['id']} · {item.get('title', '')[:40]}", f"work_task_{item['id']}")])
    _pages(rows, f"work_results_{track}", offset, data.get("sent", 0))
    return _view(lines, rows)


def history(app_id: int) -> tuple:
    from ..results import application_history
    events = application_history(app_id)[:20]
    lines = [f"ИСТОРИЯ РЕЗУЛЬТАТОВ #{app_id}"]
    for event in events:
        label = MILESTONE_LABELS.get(event["kind"], event["kind"])
        source = SOURCE_LABELS.get(event["source"], event["source"])
        lines += [f"{label} · {source}",
                  "Время события: " + _time(event.get("occurred_at")),
                  "Записано: " + _time(event.get("recorded_at")), ""]
    if not events:
        lines.append("Подтверждённых записей истории нет; старые этапы не выдумываются.")
    return _view(lines, [[_button("Диалог", f"work_task_{app_id}")]])


def quality(track: str = "all") -> tuple:
    from ..results import aggregate
    data = aggregate(track=track, limit=1)
    lines = ["КАЧЕСТВО ИСТОЧНИКОВ И ШАБЛОНОВ", "Когорта отправок за 90 дней, ожидание не меньше 14 дней.",
             "Предпочтения включаются только от 20 наблюдений; это не доказательство превосходства."]
    for key, label in (("by_source", "Источники"), ("by_template", "Шаблоны")):
        rows = data.get("group_quality", {}).get(key, [])
        lines += ["", label + ":"]
        if not rows:
            lines.append("Недостаточно наблюдений — предпочтения нейтральны.")
        for row in rows[:8]:
            lines.append(f"{row['key']}: {row['sent']} откликов, "
                         f"положительный результат {row.get('positive', 0)}")
    lines += ["", "Отказы, запрос CV и обычные ответы сами по себе не повышают рейтинг."]
    return _view(lines, [[_button("Результаты", f"work_results_{track}_0")]])


def render(name: str) -> tuple:
    parts = name.split("_")
    kind = parts[1] if len(parts) > 1 else "tasks"
    try:
        if kind == "tasks":
            return tasks(max(0, int(parts[2])))
        if kind == "task":
            return task(int(parts[2]))
        if kind == "history":
            return history(int(parts[2]))
        if kind == "sending":
            return sending(max(0, int(parts[2])))
        if kind == "results":
            return results(parts[2], max(0, int(parts[3])))
        if kind == "quality":
            return quality(parts[2])
        return {"tracks": tracks, "reading": reading, "feedback": feedback}.get(kind, tasks)()
    except (ValueError, IndexError):
        return _view(["Неизвестный экран. Вернись к списку дел."], [])


def callback(data: dict, cb_id: str, chat_id: int, msg_id: int) -> list:
    from .. import manual_telegram, taskhub
    from .. import results as outcome
    answer = {"do": "answer", "cb_id": cb_id}
    if chat_id not in get_settings().bot_owner_ids:
        return [{**answer, "text": "Недоступно"}]
    action, arg, extra = data["action"], data["arg"], data.get("extra", "")
    if action == "track":
        if arg not in manual_telegram.TRACKS:
            return [{**answer, "text": "Неизвестное направление"}]
        manual_telegram.set_track(chat_id, arg)
        return [answer, {"do": "screen", "chat_id": chat_id, "msg_id": msg_id, "name": "work_tracks"}]
    if not arg.isdigit():
        return [{**answer, "text": "Неверная заявка"}]
    aid = int(arg)
    if action == "match":
        from ..db import session_scope
        from ..match.explain import review_fingerprint
        from ..models import Application, Job
        with session_scope() as sess:
            app = sess.get(Application, aid)
            if not app:
                return [{**answer, "text": "Заявка не найдена"}]
            fingerprint = review_fingerprint(app, sess.get(Job, app.job_id))
        return [answer, {"do": "send", "chat_id": chat_id,
                "text": manual_telegram.explain_card(aid)[:3400] +
                        "\n\nПодтверди, что проверил требования и готов одобрить этот отклик. "
                        "Проверки фактов и ограничения отправки остаются в силе.",
                "markup": {"inline_keyboard": [[
                    {"text": "Проверил — одобрить", "callback_data": cb("w", "match_yes", aid, fingerprint)},
                    _button("Отмена", f"work_task_{aid}")]]}}]
    elif action == "match_yes":
        from ..match.explain import approve_reviewed
        ok, note = approve_reviewed(aid, extra, chat_id)
    elif action == "skip":
        ok, note = manual_telegram.mark(aid, "skip", next_chat_id=chat_id, reason=extra)
    elif action == "open":
        result = taskhub.open_card(aid)
        return [{**answer, "text": result.get("note", "")[:150]},
                {"do": "screen", "chat_id": chat_id, "msg_id": msg_id, "name": f"work_task_{aid}"}]
    elif action == "draft":
        item = taskhub.detail(aid)
        text = (item or {}).get("draft") or "Нет доступного черновика."
        return [answer, {"do": "send", "chat_id": chat_id, "text": text[:3900]}]
    elif action in ("manual", "event"):
        if action == "event" and extra not in EVENT_LABELS:
            return [{**answer, "text": "Неизвестное событие"}]
        if action == "manual" and not extra.isdigit():
            return [{**answer, "text": "Карточка не найдена"}]
        label = EVENT_LABELS[extra] if action == "event" else "Ответил вручную"
        return [answer, {"do": "send", "chat_id": chat_id,
                "text": f"Записать с твоих слов: «{label}» по заявке #{aid}? Это ничего не отправит рекрутёру.",
                "markup": {"inline_keyboard": [[
                    {"text": "Подтверждаю", "callback_data": cb("w", action + "_yes", aid, extra)},
                    _button("Отмена", f"work_task_{aid}")]]}}]
    elif action == "manual_yes" and extra.isdigit():
        ok, note = taskhub.complete_manual(aid, int(extra), chat_id)
    elif action == "event_yes" and extra in EVENT_LABELS:
        ok, note = outcome.owner_record(aid, extra, chat_id, cb_id)
    else:
        return [{**answer, "text": "Действие недоступно"}]
    return [answer, {"do": "edit", "chat_id": chat_id, "msg_id": msg_id,
                     "text": f"#{aid}: {note}", "markup": {"inline_keyboard": [
                         [_button("Нужно сделать", "work_tasks_0"),
                          _button("Результаты", "work_results_all_0")]]}}]
