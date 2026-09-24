"""Черновик ответа рекрутёру на нешаблонное сообщение.

Разделение обязанностей то же, что и в письмах: LLM отвечает за живой язык,
гейт — за правду, владелец — за отправку. Черновик НИКОГДА не уходит сам:
он попадает в карточку в «Избранном», и рекрутёр увидит его только после
команды владельца (см. owner.py).

Почему так, а не «отвечай сам на всё»: техвопрос, зарплата и оффер — это
места, где цена ошибки равна потере вакансии, а бесплатные модели там охотно
сочиняют опыт. Рутину (прислать резюме, предложить время) отвечает автомат
по шаблонам без всякой модели — см. reply.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..llm import generate
from ..profile import Profile, get_profile
from ..tailor.gate import DocModel, check
from ..tailor.llm_writer import _clean, allowed_terms_str, forbidden_str, quality_problem

MAX_DRAFT_CHARS = 700


@dataclass
class Draft:
    text: str = ""
    source: str = ""             # llm:<provider> | ""
    gate_passed: bool = False
    problem: str = ""
    failures: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.text) and self.gate_passed and not self.problem


def facts_block(profile: Profile, jd_text: str = "", limit: int = 10) -> str:
    """Факты о кандидате под разговор.

    В отличие от письма, здесь нет объекта скоринга: отбираем буллеты по
    пересечению терминов с текстом вакансии, а если пересечения нет — берём
    свежий опыт. Ничего, кроме профиля, в блок не попадает.
    """
    jd = (jd_text or "").lower()
    lines = []
    for e in profile.experience:
        rel = [b for b in e.bullets
               if any(t in jd for s in profile.skills if s.id in b.skills
                      for t in s.terms)]
        if not rel:
            rel = e.bullets[:2]
        lines.append("%s — %s (%s — %s)"
                     % (e.company, e.role_ru, e.start, e.end or "н.в."))
        for b in rel[:3]:
            lines.append("  • " + b.text_ru)
        if len(lines) >= limit * 2:
            break

    skills = ["%s (%s, %d г.)" % (s.canonical, s.level, s.years)
              for s in profile.skills if s.level in ("expert", "working")]
    if skills:
        lines.append("Навыки: " + ", ".join(skills[:16]))
    lines.append("Всего в разработке: %d лет (с %s)"
                 % (profile.claims.get("total_years_software", 0),
                    profile.raw["meta"]["timeline_start"]))
    # Выверенные владельцем ответы на типовые вопросы — такие же факты, как
    # опыт: модель пересказывает их, а не сочиняет свои.
    if profile.faq:
        lines.append("Готовые ответы владельца на типовые вопросы:")
        for item in profile.faq:
            if isinstance(item, dict) and item.get("answer_ru"):
                lines.append("  [%s] %s" % (item.get("id", "?"),
                                            str(item["answer_ru"]).strip()))
    if profile.salary_expectation:
        lines.append("Зарплатное ожидание (задано владельцем): %s"
                     % profile.salary_expectation)
    return "\n".join(lines)


def _history_block(history: list, limit: int = 6) -> str:
    """Последние реплики диалога: [(direction, body), ...]."""
    out = []
    for direction, body in history[-limit:]:
        who = "Я" if direction == "out" else "Рекрутёр"
        out.append("%s: %s" % (who, (body or "").strip().replace("\n", " ")[:300]))
    return "\n".join(out)


_INTENT_HINT = {
    "tech_question": "Это технический вопрос. Отвечай конкретно и по делу, "
                     "опираясь только на ФАКТЫ. Если опыта в чём-то нет — "
                     "скажи это прямо одной фразой и переведи на смежный "
                     "опыт из ФАКТОВ. Не обещай того, чего не делал.",
    # Заменяется на _MONEY_HINT_WITH_FIGURE, когда ожидание задано в профиле.
    "money": "Вопрос про деньги. Не называй сумму: её решает владелец. "
             "Ответ должен вежливо вернуть вопрос — уточнить вилку и объём "
             "задач, а конкретную цифру оставить на созвон.",
    "offer": "Это оффер. Поблагодари, уточни детали (формат, оформление, "
             "старт), но НИЧЕГО не подтверждай и не принимай.",
    "unknown": "Ответь коротко и по существу на то, что спросили.",
    "slot_proposed": "Рекрутёр предлагает время. Подтверди готовность и "
                     "переспроси формат созвона. Конкретное время не меняй.",
    "task_request": "Работодатель зовёт на следующий этап и просит материалы "
                    "или задание. Поблагодари, подтверди интерес и напиши, что "
                    "пришлёшь материалы. Ссылки, репозитории, сроки и проекты "
                    "не выдумывай — их добавит владелец.",
    "apply_link": "Просят подать отклик через сайт. Коротко поблагодари и "
                  "напиши, что подашь заявку по ссылке. Больше ничего не обещай.",
}


_MONEY_HINT_WITH_FIGURE = (
    "Вопрос про деньги. В ФАКТАХ задано зарплатное ожидание владельца — "
    "назови его ДОСЛОВНО, без торга и вилок от себя, и уточни, что открыт "
    "к обсуждению по итогам разговора о задачах.")


def prompt_reply(role: str, jd_excerpt: str, history: str, incoming: str,
                 facts: str, allowed: str, forbidden: str, intent: str) -> str:
    hint = _INTENT_HINT.get(intent, _INTENT_HINT["unknown"])
    # Черновик может назвать цифру, только когда владелец сам вписал её в
    # профиль. Отправка всё равно за владельцем: money — всегда эскалация.
    if intent == "money" and "Зарплатное ожидание" in (facts or ""):
        hint = _MONEY_HINT_WITH_FIGURE
    return f"""Ты пишешь ответ инженера рекрутёру в переписке по вакансии.

