"""Read-only, versioned explanations alongside the unchanged numerical scorer.

``matched.skill`` is a profile canonical name; evidence IDs resolve to profile
experiences or bullets. Required/desired clauses retain the vacancy's wording.
Unknown conditions are not facts, and informational unknowns (salary/format)
alone do not change the existing remote-work policy or require review.

An assessment is a preparation snapshot, not approval authority. In particular,
``approval_problem`` is a fresh advisory check for NEW automatic approvals; a
human can review its reasons and explicitly approve through the existing flow.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from html import unescape

from ..profile import Profile, get_profile
from . import workformat
from .role import classify

VERSION = 1
MAIN_TRACKS = frozenset({"backend", "ml", "architect"})

_REQUIRED = (
    r"required qualifications|minimum qualifications|basic qualifications|"
    r"essential skills|required|requirements?|qualifications|must[- ]haves?|mandatory|"
    r"what you (?:bring|need)|what we (?:expect|require)|you (?:must|need to)|"
    r"обязательные требования|требования(?: к кандидату)?|обязательно|"
    r"необходимые навыки|что мы (?:ждем|ожидаем)|мы ожидаем|важно для нас"
)
_DESIRED = (
    r"preferred qualifications|nice[- ]to[- ]haves?|desired skills|preferred|"
    r"desirable|desired|optional|bonus(?: points)?|a plus|not required|"
    r"будет плюсом|будет преимуществом|желательно|желательные навыки|"
    r"необязательно|не обязательно|не требуется"
)
_OTHER = (
    r"responsibilities|duties|what you(?:'ll| will) do|about(?: us| the company)?|"
    r"benefits|we offer|what we offer|условия|мы предлагаем|предлагаем|"
    r"обязанности|задачи|о компании|о нас|стек|tech stack|salary|зарплата"
)
_HEADER = re.compile(
    rf"^(?P<required>{_REQUIRED})(?=\s*:|\s*$)|"
    rf"^(?P<desired>{_DESIRED})(?=\s*:|\s*$)|"
    rf"^(?P<other>{_OTHER})(?=\s*:|\s*$)", re.I,
)
_OPTIONAL = re.compile(rf"\b(?:{_DESIRED})\b|\bне\s*обязател\w*", re.I)
_MANDATORY = re.compile(
    r"\b(?:must|required|mandatory|essential|need to|shall)\b|"
    r"обязател\w*|необходим\w*|требуется|требуем|нужно|должен|должны", re.I,
)
_YEARS = re.compile(r"(?P<n>\d+(?:[.,]\d+)?)\s*\+?\s*(?:years?\b|лет\b|год\w*)", re.I)
_TRAINING = re.compile(
    r"\b(?:pre[- ]?train\w*|fine[- ]?tun\w*|finetun\w*|rlhf|lora|qlora|"
    r"backpropagation|gradient descent)\b|"
    r"\btrain(?:ing|ed)?\b.{0,35}\b(?:models?|neural|llms?|networks?)\b|"
    r"\b(?:models?|neural|llms?)\b.{0,25}\btraining\b|"
    r"(?:обучени\w*|обучать|обучал\w*|дообуч\w*|предобуч\w*)\s+"
    r"(?:\w+\s+){0,3}(?:модел\w*|сет\w*|llm)|обучение с подкреплением", re.I,
)
_RESEARCH = re.compile(
    r"\b(?:research(?:er| scientist| engineer)?|publications?|neurips|icml|iclr|"
    r"ph\.?d\.?)\b|научн\w*\s+(?:стат\w*|публикац\w*|исследован\w*|сотрудник)|"
    r"исследован\w*\s+(?:модел\w*|алгоритм\w*)|исследователь", re.I,
)
_NEGATIVE = re.compile(
    r"\b(?:no|not|without|never)\b|\b(?:не|нет|без)\b|необязател\w*", re.I,
)
_FAMILIAR_OK = re.compile(r"familiar|basic|awareness|знакомств|базов|представлен", re.I)
_STRONG = re.compile(r"expert|advanced|deep|strong|proficien|уверенн|глубок|эксперт", re.I)
_GEOGRAPHY = re.compile(
    r"\b(?:worldwide|anywhere|any country|(?-i:US|USA|UK|EU|EEA)|United States|United Kingdom|"
    r"Europe|Canada|Germany|France|Spain|Portugal|Poland|Netherlands|Russia|Armenia|"
    r"Georgia|Kazakhstan|India|Australia|Brazil|LATAM|APAC|EMEA)\b|"
    r"Росси\w*|Армени\w*|Грузи\w*|Казахстан\w*|Европ\w*|США|Канад\w*|"
    r"Германи\w*|из любой страны|по всему миру|любая страна|"
    r"(?:allowed|eligible|hiring)\s+(?:countries|locations)\s*:", re.I,
)
_GEO_RESTRICTION = re.compile(
    r"\b(?:US|USA|UK|EU|EEA|Europe|Canada|Russia)[ -]+only\b|"
    r"\b(?:residents? of|residency|citizenship|work authori[sz]ation|right to work)\b|"
    r"\b(?:must|need to)\s+(?:be\s+)?(?:based|located|reside)|"
    r"только\s+(?:из|в|для)\s+|гражданств\w*|резидентств\w*|разрешение на работу", re.I,
)
# Only generic skill-list scaffolding is discarded. Any remaining words are
# unverified constraints, even if a known technology also occurs in the clause.
_SCAFFOLD = re.compile(
    r"\b(?:experience|knowledge|skills?|of|in|with|and|or|using|working|work|"
    r"have|has|you|we|are|is|a|an|the|required|must|mandatory|essential|"
    r"solid|strong|good|excellent|proficiency|proficient|expert|advanced|deep|"
    r"familiarity|familiar|basic|at|least|minimum|more|than|over|"
    r"опыт|опытом|работы|работа|знание|знания|знанием|навыки|навыков|"
    r"владение|владения|владеть|знать|уметь|умение|уверенное|уверенный|"
    r"глубокое|глубокие|хорошее|хорошие|базовое|базовые|знакомство|"
    r"с|со|s|на|и|или|в|во|от|не|менее|более|лет|года|год|"
    r"обязательно|обязателен|обязательны|требуется|необходимо)\b", re.I,
)


def _value(obj, name, default=""):
    return obj.get(name, default) if isinstance(obj, Mapping) else getattr(obj, name, default)


def _plain(text: str) -> str:
    text = re.sub(r"<(?:br\b[^>]*|/?(?:p|div|li|ul|ol|h[1-6])\b[^>]*)>", "\n", text, flags=re.I)
    return unescape(re.sub(r"<[^>]+>", " ", text))


def _term_rx(term: str) -> re.Pattern:
    escaped = re.escape(term.lower().replace("ё", "е")).replace("е", "[её]")
    return re.compile(r"(?<!\w)" + escaped + r"(?!\w)", re.I)


def _clauses(text: str) -> tuple[list[str], list[str], list[str]]:
    """Parse explicit sections and inline markers without making all stack words mandatory."""
    required, desired, context = [], [], []
    mode = ""
    # Preserve decimal numbers and names such as .NET/Next.js.
    text = re.sub(r"(?m)^\s*\d+[.)]\s+", "", text)
    pieces = re.split(r"\n|;|(?<!\d),(?!\d)|(?<=[.!?])\s+(?=[A-ZА-Я])", text)
    for piece in pieces:
        clause = re.sub(r"^\s*(?:\d+[.)]\s+|[-–—•*#]+\s*)?", "", piece).replace("**", "").strip()
        if not clause:
            continue
        if re.fullmatch(r"(?:fully\s+)?remote[.!]?|удал[её]нно[.!]?", clause, re.I):
            continue
        header = _HEADER.match(clause)
        if header:
            mode = header.lastgroup or "context"
            if mode == "other" and re.match(r"responsibilities|duties|what you|обязанности|задачи|стек|tech stack", header[0], re.I):
                mode = "context"
            clause = clause[header.end():].lstrip(" :–—-").strip()
            if not clause:
                continue
        target = mode
        if _OPTIONAL.search(clause):
            target = "desired"
        elif _MANDATORY.search(clause):
            target = "required"
        if target == "required":
            required.append(clause)
        elif target == "desired":
            desired.append(clause)
        elif mode in ("", "context"):
            context.append(clause)
    return list(dict.fromkeys(required)), list(dict.fromkeys(desired)), context


def _evidence(profile: Profile, skill) -> list[str]:
    valid = {e.id for e in profile.experience} | set(profile.bullet_by_id)
    ids = [eid for eid in skill.evidence_ids if eid in valid]
    ids.extend(b.id for b in profile.bullet_by_id.values() if skill.id in b.skills)
    return sorted(set(ids))


def _skill_hits(text: str, profile: Profile) -> list:
    return [s for s in profile.skills if any(_term_rx(t).search(text) for t in s.terms)]


def _skill_problem(skill, profile: Profile) -> str:
    if skill.level == "none" or skill.terms & profile.forbidden_terms:
        return "навык вне подтвержденного профиля: " + skill.canonical
    if not _evidence(profile, skill):
        return "нет подтверждающих evidence_ids: " + skill.canonical
    return ""


def _ml_problem(clause: str, profile: Profile) -> str:
    # A keyword in an integration bullet or an employer's R&D name cannot
    # establish training/research. Require an explicit skill AND a linked bullet
    # describing the same activity. General familiarity is insufficient.
    for pattern, label in ((_TRAINING, "обучение моделей"), (_RESEARCH, "ML research")):
        if not pattern.search(clause):
            continue
        proved = False
        for skill in profile.skills:
            if skill.level not in ("working", "expert") or _skill_problem(skill, profile):
                continue
            if not pattern.search(" ".join([skill.canonical, *skill.aliases])):
                continue
            for b in profile.bullet_by_id.values():
                if skill.id not in b.skills:
                    continue
                for text in (b.text_ru, b.text_en):
                    if pattern.search(text) and not _NEGATIVE.search(text):
                        proved = True
        if not proved:
            return label + " не подтверждено; интеграция/инференс не доказывают этот опыт"
    return ""


def _requirement_problems(clause: str, profile: Profile) -> tuple[list[str], list[str]]:
    gaps, unknowns = [], []
    skills = _skill_hits(clause, profile)
    for skill in skills:
        problem = _skill_problem(skill, profile)
        if problem:
            gaps.append(problem)
        elif skill.level == "familiar" and (_STRONG.search(clause) or not _FAMILIAR_OK.search(clause)):
            unknowns.append("подтверждено только знакомство: " + skill.canonical)
    forbidden = sorted(t for t in profile.forbidden_terms if _term_rx(t).search(clause))
    if forbidden:
        gaps.append("вне профиля: " + ", ".join(forbidden))
    ml_problem = _ml_problem(clause, profile)
    if ml_problem:
        unknowns.append(ml_problem)
    years = _YEARS.search(clause)
    if years:
        requested = float(years["n"].replace(",", "."))
        if not skills or any((s.years or 0) < requested for s in skills):
            unknowns.append("указанный стаж не подтвержден профилем")
    residue = clause
    terms = sorted({t for s in skills for t in s.terms} | set(forbidden), key=lambda t: (-len(t), t))
    for term in terms:
        residue = _term_rx(term).sub(" ", residue)
    residue = _YEARS.sub(" ", residue)
    residue = _SCAFFOLD.sub(" ", residue)
    if re.search(r"[\w+#]", residue) or not skills and not forbidden:
        unknowns.append("условие не подтверждено профилем")
    return gaps, unknowns


def _vacancy_url(job) -> str:
    raw = _value(job, "raw_json", {}) or {}
    if not isinstance(raw, Mapping):
        raw = {}
    # These are source URLs, never a guessed employer URL or a mailto contact.
    for obj in (job, raw):
        for key in ("vacancy_url", "job_url", "url", "absolute_url", "hostedUrl", "jobUrl", "alternate_url"):
            value = _value(obj, key)
            if isinstance(value, str) and re.match(r"https?://", value):
                return value
    external = _value(job, "external_uuid") or ""
    if re.fullmatch(r"tg:[A-Za-z0-9_]+/\d+", external):
        return "https://t.me/" + external[3:]
    return ""


def explain_job(job, profile=None) -> dict:
    """Return a JSON-safe v1 assessment without writing to the job/application.

    ``profile`` may be a Profile or raw profile mapping; omitted uses the usual
    local profile. Role/confidence come from role.classify without tie-breaking
    by candidate skills, so an ambiguous vacancy stays visible for review.
    """
    p = Profile(profile) if isinstance(profile, Mapping) else profile
    if p is None:
        p = get_profile()
    title, tag, body = (_value(job, key) or "" for key in ("title", "tag", "description_raw"))
    role = classify(title, tag, body)
    track = role.family if role.family in MAIN_TRACKS else ("unknown" if role.family == "unknown" else "additional")
    required, desired, context = _clauses(_plain(body))
    for clause in [title, *context]:
        if _GEO_RESTRICTION.search(clause) and not _OPTIONAL.search(clause):
            required.append(clause)
    required = list(dict.fromkeys(required))
    matched = []
    for skill in _skill_hits(_plain("\n".join((title, tag, body))), p):
        if not _skill_problem(skill, p):
            matched.append({"skill": skill.canonical, "evidence_ids": _evidence(p, skill)})
    gaps: list[str] = []
    unknowns: list[str] = []
    reasons: list[str] = []
    if role.family == "unknown":
        reasons.append("роль не распознана")
    elif role.ambiguous:
        reasons.append("неоднозначная роль: %s / %s" % (role.family, role.runner_up))
    if not role.supported and role.family != "unknown":
        reasons.append("дополнительная роль без подтвержденной базы: " + role.reason())
    for clause in required:
        clause_gaps, clause_unknowns = _requirement_problems(clause, p)
        gaps.extend(clause + " — " + problem for problem in clause_gaps)
        unknowns.extend(clause + " — " + problem for problem in clause_unknowns)
        if clause_gaps or clause_unknowns:
            reasons.append("обязательное требование требует проверки: " + clause)
    for clause in desired:
        clause_gaps, clause_unknowns = _requirement_problems(clause, p)
        gaps.extend("Желательно: " + clause + " — " + problem for problem in clause_gaps)
        unknowns.extend("Желательно: " + clause + " — " + problem for problem in clause_unknowns)
    # Role/duty descriptions may demand research without a Requirements header.
    for clause in [title, *context]:
        if _OPTIONAL.search(clause):
            continue
        problem = _ml_problem(clause, p)
        if problem:
            unknowns.append(clause + " — " + problem)
            reasons.append("проверить характер ML-задач: " + clause)
    fmt = workformat.detect(title, tag, body)
    if fmt == workformat.ONSITE:
        gaps.append("требуется удаленная работа; указан офис/релокация без удаленки")
        reasons.append("формат работы не соответствует remote-only")
    elif fmt == workformat.UNKNOWN:
        unknowns.append("формат работы не указан")
    if not _GEOGRAPHY.search("\n".join((title, body))):
        unknowns.append("география найма / допустимые страны не указаны")
    if not _value(job, "salary_raw") and not re.search(r"\d[\d\s.,kк-]*\s*(?:₽|руб|RUB|USD|EUR|\$|€)|[$€₽]\s*\d", body, re.I):
        unknowns.append("зарплата не указана")
    if not required:
        unknowns.append("обязательные требования не выделены")
    url = _vacancy_url(job)
    if not url:
        unknowns.append("ссылка на вакансию не указана")
    return {
        "version": VERSION, "track": track, "role_family": role.family,
        "needs_review": bool(reasons), "review_reasons": list(dict.fromkeys(reasons)),
        "matched": matched, "required": required, "desired": desired,
        "gaps": list(dict.fromkeys(gaps)), "unknowns": list(dict.fromkeys(unknowns)),
        "vacancy_url": url, "role_confidence": role.confidence,
    }


def assessment_for(app, job) -> dict:
    """Return a detached saved assessment, or explain now; never backfill history."""
    breakdown = _value(app, "score_breakdown_json", {}) or {}
    saved = breakdown.get("assessment") if isinstance(breakdown, Mapping) else None
    if isinstance(saved, dict) and saved.get("version") and all(
        key in saved for key in ("track", "role_family", "needs_review", "review_reasons", "matched",
                                  "required", "desired", "gaps", "unknowns", "vacancy_url", "role_confidence")
    ):
        return deepcopy(saved)
    return explain_job(job)


_REQ_PREFIX = "обязательное требование требует проверки: "
# The profile's own country: a restriction naming it is satisfied, not unknown.
_OWN_COUNTRY = re.compile(r"(?<!\w)(?:РФ|RU)(?!\w)|Росси\w*|\bRussia\w*", re.I)
# Contact details and links in a clause are not requirements at all.
_CONTACT_NOISE = re.compile(
    r"\S+@\S+|https?://\S+|t\.me/\S+|(?<!\w)@\w+|\+?\d[\d\s()\-]{6,}\d")
# Protocols, formats and practices any backend engineer has. Named in a
# posting, they are not a technology the profile could be missing.
_GENERIC_TECH = frozenset("""
    api apis json xml yaml csv http https tls ssl tcp udp ip dns ssh ftp smtp
    oop solid dry kiss crud jwt oauth cors csrf orm mvc rpc ci cd cicd ci/cd
    html html5 css css3 es6 utf-8 utf8 cli sdk ide ui ux url uri os vm vps
    it qa ai ml llm b2b b2c saas mvp kpi sla pr mr code review
    backend frontend fullstack devops sre mlops devsecops
    apache
