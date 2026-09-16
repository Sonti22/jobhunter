"""Источник: Reddit hiring-посты через официальный Data API.

Анонимный доступ Reddit закрыл: JSON и RSS отвечают 403/429, robots.txt
запрещает обход, и замер 16.09 дал ноль по пяти сабреддитам из шести.
Поэтому только зарегистрированное приложение: владелец создаёт
«script»-приложение на reddit.com/prefs/apps и кладёт REDDIT_CLIENT_ID и
REDDIT_CLIENT_SECRET в .env. Без них источник молча пропускается, остальные
источники не страдают.

Токен — client_credentials, без пароля пользователя. Лимит API — 100
запросов в минуту; нам нужно по одному на сабреддит раз в день.

Берём только посты работодателей: метка или флейр «Hiring», не «[For Hire]»,
не пост соискателя по postkind, и обязательно с email в тексте. Посты
«DM me» в ручную очередь не идут — там и так тысячи ссылок.

    python -m jobhunter.ingest.reddit --check
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from collections.abc import Iterator

import httpx

from ..config import get_settings
from ..models import ContactKind
from .base import RawJob, extract_email
from .postkind import is_seeker_post

log = logging.getLogger("reddit")

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API = "https://oauth.reddit.com"
# Reddit требует уникальный описательный User-Agent: <платформа>:<приложение>:<версия>.
UA = "windows:jobhunter:0.1 (personal job search)"

_HIRING = re.compile(r"\[\s*hiring\s*\]", re.I)
_FOR_HIRE = re.compile(r"\[\s*for\s+hire\s*\]", re.I)


class RedditHiringSource:
    name = "reddit"
    throttle = 3.0

    def __init__(self, http: httpx.Client | None = None):
        s = get_settings()
        self.client_id = s.reddit_client_id
        self.client_secret = s.reddit_client_secret
        self.subreddits = [x.strip() for x in s.reddit_subreddits.split(",") if x.strip()]
        self.http = http or httpx.Client(timeout=30.0, trust_env=False,
                                         headers={"User-Agent": UA})

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _token(self) -> str:
        r = self.http.post(TOKEN_URL, auth=(self.client_id, self.client_secret),
                           data={"grant_type": "client_credentials"})
        if r.status_code != 200:
            raise RuntimeError("Reddit: токен не выдан, HTTP %d" % r.status_code)
        token = (r.json() or {}).get("access_token", "")
        if not token:
            raise RuntimeError("Reddit: пустой токен — проверь REDDIT_CLIENT_ID/SECRET")
        return token

    def _listing(self, token: str, sub: str) -> list:
        """Посты сабреддита за месяц со словом hiring. 429 — один повтор, потом пропуск."""
        params = {"q": "hiring", "restrict_sr": "1", "sort": "new", "t": "month",
                  "limit": "100", "raw_json": "1"}
        for attempt in range(2):
            r = self.http.get(API + "/r/%s/search" % sub, params=params,
                              headers={"Authorization": "bearer " + token})
            if r.status_code == 200:
                data = (r.json() or {}).get("data") or {}
                return [c.get("data") or {} for c in data.get("children") or []]
            if r.status_code == 429 and attempt == 0:
                try:
                    wait = int(r.headers.get("retry-after") or 60)
                except ValueError:
                    wait = 60
                time.sleep(min(120, max(5, wait)))
                continue
            log.warning("reddit r/%s: HTTP %d — пропуск", sub, r.status_code)
            return []
        return []

    def iter_jobs(self, limit: int | None = None) -> Iterator[RawJob]:
        if not self.enabled:
            log.info("reddit пропущен: нет REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET")
            return
        token = self._token()
        made = 0
        for i, sub in enumerate(self.subreddits):
            if i:
                time.sleep(self.throttle)
            for post in self._listing(token, sub):
                job = job_from_post(post, sub)
                if job is None:
                    continue
                yield job
                made += 1
                if limit and made >= limit:
                    return

    def stats(self) -> list:
        """Для --check: по сабреддитам сколько постов, hiring, с email, взято."""
        out = []
        token = self._token()
        for i, sub in enumerate(self.subreddits):
            if i:
                time.sleep(self.throttle)
            posts = self._listing(token, sub)
            hiring = [p for p in posts if _is_hiring(p)]
            with_email = [p for p in hiring
                          if extract_email(p.get("selftext") or "") or extract_email(p.get("title") or "")]
            taken = [p for p in hiring if job_from_post(p, sub) is not None]
            out.append((sub, len(posts), len(hiring), len(with_email), len(taken)))
        return out


def _is_hiring(post: dict) -> bool:
    title = post.get("title") or ""
    flair = (post.get("link_flair_text") or "").strip().lower()
    if _FOR_HIRE.search(title) or flair.startswith("for hire"):
        return False
    return bool(_HIRING.search(title) or flair.startswith("hiring"))


def job_from_post(post: dict, sub: str) -> RawJob | None:
    """RawJob из поста или None, если это не почтовая вакансия под профиль."""
    from .boards import _relevant
    from .jobapis import _tag_of

    title = (post.get("title") or "").strip()
    body = (post.get("selftext") or "").strip()
    if not _is_hiring(post):
        return None
    if post.get("removed_by_category") or body in ("[removed]", "[deleted]"):
        return None
    if is_seeker_post(title + "\n" + body):
        return None
    email = extract_email(body) or extract_email(title)
    if not email:
        return None
    if not _relevant(title, body):
        return None
    clean = _HIRING.sub("", title).strip(" -–—:|")
    url = "https://www.reddit.com" + (post.get("permalink") or "")
    return RawJob(
        source="reddit", external_uuid="reddit:%s" % post.get("id"),
        title=clean[:200], company="", tag=_tag_of(clean),
        content=(title + "\n\n" + body)[:12000], mode="full",
        posted_at=int(post.get("created_utc") or 0),
        contact_kind=ContactKind.EMAIL.value, contact_email=email,
        all_links=[{"key": "other_apply", "value": url}],
        raw={"subreddit": sub, "author": post.get("author") or "",
             "flair": post.get("link_flair_text") or ""},
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Reddit hiring-посты")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    src = RedditHiringSource()
    if not src.enabled:
        print("Нет REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET в .env.")
        print("Создай приложение типа «script»: https://www.reddit.com/prefs/apps")
        return 2
    if args.check:
        print("%-22s %6s %7s %7s %6s" % ("сабреддит", "постов", "hiring", "email", "взято"))
        for sub, n, h, e, t in src.stats():
            print("%-22s %6d %7d %7d %6d" % ("r/" + sub, n, h, e, t))
        return 0
    from .base import save_jobs
    print(save_jobs(src.iter_jobs()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
