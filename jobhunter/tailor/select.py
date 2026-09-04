"""Подгонка резюме под вакансию = переранжирование и переакцентирование.

НЕ выдумка: каждый буллет резюме — дословный текст из profile.yaml (провенанс
1.0). Подгонка = выбрать самые релевантные вакансии буллеты, отсортировать,
выбрать заголовок из заранее одобренного списка, собрать саммари из шаблона на
разрешённых терминах, выбрать язык. Результат прогоняется через гейт.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..match.role import RoleMatch, classify
from ..match.scorer import Score, score_job
from ..profile import Profile, get_profile
from . import presets
from .gate import DocModel, GateResult, _find_terms, check

MAX_BULLETS = 9
MIN_PER_EXP = 1

# Английские названия для навыков с русским canonical (для EN-резюме).
EN_SKILL = {
    "Микросервисы": "Microservices",
    "Профилирование": "Profiling",
    "Классическое машинное обучение": "Classical ML",
    "Менторинг": "Mentoring",
    "Техническая документация": "Technical documentation",
    "Алгоритмы и структуры данных": "Algorithms & data structures",
    "LLM-инференс": "LLM inference",
}


def _disp(name: str, lang: str) -> str:
    return EN_SKILL.get(name, name) if lang == "en" else name


@dataclass
class TailorResult:
    doc: DocModel
    render: dict
    gate: GateResult
    score: Score
    lang: str
    role: RoleMatch | None = None
    preset: presets.RolePreset | None = None

    @property
    def ok(self) -> bool:
        return self.gate.passed

    @property
    def cv_slug(self) -> str:
        """Кусок имени PDF. Берётся из роли резюме, а не из тега вакансии."""
        return self.preset.cv_slug if self.preset else "CV"


def _pick_lang(jd_text: str, title: str = "") -> str:
    """Язык вакансии: если латиницы заметно больше кириллицы — EN.

    Кириллический ЗАГОЛОВОК перебивает статистику по телу: телеграм-посты
    сплошь смешанные («вакансия #Remote» + английское описание стека), и
    подсчёт по телу выбирал en — рекрутёру русскоязычного канала уходило
    английское письмо с вклеенным русским заголовком. Язык публикации
    надёжнее всего виден по тому, как автор назвал вакансию.
    """
    if re.search(r"[А-Яа-я]", title or ""):
        return "ru"
    cyr = len(re.findall(r"[А-Яа-я]", jd_text or ""))
    lat = len(re.findall(r"[A-Za-z]", jd_text or ""))
    return "en" if lat > cyr * 1.3 else "ru"


def _pick_headline(p: Profile, preset, middle: bool = False) -> str:
    """Заголовок = заголовок пресета. На Middle — нейтральный вариант.

    Выбор роли делает classify() в match/role.py; здесь только перевод
    семейства в строку заголовка и правка под грейд.
    """
    variants = p.identity["headline_variants"]
    if middle:
        # На Middle-позиции «Senior / Tech Lead / Architect» в шапке читается
        # как переквалификация — режем до нейтрального инженерного варианта.
        plain = [v for v in variants
                 if not re.search(r"senior|lead|architect|manager", v, re.I)]
        if plain:
            return plain[0]
        return re.sub(r"^Senior\s+", "", variants[-1])
    want = preset.headline
    # Заголовок обязан существовать в профиле: список одобрен кандидатом,
    # придумывать свой генератор не имеет права.
    for v in variants:
        if v.lower() == want.lower():
            return v
    for v in variants:
        if want.lower() in v.lower():
            return v
    return variants[0]


def _bullet_relevance(bullet, jd_skill_ids: set, p: Profile) -> float:
    """Балл буллета = сумма веса совпавших с вакансией навыков + свежесть."""
    from ..match.scorer import LEVEL_WEIGHT
    pts = 0.0
    for sid in bullet.skills:
        if sid in jd_skill_ids:
            sk = p.skill_by_id.get(sid)
            pts += 1.0 + LEVEL_WEIGHT.get(sk.level, 0.0) if sk else 1.0
    return pts


def _summary(p: Profile, score: Score, lang: str, preset=None,
             middle: bool = False) -> str:
    """Саммари: шаблон пресета + топ разрешённых совпавших навыков.

    Шаблон — запасной путь. Основной: LLM пишет живой текст из тех же фактов,
    гейт его проверяет (см. llm_writer.write_summary в pipeline).
    """
    if middle:
        # Без «7+ лет» и без «Tech Lead»: описываем работу, а не выслугу.
        # Ничего ложного — просто не выпячиваем то, что отпугивает на Middle.
        top_m = [t for t, lvl, w in sorted(score.matched_skills, key=lambda x: -x[2])
                 if lvl in ("expert", "working")][:4]
        names = ", ".join(_canon(p, top_m)) if top_m else "Python, PostgreSQL, REST API"
        if lang == "en":
            return ("Backend engineer: REST APIs, data models, integrations and "
                    "production support. Day-to-day stack: %s. Comfortable owning "
                    "a service end to end — from the contract to metrics." % names)
        return ("Backend-инженер: REST API, схемы данных, интеграции и поддержка "
                "в проде. Рабочий стек: %s. Спокойно веду сервис от контракта "
                "до метрик." % names)
    preset = preset or presets.fallback()
    total = int(p.claims.get("total_years_software", p.total_years()))
    base = (preset.summary_en if lang == "en" else preset.summary_ru) % total

    # Хвост «релевантно вакансии» — только для ролей, где саммари общее.
    # У DevOps/ML/Data текст пресета уже перечисляет нужный стек, второй
    # список навыков подряд читается как мусор.
    if preset.key in ("backend", "product", "architect"):
        top = [t for t, lvl, w in sorted(score.matched_skills, key=lambda x: -x[2])
               if lvl in ("expert", "working")][:4]
        if top:
            names = ", ".join(_disp(n, lang) for n in _canon(p, top))
            base += (" Relevant here: " if lang == "en"
                     else " Релевантно вакансии: ") + names + "."
    return base


def _canon(p: Profile, terms) -> list:
    out, seen = [], set()
    for t in terms:
        sk = next((s for s in p.skills if t in s.terms), None)
        name = sk.canonical if sk else t
        if name.lower() not in seen:
            out.append(name)
            seen.add(name.lower())
    return out


def _skill_lines(p: Profile, score: Score, lang: str, preset=None) -> list:
    """Секция навыков: сначала профильные для роли, потом совпавшие с вакансией.

    Порядок задаёт пресет семейства (tailor/presets.py). Раньше здесь были три
    жёстко зашитые ветки — PM, DevOps и «всё остальное»; из-за этого вакансия
    дата-инженера получала общий инженерный список, где SQL терялся между
    Docker и Agile.
    """
    preset = preset or presets.fallback()
    matched_ids = set()
    for t, _, _ in score.matched_skills:
        sk = next((s for s in p.skills if t in s.terms), None)
        if sk:
            matched_ids.add(sk.id)

    order = [i for i in preset.skills() if i in p.skill_by_id]
    order += [i for i in matched_ids if i in p.skill_by_id and i not in order]

    limit = 16 if preset.max_bullets > 9 else 14
    seen, names = set(), []
    for i in order:
        sk = p.skill_by_id.get(i)
        if sk and sk.level != "none" and sk.canonical.lower() not in seen:
            names.append(_disp(sk.canonical, lang))
            seen.add(sk.canonical.lower())
        if len(names) >= limit:
            break
    return names


def tailor(title: str, tag: str, jd_text: str, profile: Profile | None = None,
           lang: str | None = None) -> TailorResult:
    """Собирает резюме под вакансию.

    Для Middle-позиций резюме подаётся укороченным: только последние места
    работы, заголовок без Senior/Lead, саммари без счётчика лет. Это обычная
    практика targeted resume — резюме не обязано перечислять всю биографию.
    Даты и факты при этом НЕ меняются: занижать стаж нельзя, это проверяется
    одним звонком прошлому работодателю.
    """
    p = profile or get_profile()
    score = score_job(title, tag, jd_text, p)
    lang = lang or _pick_lang(jd_text, title)

    # навыки, релевантные вакансии → id
    jd_terms = _find_terms(jd_text or "") | _find_terms(tag or "")
    jd_skill_ids = set()
    for t in jd_terms:
        sk = next((s for s in p.skills if t in s.terms), None)
        if sk:
            jd_skill_ids.add(sk.id)

    # Кого ищут. Скоринговый классификатор вместо цепочки if/elif — иначе
    # упоминание Kubernetes в бэкенд-вакансии уводило резюме в DevOps.
    role = classify(title, tag, jd_text, skill_hint=_skill_hint(p, jd_skill_ids))
    preset = presets.get(role.family) or presets.fallback()

    # Вакансии редко перечисляют «приоритизацию» и «стейкхолдеров» как
    # технологии — но именно этот опыт покупают на PM/PjM/аналитике.
    # Поднимаем профильные навыки роли вручную.
    jd_skill_ids |= {i for i in preset.boost_skills if i in p.skill_by_id}
    max_bullets = preset.max_bullets

    # ранжируем буллеты внутри каждого опыта; гарантируем свежие
    exp_blocks = []
    all_ranked = []
    for e in p.experience:
        ranked = sorted(e.bullets, key=lambda b: -_bullet_relevance(b, jd_skill_ids, p))
        exp_blocks.append((e, ranked))
        for b in ranked:
            all_ranked.append((e, b, _bullet_relevance(b, jd_skill_ids, p)))

    # выбираем: минимум по 1 из каждого опыта + добиваем самыми релевантными
    chosen = set()
    for _e, ranked in exp_blocks:
        for b in ranked[:MIN_PER_EXP]:
            chosen.add(b.id)
    for _e, b, _pts in sorted(all_ranked, key=lambda x: -x[2]):
        if len(chosen) >= max_bullets:
            break
        chosen.add(b.id)

    # На Middle показываем только последние места работы. Даты не трогаем —
    # просто не перечисляем всю биографию, это законная селекция.
    visible_exp = p.experience
    if score.is_middle:
        visible_exp = sorted(p.experience, key=lambda e: e.start, reverse=True)[:3]
    visible_ids = {e.id for e in visible_exp}

    # собираем render в хронологическом порядке опыта
    render_jobs = []
    rendered_bullets = []
    companies = []
    for e in p.experience:
        if e.id not in visible_ids:
            continue
        picked = [b for b in e.bullets if b.id in chosen]
        picked.sort(key=lambda b: -_bullet_relevance(b, jd_skill_ids, p))
        if not picked:
            continue
        companies.append(e.company)
        texts = []
        for b in picked:
            txt = b.text_ru if lang == "ru" else b.text_en
            texts.append(txt)
            rendered_bullets.append({"source_id": b.id, "text": txt, "section": "experience"})
        render_jobs.append({
            "company": e.company if lang == "ru" else e.company_en,
            "role": e.role_ru if lang == "ru" else e.role_en,
            "start": e.start, "end": e.end, "is_own": e.is_own_project,
            "bullets": texts,
        })

    headline = _pick_headline(p, preset, middle=score.is_middle)
    summary = _summary(p, score, lang, preset, middle=score.is_middle)
    skill_names = _skill_lines(p, score, lang, preset)

    doc = DocModel(lang=lang, headline=headline, summary=summary,
                   rendered_bullets=rendered_bullets, companies=companies, kind="cv")
    gate = check(doc, jd_text=jd_text, profile=p)

    render = {
        "lang": lang,
        "name": p.identity["full_name_ru"] if lang == "ru" else p.identity["full_name_en"],
        "headline": headline,
        "contacts": _contacts(p, lang),
        "summary": summary,
        "jobs": render_jobs,
        "skills": skill_names,
        "languages": _langs(p, lang),
        "education": _edu(p, lang),
        "promoted_terms": gate.promoted_terms,
    }
    return TailorResult(doc=doc, render=render, gate=gate, score=score, lang=lang,
                        role=role, preset=preset)


def _skill_hint(p: Profile, jd_skill_ids: set) -> dict:
    """Сколько подтверждённого опыта у кандидата под каждое семейство.

    Нужно только для разрешения спорных случаев в классификаторе: когда две
    роли набрали почти поровну, выигрывает та, под которую опыт реальнее.
    """
    from ..match.scorer import LEVEL_WEIGHT
    hint = {}
    for key, pr in presets.PRESETS.items():
        pts = 0.0
        for sid in pr.boost_skills:
            if sid not in jd_skill_ids:
                continue
            sk = p.skill_by_id.get(sid)
            if sk:
                pts += LEVEL_WEIGHT.get(sk.level, 0.0)
        hint[key] = pts
    return hint


def _contacts(p: Profile, lang: str) -> str:
    i = p.identity
    loc = "Москва, Россия" if lang == "ru" else "Moscow, Russia"
    return "%s  •  %s  •  %s  •  t.me/%s" % (
        loc, i["phone"], i["email"], i["telegram"].rstrip("/").split("/")[-1])


def _langs(p: Profile, lang: str) -> str:
    names = []
    for L in p.languages:
        lvl = L["level"].replace("_plus", "+")
        nm = L["name_ru"] if lang == "ru" else L["name_en"]
        names.append("%s — %s" % (nm, lvl))
    return "  •  ".join(names)


def _edu(p: Profile, lang: str) -> str:
    e = p.education[0]
    if lang == "ru":
        return "%s — %s, %s, %d" % (e["institution_ru"], e["degree_ru"].lower(),
                                    e["field_ru"].lower(), e["year"])
    return "%s — %s, %s, %d" % (e["institution_en"], e["degree_en"], e["field_en"], e["year"])
