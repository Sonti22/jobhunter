"""Письмо без вакансии: руководителю компании или тому, кто может зареферить.

Отличия от отклика на вакансию:
  - просим не работу, а подсказку — «к кому обратиться по <роль>»: на такой
    вопрос руководителю легко ответить одной строкой или переслать письмо;
  - всегда сказано, почему пишем именно этому человеку (открытая вакансия
    компании или его собственное публичное «we're hiring»);
  - последняя строка — отказ одним словом: ответ «no» закрывает контакт.

Факты — только из профиля: вступления и строка стека берутся из
tailor/message.py, где они уже проходят гейт правды, и результат всё равно
проверяется гейтом целиком. Сочинять о себе здесь нечем и незачем.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass

from ..profile import Profile, get_profile
from .gate import DocModel, GateResult, check
from .llm_writer import quality_problem
from .message import INTROS, INTROS_EN, _stack_line

EXEC, REFERRAL = "exec", "referral"
_RU_TLD = (".ru", ".by", ".kz", ".su", ".xn--p1ai")
_CYR = re.compile(r"[а-яё]", re.I)

GREET_EN = ["Hi{name},", "Hello{name},"]
GREET_RU = ["Здравствуйте{name}!", "Добрый день{name}!"]

# {role}/{company} подставляются; варианты нужны, чтобы письма разным людям
# не были копией друг друга — шаблонную рассылку видно и людям, и фильтрам.
WHY_ROLE_EN = [
    # Без «I'm applying»: бот не знает, подал ли владелец отклик на самом деле.
    "I saw your {role} opening at {company} and wanted to reach a person, not only a form.",
    "I saw that {company} is hiring for {role} and decided to write to you directly.",
    "{company} has an open {role} role that matches what I do, so I'm writing to you directly.",
]
WHY_COMPANY_EN = [
    "I'm interested in backend and platform engineering work at {company}, so I'm writing to you directly.",
    "I'd like to work on backend or platform engineering at {company} and wanted to reach a person directly.",
]
WHY_REFERRAL_EN = [
    "I saw on your {where} that your team{at_company} is hiring, so I'm writing to you directly.",
    "Your {where} says your team{at_company} is hiring — that's why I'm writing.",
]
ASK_EXEC_EN = [
    "Could you point me to the right person on your team to talk to?",
    "Who on your team would be the right person to talk to about this?",
    "If it's easier, feel free to forward this to whoever handles engineering hiring.",
]
ASK_REFERRAL_EN = [
    "Would you be open to referring me, or telling me how referrals work on your team?",
    "Would you be open to a referral, or could you tell me the best way to apply?",
]
TAIL_EN = "My CV is attached. I work remotely only — any country. English: C1."
OPTOUT_EN = "If this isn't relevant, just reply “no” and I won't write again."

WHY_ROLE_RU = [
    "Увидел вашу вакансию «{role}» в {company} и хотел написать человеку, а не только в форму.",
    "Увидел, что {company} ищет «{role}», и решил написать вам напрямую.",
    "В {company} открыта роль «{role}», она совпадает с тем, чем я занимаюсь, — поэтому пишу напрямую.",
]
WHY_COMPANY_RU = [
    "Мне интересна бэкенд- и платформенная разработка в {company}, поэтому пишу вам напрямую.",
    "Хотел бы заниматься бэкендом или платформой в {company} и решил написать напрямую.",
]
WHY_REFERRAL_RU = [
    "Увидел в вашем {where}, что ваша команда{at_company} нанимает, поэтому пишу напрямую.",
    "В вашем {where} сказано, что команда{at_company} ищет людей — поэтому и пишу.",
]
ASK_EXEC_RU = [
    "Подскажите, пожалуйста, к кому в команде лучше обратиться?",
    "С кем в вашей команде правильно поговорить об этом?",
    "Если удобнее — перешлите, пожалуйста, письмо тому, кто занимается наймом разработчиков.",
]
ASK_REFERRAL_RU = [
    "Готовы ли вы порекомендовать меня — или подсказать, как у вас устроены рекомендации?",
    "Возможна ли рекомендация, или подскажите, как лучше откликнуться?",
]
TAIL_RU = "Резюме во вложении. Работаю только удалённо, страна не важна. Английский C1."
OPTOUT_RU = "Если письмо не по адресу — ответьте «нет», больше не напишу."


@dataclass
class DirectLetter:
    text: str
    lang: str
    gate: GateResult
    problem: str = ""

    @property
    def ok(self) -> bool:
        return self.gate.passed and not self.problem


def lang_for(email: str, *texts: str) -> str:
    """RU — русскоязычным доменам и текстам, остальным EN."""
    domain = (email or "").rsplit("@", 1)[-1].lower()
    if domain.endswith(_RU_TLD):
        return "ru"
    blob = " ".join(t or "" for t in texts)
    return "ru" if len(_CYR.findall(blob)) > max(20, len(blob) // 8) else "en"


def _first_name(person: str) -> str:
    name = (person or "").strip().split()[0] if (person or "").strip() else ""
    # Логин или «Team» вместо имени в приветствии хуже, чем приветствие без имени.
    return name if re.fullmatch(r"[A-ZА-ЯЁ][a-zа-яё]{1,20}", name) else ""


def compose(kind: str, *, company: str, role: str = "", person: str = "", jd_text: str = "",
            lang: str = "en", seed: str = "", profile: Profile | None = None,
            where: str = "profile") -> DirectLetter:
    """Собрать письмо и проверить его гейтом правды и читаемостью."""
    from ..match.scorer import score_job

    p = profile or get_profile()
    rng = random.Random(seed or company + role)
    en = lang == "en"
    name = _first_name(person)
    sep = " " if en else ", "                            # «Hi Anna,» но «Добрый день, Анна!»
    greet = rng.choice(GREET_EN if en else GREET_RU).format(name=(sep + name) if name else "")
    company = (company or "").strip()
    fmt = {"role": (role or "").strip(), "company": company or ("your company" if en else "вашей компании"),
           "at_company": ((" at " if en else " в ") + company) if company else "",
           # где человек сам написал, что нанимает: профиль GitHub или пост в канале
           "where": (where if en else {"profile": "профиле", "post": "посте"}.get(where, "профиле"))}
    if kind == REFERRAL:
        why = rng.choice(WHY_REFERRAL_EN if en else WHY_REFERRAL_RU)
        ask = rng.choice(ASK_REFERRAL_EN if en else ASK_REFERRAL_RU)
    else:
        pool = (WHY_ROLE_EN if en else WHY_ROLE_RU) if fmt["role"] else \
            (WHY_COMPANY_EN if en else WHY_COMPANY_RU)
        why = rng.choice(pool)
        ask = rng.choice(ASK_EXEC_EN if en else ASK_EXEC_RU)
    intro = rng.choice(INTROS_EN if en else INTROS)
    stack = ""
    if jd_text:
        stack = _stack_line(p, score_job(role, "", jd_text, p).matched_skills, lang)
    body = " ".join(x for x in (why.format(**fmt), intro, stack) if x)
    text = "\n\n".join([greet, body, ask, TAIL_EN if en else TAIL_RU, OPTOUT_EN if en else OPTOUT_RU])
    gate = check(DocModel(lang=lang, kind="message", free_text=text, rendered_bullets=[]),
                 jd_text=jd_text or "", profile=p)
    return DirectLetter(text=text, lang=lang, gate=gate, problem=quality_problem(text))
