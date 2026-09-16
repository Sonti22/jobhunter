"""Источник: Hacker News «Ask HN: Who is hiring?».

Официальный публичный API Algolia, без ключа и без логина:
  тред месяца: /api/v1/search_by_date?tags=story,author_whoishiring
  комментарии: /api/v1/items/{id}  → children[]

Каждый топ-левел комментарий = одна компания. Контакт обычно email прямо
в тексте, включая обфускацию «name [at] company [dot] com».
Англоязычные remote-вакансии, зарплаты выше рынка СНГ.
"""
from __future__ import annotations

import html
import re
import time
from collections.abc import Iterator

import httpx

from ..models import ContactKind
from .base import RawJob, extract_email

API = "https://hn.algolia.com/api/v1"
_TAG_RE = re.compile(r"<[^>]+>")
_P_RE = re.compile(r"</?p>", re.I)

# Первая строка HN-поста почти всегда: "Company | Role | Location | REMOTE | $range"
_REMOTE_RE = re.compile(r"\bremote\b", re.I)
_SALARY_RE = re.compile(r"[$€£]\s?\d[\d,.]*\s*(?:k|K)?(?:\s*[-–—]\s*[$€£]?\s?\d[\d,.]*\s*(?:k|K)?)?")


def _strip(fragment: str) -> str:
    t = _P_RE.sub("\n", fragment or "")
    t = _TAG_RE.sub("", t)
    t = html.unescape(t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _guess_tag(text: str) -> str:
    t = (text or "").lower()
    pairs = [
        ("Product Manager", r"product manager|head of product|\bpm\b|product owner"),
        ("DS / ML", r"\bml\b|machine learning|\bai\b|data scien|llm"),
        ("DevOps", r"devops|\bsre\b|infrastructure|platform engineer|kubernetes"),
        ("Python", r"\bpython\b|django|fastapi"),
        ("Backend", r"back-?end|backend|server-side|distributed systems"),
        ("Frontend", r"front-?end|react|typescript|vue\b"),
        ("Fullstack", r"full-?stack"),
        ("QA Auto", r"\bqa\b|test engineer|sdet"),
    ]
    for name, pat in pairs:
        if re.search(pat, t):
            return name
    return "IT"


# Слово, по которому сегмент заголовка — это должность.
_ROLE_WORD = re.compile(
    r"\b(?:engineers?|developers?|programmers?|architects?|scientists?|researchers?|"
    r"analysts?|administrators?|designers?|managers?|leads?|head|director|vp|cto|"
    r"founding|devops|sre|swe|mlops|trainer|builder|consultant|specialist|intern|"
    r"roles|positions|openings)\b", re.I)
# Сегменты, которые точно не должность: локация, формат, занятость, ссылка, деньги.
_NOISE_SEG = re.compile(
    r"^(?:https?://|www\.)|\.(?:com|io|ai|dev|co|org|net|app)/?$|"
    r"\b(?:remote|onsite|on-site|hybrid|in-office|full[- ]?time|part[- ]?time|contract|"
    r"visa|relocation|usd|eur|gbp|equity|hiring)\b|[$€£]\s?\d|\d+\s?k\b|"
    r"^[A-Z][\w .'-]+,\s*[A-Z][\w .'-]+$", re.I)


def _company_role(first_line: str) -> tuple:
    """HN-конвенция: 'Company | Role | Location | REMOTE | salary'.

    Порядок сегментов соблюдают не все: вторым бывает локация («NY, USA»),
    ссылка или «REMOTE ALMOST ANYWHERE», и такой «заголовок» скорер честно
    не узнавал как роль — треть HN-вакансий отсеивалась с «роль не
    распознана». Роль ищем по смыслу, а не по позиции.
    """
    line = first_line or ""
    parts = [p.strip() for p in re.split(r"\s*\|\s*", line) if p.strip()]
    if len(parts) < 2:
        parts = [p.strip() for p in re.split(r"\s+[—–-]\s+", line) if p.strip()]
    if not parts:
        return "", ""
    company, rest = parts[0][:120], parts[1:]
    for seg in rest:
        if _ROLE_WORD.search(seg):
            return company, seg[:180]
    if _ROLE_WORD.search(parts[0]):
        # «Software Engineer — Remote (US Only)»: компании в строке нет.
        return "", parts[0][:180]
    for seg in rest:
        if not _NOISE_SEG.search(seg) and len(seg) > 3:
            return company, seg[:180]
    return company, ""


class HackerNewsSource:
    name = "hn"

    def __init__(self, throttle: float = 1.0):
        self.throttle = throttle
        self.http = httpx.Client(
            headers={"User-Agent": "jobhunter/0.1 (personal job search)"},
            timeout=30.0, trust_env=False)

    def _get(self, path: str) -> dict:
        time.sleep(self.throttle)
        for attempt in range(3):
            try:
                r = self.http.get(API + path)
                if r.status_code == 200:
                    return r.json()
            except Exception:
                pass
            time.sleep(2.0 * (attempt + 1))
        return {}

    def latest_thread_ids(self, count: int = 2) -> list:
        """ID последних тредов «Who is hiring» (свежий сверху)."""
        data = self._get("/search_by_date?tags=story,author_whoishiring&hitsPerPage=12")
        ids = []
        for hit in data.get("hits", []):
            title = (hit.get("title") or "").lower()
            if "who is hiring" in title and "freelancer" not in title:
                ids.append(hit.get("objectID"))
            if len(ids) >= count:
                break
        return ids

    def iter_jobs(self, limit: int | None = None, threads: int = 1,
                  remote_only: bool = True) -> Iterator[RawJob]:
        produced = 0
        for thread_id in self.latest_thread_ids(threads):
            item = self._get("/items/%s" % thread_id)
            for child in (item.get("children") or []):
                text = _strip(child.get("text") or "")
                if len(text) < 80:
                    continue
                if remote_only and not _REMOTE_RE.search(text):
                    continue
                email = extract_email(text)
                lines = [l for l in text.splitlines() if l.strip()]
                first = lines[0] if lines else ""
                company, role = _company_role(first)
                sal = _SALARY_RE.search(text)
                yield RawJob(
                    source="hn",
                    external_uuid="hn:%s" % child.get("id"),
                    title=role, company=company,
                    tag=_guess_tag(text), content=text, mode="full",
                    posted_at=int(child.get("created_at_i") or 0),
                    salary_raw=sal.group(0) if sal else "",
                    contact_kind=(ContactKind.EMAIL.value if email
                                  else ContactKind.EXTERNAL_URL.value),
                    contact_email=email,
                    all_links=[{"key": "mail", "value": email}] if email else [],
                    raw={"thread": thread_id, "author": child.get("author")},
                )
                produced += 1
                if limit and produced >= limit:
                    return