""".split())
_PRODUCT_NAME = re.compile(r"[a-z][A-Z]|[A-Za-z]{2}\d|\d[A-Za-z]{2}")


def _unknown_technologies(clause: str, profile: Profile) -> list[str]:
    """Technologies the clause requires and the profile does not have.

    A term from the gate's fixed lexicon, or a product-shaped name outside it
    (AtlantisDB, LangGraph). Russian descriptive words, contact details and
    generic protocols are not technologies and never block.
    """
    from ..tailor.gate import _find_terms

    text = _CONTACT_NOISE.sub(" ", clause)
    found = [t for t in sorted(_find_terms(text))
             if t not in profile.allowed_terms and t not in _GENERIC_TECH]
    seen = {w for t in found for w in t.split()}
    # Latin-only tokens: \w would glue "DevOps-инженера" into one product name.
    for token in re.findall(r"(?<![\w.])[A-Za-z][A-Za-z0-9.+#-]*[A-Za-z0-9+#]", text):
        low = token.lower()
        if (low in seen or low in _GENERIC_TECH or low in profile.allowed_terms
                or not _PRODUCT_NAME.search(token)):
            continue
        found.append(token)
        seen.add(low)
    return found


def _blocking_requirement(clause: str, profile: Profile) -> str:
    """What is actually wrong with a required clause, or "" if only unverifiable.

    The review card treats every word the profile does not name as an
    unverified condition. That is right for a human reading the card and wrong
    for automatic approval: on 16.09 a stray "Контакты:", "API", "JSON", an
    e-mail address or a phone number in the posting blocked all 58 fresh
    candidates, and the e-mail queue starved. Automatic approval stops only on
    what is known to be wrong or unconfirmed about the candidate himself.
    """
    gaps, unknowns = _requirement_problems(clause, profile)
    if gaps:
        return "; ".join(gaps)
    for problem in unknowns:
        if problem.startswith("подтверждено только знакомство"):
            return problem
    ml = _ml_problem(clause, profile)
    if ml:
        return ml
    years = _YEARS.search(clause)
    skills = _skill_hits(clause, profile)
    if years and skills:
        requested = float(years["n"].replace(",", "."))
        short = [s.canonical for s in skills if (s.years or 0) < requested]
        if short:
            return "стаж меньше требуемого: " + ", ".join(short)
    if (_GEO_RESTRICTION.search(clause) and not _OPTIONAL.search(clause)
            and not _OWN_COUNTRY.search(clause)):
        return "ограничение по географии или праву на работу"
    unknown = _unknown_technologies(clause, profile)
    if unknown:
        return "нет в профиле: " + ", ".join(unknown)
    return ""


def approval_problem(app, job) -> str:
    """Fresh reason against NEW automatic approval, empty when clear.

    Deliberately ignores saved assessments. Does not revoke/override a previous
    owner decision and must not be installed as an unconditional send-time gate.
    Stricter review reasons stay in explain_job for the owner's card; here a
    required clause blocks only when _blocking_requirement finds a real problem.
    """
    if job is None:
        return "вакансия не найдена"
    profile = get_profile()
    reasons = []
    for reason in explain_job(job, profile)["review_reasons"]:
        if not reason.startswith(_REQ_PREFIX):
            reasons.append(reason)
            continue
        clause = reason[len(_REQ_PREFIX):]
        problem = _blocking_requirement(clause, profile)
        if problem:
            reasons.append("обязательное требование не выполнено: %s — %s"
                           % (clause, problem))
    note = _value(app, "review_note")
    if note:
        reasons.append(str(note))
    return "; ".join(dict.fromkeys(reasons))


def review_fingerprint(app, job) -> str:
    """16-character SHA-256 binding a review card to current vacancy/content.

    Includes the stored numerical score and the current explanation version;
    recalculation, edits or a version change invalidate an old confirmation.
    No write or profile/settings lookup is involved.
    """
    payload = {key: _value(job, key) or "" for key in ("title", "tag", "description_raw")}
    payload.update(message_body=_value(app, "message_body") or "",
                   score=_value(app, "score", 0), assessment_version=VERSION)
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def approve_reviewed(app_id: int, fingerprint: str, actor_id: int) -> tuple[bool, str]:
    """Atomically approve reviewed match concerns, never content/safety failures.

    Only configured owners can confirm an unchanged PENDING_APPROVAL card.
    Does not rewrite scores, saved assessments, letters or outreach history.
    SQLite BEGIN IMMEDIATE serializes a confirmation against edits/other owners.
    """
    from sqlalchemy import select

    from ..config import get_settings
    from ..db import session_scope
    from ..models import Application, Job, SendLog, Status, utcnow
    from ..outreach.eligibility import vacancy_problem
    from .scorer import score_job

    if isinstance(actor_id, bool) or actor_id not in get_settings().bot_owner_ids:
        return False, "Подтвердить соответствие может только владелец"
    with session_scope() as sess:
        sess.connection().exec_driver_sql("BEGIN IMMEDIATE")
        app = sess.get(Application, app_id)
        if app is None:
            return False, "Заявка не найдена"
        if app.status != Status.PENDING_APPROVAL.value:
            return False, "Заявка больше не ожидает одобрения"
        if (app.sent_at or app.send_attempts or app.send_last_attempt_at or app.applied_at
                or app.sending_lease_until or app.telegram_msg_id
                or sess.scalar(select(SendLog.id).where(
                    SendLog.application_id == app.id).limit(1)) is not None):
            return False, "У заявки уже есть история отправки; повторное одобрение недоступно"
        if not app.gate_passed or app.review_note or not (app.message_body or "").strip():
            return False, "Сначала требуется проверка достоверности и текста отклика"
        job = sess.get(Job, app.job_id)
        if job is None:
            return False, "Вакансия не найдена"
        if fingerprint != review_fingerprint(app, job):
            return False, "Вакансия или отклик изменились — откройте новую карточку проверки"
        eligibility = vacancy_problem(job)
        if not eligibility.allowed:
            return False, eligibility.reason
        profile = get_profile()
        score = score_job(job.title, job.tag, job.description_raw, profile)
        if not score.recommend:
            return False, score.reason or "Вакансия не проходит обязательные правила отбора"
        # Match uncertainty can be explicitly accepted, but a requirement from
        # never_claim/level=none is a known exclusion, not an unknown condition.
        required = explain_job(job, profile)["required"]
        for clause in required:
            forbidden = [term for term in profile.forbidden_terms if _term_rx(term).search(clause)]
            if forbidden:
                return False, "Обязательный навык вне профиля: " + ", ".join(sorted(forbidden))
        app.transition(Status.APPROVED)
        app.approved_at = utcnow()
        return True, "Соответствие подтверждено владельцем; отклик одобрен"