ВАКАНСИЯ: {role}
ФРАГМЕНТ ОПИСАНИЯ:
{(jd_excerpt or "")[:900]}

ПЕРЕПИСКА ДО ЭТОГО:
{history or "(это первый ответ)"}

СООБЩЕНИЕ РЕКРУТЁРА, НА КОТОРОЕ ОТВЕЧАЕМ:
{(incoming or "")[:900]}

ФАКТЫ О КАНДИДАТЕ (использовать можно ТОЛЬКО это):
{facts}

РАЗРЕШЁННЫЕ ТЕХНОЛОГИИ: {allowed}
ЗАПРЕЩЁННЫЕ (не упоминать как свой опыт ни при каких условиях): {forbidden}

ЗАДАЧА: {hint}

ЖЁСТКИЕ ПРАВИЛА:
1. Не выдумывай опыт, компании, проекты, цифры и годы. Всё — из ФАКТОВ.
2. Если чего-то в ФАКТАХ нет — так и напиши, что этого опыта нет.
3. Не называй зарплатные ожидания, не соглашайся на условия, не подтверждай
   выход на работу — это решает владелец, а не ты.
4. Не придумывай доступное время и даты, если их нет в переписке.
5. Без эмодзи, ссылок, списков и форматирования. Обычный текст.
6. Длина 200-{MAX_DRAFT_CHARS} знаков, деловой тон, первое лицо.
7. Отвечай НА ТОМ ЖЕ ЯЗЫКЕ, на котором написано сообщение рекрутёра:
   английское сообщение — английский ответ, без единого русского слова.

