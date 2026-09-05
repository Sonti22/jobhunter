"""Экраны бота: чистые функции (данные из БД) → (текст, клавиатура).

Ни одного сетевого вызова: всё, что здесь происходит, проверяется тестом на
временной базе без моков Telegram. Ровно так же устроен owner.py, и это уже
окупилось на тестах переписки.

Модель «один экран»: бот держит одно сообщение и правит его. Иначе за неделю
чат превращается в ленту из сотни сводок, среди которых теряются карточки на
решение — то единственное, что требует внимания.
"""
from __future__ import annotations

from datetime import datetime

from .. import health, report
from ..config import get_settings
from ..convo.slots import fmt
from ..outreach import archive
from .cards import cb

BAR = "▁▂▃▄▅▆▇█"


def _meter(cur: int, cap: int, width: int = 6) -> str:
    """Квота как полоска: «▓▓░░░░ 2/7» читается быстрее дроби."""
    cap = max(cap, 1)
    filled = min(width, round(width * min(cur, cap) / cap))
    return "▓" * filled + "░" * (width - filled) + " %d/%d" % (cur, cap)


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русская плюрализация: 1 карточку, 2 карточки, 5 карточек, 21 карточку."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


def _lock_reason(verdict: str) -> str:
    """Технический вердикт политики → человеческое объяснение."""
    v = (verdict or "").lower()
    # Порядок веток не случаен: вердикты ручного режима из policy.py содержат
    # слово «PeerFlood» («кампания переведена в ручной режим (2× PeerFlood)»),
    # и ветка антиспам-паузы перехватывала их, обещая «снимется сама» — а
    # ручной режим сам не снимается никогда.
    if "kill-switch" in v:
        return "отправка выключена тобой — жми ▶️ чтобы включить"
    if "ручной режим" in v:
        return ("ручной режим после предупреждений Telegram — "
                "сам не снимется, реши в /queue")
    if "peerflood" in v or "лок до" in v:
        # вытащим дату из «лок до 2026-08-30 13:45:00…»
        import re
        m = re.search(r"лок до (\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})", verdict)
        if m:
            from datetime import datetime, timezone
            from zoneinfo import ZoneInfo
            when = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            int(m.group(4)), int(m.group(5)),
                            tzinfo=timezone.utc).astimezone(
                ZoneInfo(get_settings().owner_tz))
            return ("антиспам-пауза Telegram до %s — защита аккаунта, "
                    "снимется сама" % when.strftime("%d.%m %H:%M"))
        return "антиспам-пауза Telegram — снимется сама"
    if "квота" in v:
        return "дневная квота выбрана — продолжит завтра"
    return verdict


def _kb(rows: list) -> dict:
    return {"inline_keyboard": rows}


def _now() -> str:
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(get_settings().owner_tz)
    return datetime.now(tz).strftime("%d.%m %H:%M")


def _nav(active: str = "") -> list:
    """Нижняя навигация. Активный экран не дублируется кнопкой."""
    q = report.quota()
    f = report.funnel()
    rows = [
        [{"text": "✍️ Отправлю сам — Telegram", "callback_data": "t:0:next"}],
        [{"text": "📊 Статистика", "callback_data": cb("s", "stats")},
         {"text": "🔻 Воронка", "callback_data": cb("s", "funnel")}],
        [{"text": "📤 Очередь %d" % f["counts"].get("PENDING_APPROVAL", 0),
          "callback_data": cb("s", "queue")},
         {"text": "🗓 Интервью %d" % f["counts"].get("INTERVIEW_CONFIRMED", 0),
          "callback_data": cb("s", "iv")}],
        [{"text": "✋ Решения %d" % f["open_cards"],
          "callback_data": cb("s", "cards")},
         {"text": "🖐 Ручные", "callback_data": cb("s", "manual")}],
        [{"text": "📡 Каналы", "callback_data": cb("s", "ch")},
         {"text": "🧠 Входящие", "callback_data": cb("s", "intents")}],
    ]
    if q["kill_switch"]:
        rows.append([{"text": "▶️ СНЯТЬ СТОП", "callback_data": cb("k", "off")},
                     {"text": "🔄 Обновить", "callback_data": cb("s", active or "main")}])
    else:
        rows.append([{"text": "⛔ СТОП", "callback_data": cb("k", "on")},
                     {"text": "🔄 Обновить", "callback_data": cb("s", active or "main")}])
    return rows


