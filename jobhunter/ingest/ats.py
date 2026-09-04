"""Источник: публичные фиды ATS-систем (Greenhouse, Lever, Ashby, Workable...).

Лучший источник в системе. Компании сами публикуют вакансии открытым JSON,
чтобы встраивать их на свои сайты: без авторизации, без скрейпинга, без
Cloudflare и без серой зоны в правилах. Это первичные данные от работодателя,
а не перепечатка агрегатора.

Ограничение: поиска нет — эндпоинт отдаёт всё, что у компании открыто.
Поэтому ведём реестр компаний и опрашиваем их по расписанию.

    python -m jobhunter.ingest.ats --list          # реестр
    python -m jobhunter.ingest.ats --check         # канареечная проверка
    python -m jobhunter.ingest.ats                 # сбор
"""
from __future__ import annotations

import argparse
import html
import re
import sys
import time
from collections.abc import Iterator

import httpx

from ..models import ContactKind
from .base import RawJob, parse_ts

_TAG_RE = re.compile(r"<[^>]+>")


def _strip(fragment: str) -> str:
    """HTML → текст.

    Greenhouse отдаёт контент, где разметка ещё и HTML-экранирована (&lt;li&gt;).
    Поэтому раскодируем ПЕРЕД снятием тегов и повторяем цикл: иначе теги
    всплывают текстом уже после очистки и уезжают в письмо.
    """
    t = fragment or ""
    for _ in range(3):
        prev = t
        t = html.unescape(t)
        t = re.sub(r"<br\s*/?>|</p>|</li>|</div>|</h\d>", "\n", t, flags=re.I)
        t = _TAG_RE.sub("", t)
        if t == prev:
            break
    t = re.sub(r"[ \t]{2,}", " ", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


# ── реестр компаний ──────────────────────────────────────────────────────
# (провайдер, токен доски, человекочитаемое имя)
# Пополняется вручную: находишь компанию → смотришь её страницу вакансий →
# берёшь токен из URL. Ниже — стартовый набор remote-friendly компаний.
REGISTRY = [
    # проверено 2026-08-04: отвечают и отдают вакансии
    ("greenhouse", "gitlab", "GitLab"),
    ("greenhouse", "doximity", "Doximity"),
    ("greenhouse", "cloudflare", "Cloudflare"),
    ("greenhouse", "databricks", "Databricks"),
    ("greenhouse", "brex", "Brex"),
    ("greenhouse", "airtable", "Airtable"),
    ("greenhouse", "grafanalabs", "Grafana Labs"),
    ("lever", "veeva", "Veeva"),
    ("lever", "spotify", "Spotify"),
    ("lever", "ro", "Ro"),
    ("ashby", "ramp", "Ramp"),
    ("ashby", "linear", "Linear"),
    ("ashby", "posthog", "PostHog"),
    ("ashby", "deel", "Deel"),
]
# Workable отключён: у него другой формат субдомена на аккаунт, публичный
# фид по /api/accounts/{token} для проверенных компаний отдавал пусто.
# Вернуть, когда найдётся рабочий токен.

# Ключевые слова: без фильтра ATS-фид отдаёт все вакансии компании,
# включая продавцов и юристов.
RELEVANT = re.compile(
    r"(product\s*manager|product\s*owner|technical\s*product|"
    r"backend|back-end|python|platform\s*engineer|integration|"
    r"solutions?\s*engineer|tech\s*lead|engineering\s*manager|"
    r"software\s*engineer|ml\s*engineer|data\s*engineer)", re.I)

REMOTE = re.compile(r"(remote|anywhere|distributed|worldwide|удал[её]нн)", re.I)


class ATSSource:
    name = "ats"

    def __init__(self, registry=None, throttle: float = 1.5,
                 relevant_only: bool = True, remote_only: bool = False,
                 include_discovered: bool = True):
        # registry=[] — легитимный «пустой реестр» (его использует
        # ats_discover ради одних провайдеров), поэтому сравнение с None,
        # а не truthiness.
        self.registry = list(REGISTRY if registry is None else registry)
        if include_discovered and registry is None:
            # Доски, найденные в ссылках собранных вакансий и включённые
            # владельцем (ats_discover --apply). Сбой чтения БД не должен
            # ломать сбор по основному реестру.
            try:
                from .ats_discover import enabled_boards
                have = {(p, t) for p, t, _ in self.registry}
                self.registry += [b for b in enabled_boards()
                                  if (b[0], b[1]) not in have]
            except Exception:
                pass
        self.throttle = throttle
        self.relevant_only = relevant_only
        self.remote_only = remote_only
        self.http = httpx.Client(
            headers={"User-Agent": "jobhunter/0.1 (personal job search)",
                     "Accept": "application/json"},
            timeout=30.0, trust_env=False, follow_redirects=True)

    def _get(self, url: str):
        time.sleep(self.throttle)
        for attempt in range(3):
            try:
                r = self.http.get(url)
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 404:
                    return None
            except Exception:
                pass
            time.sleep(1.5 * (attempt + 1))
        return None

    # ── провайдеры ──
    def _greenhouse(self, token: str, company: str):
        data = self._get("https://boards-api.greenhouse.io/v1/boards/%s/jobs?content=true"
                         % token)
        for j in (data or {}).get("jobs", []) or []:
            yield {
                "id": str(j.get("id")),
                "title": j.get("title", ""),
                "location": (j.get("location") or {}).get("name", ""),
                "content": _strip(j.get("content", "")),
                "url": j.get("absolute_url", ""),
                "updated": j.get("updated_at", ""),
                "salary": "",
            }

    def _lever(self, token: str, company: str):
        data = self._get("https://api.lever.co/v0/postings/%s?mode=json" % token)
        for j in (data or []):
            cats = j.get("categories") or {}
            yield {
                "id": str(j.get("id")),
                "title": j.get("text", ""),
                "location": cats.get("location", "") or "",
                "content": _strip(j.get("descriptionPlain") or j.get("description") or ""),
                "url": j.get("hostedUrl", ""),
                "updated": str(j.get("createdAt", "")),
                "salary": cats.get("commitment", "") or "",
            }

    def _ashby(self, token: str, company: str):
        data = self._get("https://api.ashbyhq.com/posting-api/job-board/%s"
                         "?includeCompensation=true" % token)
        for j in (data or {}).get("jobs", []) or []:
            comp = j.get("compensation") or {}
            summary = comp.get("compensationTierSummary") or ""
            yield {
                "id": str(j.get("id")),
                "title": j.get("title", ""),
                "location": j.get("location", "") or "",
                "content": _strip(j.get("descriptionHtml") or j.get("descriptionPlain") or ""),
                "url": j.get("jobUrl", ""),
                "updated": j.get("publishedAt", ""),
                "salary": summary,
            }

    def _workable(self, token: str, company: str):
        data = self._get("https://www.workable.com/api/accounts/%s?details=true" % token)
        for j in (data or {}).get("jobs", []) or []:
            yield {
                "id": str(j.get("shortcode") or j.get("id")),
                "title": j.get("title", ""),
                "location": "%s %s" % (j.get("city") or "", j.get("country") or ""),
                "content": _strip(j.get("description", "")),
                "url": j.get("url", ""),
                "updated": j.get("published_on", ""),
                "salary": "",
            }

    def _fetch(self, provider: str, token: str, company: str):
        fn = {"greenhouse": self._greenhouse, "lever": self._lever,
              "ashby": self._ashby, "workable": self._workable}.get(provider)
        return list(fn(token, company)) if fn else []

    # ── канарейка ──
    def check(self) -> list:
        """Проверяет, что каждый провайдер ещё отвечает. Эндпоинты меняются молча."""
        out = []
        seen = set()
        for provider, token, company in self.registry:
            if provider in seen:
                continue
            seen.add(provider)
            try:
                rows = self._fetch(provider, token, company)
                out.append((provider, company, len(rows), "ok" if rows else "пусто"))
            except Exception as e:
                out.append((provider, company, 0, "ОШИБКА: %s" % str(e)[:50]))
        return out

    # ── основной поток ──
    def iter_jobs(self, limit: int | None = None) -> Iterator[RawJob]:
        produced = 0
        for provider, token, company in self.registry:
            try:
                rows = self._fetch(provider, token, company)
            except Exception:
                continue
            for j in rows:
                title = j["title"] or ""
                blob = "%s %s" % (title, j["location"])
                if self.relevant_only and not RELEVANT.search(title):
                    continue
                if self.remote_only and not REMOTE.search(blob + " " + j["content"][:600]):
                    continue
                # Email из текста ATS-вакансии — почти всегда служебный адрес
                # («по вопросам доступности пишите hr@…»), а не канал отклика.
                # У таких вакансий есть форма Apply, поэтому канал — ссылка,
                # а отклик подаёт человек. Автозаполнение форм не делаем:
                # отсеивающие вопросы там дают уверенно-неверные ответы.
                email = ""
                yield RawJob(
                    source="ats:%s" % provider,
                    external_uuid="ats:%s:%s:%s" % (provider, token, j["id"]),
                    title=title, company=company,
                    tag=_guess_tag(title),
                    content=j["content"], mode="full",
                    # Greenhouse/Ashby отдают ISO, Lever — миллисекунды эпохи,
                    # Workable — «2026-01-15». Разбор один на всех в base.parse_ts.
                    posted_at=parse_ts(j.get("updated")),
                    salary_raw=j.get("salary", "") or "",
                    contact_kind=(ContactKind.EMAIL.value if email
                                  else ContactKind.EXTERNAL_URL.value),
                    contact_email=email,
                    # contact_url — это КАНАЛ связи. Если есть почта, сюда идёт
                    # mailto:, иначе почтовик не увидит адрес и заявка зависнет.
                    # Ссылка на саму вакансию всегда остаётся в all_links.
                    contact_url=("mailto:" + email) if email else j["url"],
                    all_links=[{"key": "other_apply", "value": j["url"]}],
                    raw={"provider": provider, "board": token,
                         "location": j["location"]},
                )
                produced += 1
                if limit and produced >= limit:
                    return


def _guess_tag(title: str) -> str:
    t = (title or "").lower()
    pairs = [("Product Manager", r"product\s*manager|product\s*owner|head of product"),
             ("DS / ML", r"\bml\b|machine learning|data scien"),
             ("DevOps", r"devops|\bsre\b|platform|infrastructure"),
             ("Python", r"\bpython\b"),
             ("Backend", r"back-?end|server"),
             ("Frontend", r"front-?end|react"),
             ("Data", r"data engineer|analytics")]
    for name, pat in pairs:
        if re.search(pat, t):
            return name
    return "IT"


def main() -> int:
    ap = argparse.ArgumentParser(description="ATS-фиды компаний")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--all-roles", action="store_true",
                    help="без фильтра релевантности")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    src = ATSSource(relevant_only=not args.all_roles)

    if args.list:
        print("Реестр досок: %d" % len(src.registry))
        for p, t, c in src.registry:
            print("  %-12s %-16s %s" % (p, t, c))
        return 0

    if args.check:
        print("Канареечная проверка провайдеров:")
        for provider, company, n, status in src.check():
            print("  %-12s %-16s %4d  %s" % (provider, company, n, status))
        return 0

    from .base import save_jobs
    stats = save_jobs(src.iter_jobs(limit=args.limit))
    print("\nATS-фиды:")
    for k, v in stats.items():
        print("  %-14s %d" % (k, v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
