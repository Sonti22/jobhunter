"""Что бот говорит о кандидате от первого лица: тексты из profile.yaml → outreach.

Код один на всех, кандидат у каждого свой. Имя в теме письма, стаж, формат
работы, вступительные фразы писем и ответы про формат — это факты о человеке,
поэтому живут в профиле, рядом с остальными фактами, а не в коде. Гейт правды
проверяет эти тексты так же, как всё остальное: числа и технологии — только из
профиля.

Раздела нет или ключа нет — собирается нейтральный текст из identity и claims.
Ничего не выдумывается: без раздела письма просто скромнее.
"""
from __future__ import annotations

import re
from functools import lru_cache

from .profile import Profile, get_profile

_FORMATS_RU = {"remote": "удалённо", "hybrid": "гибрид", "office": "офис"}


def _section(p: Profile | None) -> dict:
    prof = p or get_profile()
    return prof.raw.get("outreach", {}) or {}


def _lang_key(key: str, lang: str) -> str:
    return "%s_%s" % (key, "en" if lang == "en" else "ru")


def _years(p: Profile) -> int:
    try:
        return int(float(p.claims.get("total_years_software", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _name(p: Profile, lang: str) -> str:
    i = p.identity
    if lang == "en":
        return str(i.get("full_name_en") or i.get("full_name_ru") or "").strip()
    # «Фамилия Имя Отчество» → «Имя Фамилия», как подписываются в письмах.
    parts = str(i.get("full_name_ru") or "").split()
    return " ".join(parts[1:2] + parts[:1]) if len(parts) >= 2 else " ".join(parts)


def english_level(p: Profile | None = None) -> str:
    prof = p or get_profile()
    for lang in prof.languages:
        if isinstance(lang, dict) and lang.get("code") == "en":
            return str(lang.get("level") or "").strip()
    return ""


def formats(p: Profile | None = None) -> list[str]:
    """Форматы работы без привязки к городу: remote / hybrid / office."""
    prof = p or get_profile()
    out = []
    for f in prof.identity.get("work_formats", []) or []:
        base = str(f).split("_", 1)[0].lower()
        if base in _FORMATS_RU and base not in out:
            out.append(base)
    return out or ["remote"]


def relocation(p: Profile | None = None) -> bool:
    return bool((p or get_profile()).identity.get("relocation", False))


def city(p: Profile | None = None) -> str:
    """Город кандидата в именительном падеже: «Москва»."""
    prof = p or get_profile()
    explicit = str(_section(prof).get("city_ru") or "").strip()
    if explicit:
        return explicit
    return str(prof.identity.get("location") or "").split(",")[0].strip()


def location(lang: str, p: Profile | None = None) -> str:
    """Строка локации для шапки резюме: «Москва, Россия» / «Moscow, Russia»."""
    prof = p or get_profile()
    val = str(_section(prof).get(_lang_key("location", lang)) or "").strip()
    return val or str(prof.identity.get("location") or "").strip()


def text(key: str, lang: str = "ru", p: Profile | None = None) -> str:
    """Одна фраза из outreach (format_line, letter_tail, …) или нейтральный фолбэк."""
    prof = p or get_profile()
    val = _section(prof).get(_lang_key(key, lang))
    if isinstance(val, str) and val.strip():
        return val.strip()
    return _fallback_text(key, lang, prof)


def pool(key: str, lang: str = "ru", p: Profile | None = None) -> list[str]:
    """Набор равноправных формулировок (intros, format_replies, …)."""
    prof = p or get_profile()
    val = _section(prof).get(_lang_key(key, lang))
    if isinstance(val, list):
        items = [str(v).strip() for v in val if str(v).strip()]
        if items:
            return items
    return _fallback_pool(key, lang, prof)


def subject(kind: str, lang: str, p: Profile | None = None, **fields: str) -> str:
    """Тема письма: kind = direct (человеку) или apply (в форму отклика)."""
    prof = p or get_profile()
    tpl = _section(prof).get(_lang_key("subject_" + kind, lang))
    if not (isinstance(tpl, str) and tpl.strip()):
        tpl = _fallback_subject(kind, lang, prof)
    return tpl.strip().format(**fields)


def cv_file_prefix(p: Profile | None = None) -> str:
    """Начало имени файла резюме: «Hakobyan_Python_ab12.pdf»."""
    prof = p or get_profile()
    explicit = str(_section(prof).get("cv_file_prefix") or "").strip()
    if not explicit:
        en = _name(prof, "en").split()
        explicit = en[-1] if en else "CV"
    return re.sub(r"[^A-Za-z0-9_-]+", "", explicit) or "CV"


@lru_cache(maxsize=8)
def _compile(patterns: tuple) -> re.Pattern | None:
    alts = [p for p in patterns if p]
    return re.compile("|".join("(?:%s)" % a for a in alts), re.I) if alts else None


def office_city_re(p: Profile | None = None) -> re.Pattern | None:
    """Где кандидату подходит офис или гибрид; None — нигде."""
    prof = p or get_profile()
    pats = _section(prof).get("office_city_patterns")
    if isinstance(pats, list) and pats:
        return _compile(tuple(str(x) for x in pats))
    c = city(prof)
    return _compile((re.escape(c[:-1] if len(c) > 4 else c) + r"\w*",)) if c else None


def never_say(p: Profile | None = None) -> list[tuple[re.Pattern, str]]:
    """Слова, которых владелец просил не говорить в автоответах, и почему."""
    out = []
    for item in _section(p).get("never_say", []) or []:
        if isinstance(item, dict) and item.get("pattern"):
            out.append((re.compile(str(item["pattern"]), re.I), str(item.get("why") or "")))
    return out


# ── нейтральные фолбэки: только то, что следует из identity и claims ──

def _format_phrase_ru(p: Profile) -> str:
    fm = [_FORMATS_RU[f] for f in formats(p)]
    phrase = ", ".join(fm[:-1]) + " или " + fm[-1] if len(fm) > 1 else fm[0]
    c = city(p)
    if c and any(f != "remote" for f in formats(p)):
        phrase += " (%s)" % c
    return phrase


def _fallback_text(key: str, lang: str, p: Profile) -> str:
    en = lang == "en"
    eng = english_level(p)
    if key == "format_line":
        if en:
            return "Remote work, please." + (" English: %s." % eng if eng else "")
        return "Формат — %s." % _format_phrase_ru(p) + (" Английский %s." % eng if eng else "")
    if key == "letter_tail":
        return "My CV is attached." if en else "Резюме во вложении."
    if key == "format_rule_llm":
        if en:
            return "remote work" + (", English %s" % eng if eng else "")
        return _format_phrase_ru(p) + ("" if relocation(p) else " (к переезду не готов)") \
            + (", английский %s" % eng if eng else "")
    return ""


def _fallback_pool(key: str, lang: str, p: Profile) -> list[str]:
    en = lang == "en"
    years = _years(p)
    if key in ("intros", "intros_short"):
        if years:
            return ["%d+ years in software development." % years] if en else \
                ["%d+ лет в разработке." % years]
        return ["Software engineer."] if en else ["Инженер-разработчик."]
    if key == "intros_middle":
        return ["Software engineer."] if en else ["Инженер-разработчик."]
    if key == "interest":
        return (["I'd like to work at {company}, so I'm writing to you directly."] if en else
                ["Мне интересна работа в {company}, поэтому пишу вам напрямую."])
    if key == "format_replies":
        return [text("format_line", lang, p)]
    return []


def _fallback_subject(kind: str, lang: str, p: Profile) -> str:
    name = _name(p, lang)
    if kind == "direct":
        return "{about} — %s" % name if name else "{about}"
    return ("Application: {role} — %s" if lang == "en" else "Отклик: {role} — %s") % name \
        if name else ("Application: {role}" if lang == "en" else "Отклик: {role}")
