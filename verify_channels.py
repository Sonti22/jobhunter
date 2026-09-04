# -*- coding: utf-8 -*-
"""Проверка списка Telegram-каналов перед добавлением в источники.

Агенты-исследователи возвращают юзернеймы, часть из которых не существует,
закрыта, переименована или оказывается не про вакансии. Гонять ingest по
такому списку — значит впустую тратить запросы и засорять базу.

Скрипт по каждому каналу открывает публичное превью t.me/s/<username> и
считает измеримое:
  posts        сколько постов на первой странице
  fresh7       сколько из них моложе недели  → канал живой
  contacts     в скольких есть @handle или email → есть кому писать
  py           в скольких упоминается Python/Django/FastAPI → наш стек

Годным считается канал, где есть свежие посты И хоть какие-то контакты.

    python verify_channels.py channels.json        # список из JSON
    python verify_channels.py a b c                # или прямо аргументами

Результат — channels_verified.json, готовый к подстановке в CHANNELS.
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "channels_verified.json"

_TIME_RE = re.compile(r'<time datetime="([^"]+)"')
_MSG_RE = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
                     re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_HANDLE_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]{4,31})")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PY_RE = re.compile(r"python|django|fastapi|бэкенд|backend|питон", re.I)

# Юзернеймы самих каналов-агрегаторов: встречаются в каждом посте как
# кросс-промо и контактом работодателя не являются.
def _own_handles(username: str) -> set:
    return {username.lower()}


def check(client: httpx.Client, username: str) -> dict:
    row = {"username": username, "ok": False, "reason": "",
           "posts": 0, "fresh7": 0, "contacts": 0, "py": 0}
    try:
        r = client.get("https://t.me/s/%s" % username)
    except Exception as e:
        row["reason"] = "сеть: %s" % type(e).__name__
        return row
    if r.status_code != 200:
        row["reason"] = "HTTP %d" % r.status_code
        return row
    html_text = r.text
    if "tgme_widget_message" not in html_text:
        # приватный канал, группа или страница-заглушка
        row["reason"] = "нет публичной ленты (приват/группа/не существует)"
        return row

    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    times = _TIME_RE.findall(html_text)
    bodies = [_TAG_RE.sub(" ", b) for b in _MSG_RE.findall(html_text)]
    row["posts"] = len(bodies)
    for t in times:
        try:
            dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt >= week_ago:
            row["fresh7"] += 1
    own = _own_handles(username)
    for b in bodies:
        handles = {h.lower() for h in _HANDLE_RE.findall(b)} - own
        if handles or _EMAIL_RE.search(b):
            row["contacts"] += 1
        if _PY_RE.search(b):
            row["py"] += 1

    if row["posts"] == 0:
        row["reason"] = "лента пуста"
    elif row["fresh7"] == 0:
        row["reason"] = "нет постов за неделю — канал мёртв"
    elif row["contacts"] == 0:
        row["reason"] = "ни одного контакта в постах"
    else:
        row["ok"] = True
    return row


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    if len(args) == 1 and args[0].endswith(".json"):
        data = json.loads(Path(args[0]).read_text(encoding="utf-8"))
        items = data.get("channels", data) if isinstance(data, dict) else data
        names = [(c["username"] if isinstance(c, dict) else str(c)) for c in items]
    else:
        names = args
    names = [n.replace("@", "").strip().lower() for n in names]
    names = list(dict.fromkeys(n for n in names if n))

    client = httpx.Client(
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/126.0 Safari/537.36",
                 "Accept": "text/html,application/xhtml+xml"},
        timeout=25.0, trust_env=False, follow_redirects=True)

    rows = []
    for i, n in enumerate(names, 1):
        row = check(client, n)
        rows.append(row)
        mark = "OK " if row["ok"] else "-- "
        print("%s%3d/%d  %-28s posts=%-3d fresh7=%-3d contacts=%-3d py=%-3d %s"
              % (mark, i, len(names), n, row["posts"], row["fresh7"],
                 row["contacts"], row["py"], row["reason"]))
        time.sleep(1.0)                     # вежливый темп к t.me

    good = [r for r in rows if r["ok"]]
    good.sort(key=lambda r: (-r["py"], -r["contacts"]))
    OUT.write_text(json.dumps(good, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\nживых каналов: %d из %d  -> %s" % (len(good), len(rows), OUT.name))
    print("с Python-постами: %d" % sum(1 for r in good if r["py"] > 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
