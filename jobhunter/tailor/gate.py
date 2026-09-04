"""Анти-фабрикация гейт (§4 плана).

Чистая функция. Проверяет сгенерированный документ (резюме ИЛИ письмо) против
profile.yaml. Любой hard-фейл → документ не может уйти в PENDING_APPROVAL.

Модель документа (то, что отдаёт генератор):
    DocModel(
      lang="ru"|"en",
      headline="...",              # заголовок/роль
      summary="...",               # текст «о себе» (может быть пустым)
      rendered_bullets=[           # каждый буллет несёт source_id
        {"source_id": "b_1xb_1", "text": "...", "section": "experience"},
        ...
      ],
      free_text="...",             # доп. текст письма вне буллетов (для DM)
      companies=["1xbet", ...],    # названия компаний, упомянутые в документе
    )

Термины технологий детектируются ТОЛЬКО по фиксированному lexicon/tech_terms.txt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

from ..config import get_settings
from ..profile import Profile, get_profile
from ..textutil import bare_numbers, norm

# Порог провенанса: рендер должен быть похож на источник (переформулировка ок).
PROVENANCE_MIN = 0.60
# Слова превосходной степени рядом с familiar-навыком = инфляция уровня.
SUPERLATIVE = re.compile(
    r"(эксперт|экспертн|глубок|продвинут|advanced|expert|deep|extensive|"
    r"в совершенстве|свободно|\d+\s*\+?\s*(?:лет|год|years?))", re.I)


@dataclass
class Failure:
    rule_id: str
    severity: str            # hard | warn
    offending: str
    detail: str = ""


@dataclass
class GateResult:
    passed: bool
    failures: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    promoted_terms: list = field(default_factory=list)

    @property
    def hard(self) -> list:
        return [f for f in self.failures if f.severity == "hard"]


@dataclass
class DocModel:
    lang: str = "ru"
    headline: str = ""
    summary: str = ""
    rendered_bullets: list = field(default_factory=list)   # [{source_id,text,section}]
    free_text: str = ""
    companies: list = field(default_factory=list)
    kind: str = "cv"                                        # cv | message


@lru_cache
def _lexicon() -> list:
    path = Path(get_settings().lexicon_path)
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            terms.append(s.lower())
    # длинные раньше коротких — чтобы ".net core" матчился раньше ".net"
    return sorted(set(terms), key=len, reverse=True)


def _find_terms(text: str) -> set:
    """Технологические термины из лексикона, встреченные в тексте (по границам слов)."""
    low = " " + re.sub(r"\s+", " ", (text or "").lower()) + " "
    # заменим пунктуацию на пробелы, но сохраним # + . в терминах типа c#, .net
    low = re.sub(r"[^\w\s#+.]", " ", low)
    hits = set()
    for term in _lexicon():
        # граница: term окружён неалфанумериком
        pat = r"(?<![\w#+.])" + re.escape(term) + r"(?![\w#+])"
        if re.search(pat, low):
            hits.add(term)
    return hits


# Отдельное число: цифры, НЕ примыкающие к букве (исключает p95, s3, oauth2, yolov8,
# utf8, sha256, torch.profiler и т.п. — это токены, а не числовые утверждения).
_CLAIM_NUM = re.compile(r"(?<![\wА-Яа-я.])\d+(?:[.,]\d+)?(?![\wА-Яа-я])")


def _claim_numbers(text: str) -> set:
    out = set()
    for tok in _CLAIM_NUM.findall(text or ""):
        out.add(tok.replace(",", "."))
    return out


def _all_text(doc: DocModel) -> str:
    parts = [doc.headline, doc.summary, doc.free_text]
    parts += [b.get("text", "") for b in doc.rendered_bullets]
    return "\n".join(p for p in parts if p)


def check(doc: DocModel, jd_text: str = "", profile: Profile | None = None,
          baseline_terms: set | None = None) -> GateResult:
    p = profile or get_profile()
    res = GateResult(passed=True)
    full_text = _all_text(doc)

    # ── 1. провенанс буллетов ──
    for b in doc.rendered_bullets:
        sid = b.get("source_id")
        rendered = b.get("text", "")
        if not sid:
            res.failures.append(Failure("provenance.no_source", "hard", rendered[:80],
                                        "буллет без source_id"))
            continue
        src = p.bullet_by_id.get(sid)
        if not src:
            res.failures.append(Failure("provenance.bad_source", "hard", sid,
                                        "source_id не найден в профиле"))
            continue
        source_text = src.text_ru if doc.lang == "ru" else src.text_en
        ratio = SequenceMatcher(None, norm(source_text), norm(rendered)).ratio()
        if ratio < PROVENANCE_MIN:
            res.failures.append(Failure("provenance.drift", "hard", rendered[:80],
                                        "похожесть %.2f < %.2f к источнику %s"
                                        % (ratio, PROVENANCE_MIN, sid)))

    # ── 2. инвариантность чисел ──
    # 2a. отдельные числовые утверждения (не цифры внутри токенов p95/s3/oauth2/yolov8)
    allowed_nums = p.allowed_numbers()
    for num in _claim_numbers(full_text):
        if num in allowed_nums:
            continue
        if num in {"1", "2", "3"}:            # перечислительные («3 канала»)
            continue
        res.failures.append(Failure("numbers.unbacked", "hard", num,
                                    "число %s не подтверждено профилем" % num))
    # 2b. множители («в N раз», «Nx», «N-fold») — должны опираться на метрику-множитель,
    # иначе 5 из «в 5 раз» проскочит как совпадение со стажем навыка.
    mult_ok = {"2", "3"}
    for e in p.experience:
        for b in e.bullets:
            for m in b.metrics:
                if str(m.get("unit", "")).lower() in ("x", "раз", "раза", "fold"):
                    mult_ok |= bare_numbers(str(m.get("value", "")))
    for m in re.finditer(r"(?:в\s+(\d+)\s*раз|(\d+)\s*[-\s]?fold|(\d+)\s*[xх]\b)",
                         full_text, re.I):
        n = next(g for g in m.groups() if g)
        if n not in mult_ok:
            res.failures.append(Failure("numbers.unbacked", "hard", m.group(0),
                                        "множитель %s не подтверждён метрикой" % n))

    # ── 3. белый/чёрный список технологий ──
    # Буллеты — дословный текст из profile.yaml (провенанс ≈1.0), в них законно
    # встречаются инструменты (flake8, openai, black), не вынесенные в skills.
    # Поэтому whitelist ("unknown") проверяем ТОЛЬКО в сочинённом тексте
    # (заголовок/саммари/free_text). Запрещённые термины (never_claim/level:none)
    # ловим ВЕЗДЕ, включая буллеты — их там быть не должно никогда.
    composed = " ".join(x for x in (doc.headline, doc.summary, doc.free_text) if x)
    composed_terms = _find_terms(composed)
    bullet_terms = _find_terms("\n".join(b.get("text", "") for b in doc.rendered_bullets))
    doc_terms = composed_terms | bullet_terms
    head_sum_terms = _find_terms((doc.headline or "") + " " + (doc.summary or ""))

    for term in doc_terms:                       # forbidden — универсально
        if term in p.forbidden_terms:
            res.failures.append(Failure("lexicon.forbidden", "hard", term,
                                        "запрещённый термин (never_claim/level:none)"))

    for term in composed_terms:                  # unknown — только сочинённое
        if term in p.forbidden_terms:
            continue
        if term not in p.allowed_terms:
            res.failures.append(Failure("lexicon.unknown", "hard", term,
                                        "технология вне профиля (в сочинённом тексте)"))

    # инфляция уровня: familiar-навык нельзя в заголовке/саммари; превосходную
    # степень рядом с familiar ловим ВЕЗДЕ (включая буллеты — паттерн вранья).
    ctx_src = full_text.lower()
    for term in (p.familiar_terms & doc_terms):
        if term in head_sum_terms:
            res.failures.append(Failure("lexicon.level_inflation", "hard", term,
                                        "familiar-навык в заголовке/о-себе"))
            continue
        for m in re.finditer(re.escape(term), ctx_src):
            ctx = ctx_src[max(0, m.start()-40): m.end()+40]
            if SUPERLATIVE.search(ctx):
                res.failures.append(Failure("lexicon.level_inflation", "hard", term,
                                            "familiar-навык + превосходная степень"))
                break

    # ── 4. утечка терминов работодателя ──
    if jd_text:
        jd_terms = _find_terms(jd_text)
        base = baseline_terms or set()
        promoted = sorted((jd_terms & doc_terms) - base)
        res.promoted_terms = promoted
        # если промотированный термин — familiar, предупреждаем (не хард)
        for t in promoted:
            if t in p.familiar_terms:
                res.warnings.append(Failure("promotion.familiar", "warn", t,
                                            "термин работодателя всплыл на familiar-навыке"))

    # ── 5. согласованность лет ──
    claimed_total = int(p.claims.get("total_years_software", 0))
    if claimed_total and abs(claimed_total - p.total_years()) > 1:
        res.failures.append(Failure("years.total_mismatch", "hard", str(claimed_total),
                                    "заявленный общий стаж расходится с таймлайном"))
    # «N+ лет <навык>» в тексте — сверяем с skill.years
    for m in re.finditer(r"(\d+)\s*\+?\s*(?:лет|год[а-я]*|years?)\s+([A-Za-zА-Яа-я#+.]+)",
                         full_text, re.I):
        n = int(m.group(1))
        near = _find_terms(m.group(2))
        for term in near:
            sk = next((s for s in p.skills if term in s.terms), None)
            if sk and n > sk.years:
                res.failures.append(Failure("years.skill_inflation", "hard",
                                            "%d лет %s" % (n, term),
                                            "профиль подтверждает только %d" % sk.years))

    # ── 6. целостность сущностей (компании) ──
    for c in doc.companies:
        cl = (c or "").lower().strip()
        if not cl:
            continue
        if not any(cl in ct or ct in cl for ct in p.company_terms):
            res.failures.append(Failure("entity.unknown_company", "hard", c,
                                        "компания не из опыта"))

    res.passed = not res.hard
    return res
