"""Ответы на поля формы — только из фактов, без единой догадки.

Правило то же, что у анти-фабрикация гейта в резюме и письмах: нет
источника — нет ответа. Поле уходит в «нужен владелец», и это лучше, чем
уверенно неверный ответ: скрининг закрывает компанию навсегда, второго
захода не будет.

Банк ответов (таблица ApplyAnswer) — способ спросить владельца один раз.
Вопросы в формах повторяются дословно от компании к компании: «требуется
ли спонсорство визы», «страна проживания», «работали ли вы у нас раньше».
Ответив однажды, владелец закрывает их для всех будущих откликов.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select

from ..db import session_scope
from ..models import ApplyAnswer, utcnow
from ..profile import Profile, get_profile
from ..textutil import norm_hash


@dataclass
class Answer:
    field: str
    label: str
    value: str
    source: str          # profile | bank | derived
    confidence: float = 1.0


# Поля, которые заполняются прямо из identity профиля.
_IDENTITY_RULES = (
    (re.compile(r"^first\s*name|^имя", re.I), "first_name"),
    (re.compile(r"^last\s*name|фамили", re.I), "last_name"),
    (re.compile(r"full\s*name|^name$|полное\s+имя", re.I), "full_name"),
    (re.compile(r"e-?mail|почта", re.I), "email"),
    (re.compile(r"phone|телефон", re.I), "phone"),
    (re.compile(r"linkedin", re.I), "linkedin"),
    (re.compile(r"github", re.I), "github"),
    (re.compile(r"preferred\s+name|как\s+к\s+вам", re.I), "first_name"),
)

# Вопросы про деньги — всегда владельцу: в профиле salary_expectation пуст,
# и это его осознанное решение, а не пробел в данных.
_MONEY_RE = re.compile(
    r"salary|compensation|rate|зарплат|вилк|оклад|expected\s+pay", re.I)

# Вопросы про формат работы: ответ известен жёстко и одинаков везде.
_REMOTE_RE = re.compile(
    r"remote|relocat|on-?site|hybrid|willing\s+to\s+work|office|"
    r"удал[её]нн|релокац|офис", re.I)


def _identity_value(profile: Profile, key: str) -> str:
    ident = profile.raw.get("identity", {}) or {}
    if key == "first_name":
        return (ident.get("full_name_en") or "").split()[0] if \
            ident.get("full_name_en") else ""
    if key == "last_name":
        parts = (ident.get("full_name_en") or "").split()
        return parts[-1] if len(parts) > 1 else ""
    if key == "full_name":
        return ident.get("full_name_en", "")
    return str(ident.get(key, "") or "")


def _from_bank(label: str) -> str:
    """Ответ, который владелец уже давал на этот же вопрос."""
    key = norm_hash(label)
    with session_scope() as sess:
        row = sess.scalars(select(ApplyAnswer).where(
            ApplyAnswer.question_key == key)).first()
        if row is None:
            return ""
        row.used_count = (row.used_count or 0) + 1
        return row.answer_value or ""


def _match_choice(field, wanted: str) -> str:
    """Подбор варианта из списка. Возвращает ТЕКСТ варианта, не его id.

    Заявку подаёт человек и выбирает пункт в выпадающем списке глазами:
    ему нужен «No», а не внутренний идентификатор «0», под которым этот
    вариант живёт в API Greenhouse.
    """
    if not field.values:
        return wanted
    low = wanted.lower()
    for _value, label in field.values:
        if low and low in (label or "").lower():
            return label
    return ""


def answer_for(field, profile: Profile, job=None) -> Answer | None:
    """Ответ на одно поле. None — значит нужен владелец."""
    label = field.label or ""

    # Файлы подставляет packet (резюме и письмо), не эта функция.
    if field.is_file:
        return None

    for rx, key in _IDENTITY_RULES:
        if rx.search(label):
            val = _identity_value(profile, key)
            if val:
                return Answer(field.name, label, val, "profile")
            return None

    if _MONEY_RE.search(label):
        return None                       # деньги — всегда владельцу

    # Формат работы: ответ один и тот же во всех формах и подтверждён
    # профилем — кандидат рассматривает только удалённый формат.
    if _REMOTE_RE.search(label) and field.is_choice:
        for want in ("no", "нет", "remote", "yes"):
            picked = _match_choice(field, want)
            if picked and want in ("no", "нет") and \
                    re.search(r"relocat|on-?site|office|офис|релокац", label, re.I):
                return Answer(field.name, label, picked, "derived", 0.9)
            if picked and want == "remote":
                return Answer(field.name, label, picked, "derived", 0.9)

    # «Have you previously worked at / consulted for X» — не догадка, а
    # проверяемый факт: места работы владельца перечислены в профиле.
    m = re.search(r"(?:worked\s+at|consulted\s+for|employed\s+by)\s+"
                  r"([A-Za-z][\w .&-]{1,40})", label, re.I)
    if m and field.is_choice:
        company = m.group(1).strip(" ?.").lower()
        known = {(e.company or "").lower() for e in profile.experience}
        known |= {(e.company_en or "").lower() for e in profile.experience}
        if company and not any(company in k or k in company
                               for k in known if k):
            picked = _match_choice(field, "no")
            if picked:
                return Answer(field.name, label, picked, "derived", 0.95)

    saved = _from_bank(label)
    if saved:
        # Для селекта сохранённый текст надо ещё сопоставить с вариантами
        # этой конкретной формы: списки у компаний разные.
        if field.is_choice:
            picked = _match_choice(field, saved)
            return Answer(field.name, label, picked, "bank") if picked else None
        return Answer(field.name, label, saved, "bank")
    return None


def answer_all(spec, profile: Profile | None = None, job=None) -> tuple:
    """(ответы, поля без ответа). Второе — то, что спросим у владельца."""
    p = profile or get_profile()
    answers, unresolved = [], []
    for f in spec.fields:
        if f.is_file:
            continue                      # резюме и письмо кладёт packet
        a = answer_for(f, p, job)
        if a is not None and a.value:
            answers.append(a)
        elif f.required:
            unresolved.append(f)
    return answers, unresolved


def remember(label: str, value: str, provider: str = "",
             field_type: str = "") -> None:
    """Запомнить ответ владельца — чтобы больше не спрашивать."""
    key = norm_hash(label)
    with session_scope() as sess:
        row = sess.scalars(select(ApplyAnswer).where(
            ApplyAnswer.question_key == key)).first()
        if row is None:
            row = ApplyAnswer(question_key=key, label=label[:400],
                              provider=provider, field_type=field_type)
            sess.add(row)
        row.answer_value = value[:800]
        row.created_at = row.created_at or utcnow()
