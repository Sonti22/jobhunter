"""Автопоиск Telegram-каналов с вакансиями.

Три источника кандидатов, от дешёвого к дорогому:

  1. Рекомендации Telegram (channels.GetChannelRecommendations) — «похожие
     каналы» для тех, что уже в списке. Самый качественный сигнал: его
     считает сам Telegram по пересечению аудиторий, и он бесплатен по
     лимитам — один запрос на канал.
  2. Поиск по названиям (contacts.Search) — ищет каналы, чьё имя или
     @username содержит запрос: «вакансии python», «it jobs remote».
  3. Глобальный поиск сообщений (messages.SearchGlobal) — находит каналы по
     тексту постов. Дороже и шумнее, поэтому идёт последним и только если
     первые два дали мало.

Дальше каждый кандидат проверяется по открытой веб-версии t.me/s/<name>:
свежесть постов, доля постов с прямым контактом рекрутёра, объём аудитории.
Это тот же путь, которым канал читает обычный посетитель без входа в аккаунт,
и он не тратит лимиты MTProto.

Почему проверка через t.me, а не через client.get_messages: массовое чтение
незнакомых каналов с личного аккаунта — характерная активность парсера, и
именно она приводит к ограничениям. Читаем ровно то, что открыто всем.

    python -m jobhunter.ingest.discover            # найти и оценить
    python -m jobhunter.ingest.discover --apply    # добавить хорошие в реестр
    python -m jobhunter.ingest.discover --list     # что уже найдено
"""
from __future__ import annotations

import argparse
import asyncio
import random
import re
import sys
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from ..config import get_settings
from ..db import session_scope
from ..models import ChannelCandidate, utcnow

# Запросы под профиль владельца. Держим короткими: Telegram ищет по вхождению
# в название, длинная фраза не находит ничего.
# Запросов про релокацию здесь больше нет намеренно: владелец рассматривает
# только удалённый формат, и каналы «переезд + офис» тратили лимит проверок
# на вакансии, которые скоринг всё равно отсечёт.
QUERIES_RU = [
    "вакансии python", "python работа", "backend вакансии", "it вакансии",
    "devops вакансии", "работа программист", "удаленная работа it",
    "удаленка разработчик", "удаленные вакансии", "data engineer вакансии",
    "вакансии разработчик", "job python", "team lead вакансии",
    "вакансии тимлид", "айти вакансии", "вакансии backend python",
    "работа удаленно it", "python developer вакансии",
]
QUERIES_EN = [
    "python jobs", "backend jobs", "remote it jobs", "developer jobs",
    "devops jobs", "remote python jobs", "remote developer",
    "backend engineer jobs", "remote work tech",
]

# Слова, по которым канал считается вакансионным (в названии или описании).
JOB_WORDS = re.compile(
    r"(ваканс|работа|job|hiring|career|карьер|рекрут|hr\b|найм|подбор|"
    r"trud|занятост|remote|релокац)", re.I)

# Явный мусор: сливы курсов, крипта, ставки, «работа на дому» без IT.
JUNK_WORDS = re.compile(
    r"(казино|ставк|betting|crypto|крипт|заработок|инвест|forex|"
    r"курсы|слив|складчина|18\+|интим|подработка на дому|дропшип)", re.I)

# Контакт рекрутёра в посте: @handle или t.me/handle, но не хендл самого канала.
CONTACT_RE = re.compile(r"(?:@|t\.me/)([A-Za-z][A-Za-z0-9_]{4,31})")
MSG_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(?P<body>.*?)</div>',
    re.S)
TIME_RE = re.compile(r'<time[^>]+datetime="(?P<dt>[^"]+)"')
SUBS_RE = re.compile(r'<div class="tgme_page_extra">([^<]*)</div>')
TITLE_RE = re.compile(r'<div class="tgme_channel_info_header_title[^"]*"[^>]*>'
                      r'<span[^>]*>(?P<t>[^<]+)</span>')
TAG_RE = re.compile(r"<[^>]+>")

UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/140.0.0.0 Safari/537.36"),
      "Accept-Language": "ru,en;q=0.9"}


# ─────────────────────────────────────────── сбор кандидатов через MTProto ──

async def _recommendations(client, seeds: list, limit: int) -> dict:
    """Похожие каналы для каждого seed. {username: откуда}."""
    from telethon.tl.functions.channels import GetChannelRecommendationsRequest

    found = {}
    for name in seeds:
        if len(found) >= limit:
            break
        try:
            res = await client(GetChannelRecommendationsRequest(channel=name))
        except Exception:
            continue                      # канал недоступен — не беда
        for ch in getattr(res, "chats", []) or []:
            u = getattr(ch, "username", None)
            if u:
                found.setdefault(u.lower(), "похож на @" + name)
        await asyncio.sleep(random.uniform(1.0, 2.5))
    return found


