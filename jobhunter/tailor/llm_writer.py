"""LLM пишет текст, гейт его проверяет, шаблон подстраховывает.

Порядок для каждого текста:
  1. собираем факты из profile.yaml (только релевантные вакансии)
  2. просим LLM написать
  3. прогоняем через анти-фабрикация гейт
  4. не прошло → вторая попытка с указанием на ошибку
  5. опять не прошло → берём шаблонный вариант

Так LLM не может протащить выдумку: гейт проверяет её вывод теми же
правилами, что и шаблонный. Максимум, что случится — откат к шаблону.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..llm import generate, prompt_message, prompt_summary
from ..profile import Profile, get_profile
from .gate import DocModel, check


@dataclass
class WriteResult:
    text: str
    source: str            # llm:<provider> | template
    gate_passed: bool
    attempts: int = 1
    failures: list = None
    # Самопроверка: прочитала ли модель свой текст глазами получателя и что
    # решила. review_ok=False означает «отправлять нельзя, зови владельца».
    review_ok: bool = True
    review_reason: str = ""
    review_done: bool = False


def facts_for(profile: Profile, score, limit: int = 12, lang: str = "ru") -> str:
    """Факты под вакансию: только релевантные буллеты + подтверждённые навыки.

    Для EN-письма факты подаются по-английски (text_en заполнены 49/49):
    модель, получившая русские факты, пишет английский текст с кириллическими
    вкраплениями — и письмо бракуется проверкой качества.
    """
    en = lang == "en"
    jd_skill_ids = set()
    for t, _, _ in score.matched_skills:
        sk = next((s for s in profile.skills if t in s.terms), None)
        if sk:
            jd_skill_ids.add(sk.id)

    lines = []
    for e in profile.experience:
        rel = [b for b in e.bullets if set(b.skills) & jd_skill_ids]
        if not rel:
            rel = e.bullets[:1]
        if en:
            head = "%s — %s (%s — %s)" % (e.company_en, e.role_en or e.role_ru,
                                          e.start, e.end or "present")
        else:
            head = "%s — %s (%s — %s)" % (e.company, e.role_ru, e.start,
                                          e.end or "н.в.")
        lines.append(head)
        for b in rel[:3]:
            lines.append("  • " + ((b.text_en if en else b.text_ru) or b.text_ru))
        if len(lines) > limit * 2:
            break

    skills = []
    for s in profile.skills:
        if s.level in ("expert", "working") and s.id in jd_skill_ids:
            skills.append("%s (%s, %d г.)" % (s.canonical, s.level, s.years))
    if skills:
        lines.append("Подтверждённые навыки под эту вакансию: " + ", ".join(skills[:14]))
    lines.append("Всего в разработке: %d лет (с %s)"
                 % (profile.claims.get("total_years_software", 7),
                    profile.raw["meta"]["timeline_start"]))
    return "\n".join(lines)


def allowed_terms_str(profile: Profile, limit: int = 40) -> str:
    names = sorted({s.canonical for s in profile.skills if s.level != "none"})
    return ", ".join(names[:limit])


def forbidden_str(profile: Profile) -> str:
    names = [n["canonical"] for n in profile.never]
    names += [s.canonical for s in profile.skills if s.level == "none"]
    return ", ".join(sorted(set(names)))


def _clean(text: str) -> str:
    """Убирает обёртки, которые модели любят добавлять."""
    t = (text or "").strip()
    t = re.sub(r"^```[a-z]*\s*|\s*```$", "", t)
    t = re.sub(r'^["«]\s*|\s*["»]$', "", t)
    t = re.sub(r"^(вот|here'?s|текст сообщения|сообщение)\s*[:—-]\s*", "", t, flags=re.I)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


# Иероглифы, арабица, иврит, деванагари. Модели бесплатных тиров иногда роняют
# в русский текст символ из другого письма («опыт работы с технологиями如
# Python»). Гейт это пропускает — факты не нарушены, — а рекрутёр видит явно
# машинный текст и закрывает диалог.
_ALIEN_SCRIPT = re.compile(
    "[֐-׿؀-ۿऀ-ॿ"
    "　-ヿ一-鿿가-힯]")

# «Работал с требованиями, как указано в требовании "Работа с требованиями"» —
# модель цитирует пункт и тут же пересказывает его теми же словами.
_QUOTED = re.compile(r"[«\"]([^«»\"]{8,90})[»\"]")

# Пустые конструкции, которые модель выдаёт вместо конкретики. Каждая взята
# из реально сгенерированных писем — это не гипотезы:
#   «Работал с "SQL" и подтверждаю, что это технология, с которой я знаком»
#   «Работал с требованиями, что соответствует одному из требований вакансии»
# Формально правда, читается как отписка робота. Такие письма лучше заменить
# шаблоном: он суше, но человеческий.
_VACUOUS = re.compile(
    r"подтверждаю,?\s+что\s+это|"
    r"что\s+соответствует\s+(?:одному\s+из\s+)?требовани|"
    r"работал\s+с\s+требованиями\b|"
    r"технолог\w+,?\s+с\s+котор\w+\s+я\s+знаком|"
    r"как\s+указано\s+в\s+требовани|"
    r"может\s+быть\s+полезно\s+для\s+этой\s+(?:вакансии|роли|позиции)", re.I)


def quality_problem(text: str) -> str:
    """Причина, по которой текст нельзя отправлять. Пустая строка — можно.

    Гейт проверяет правду, эта функция — читаемость. Одно без другого не
    работает: правдивое, но косноязычное письмо тратит контакт ровно так же,
    как выдуманное.
    """
    t = text or ""
    m = _ALIEN_SCRIPT.search(t)
    if m:
        return "чужое письмо в тексте: %r" % m.group(0)
    if t.count("(") != t.count(")") or t.count("«") != t.count("»"):
        return "непарные скобки или кавычки"
    m = _VACUOUS.search(t)
    if m:
        return "пустая формулировка: %r" % m.group(0)
    for q in _QUOTED.finditer(t):
        inner = q.group(1).lower()
        rest = (t[:q.start()] + t[q.end():]).lower()
        words = re.findall(r"[а-яёa-z]{5,}", inner)
        if words and sum(w in rest for w in words) >= max(2, len(words) - 1):
            return "цитата требования пересказана теми же словами"
    return ""


# Явное заявление про удалённый формат. Проверяем по тексту, а не доверием
# к модели: это жёсткое условие владельца, а не пожелание.
_REMOTE_CLAIM = re.compile(r"удал[ёе]нн?\w*|удал[ёе]нк\w*|\bremote\b", re.I)


def _states_remote(text: str) -> bool:
    return bool(_REMOTE_CLAIM.search(text or ""))


def write_message(role: str, jd_text: str, score, source: str,
                  fallback: str, max_chars: int = 400,
                  profile: Profile | None = None,
                  lang: str = "ru") -> WriteResult:
    """Сопроводительное письмо. Фолбэк — готовый шаблонный текст."""
    p = profile or get_profile()
    en = lang == "en"
    from ..config import get_settings
    if not get_settings().llm_enabled:
        return WriteResult(fallback, "template", True)

    # Цитату выбираем МЫ, а не модель. Дав ей весь текст вакансии, мы каждый
    # раз получали подтверждение произвольного пункта: «Работал с "опыт работы
    # в продуктовой компании, чей основной продукт — скрапер" в прошлых
    # проектах». _extract_requirement уже отсеивает строки про стаж, про тип
    # компании и всё, что не подтверждается профилем; модели остаётся только
    # вписать готовую цитату или обойтись без неё.
    from .message import _extract_requirement
    safe_quote = _extract_requirement(jd_text, score.matched_skills, p, role,
                                      lang=lang)
    prompt = prompt_message(role, jd_text, facts_for(p, score, lang=lang),
                            allowed_terms_str(p), forbidden_str(p),
                            source, max_chars, safe_quote=safe_quote,
                            lang=lang)
    hint = ""
    for attempt in (1, 2):
        res = generate(prompt + hint)
        if not res.ok or not res.text:
            break
        text = _clean(res.text)
        if len(text) > max_chars:
            hint = (("\n\nPREVIOUS ANSWER EXCEEDED %d characters. Shorten it."
                     if en else
                     "\n\nПРЕДЫДУЩИЙ ОТВЕТ БЫЛ ДЛИННЕЕ %d знаков. Сократи.")
                    % max_chars)
            continue
        # В английском письме не должно быть кириллицы: даже одно русское
        # слово выдаёт автомат и утекает из русских фактов промпта.
        if en and re.search(r"[а-яёА-ЯЁ]", text):
            hint = ("\n\nPREVIOUS ANSWER REJECTED: it contains Cyrillic "
                    "characters. Rewrite entirely in English.")
            continue
        bad = quality_problem(text)
        if bad:
            hint = (("\n\nPREVIOUS ANSWER REJECTED: %s. Rewrite from scratch, "
                     "in English only." % bad) if en else
                    ("\n\nПРЕДЫДУЩИЙ ОТВЕТ ОТКЛОНЁН: %s. Перепиши целиком, "
                     "по-русски, без вставок на других языках." % bad))
            continue
        # Условие владельца обязано быть в КАЖДОМ письме. Просьбы в промпте
        # для этого мало: модель регулярно ужимает текст под лимит знаков,
        # выбрасывая ровно то, что кажется ей необязательным. Не выполнила
        # после двух попыток — уходит шаблон, где строка стоит всегда.
        if not _states_remote(text):
            hint = (("\n\nPREVIOUS ANSWER REJECTED: it does not say the "
                     "candidate is looking for REMOTE-ONLY work. State it "
                     "explicitly.") if en else
                    ("\n\nПРЕДЫДУЩИЙ ОТВЕТ ОТКЛОНЁН: в нём не сказано, что "
                     "кандидат ищет ТОЛЬКО удалённую работу. Добавь это явно."))
            continue
        doc = DocModel(lang=lang, kind="message", free_text=text,
                       rendered_bullets=[])
        gate = check(doc, jd_text=jd_text, profile=p)
        if gate.passed:
            # Гейт сказал «не выдумано». Остаётся второй вопрос — читается ли
            # это вообще: текст бывает безупречно правдивым и при этом
            # бессвязным, а тратится на него единственная попытка у рекрутёра.
            from .review import review_with_retry

            def _gate_ok(candidate: str) -> bool:
                return check(DocModel(lang=lang, kind="message",
                                      free_text=candidate, rendered_bullets=[]),
                             jd_text=jd_text, profile=p).passed

            final, verdict = review_with_retry(
                text, role=role, jd_text=jd_text, gate_check=_gate_ok)
            return WriteResult(final, "llm:" + res.provider, True, attempt,
                               review_ok=verdict.ok or not verdict.checked,
                               review_reason=verdict.reason,
                               review_done=verdict.checked)
        bad = ", ".join("%s(%s)" % (f.rule_id, f.offending) for f in gate.hard[:4])
        hint = ("\n\nПРЕДЫДУЩИЙ ОТВЕТ ОТКЛОНЁН проверкой: %s. "
                "Убери это и перепиши." % bad)
    return WriteResult(fallback, "template", True, 2)


def write_summary(role: str, jd_text: str, score, fallback: str,
                  lang: str = "ru", profile: Profile | None = None) -> WriteResult:
    """Раздел «О себе» резюме."""
    p = profile or get_profile()
    from ..config import get_settings
    if not get_settings().llm_enabled:
        return WriteResult(fallback, "template", True)

    prompt = prompt_summary(role, jd_text, facts_for(p, score),
                            allowed_terms_str(p), forbidden_str(p), lang)
    hint = ""
    for attempt in (1, 2):
        res = generate(prompt + hint)
        if not res.ok or not res.text:
            break
        text = _clean(res.text)
        if not (200 <= len(text) <= 700):
            hint = "\n\nДлина должна быть 350-550 знаков. Перепиши."
            continue
        bad = quality_problem(text)
        if bad:
            hint = ("\n\nПРЕДЫДУЩИЙ ОТВЕТ ОТКЛОНЁН: %s. Перепиши целиком, "
                    "на одном языке." % bad)
            continue
        doc = DocModel(lang=lang, headline=role, summary=text, rendered_bullets=[])
        gate = check(doc, jd_text=jd_text, profile=p)
        if gate.passed:
            return WriteResult(text, "llm:" + res.provider, True, attempt)
        bad = ", ".join("%s(%s)" % (f.rule_id, f.offending) for f in gate.hard[:4])
        hint = ("\n\nПРЕДЫДУЩИЙ ОТВЕТ ОТКЛОНЁН проверкой: %s. "
                "Убери это и перепиши." % bad)
    return WriteResult(fallback, "template", True, 2)
