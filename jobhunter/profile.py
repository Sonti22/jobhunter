"""Загрузка profile.yaml и индексы для гейта/подгонки."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml

from .config import get_settings


def _months(start: str, end) -> int:
    sy, sm = (int(x) for x in start.split("-"))
    if end:
        ey, em = (int(x) for x in end.split("-"))
    else:
        t = date.today()
        ey, em = t.year, t.month
    return (ey - sy) * 12 + (em - sm)


@dataclass
class Bullet:
    id: str
    text_ru: str
    text_en: str
    skills: list
    metrics: list
    exp_id: str


@dataclass
class Experience:
    id: str
    company: str
    company_en: str
    role_ru: str
    role_en: str
    start: str
    end: object
    bullets: list = field(default_factory=list)
    is_own_project: bool = False


@dataclass
class Skill:
    id: str
    canonical: str
    aliases: list
    level: str
    years: int
    evidence_ids: list

    @property
    def terms(self) -> set:
        return {self.canonical.lower()} | {a.lower() for a in self.aliases}


class Profile:
    def __init__(self, raw: dict):
        self.raw = raw
        self.identity = raw["identity"]
        self.languages = raw.get("languages", [])
        self.education = raw.get("education", [])
        self.claims = raw.get("claims", {})
        # Готовые ответы владельца на типовые вопросы рекрутёров и зарплатное
        # ожидание. Оба раздела опциональны: пусто = поведение как без них.
        self.faq = raw.get("faq", []) or []
        self.salary_expectation = str(raw.get("salary_expectation", "") or "").strip()

        self.skills = [Skill(s["id"], s["canonical"], s.get("aliases", []),
                             s["level"], s.get("years", 0), s.get("evidence_ids", []))
                       for s in raw["skills"]]
        self.skill_by_id = {s.id: s for s in self.skills}

        self.experience = []
        self.bullet_by_id = {}
        for e in raw["experience"]:
            exp = Experience(
                id=e["id"], company=e["company"], company_en=e.get("company_en", e["company"]),
                role_ru=e.get("role_ru", ""), role_en=e.get("role_en", ""),
                start=e["start"], end=e.get("end"),
                is_own_project=e.get("is_own_project", False),
            )
            for b in e.get("bullets", []):
                bl = Bullet(b["id"], b.get("text_ru", ""), b.get("text_en", ""),
                            b.get("skills", []), b.get("metrics", []), e["id"])
                exp.bullets.append(bl)
                self.bullet_by_id[bl.id] = bl
            self.experience.append(exp)

        self.never = raw.get("never_claim", [])

        # ── производные множества терминов ──
        self.allowed_terms = set()      # можно упоминать
        self.forbidden_terms = set()    # хард-фейл
        self.familiar_terms = set()     # нельзя в заголовок/со превосходной степенью
        for s in self.skills:
            if s.level == "none":
                self.forbidden_terms |= s.terms
            else:
                self.allowed_terms |= s.terms
                if s.level == "familiar":
                    self.familiar_terms |= s.terms
        for n in self.never:
            self.forbidden_terms.add(n["canonical"].lower())
            self.forbidden_terms |= {a.lower() for a in n.get("aliases", [])}
        # forbidden побеждает allowed при коллизии
        self.allowed_terms -= self.forbidden_terms

        # названия компаний (для проверки сущностей)
        self.company_terms = set()
        for e in self.experience:
            for c in (e.company, e.company_en):
                if c:
                    self.company_terms.add(c.lower())

    # ── допустимые числа ──
    def allowed_numbers(self) -> set:
        """Все числа, которые резюме имеет право называть."""
        from .textutil import bare_numbers
        nums = set()
        for e in self.experience:
            for d in (e.start, e.end):
                if d:
                    nums |= set(str(int(x)) for x in d.split("-"))
            for b in e.bullets:
                for m in b.metrics:
                    nums |= bare_numbers(str(m.get("value", "")))
        for s in self.skills:
            nums.add(str(s.years))
        for v in self.claims.values():
            nums |= bare_numbers(str(v))
        # Числа зарплатного ожидания разрешены явно: владелец сам вписал их
        # в профиль, и гейт не должен резать цифру, которую тот санкционировал.
        # Побочный эффект осознан: число становится разрешённым во всех
        # текстах, но оно одно и происходит от владельца.
        if self.salary_expectation:
            nums |= bare_numbers(self.salary_expectation)
        # годы стажа и календарные годы
        nums.add(str(int(self.claims.get("total_years_software", 0))))
        for e in self.education:
            nums.add(str(e.get("year", "")))
        nums.discard("")
        return nums

    def faq_answer(self, faq_id: str, lang: str = "ru") -> str:
        """Выверенный владельцем ответ на типовой вопрос, или пусто.

        Для EN — answer_en с фолбэком на answer_ru: лучше русский ответ,
        чем молчание, а машинного перевода фактов здесь не бывает.
        """
        for item in self.faq:
            if isinstance(item, dict) and item.get("id") == faq_id:
                if lang == "en":
                    en = str(item.get("answer_en", "") or "").strip()
                    if en:
                        return en
                return str(item.get("answer_ru", "") or "").strip()
        return ""

    def skill_years(self, skill_id: str) -> int:
        return self.skill_by_id[skill_id].years if skill_id in self.skill_by_id else 0

    def total_years(self) -> int:
        start = self.raw["meta"]["timeline_start"]
        return round(_months(start, None) / 12)


@lru_cache
def get_profile() -> Profile:
    path = Path(get_settings().profile_path)
    return Profile(yaml.safe_load(path.read_text(encoding="utf-8")))