Верни ТОЛЬКО текст ответа."""


_ROUTINE_TASK = {
    "ask_cv": "Рекрутёр просит резюме. Скажи, что прикладываешь его, одной-"
              "двумя фразами, можно с готовностью ответить на вопросы. "
              "Файл прикрепит система — не пиши ссылок и не обещай «вышлю "
              "позже».",
    "ask_call": "Рекрутёр предлагает созвониться. Согласись и предложи "
                "время СТРОГО этой строкой, вставь её ДОСЛОВНО, не меняя "
                "ни цифр, ни порядка: «{slots}». Другого времени не называй.",
    "ack": "Рекрутёр вежливо подтвердил получение. Ответь одной короткой "
           "фразой без вопроса — поблагодари и оставь дверь открытой.",
    "about": "Рекрутёр просит рассказать о себе. Перескажи 2-3 фразами "
             "готовый ответ владельца из ФАКТОВ, не добавляя ничего сверх.",
    # Смелый режим (bold_autonomy) шлёт сюда техвопросы, план помечает их
    # «черновиком по фактам», движок считает auto_tech_replies_count — а
    # задачи не было, и каждый такой вопрос уходил владельцу карточкой
    # (24.09: вопросы GDL IT пролежали месяц). Текст уходит сам, поэтому
    # рамки жёсткие; гейт правды и самопроверка стоят после.
    "tech_question": "Рекрутёр задаёт вопросы об опыте, проектах, формате "
                     "работы или сроках выхода. Ответь на каждый по порядку, "
                     "коротко и по делу, только по ФАКТАМ и готовым ответам "
                     "владельца. Чего нет в ФАКТАХ — не утверждай, а скажи, "
                     "что обсудишь на созвоне. Не называй цифр, которых нет "
                     "в ФАКТАХ, и не обещай того, чего не делал.",
}


# Позиции владельца, которые гейт правды не видит (он сверяет технологии и
# цифры, а не формат работы). 24.09 черновик ответил на «готовы к офису?»
# «переезд в офис обсуждаем» — самопроверка это пропустила.
_ASKS_FORMAT = re.compile(r"офис|office|удал[её]н|remote|гибрид|hybrid|переезд|релокац|relocat",
                          re.I)
_STATES_REMOTE_ONLY = re.compile(r"только\s+удал[её]нн?\w*|remote[- ]only|only\s+remote|"
                                 r"remotely\s+only|work\s+remotely\s+only", re.I)
_FINTECH = re.compile(r"финтех|fintech", re.I)


def _tech_stance_problem(incoming: str, text: str) -> str:
    """Почему автоответ на техвопрос нельзя отправлять без владельца; пусто — можно."""
    if _FINTECH.search(text or ""):
        return "в ответе «финтех» — владелец велел говорить о платёжных интеграциях"
    if _ASKS_FORMAT.search(incoming or "") and not _STATES_REMOTE_ONLY.search(text or ""):
        return "спросили про офис или формат, а в ответе нет «только удалённо»"
    return ""


def draft_routine_reply(intent: str, role: str, jd_text: str, incoming: str,
                        history: list, slots_line: str = "",
                        profile: Profile | None = None) -> Draft:
    """Живой автоответ на рутину вместо random.choice из трёх строк.

    Отличия от draft_reply, и оба принципиальны:
      - текст УХОДИТ сам, без владельца, поэтому проверок больше, а попытка
        одна: не вышло с первого раза — вызывающий откатится на шаблон,
        который выдумать ничего не может;
      - время предлагает КОД (слоты посчитаны с учётом занятости), модель
        обязана вставить строку дословно. Проверка на вызывающем: нет
        строки в тексте — текст не годится.
    """
    from ..config import get_settings
    p = profile or get_profile()
    if not get_settings().llm_enabled:
        return Draft(problem="LLM выключена")
    task = _ROUTINE_TASK.get(intent)
    if not task:
        return Draft(problem="не рутинный интент: %s" % intent)
    if intent == "ask_call":
        if not slots_line:
            return Draft(problem="нет строки слотов")
        task = task.format(slots=slots_line)

    prompt = prompt_reply(role, jd_text, _history_block(history), incoming,
                          facts_block(p, jd_text), allowed_terms_str(p),
                          forbidden_str(p), intent)
    # Подменяем задачу: prompt_reply не знает рутинных интентов.
    prompt = prompt.replace(_INTENT_HINT.get(intent, _INTENT_HINT["unknown"]),
                            task) if intent in _INTENT_HINT else \
        prompt.replace(_INTENT_HINT["unknown"], task)

    res = generate(prompt)
    if not res.ok or not res.text:
        return Draft(problem=res.error or "модель не ответила")
    text = _clean(res.text)
    if len(text) > MAX_DRAFT_CHARS:
        return Draft(problem="слишком длинно")
    # Модель видит входящее с PII-плейсхолдерами и может процитировать их:
    # «созвонимся по <PHONE>» в реальном сообщении рекрутёру — брак. Текст
    # уходит без владельца, поэтому отказ, а не тихая чистка.
    if "<PHONE>" in text or "<EMAIL>" in text:
        return Draft(problem="в ответе PII-плейсхолдер — нельзя отправлять")
    if slots_line and slots_line not in text:
        return Draft(problem="модель переписала время — нельзя")
    bad = quality_problem(text)
    if bad:
        return Draft(problem=bad)
    if intent == "tech_question":
        stance = _tech_stance_problem(incoming, text)
        if stance:
            return Draft(problem=stance)
    _lang = "en" if not __import__("re").search(r"[а-яёА-ЯЁ]", text or "") else "ru"
    gate = check(DocModel(lang=_lang, kind="message", free_text=text),
                 jd_text=jd_text, profile=p)
    if not gate.passed:
        return Draft(problem="гейт: " + ", ".join(
            f.rule_id for f in gate.hard[:4]))
    return Draft(text, "llm:" + res.provider, True)


def draft_reply(role: str, jd_text: str, incoming: str, history: list,
                intent: str = "unknown", profile: Profile | None = None) -> Draft:
    """Черновик для владельца. Пустой текст — значит писать придётся руками."""
    from ..config import get_settings
    p = profile or get_profile()
    if not get_settings().llm_enabled:
        return Draft(problem="LLM выключена (LLM_ENABLED=false)")

    prompt = prompt_reply(role, jd_text, _history_block(history), incoming,
                          facts_block(p, jd_text), allowed_terms_str(p),
                          forbidden_str(p), intent)
    hint = ""
    last_problem = "модель не ответила"
    for _attempt in (1, 2):
        res = generate(prompt + hint)
        if not res.ok or not res.text:
            last_problem = res.error or "модель не ответила"
            break
        text = _clean(res.text)
        if len(text) > MAX_DRAFT_CHARS:
            hint = ("\n\nПРЕДЫДУЩИЙ ОТВЕТ ДЛИННЕЕ %d знаков. Сократи."
                    % MAX_DRAFT_CHARS)
            last_problem = "слишком длинно"
            continue
        bad = quality_problem(text)
        if bad:
            hint = "\n\nПРЕДЫДУЩИЙ ОТВЕТ ОТКЛОНЁН: %s. Перепиши целиком." % bad
            last_problem = bad
            continue
        _lang = "en" if not __import__("re").search(r"[а-яёА-ЯЁ]", text or "") else "ru"
        gate = check(DocModel(lang=_lang, kind="message", free_text=text),
                     jd_text=jd_text, profile=p)
        if gate.passed:
            return Draft(text, "llm:" + res.provider, True, "",
                         [f.rule_id for f in gate.failures])
        bad = ", ".join("%s(%s)" % (f.rule_id, f.offending) for f in gate.hard[:4])
        hint = ("\n\nПРЕДЫДУЩИЙ ОТВЕТ ОТКЛОНЁН проверкой фактов: %s. "
                "Убери это и перепиши." % bad)
        last_problem = "гейт: " + bad
    return Draft(problem=last_problem)
