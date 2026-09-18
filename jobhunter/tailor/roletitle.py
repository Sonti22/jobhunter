"""Human job title for a letter's first line and an e-mail subject.

A Telegram post's first line is the author's caption, not a job title:
«lead #гибрид #нижнийновгород», «удаленно #DevOps», «120k #удаленка #офис».
Quoted into «По вакансии «удаленно #DevOps»» it reads as bulk mail — exactly
what recipients report and what earned the account its PeerFlood strikes.
One helper for subjects and letters, so they never disagree.
"""
from __future__ import annotations

import re

_PLACEHOLDER = re.compile(
    r"^(?:текст\s+вакансии|vacancy\s+text|job\s+description|description)"
    r"\s*:?[\s-]*$", re.I)
_HASHTAG = re.compile(r"#\S+")
_MONEY_ONLY = re.compile(r"^[\d\s.,]+\s*(?:k|к|тыс|руб|₽|\$|€)?$", re.I)
# A grade, a work format or a bare generic word is not a job title.
_NOT_A_TITLE = re.compile(
    r"^(?:senior|middle|junior|lead|team\s*lead|tech\s*lead|стажёр|интерн|"
    r"удал[её]нно|удал[её]нка|remote(?:ly)?|full[\s-]?time|part[\s-]?time|"
    r"гибрид|hybrid|офис|office|оффлайн|relocation|релокация|"
    r"вакансия|vacancy|job|hiring|"
    r"разработчик|developer|инженер|engineer|программист|programmer|специалист)"
    r"[\s+/,-]*$", re.I)
# A location or work-format line from an HN header is not a title either:
# «Regarding the “ON SITE TORONTO MUST BE ON SITE. REMOTE WILL BE IGNORED” role»
# and «Location: Florianopolis, Brazil» were about to be mailed on 16.09.
_MAX_TITLE_LEN = 60
_FORMAT_LINE = re.compile(
    r"^(?:location\s*:|remote\b|remote-first|fully\s+remote|on[\s-]?site\b|onsite\b|"
    r"hybrid\b|in[\s-]office\b|us-based|usa?\b|eu\b|worldwide|anywhere|https?://|www\.)",
    re.I)
_ROLE_WORD = re.compile(
    r"\b(?:engineers?|developers?|programmers?|architects?|scientists?|researchers?|"
    r"analysts?|administrators?|managers?|leads?|head|director|cto|founding|devops|"
    r"sre|swe|mlops|consultant|specialist|sysadmin|testers?|qa)\b|разработчик|инженер|"
    r"аналитик|архитектор|программист|тимлид|техлид|руководител|администратор|"
    r"тестировщик|менеджер|специалист|девопс", re.I)
# The second line of a Telegram post is the real title, wrapped in noise:
# «🚀 We’re hiring a Senior System Administrator | Cyprus»,
# «Middle DevOps Engineer ×2 - Emerging Travel Group».
_BODY_PREFIX = re.compile(
    r"^(?:we(?:'|’)?re\s+hiring|we\s+are\s+hiring|we(?:'|’)?re\s+looking\s+for|"
    r"we\s+are\s+looking\s+for|looking\s+for|hiring|мы\s+ищем|ищем|требуется|нужен|нужна|"
    r"в\s+поиске|вакансия|позиция|должность|position|role|job\s+title)"
    r"\s*[:\-–—]?\s*(?:an?\s+|the\s+)?", re.I)
_BODY_TAIL = re.compile(r"\s+[|—–-]\s+|\s+в\s+(?:гк|компани\w+|ооо|ао|зао)\b|\s+(?:at|@)\s+", re.I)


def clean_title(raw: str) -> str:
    """Title without hashtags and noise, or "" when no job title is left."""
    role = _HASHTAG.sub(" ", raw or "")
    role = re.sub(r"[\s,;·|/–—-]+$", "", re.sub(r"\s{2,}", " ", role)).strip()
    # «Hiring principal and distinguished engineers to build…» is a sentence,
    # not a title: quoted in a subject line it reads as careless bulk mail.
    role = re.sub(r"^(?:we(?:'re|\s+are)\s+)?hiring\s+(?:for\s+)?", "", role, flags=re.I)
    if len(role) < 3 or len(role) > _MAX_TITLE_LEN or _MONEY_ONLY.match(role) \
            or _NOT_A_TITLE.match(role):
        return ""
    if _FORMAT_LINE.match(role) and not _ROLE_WORD.search(role):
        return ""
    # A caption, not a title: «limassol #cyprus #fintech #sysadmin» produced the
    # letter «“limassol” (your post in @cyithr)» — it sat in the Telegram queue
    # on 18.09. Without a role word, a hashtag caption or a single word
    # («воронеж», «астана») is not a job title.
    if not _ROLE_WORD.search(role) and ("#" in (raw or "") or len(role.split()) == 1):
        return ""
    return role


