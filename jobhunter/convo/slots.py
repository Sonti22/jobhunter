"""Разбор предложенного времени встречи из текста рекрутёра.

Вход — живое сообщение («давайте завтра в 15:00 по мск», «удобно в чт 28.08
с 11 до 12?», «Mon 3pm CET»), выход — список кандидатов в UTC.

Почему регулярками, а не языковой моделью: ошибка здесь стоит пропущенного
интервью, а LLM на таких фразах уверенно выдумывает несуществующие даты.
Разбор детерминированный и проверяемый тестами, а последнее слово всё равно
за владельцем: слот уходит ему на подтверждение (см. owner.py).

Ложные срабатывания отсекаются отдельно: «график с 9 до 18», «опыт от 3 лет»,
«зарплата 250 000» временем встречи не являются.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

OWNER_TZ = "Europe/Moscow"

# Порог, ниже которого кандидат не показывается владельцу: шум дороже
# пропуска — на нешаблонное сообщение он всё равно посмотрит глазами.
MIN_CONFIDENCE = 0.45

_WEEKDAYS = {
    "понедельник": 0, "понедельника": 0, "пн": 0, "mon": 0, "monday": 0,
    "вторник": 1, "вторника": 1, "вт": 1, "tue": 1, "tuesday": 1,
    "среда": 2, "среду": 2, "среды": 2, "ср": 2, "wed": 2, "wednesday": 2,
    "четверг": 3, "четверга": 3, "чт": 3, "thu": 3, "thursday": 3,
    "пятница": 4, "пятницу": 4, "пятницы": 4, "пт": 4, "fri": 4, "friday": 4,
    "суббота": 5, "субботу": 5, "субботы": 5, "сб": 5, "sat": 5, "saturday": 5,
    "воскресенье": 6, "воскресенья": 6, "вс": 6, "sun": 6, "sunday": 6,
}

_MONTHS = {
    "янв": 1, "января": 1, "январь": 1, "jan": 1, "january": 1,
    "фев": 2, "февраля": 2, "февраль": 2, "feb": 2, "february": 2,
    "мар": 3, "марта": 3, "март": 3, "mar": 3, "march": 3,
    "апр": 4, "апреля": 4, "апрель": 4, "apr": 4, "april": 4,
    "мая": 5, "май": 5, "may": 5,
    "июн": 6, "июня": 6, "июнь": 6, "jun": 6, "june": 6,
    "июл": 7, "июля": 7, "июль": 7, "jul": 7, "july": 7,
    "авг": 8, "августа": 8, "август": 8, "aug": 8, "august": 8,
    "сен": 9, "сентября": 9, "сентябрь": 9, "sep": 9, "sept": 9, "september": 9,
    "окт": 10, "октября": 10, "октябрь": 10, "oct": 10, "october": 10,
    "ноя": 11, "ноября": 11, "ноябрь": 11, "nov": 11, "november": 11,
    "дек": 12, "декабря": 12, "декабрь": 12, "dec": 12, "december": 12,
}

# Города и аббревиатуры → IANA. Рекрутёры пишут «по мск», «CET», «по Киеву».
_TZ_WORDS = [
    (r"\b(?:мск|msk|москв\w*|по\s+москве|спб|питер\w*)\b", "Europe/Moscow"),
    (r"\b(?:киев\w*|киеву|kyiv|kiev)\b", "Europe/Kyiv"),
    (r"\b(?:минск\w*|minsk)\b", "Europe/Minsk"),
    (r"\b(?:ереван\w*|yerevan)\b", "Asia/Yerevan"),
    (r"\b(?:тбилиси|tbilisi)\b", "Asia/Tbilisi"),
    (r"\b(?:алмат\w*|almaty)\b", "Asia/Almaty"),
    (r"\b(?:ташкент\w*|tashkent)\b", "Asia/Tashkent"),
    (r"\b(?:баку|baku)\b", "Asia/Baku"),
    (r"\b(?:дубай\w*|dubai|gst)\b", "Asia/Dubai"),
    (r"\b(?:белград\w*|belgrade)\b", "Europe/Belgrade"),
    (r"\b(?:варшав\w*|warsaw)\b", "Europe/Warsaw"),
    (r"\b(?:берлин\w*|berlin|cet|cest)\b", "Europe/Berlin"),
    (r"\b(?:лиссабон\w*|lisbon|wet)\b", "Europe/Lisbon"),
    (r"\b(?:лондон\w*|london|gmt|bst)\b", "Europe/London"),
    (r"\b(?:нью-?йорк\w*|new\s*york|\best\b|\bedt\b)\b", "America/New_York"),
    (r"\butc\b(?!\s*[+-])", "UTC"),
]
_TZ_OFFSET = re.compile(r"\b(?:utc|gmt|мск)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?\b", re.I)

# Слова, из-за которых сообщение вообще рассматривается как разговор о встрече.
_MEETING_WORDS = re.compile(
    r"(созвон|созвонимся|созвониться|звонок|встреч|интервью|собеседован|"
    r"поговорить|пообщаться|удобно|назначим|слот|zoom|meet|телемост|"
    r"\bcall\b|interview|schedule|available|meeting)", re.I)

# Контексты, где число со временем — не приглашение.
_WORKDAY = re.compile(
    r"(график|режим|рабочий\s+день|рабочего\s+дня|рабочее\s+время|"
    r"часов\s+в\s+день|working\s+hours|\b5/2\b|\b2/2\b)", re.I)
_MONEY_CTX = re.compile(r"(зарплат|вилк|оклад|ставк|salary|₽|руб|\$|usd|eur)", re.I)

_TIME_RE = re.compile(
    r"(?<![\d.,:])"
    r"(?:(?P<h1>[01]?\d|2[0-3])\s*[:.\-]\s*(?P<m1>[0-5]\d)"      # 15:00 / 15.00 / 15-00
    r"|(?P<h2>[01]?\d|2[0-3])\s*(?P<ampm>am|pm|a\.m\.|p\.m\.)"    # 3pm
    # «в 15», «с 11 до 12», «at 3». Отрицательный просмотр на am/pm обязателен:
    # иначе «at 3pm» съедается этой веткой как «3 часа» и вечер превращается в ночь.
    r"|(?:в|во|к|с|со|from|around|at)\s+(?P<h3>[01]?\d|2[0-3])"
    r"(?![\d:.,])(?!\s*[ap]\.?m)"
    r"(?P<h3tail>\s*(?:час\w*|ч\b)?))",
    re.I)

_DATE_DMY = re.compile(
    r"(?<!\d)(?P<d>[0-3]?\d)[.\-/](?P<m>[01]?\d)(?:[.\-/](?P<y>20\d{2}|\d{2}))?(?!\d)")
_DATE_WORD = re.compile(
    r"(?<!\d)(?P<d>[0-3]?\d)\s*(?:-?(?:го|е))?\s+(?P<mon>[а-яa-z]{3,10})\.?",
    re.I)
_DATE_WORD_EN = re.compile(r"\b(?P<mon>[a-z]{3,9})\.?\s+(?P<d>[0-3]?\d)(?:st|nd|rd|th)?\b",
                           re.I)
_RELATIVE = re.compile(r"\b(сегодня|завтра|послезавтра|today|tomorrow)\b", re.I)
_WEEKDAY_RE = re.compile(
    r"\b(?:в|во|on|next|след\w*|ближайш\w*)?\s*("
    + "|".join(sorted(_WEEKDAYS, key=len, reverse=True)) + r")\b", re.I)

# «с 10 до 19» — если разброс больше четырёх часов, это режим работы компании,
# а не предложение созвониться.
_RANGE = re.compile(r"с\s*(\d{1,2})(?::\d{2})?\s*(?:до|-|—|по)\s*(\d{1,2})(?::\d{2})?",
                    re.I)


@dataclass
class Slot:
    dt_utc: datetime                 # aware UTC — то, что уходит в календарь
    dt_local: datetime               # то же время в поясе разбора
    tz: str = OWNER_TZ
    raw: str = ""                    # фрагмент исходного текста
    confidence: float = 0.5
    has_time: bool = True
    notes: list = field(default_factory=list)

    def key(self) -> str:
        return self.dt_utc.strftime("%Y-%m-%dT%H:%M")

    def to_json(self) -> dict:
        return {"utc": self.dt_utc.replace(microsecond=0).isoformat(),
                "tz": self.tz, "raw": self.raw[:120],
                "confidence": round(self.confidence, 2),
                "has_time": self.has_time}


def detect_tz(text: str, default: str = OWNER_TZ) -> str:
    """Часовой пояс, названный в сообщении. Нет упоминания — пояс владельца."""
    t = (text or "").lower()
    m = _TZ_OFFSET.search(t)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        hours = int(m.group(2))
        minutes = int(m.group(3) or 0)
        if hours <= 14:
            return "UTC%+d%s" % (sign * hours, ":%02d" % minutes if minutes else "")
    for pat, name in _TZ_WORDS:
        if re.search(pat, t, re.I):
            return name
    return default


def tzinfo_of(name: str):
    """ZoneInfo по имени; «UTC+3» — фиксированный офсет."""
    m = re.fullmatch(r"UTC([+-])(\d{1,2})(?::(\d{2}))?", (name or "").strip(), re.I)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
        return timezone(sign * delta)
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(OWNER_TZ)


def _year_for(day: int, month: int, today: date) -> int:
    """Год не назван: ближайшее будущее. 3 января в декабре — это следующий год."""
    year = today.year
    try:
        cand = date(year, month, day)
    except ValueError:
        return year
    if (today - cand).days > 14:
        return year + 1
    return year


def _dates_in(text: str, now_local: datetime) -> list:
    """Все датовые якоря: (позиция_в_тексте, date, уверенность, фрагмент)."""
    out = []
    today = now_local.date()

    for m in _RELATIVE.finditer(text):
        w = m.group(1).lower()
        shift = {"сегодня": 0, "today": 0, "завтра": 1,
                 "tomorrow": 1, "послезавтра": 2}[w]
        out.append((m.start(), today + timedelta(days=shift), 0.95, m.group(0)))

    for m in _DATE_DMY.finditer(text):
        d, mo = int(m.group("d")), int(m.group("m"))
        if not (1 <= d <= 31 and 1 <= mo <= 12):
            continue
        y = m.group("y")
        year = (int(y) + 2000 if y and len(y) == 2 else int(y) if y
                else _year_for(d, mo, today))
        try:
            out.append((m.start(), date(year, mo, d), 0.9, m.group(0)))
        except ValueError:
            continue

    for rex, dgrp, mgrp in ((_DATE_WORD, "d", "mon"), (_DATE_WORD_EN, "d", "mon")):
        for m in rex.finditer(text):
            mon = _MONTHS.get(m.group(mgrp).lower())
            if not mon:
                continue
            d = int(m.group(dgrp))
            if not 1 <= d <= 31:
                continue
            try:
                out.append((m.start(), date(_year_for(d, mon, today), mon, d),
                            0.9, m.group(0)))
            except ValueError:
                continue

    for m in _WEEKDAY_RE.finditer(text):
        wd = _WEEKDAYS.get(m.group(1).lower())
        if wd is None:
            continue
        ahead = (wd - today.weekday()) % 7
        if ahead == 0:                      # «во вторник», сказанное во вторник
            ahead = 7
        if re.search(r"след|next", m.group(0), re.I) and ahead < 7:
            ahead += 7
        out.append((m.start(), today + timedelta(days=ahead), 0.75, m.group(0).strip()))

    out.sort(key=lambda x: x[0])
    return out


def _times_in(text: str) -> list:
    """Все временные якоря: (позиция, час, минута, уверенность, фрагмент)."""
    out = []
    for m in _TIME_RE.finditer(text):
        if m.group("h1") is not None:
            h, mi, conf = int(m.group("h1")), int(m.group("m1")), 0.95
        elif m.group("h2") is not None:
            h, mi, conf = int(m.group("h2")), 0, 0.9
            if m.group("ampm").lower().startswith("p") and h < 12:
                h += 12
            if m.group("ampm").lower().startswith("a") and h == 12:
                h = 0
        else:
            h, mi, conf = int(m.group("h3")), 0, 0.7
            # «в 9» без уточнения почти всегда утро рабочего дня, но «в 9»
            # вечером — 21:00. Рабочий диапазон 8-20 оставляем как есть.
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            continue
        out.append((m.start(), h, mi, conf, m.group(0).strip()))
    return out


def parse_slots(text: str, now: datetime | None = None,
                default_tz: str = OWNER_TZ, max_slots: int = 4) -> list:
    """Кандидаты во время встречи, отсортированные по времени.

    Пустой список означает «времени в сообщении нет» — не «ошибка разбора».
    """
    if not (text or "").strip():
        return []
    tz_name = detect_tz(text, default_tz)
    tz = tzinfo_of(tz_name)
    now_local = (now or datetime.now(timezone.utc)).astimezone(tz)

    low = text.lower()
    meeting_ctx = bool(_MEETING_WORDS.search(low))
    workday_ctx = bool(_WORKDAY.search(low))
    money_ctx = bool(_MONEY_CTX.search(low))

    # Режим работы «с 10 до 19»: широкий диапазон — не приглашение.
    wide_range = False
    for m in _RANGE.finditer(low):
        try:
            if int(m.group(2)) - int(m.group(1)) >= 4:
                wide_range = True
        except ValueError:
            pass

    dates = _dates_in(low, now_local)
    times = _times_in(low)
    slots = {}

    for pos, h, mi, tconf, raw in times:
        # дата берётся ближайшая слева, иначе ближайшая справа
        anchor = None
        for dpos, d, dconf, draw in dates:
            if dpos <= pos:
                anchor = (d, dconf, draw)
        if anchor is None and dates:
            d, dconf, draw = dates[0]
            anchor = (d, dconf * 0.8, draw)

        if anchor is None:
            # только время: сегодня, если ещё не прошло, иначе завтра
            cand_local = now_local.replace(hour=h, minute=mi, second=0, microsecond=0)
            if cand_local <= now_local + timedelta(minutes=30):
                cand_local += timedelta(days=1)
            conf = tconf * 0.6
            raw_full = raw
        else:
            d, dconf, draw = anchor
            cand_local = datetime(d.year, d.month, d.day, h, mi, tzinfo=tz)
            conf = min(0.98, (tconf + dconf) / 2 + 0.1)
            raw_full = "%s %s" % (draw, raw)

        if cand_local < now_local - timedelta(hours=1):
            continue
        if cand_local > now_local + timedelta(days=60):
            continue

        if meeting_ctx:
            conf += 0.1
        if workday_ctx or wide_range:
            conf -= 0.35
        if money_ctx and not meeting_ctx:
            conf -= 0.25
        conf = max(0.0, min(0.99, conf))

        s = Slot(dt_utc=cand_local.astimezone(timezone.utc), dt_local=cand_local,
                 tz=tz_name, raw=raw_full.strip(), confidence=conf)
        prev = slots.get(s.key())
        if prev is None or prev.confidence < conf:
            slots[s.key()] = s

    # Дата названа, времени нет — тоже кандидат, но владельцу придётся
    # дослать час командой /time.
    if not slots and dates:
        for _dpos, d, dconf, draw in dates[:2]:
            cand_local = datetime(d.year, d.month, d.day, 12, 0, tzinfo=tz)
            if cand_local < now_local - timedelta(hours=1):
                continue
            conf = dconf * (0.7 if meeting_ctx else 0.4)
            s = Slot(dt_utc=cand_local.astimezone(timezone.utc), dt_local=cand_local,
                     tz=tz_name, raw=draw, confidence=conf, has_time=False,
                     notes=["время не названо"])
            slots.setdefault(s.key(), s)

    out = [s for s in slots.values() if s.confidence >= MIN_CONFIDENCE]
    out.sort(key=lambda s: s.dt_utc)
    return out[:max_slots]


_DAYS_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def fmt(dt_utc: datetime, tz_name: str = OWNER_TZ) -> str:
    """«чт 28.08 15:00 (Europe/Moscow, UTC+3)» — без сокращений и догадок."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    local = dt_utc.astimezone(tzinfo_of(tz_name))
    off = local.utcoffset() or timedelta(0)
    return "%s %02d.%02d %02d:%02d (%s, UTC%+d)" % (
        _DAYS_RU[local.weekday()], local.day, local.month, local.hour,
        local.minute, tz_name, int(off.total_seconds() // 3600))


def parse_owner_time(raw: str, tz_name: str = OWNER_TZ,
                     now: datetime | None = None) -> datetime | None:
    """Время, названное владельцем в команде /time. Возвращает aware UTC.

    Здесь допущений меньше: владелец пишет «29.08 16:00» или «завтра 11:00»,
    и если разобрать не вышло — лучше отказать, чем угадать.
    """
    slots = parse_slots(raw, now=now, default_tz=tz_name, max_slots=1)
    if slots and slots[0].has_time:
        return slots[0].dt_utc
    # «2026-08-29 16:00» — ISO, которого нет в разговорных шаблонах
    m = re.search(r"(20\d{2})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})", raw or "")
    if m:
        tz = tzinfo_of(tz_name)
        local = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                         int(m.group(4)), int(m.group(5)), tzinfo=tz)
        return local.astimezone(timezone.utc)
    return None