async def _search_titles(client, queries: list, limit: int) -> dict:
    """Поиск каналов по названию. {username: откуда}."""
    from telethon.tl.functions.contacts import SearchRequest

    found = {}
    for q in queries:
        if len(found) >= limit:
            break
        try:
            res = await client(SearchRequest(q=q, limit=25))
        except Exception:
            continue
        for ch in list(getattr(res, "chats", []) or []):
            u = getattr(ch, "username", None)
            title = getattr(ch, "title", "") or ""
            if not u:
                continue
            if not (JOB_WORDS.search(title) or JOB_WORDS.search(u)):
                continue
            found.setdefault(u.lower(), "поиск «%s»" % q)
        await asyncio.sleep(random.uniform(1.5, 3.0))
    return found


async def collect_candidates(client, seeds: list, max_checks: int) -> dict:
    """Кандидаты из всех источников. {username: откуда}."""
    out = {}
    out.update(await _recommendations(client, seeds[:12], max_checks))
    print("   рекомендации Telegram: %d" % len(out))
    if len(out) < max_checks:
        titles = await _search_titles(client, QUERIES_RU + QUERIES_EN,
                                      max_checks - len(out))
        for k, v in titles.items():
            out.setdefault(k, v)
        print("   поиск по названиям: всего %d" % len(out))
    return out


# ────────────────────────────────────────────── оценка канала через t.me ──

def _clean(html_text: str) -> str:
    t = re.sub(r"<br\s*/?>", "\n", html_text or "", flags=re.I)
    t = TAG_RE.sub("", t)
    import html as _h
    return _h.unescape(t)


def _subs_count(raw: str) -> int:
    m = re.search(r"([\d\s.,]+)\s*(?:subscribers|подписчик)", raw or "", re.I)
    if not m:
        return 0
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else 0