def main() -> tuple:
    """Панель дня: сверху — что требует владельца, ниже — что сделала система.

    Прежний порядок был обратным (сначала счётчики, потом дела), и главное
    терялось: владелец видел цифры, но не видел, что от него ждут двух
    нажатий. Никакого жаргона: «PeerFlood» и «kill-switch» переведены.
    """
    q = report.quota()
    f = report.funnel()
    msg = report.messages_today()
    ages = health.ages()

    lines = ["🎛 ПУЛЬТ · %s" % _now(), ""]

    # 1. Что требует владельца — всегда первым.
    todo = []
    if f["open_cards"]:
        todo.append("✋ Ответить на %d %s — жми /cards"
                    % (f["open_cards"],
                       _plural(f["open_cards"],
                               "карточку", "карточки", "карточек")))
    iv = f["counts"].get("INTERVIEW_PROPOSED", 0)
    if iv:
        todo.append("🗓 Подтвердить время интервью (%d) — /cards" % iv)
    if todo:
        lines.append("── НУЖЕН ТЫ ─────────────")
        lines += todo
        lines.append("")
    else:
        lines.append("✅ От тебя сейчас ничего не нужно")
        lines.append("")

    # 2. Что сделала система сегодня.
    lines.append("── СЕГОДНЯ ──────────────")
    lines.append("Telegram  %s" % _meter(q["sent"], q["cap"]))
    lines.append("Почта     %s" % _meter(q.get("email_sent", 0),
                                         q.get("email_cap", 0)))
    if msg["incoming"] or msg["auto"]:
        lines.append("Переписка: пришло %d · бот ответил %d"
                     % (msg["incoming"], msg["auto"]))

    # 3. Здоровье каналов — человеческим языком.
    status = []
    if q["kill_switch"]:
        status.append("⛔ Отправка выключена тобой (жми ▶️ чтобы включить)")
    elif not q["can_send"]:
        status.append("⏸ %s" % _lock_reason(q["verdict"]))
    # Не дублировать: если вердикт выше уже объяснил ручной режим,
    # вторая строка о том же только путает.
    if q["manual_only"] and not any("ручной режим" in s for s in status):
        status.append("🛑 " + _lock_reason("ручной режим"))
    if status:
        lines.append("")
        lines += status

    # 4. Итог по воронке — одной строкой.
    if f["sent"]:
        lines += ["", "Всего: отправлено %d → ответили %d (%.0f%%)"
                  % (f["sent"], f["replied"], f["reply_rate"])]

    beat_ap = ages.get("autopilot", float("inf"))
    if beat_ap == float("inf"):
        # health.human(inf) — «нет отметки»: в шаблон «молчит …» не ложится.
        lines += ["", "⚠️ Автопилот ещё не выходил на связь — проверь Docker"]
    elif beat_ap > 900:
        lines += ["", "⚠️ Автопилот молчит %s — проверь Docker"
                  % health.human(beat_ap)]
    return "\n".join(lines), _kb(_nav("main"))


def stats() -> tuple:
    q = report.quota()
    a = archive.stats(7)
    series = report.daily_series(7)
    src = report.sources(30)

    lines = ["📊 Статистика · %s" % _now(), ""]
    lines.append("Сегодня  Telegram %s" % _meter(q["sent"], q["cap"]))
    lines.append("         Почта    %s" % _meter(q.get("email_sent", 0),
                                                 q.get("email_cap", 0)))
    lines.append("Дней без предупреждений Telegram: %d (предупреждений "
                 "всего: %d)" % (q["clean_days"], q["peerflood_total"]))

    if series:
        top = max((d["sent"] for d in series), default=0) or 1
        lines += ["", "ОТПРАВКИ ПО ДНЯМ"]
        for d in series:
            level = min(len(BAR) - 1, int(d["sent"] / top * (len(BAR) - 1)))
            lines.append("  %s %s %d" % (d["date"][5:], BAR[level] * 3, d["sent"]))

    if a["total"]:
        lines += ["", "ПЕРЕПИСКА ЗА НЕДЕЛЮ: %d сообщений" % a["total"]]
        for ch, n in sorted(a["by_channel"].items(), key=lambda x: -x[1]):
            lines.append("  %s: %d" % (ch, n))

    if src:
        lines += ["", "ОТКУДА ВАКАНСИИ (за месяц)"]
        for name, n in list(src.items())[:8]:
            lines.append("  %-12s %d" % (name, n))
    return "\n".join(lines), _kb(_nav("stats"))