def _decap(s: str) -> str:
    """«РУКОВОДИТЕЛЬ ИТ-ОТДЕЛА» → «Руководитель ИТ-отдела»: all caps shouts in a letter."""
    words = []
    for w in s.split():
        parts = [x if 1 < len(x) <= 3 and x.isalpha() else x.lower() for x in w.split("-")]
        words.append("-".join(parts))
    out = " ".join(words)
    return out[:1].upper() + out[1:]


def role_from_body(description: str) -> str:
    """Job title from the first lines of a post whose headline is a hashtag caption."""
    lines = [ln.strip() for ln in (description or "").splitlines() if ln.strip()][:5]
    for ln in lines:
        cand = re.sub(r"^[^\w«\"(]+", "", _HASHTAG.sub(" ", ln)).strip()
        cand = _BODY_PREFIX.sub("", cand, count=1).strip()
        cand = _BODY_TAIL.split(cand, maxsplit=1)[0].strip()
        cand = re.sub(r"\s*[×xх]\s*\d+$", "", cand).strip()              # «Engineer ×2»
        if len(cand) > _MAX_TITLE_LEN:                                   # «… (Python Backend + AI-агенты)»
            cand = re.sub(r"\s*\([^)]*\)\s*$", "", cand).strip()
        if not cand or len(cand.split()) > 8 or re.search(r"[.!?:]\s|[?:]$", cand):
            continue
        if cand.isupper():
            cand = _decap(cand)
        role = clean_title(cand)
        if role and _ROLE_WORD.search(role):
            return role
    return ""


def display_role(title: str, tag: str, description: str, lang: str) -> str:
    """Best human-readable role: cleaned title, then tag, then a guess from text."""
    en = lang == "en"
    generic = {
        "python": "Backend Engineer" if en else "Backend-разработчик",
        "backend": "Backend Engineer" if en else "Backend-разработчик",
        "devops": "DevOps Engineer" if en else "DevOps-инженер",
        "product": "Product Manager" if en else "Продакт-менеджер",
        "ml": "ML Engineer" if en else "ML-инженер",
        "ds / ml": "ML Engineer" if en else "ML-инженер",
        "data": "Data Engineer" if en else "Data-инженер",
        "fullstack": "Full-Stack Engineer" if en else "Fullstack-разработчик",
    }
    sources = [title or ""]
    if not clean_title(title or "") and _HASHTAG.sub(" ", title or "").strip().lower() not in generic:
        # The channel tag lies more often than the post's second line: a «Python
        # Backend + AI-агенты» vacancy from @program_job was called «DevOps-инженер».
        sources.append(role_from_body(description))
    sources.append(tag or "")
    for raw in sources:
        role = raw.strip()
        # Some sources keep the prefix literally: «Текст вакансии: Python Backend».
        if re.match(r"^текст\s+вакансии\s*:", role, re.I):
            role = role.split(":", 1)[1].strip()
        if not role or _PLACEHOLDER.match(role):
            continue
        if re.match(r"^(?:https?://|www\.)", role, re.I):
            continue
        low = _HASHTAG.sub(" ", role).strip().lower()
        if low in generic:
            return generic[low]
        role = clean_title(role)
        if not role:
            continue
        if role.lower() in generic:
            return generic[role.lower()]
        return role

    blob = " ".join([title or "", tag or "", description or ""])
    if re.search(r"product\s*(?:manager|owner)|продакт|продуктов\w*\s+менедж", blob, re.I):
        return "Product Manager" if en else "Продакт-менеджер"
    if re.search(r"devops|\bsre\b|kubernetes|terraform", blob, re.I):
        return "DevOps Engineer" if en else "DevOps-инженер"
    if re.search(r"data\s*engineer|etl|airflow|\bdwh\b|аналитик", blob, re.I):
        return "Data Engineer" if en else "Data-инженер"
    if re.search(r"python|backend|back-end|fastapi|django|разработчик|инженер", blob, re.I):
        return "Backend Engineer" if en else "Backend-разработчик"
    return "Software Engineer" if en else "Разработчик"