def evaluate(username: str, client_http: httpx.Client,
             known: set | None = None) -> dict:
    """Оценка канала по его публичной странице. Сеть, но без аккаунта."""
    known = known or set()
    info = {"username": username, "ok": False, "reason": "", "posts": 0,
            "fresh7": 0, "with_contact": 0, "subscribers": 0, "title": ""}
    try:
        r = client_http.get("https://t.me/s/" + username, headers=UA,
                            timeout=20, follow_redirects=True)
    except Exception as e:
        info["reason"] = type(e).__name__
        return info
    if r.status_code != 200:
        info["reason"] = "HTTP %d" % r.status_code
        return info

    page = r.text
    tm = TITLE_RE.search(page)
    info["title"] = _clean(tm.group("t")) if tm else username
    sm = SUBS_RE.search(page)
    info["subscribers"] = _subs_count(_clean(sm.group(1)) if sm else "")

    if JUNK_WORDS.search(info["title"]):
        info["reason"] = "мусорная тематика"
        return info

    bodies = [_clean(m.group("body")) for m in MSG_RE.finditer(page)]
    times = [t.group("dt") for t in TIME_RE.finditer(page)]
    info["posts"] = len(bodies)
    if not bodies:
        info["reason"] = "нет постов (закрытый или пустой)"
        return info

    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    for raw in times:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt >= week_ago:
            info["fresh7"] += 1

    self_names = {username.lower()}
    for body in bodies:
        handles = {h.lower() for h in CONTACT_RE.findall(body)}
        if handles - self_names - known:
            info["with_contact"] += 1

    job_posts = sum(1 for b in bodies if JOB_WORDS.search(b))
    if job_posts < max(2, len(bodies) // 5):
        info["reason"] = "не про вакансии (%d/%d постов)" % (job_posts, len(bodies))
        return info

    s = get_settings()
    if info["fresh7"] < s.discover_min_fresh7:
        info["reason"] = "мёртвый: %d постов за неделю" % info["fresh7"]
        return info
    if info["with_contact"] < s.discover_min_contacts:
        info["reason"] = "мало прямых контактов (%d)" % info["with_contact"]
        return info

    info["ok"] = True
    info["reason"] = "%d постов, %d за неделю, %d с контактом" % (
        info["posts"], info["fresh7"], info["with_contact"])
    return info


# ──────────────────────────────────────────────────────────── прогон ──

def _known_usernames() -> set:
    """Хендлы, которые уже в реестре или в кросс-промо — не контакт рекрутёра."""
    from .tgchannels import CHANNELS, CROSS_PROMO
    known = {c[0].lower() for c in CHANNELS} | {c.lower() for c in CROSS_PROMO}
    with session_scope() as sess:
        for row in sess.scalars(select(ChannelCandidate)).all():
            known.add(row.username.lower())
    return known


async def run(max_checks: int | None = None, apply: bool = False) -> dict:
    """Полный цикл: собрать кандидатов → оценить → записать в БД."""
    from telethon import TelegramClient

    from .tgchannels import CHANNELS

    s = get_settings()
    if not s.discover_enabled:
        return {"error": "DISCOVER_ENABLED=false"}
    if not (s.tg_api_id and s.telegram_api_hash):
        return {"error": "нет ключей Telegram"}
    max_checks = max_checks or s.discover_max_checks

    known = _known_usernames()
    seeds = [c[0] for c in CHANNELS]

    client = TelegramClient(s.telegram_session_path, s.tg_api_id,
                            s.telegram_api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return {"error": "сессия не авторизована (python tg_login.py)"}
        print("Ищу каналы (лимит проверок: %d)..." % max_checks)
        candidates = await collect_candidates(client, seeds, max_checks * 2)
    finally:
        await client.disconnect()

    fresh = {u: src for u, src in candidates.items() if u not in known}
    print("   новых кандидатов: %d (из %d найденных)" % (len(fresh), len(candidates)))

    stats = {"checked": 0, "good": 0, "rejected": 0, "added": 0}
    good = []
    # trust_env=False: в окружении может стоять socks-прокси для других задач,
    # httpx на него ругается, а t.me открывается напрямую.
    with httpx.Client(http2=False, trust_env=False, follow_redirects=True) as http:
        for username, source in list(fresh.items())[:max_checks]:
            info = evaluate(username, http, known)
            stats["checked"] += 1
            mark = "+" if info["ok"] else "-"
            print("   %s @%-28s %s" % (mark, username, info["reason"][:60]))
            if info["ok"]:
                stats["good"] += 1
                good.append((info, source))
            else:
                stats["rejected"] += 1
            _save_candidate(info, source)
            # t.me читаем как обычный посетитель: не чаще раза в 2 секунды
            import time as _t
            _t.sleep(random.uniform(1.8, 3.2))

    if apply and good:
        stats["added"] = _apply_to_registry(good)
    return stats


def _save_candidate(info: dict, source: str) -> None:
    with session_scope() as sess:
        row = sess.scalars(select(ChannelCandidate).where(
            ChannelCandidate.username == info["username"])).first()
        if not row:
            row = ChannelCandidate(username=info["username"])
            sess.add(row)
        row.title = info["title"][:200]
        row.found_via = source[:120]
        row.subscribers = info["subscribers"]
        row.posts_seen = info["posts"]
        row.fresh_7d = info["fresh7"]
        row.posts_with_contact = info["with_contact"]
        row.passed = info["ok"]
        row.reason = info["reason"][:200]
        row.checked_at = utcnow()


def _apply_to_registry(good: list) -> int:
    """Помечает каналы принятыми — сборщик берёт их со следующего прогона."""
    n = 0
    with session_scope() as sess:
        for info, _ in good:
            row = sess.scalars(select(ChannelCandidate).where(
                ChannelCandidate.username == info["username"])).first()
            if row and not row.enabled:
                row.enabled = True
                row.enabled_at = utcnow()
                n += 1
    return n


def enabled_channels() -> list:
    """Принятые автопоиском каналы — их подмешивает tgchannels.py."""
    with session_scope() as sess:
        return [r.username for r in sess.scalars(
            select(ChannelCandidate).where(ChannelCandidate.enabled.is_(True))).all()]


def main() -> int:
    ap = argparse.ArgumentParser(description="Автопоиск каналов с вакансиями")
    ap.add_argument("--apply", action="store_true",
                    help="включить найденные каналы в сбор")
    ap.add_argument("--limit", type=int, default=0, help="сколько проверить")
    ap.add_argument("--list", action="store_true", help="показать найденное")
    args = ap.parse_args()

    if args.list:
        with session_scope() as sess:
            rows = sess.scalars(select(ChannelCandidate)
                                .order_by(ChannelCandidate.passed.desc(),
                                          ChannelCandidate.fresh_7d.desc())).all()
            print("Кандидатов: %d" % len(rows))
            for r in rows:
                print("  %s @%-28s %6d подп.  %s%s"
                      % ("+" if r.passed else "-", r.username, r.subscribers,
                         (r.reason or "")[:52],
                         "  [в сборе]" if r.enabled else ""))
        return 0

    stats = asyncio.run(run(args.limit or None, apply=args.apply))
    if stats.get("error"):
        print("Ошибка:", stats["error"])
        return 1
    print("\nПроверено %d: годных %d, отсеяно %d%s"
          % (stats["checked"], stats["good"], stats["rejected"],
             ", включено в сбор %d" % stats["added"] if stats.get("added") else ""))
    if not stats.get("added") and stats["good"]:
        print("Включить найденное в сбор: python -m jobhunter.ingest.discover --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