def funnel() -> tuple:
    f = report.funnel()
    lines = ["🔻 ВОРОНКА · %s" % _now(),
             "Путь вакансии: от находки до ответа рекрутёра", ""]
    width = max((n for _, n in f["stages"]), default=0) or 1
    for label, n in f["stages"]:
        if not n:
            continue
        bar = "█" * max(1, int(n / width * 12))
        lines.append("%-18s %4d %s" % (label, n, bar))
    lines += ["", "отправлено всего: %d · ответили: %d (%.0f%%)"
              % (f["sent"], f["replied"], f["reply_rate"])]
    return "\n".join(lines), _kb(_nav("funnel"))


def queue() -> tuple:
    q = report.quota()
    rows = report.queue_top(10)
    lines = ["📤 ОЧЕРЕДЬ НА ОТПРАВКУ · %s" % _now(),
             "Письма готовы и ждут своего окна:", ""]
    if q["can_send"]:
        lines.append("✅ Отправка идёт по расписанию")
    else:
        lines.append("⏸ %s" % _lock_reason(q["verdict"]))
    lines.append("")
    if not rows:
        lines.append("Пусто — всё, что подготовлено, уже одобрено или отправлено.")
    for r in rows:
        who = ("@" + r["contact"]) if r["kind"] == "user_handle" else r["contact"]
        lines.append("%3.0f  %s" % (r["score"], r["title"]))
        lines.append("      %s · %s" % (who[:40] or "—", r["source"]))
    kb = _nav("queue")
    if rows:
        kb.insert(0, [{"text": "✅ Одобрить топ-10",
                       "callback_data": cb("q", "approve10")}])
    return "\n".join(lines), _kb(kb)


def interviews() -> tuple:
    rows = report.upcoming_interviews(10)
    lines = ["🗓 ИНТЕРВЬЮ · %s" % _now(), ""]
    if not rows:
        lines.append("Пока пусто. Как только рекрутёр предложит время")
        lines.append("и ты подтвердишь его в карточке — интервью появится")
        lines.append("здесь и в Google Calendar, с напоминаниями за сутки")
        lines.append("и за час.")
    for r in rows:
        lines.append("%s" % fmt(r["at_utc"], r["tz"]) if r["at_utc"] else "—")
        lines.append("  %s%s" % (r["title"],
                                 " · " + r["company"] if r["company"] else ""))
        if r["contact"]:
            lines.append("  @%s" % r["contact"])
        if r["link"]:
            lines.append("  %s" % r["link"])
        lines.append("")
    return "\n".join(lines), _kb(_nav("iv"))


def cards() -> tuple:
    rows = report.open_cards(10)
    lines = ["✋ ЖДУТ ТВОЕГО РЕШЕНИЯ · %s" % _now(), ""]
    if not rows:
        lines.append("Ничего не ждёт — на всё отвечено. 👌")
    for r in rows:
        head = (r["question"] or "").split("\n")[0]
        lines.append(head[:70])
    if rows:
        lines += ["", "Карточки приходят отдельными сообщениями с кнопками.",
                  "Найди их выше в этом чате."]
    return "\n".join(lines), _kb(_nav("cards"))


def channels() -> tuple:
    c = report.channels_summary()
    lines = ["📡 ОТКУДА БЕРУ ВАКАНСИИ · %s" % _now(), ""]
    lines.append("Telegram-каналов проверено: %d" % c["checked"])
    lines.append("годных: %d · читаю сейчас: %d" % (c["passed"], c["enabled"]))
    if c["top"]:
        lines += ["", "САМЫЕ ПОЛЕЗНЫЕ (вакансии с прямым контактом)"]
        for t in c["top"]:
            lines.append("  @%-24s %2d контактов, %2d постов за неделю"
                         % (t["username"][:24], t["contacts"], t["fresh7"]))
    return "\n".join(lines), _kb(_nav("ch"))


