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


def clean_title(raw: str) -> str:
    """Title without hashtags and noise, or "" when no job title is left."""
    role = _HASHTAG.sub(" ", raw or "")
    role = re.sub(r"[\s,;·|/–—-]+$", "", re.sub(r"\s{2,}", " ", role)).strip()
    if len(role) < 3 or _MONEY_ONLY.match(role) or _NOT_A_TITLE.match(role):
        return ""
    return role


def display_role(title: str, tag: str, description: str, lang: str) -> str:
    """Best human-readable role: cleaned title, then tag, then a guess from text."""
    en = lang == "en"
    generic = {
        "python": "Backend Engineer" if en else "Backend-разработчик",
        "backend": "Backend Engineer" if en else "Backend-разработчик",
        "devops": "DevOps Engineer" if en else "DevOps-инженер",
        "product": "Product Manager" if en else "Продакт-менеджер",
        "ml": "ML Engineer" if en else "ML-инженер",
        "data": "Data Engineer" if en else "Data-инженер",
    }
    for raw in (title or "", tag or ""):
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
