"""Статус аккаунта у @SpamBot: можно ли сейчас вообще писать незнакомцам.

PeerFlood — ответ Telegram на уже сделанную попытку. С 26.08 по 23.09 их было семь;
23.09 после ручного «▶️» первое же сообщение снова получило PeerFlood, хотя до этого
пять дней не уходило ничего. Каждый страйк продлевает ограничение, так что пробовать
вслепую — значит продлевать его самим. @SpamBot — официальный бот Telegram: он
называет статус аккаунта и срок ограничения заранее, без попытки.

Пока кампания в ручном режиме, бот спрашивает его сам:
  * «ограничений нет»   → ручной режим снимается (48-часовой лок после PeerFlood не
                           трогаем — он истекает сам);
  * «ограничен до даты» → пауза до этой даты, до неё не пишем и не спрашиваем;
  * без срока или ответ не распознан → остаёмся в ручном, владельцу — текст SpamBot.
    Апелляцию («This is a mistake») подаёт только владелец: там его собственные ответы.

Это не обход антиспама: отправка идёт, только когда Telegram сам говорит, что можно.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..config import get_settings
from ..db import session_scope
from ..models import AccountHealth, utcnow
from . import policy

SPAMBOT = "SpamBot"
ASK_TIMEOUT_S = 40
LOCK_PREFIX = "spambot:"

# «Ограничений нет» проверяется первым: в этой фразе тоже есть слово «limits».
_FREE = re.compile(r"no limits are currently applied|free as a bird|"
                   r"никаких ограничений|свободн\w* как птица", re.I)
_LIMITED = re.compile(r"\blimited\b|ограничен", re.I)

_EN_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_RU_MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6,
              "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12}
_TIME = r"(?:[^\d\n]{0,12}(\d{1,2}):(\d{2}))?"
_DAY_MONTH_YEAR = re.compile(r"(\d{1,2})\s+([A-Za-zА-Яа-яё]{3,9})\.?\s+(\d{4})" + _TIME)
_MONTH_DAY_YEAR = re.compile(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})" + _TIME)


@dataclass
class SpamVerdict:
    status: str                      # free | limited | unknown
    until: datetime | None = None    # наивное UTC


def _month(word: str) -> int:
    w = word.lower()
    return _EN_MONTHS.get(w[:3]) or _RU_MONTHS.get(w[:3]) or 0


def _dates(text: str) -> list:
    out = []
    for m in _DAY_MONTH_YEAR.finditer(text):
        out.append((int(m.group(1)), _month(m.group(2)), int(m.group(3)), m.group(4), m.group(5)))
    for m in _MONTH_DAY_YEAR.finditer(text):
        out.append((int(m.group(2)), _month(m.group(1)), int(m.group(3)), m.group(4), m.group(5)))
    found = []
    for day, month, year, hh, mm in out:
        if not month:
            continue
        try:
            found.append(datetime(year, month, day, int(hh or 0), int(mm or 0)))
        except ValueError:
            continue
    return found


def parse(text: str, now: datetime | None = None) -> SpamVerdict:
    """Ответ SpamBot → статус и срок. Срок — ближайшая будущая дата в тексте (UTC)."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    text = text or ""
    if _FREE.search(text):
        return SpamVerdict("free")
    if _LIMITED.search(text):
        future = [d for d in _dates(text) if now < d < now + timedelta(days=7300)]
        return SpamVerdict("limited", min(future) if future else None)
    return SpamVerdict("unknown")


def _msk(dt: datetime) -> str:
    return dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m %H:%M")


def apply(text: str, restricted: bool = False, restriction_reason: str = "") -> SpamVerdict:
    """Записать проверку и сделать вывод одной транзакцией."""
    from .. import notify
    v = parse(text)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    excerpt = " ".join((text or "").split())[:500]
    with session_scope() as sess:
        sess.add(AccountHealth(is_restricted=restricted,
                               restriction_reason=(restriction_reason or "")[:200],
                               spambot_raw=(text or "")[:4000], spambot_verdict=v.status))
        st = policy.get_state(sess)
        if v.status == "free":
            policy.resume_manual_only(sess, by_spambot=True)
            return v
        st.manual_only = True
        if v.status == "limited" and v.until:
            lk = policy.get_lock(sess)
            if not lk.locked_until or lk.locked_until < v.until:
                lk.locked_until = v.until
                lk.scope = "cold_only"
                lk.reason = "%s ограничение Telegram до %s UTC" % (LOCK_PREFIX, v.until)
                lk.set_at = utcnow()
                lk.set_by = "spambot"
            notify.push_once(
                "peerflood",
                "🧊 @SpamBot: аккаунт ограничен до %s МСК — писать незнакомцам Telegram не даст.\n"
                "До этого срока бот не пробует (каждая попытка продлевала бы ограничение), "
                "потом проверит сам и возобновит отправку.\nПочта работает как обычно."
                % _msk(v.until),
                dedup="spambot:until:%s" % v.until.strftime("%Y-%m-%d"), sess=sess)
        else:
            what = ("аккаунт ограничен без срока" if v.status == "limited"
                    else "ответ не распознан")
            notify.push_once(
                "peerflood",
                "🧊 @SpamBot: %s. Бот не пишет незнакомцам и спросит снова завтра.\n"
                "Если считаешь ограничение ошибкой — открой @SpamBot и нажми «This is a mistake» "
                "(апелляцию подаёшь ты сам).\nОтвет SpamBot: %s" % (what, excerpt),
                dedup="spambot:%s:%s" % (v.status, today), sess=sess)
    return v


async def ask(client) -> str:
    async with client.conversation(SPAMBOT, timeout=ASK_TIMEOUT_S) as conv:
        await conv.send_message("/start")
        resp = await conv.get_response()
        return resp.raw_text or ""


async def run() -> SpamVerdict:
    """Одна проверка: статус аккаунта из get_me и ответ SpamBot."""
    from telethon import TelegramClient
    s = get_settings()
    client = TelegramClient(s.telegram_session_path, s.tg_api_id, s.telegram_api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram-сессия не авторизована")
        me = await client.get_me()
        text = await asyncio.wait_for(ask(client), ASK_TIMEOUT_S + 10)
    finally:
        await client.disconnect()
    return apply(text, bool(getattr(me, "restricted", False)),
                 str(getattr(me, "restriction_reason", "") or ""))