def manual() -> tuple:
    """Ручные отклики: вакансии без прямого контакта."""
    from ..manual_apply import listing
    from ..manual_apply import stats as manual_stats

    st = manual_stats()
    rows = listing(8)
    lines = ["🖐 РУЧНЫЕ ОТКЛИКИ · %s" % _now(),
             "Вакансии без прямого контакта — тут нужен ты:", ""]
    # «Всего 5517» пугало и ничего не значило: треть этих вакансий открыть
    # нельзя вовсе, а работать есть с чем ровно среди подходящих.
    lines.append("можно откликнуться: %d · подходящих: %d"
                 % (st.get("reachable", st["total"]), st["ready"]))
    if st.get("unreachable"):
        lines.append("(ещё %d без канала связи — закрываются автоматически)"
                     % st["unreachable"])
    lines.append("откликнулся: %d · не подошло: %d · отложено: %d"
                 % (st["applied"], st["not_fit"], st["snoozed"]))
    lines.append("")
    if not rows:
        lines.append("Очередь пуста — всё разобрано.")
    else:
        lines.append("Ближайшие по соответствию:")
        for r in rows:
            lines.append("%3.0f  %s" % (r["score"], r["title"][:44]))
            if r["company"]:
                lines.append("      %s" % r["company"][:44])
        lines += ["", "Каждое утро пришлю пачку с кнопками.",
                  "Полный список: http://127.0.0.1:8765/manual"]
    return "\n".join(lines), _kb(_nav("manual"))


def intents() -> tuple:
    """Как классификатор разбирает входящие и где LLM его поправляет."""
    data = report.intents_daily(7)
    lines = ["🧠 КАК Я ПОНЯЛ ВХОДЯЩИЕ · за неделю · %s" % _now(), ""]
    if not data["days"]:
        lines.append("Рекрутёры пока не писали.")
    human = {"ask_cv": "просят резюме", "ask_call": "зовут созвониться",
             "slot_proposed": "предлагают время", "ack": "вежливость",
             "about": "просят рассказать о себе", "tech_question": "техвопрос",
             "money": "про деньги", "offer": "оффер", "rejection": "отказ",
             "work_format": "про формат работы", "unknown": "не понял"}
    for day, counts in data["days"].items():
        row = ", ".join("%s %d" % (human.get(k, k), v) for k, v in
                        sorted(counts.items(), key=lambda kv: -kv[1]))
        lines.append("  %s  %s" % (day, row))
    lines += ["", "Раз я ошибся и LLM меня поправила: %d" % data["llm_corrections"]]
    return "\n".join(lines), _kb(_nav("intents"))


SCREENS = {"main": main, "stats": stats, "funnel": funnel, "queue": queue,
           "iv": interviews, "cards": cards, "ch": channels, "manual": manual,
           "intents": intents}


def render(name: str) -> tuple:
    return SCREENS.get(name, main)()


HELP = (
    "jobhunter — пульт\n\n"
    "/start, /stats — главный экран\n"
    "/mail — сводка почты: что непрочитано и от кого\n"
    "/queue — очередь на отправку\n"
    "/outreach — ник, текст и PDF; отметил отправку — получаешь следующий отклик\n"
    "/cards — что ждёт решения\n"
    "/interviews — ближайшие интервью\n"
    "/channels — каналы-источники\n"
    "/stop — остановить отправку\n"
    "/go — возобновить\n"
    "/help — эта справка\n\n"
    "Карточки на решение приходят сами, с кнопками. "
    "Нажатие принимается сразу, а сообщение рекрутёру уходит в течение "
    "пяти минут — отправлять может только процесс с личной сессией Telegram."
)

COMMANDS = [
    ("start", "главный экран"),
    ("stats", "статистика"),
    ("queue", "очередь на отправку"),
    ("cards", "ждут решения"),
    ("interviews", "ближайшие интервью"),
    ("channels", "каналы-источники"),
    ("manual", "ручные отклики"),
    ("outreach", "Telegram: отправлю сам"),
    ("stop", "остановить отправку"),
    ("go", "возобновить отправку"),
    ("help", "справка"),
]
