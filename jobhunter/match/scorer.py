"""Скоринг вакансия ↔ профиль.

Определяет, стоит ли откликаться, и служит основой ранжирования буллетов.
Ключевой сигнал — какие технологии из вакансии кандидат реально знает
(и на каком уровне), и сколько из требований попадает в never_claim.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..profile import Profile, get_profile
from ..tailor.gate import _find_terms
from . import workformat

LEVEL_WEIGHT = {"expert": 1.0, "working": 0.7, "familiar": 0.35, "none": 0.0}

# Роли, под которые Сурен реально подходит (по опыту в профиле).
FIT_ROLES = re.compile(
    r"(product\s*manager|product\s*owner|technical\s*pm|tech\s*lead|"
    r"backend|back-end|python|software\s*(engineer|architect)|"
    # «продукт» голым словом было в тексте бьюти-дистрибутора и бренда одежды
    # (24.09) — оценка ставила им «роль профильная». Только продуктовые роли.
    r"продакт|продуктов\w*\s+(?:менеджер|оунер)|"
    r"разработчик|инженер|архитектор|team\s*lead|teamlead|"
    r"ml\s*engineer|mlops|ml\s*systems|computer\s*vision|"
    r"integration\s*engineer|platform\s*engineer|"
    r"devops|\bsre\b|site\s*reliability|infrastructure\s*engineer|"
    r"solution\s*architect|системный\s*архитектор|data\s*engineer)", re.I)

# Роли из headline_variants профиля, которые раньше проходили только за счёт голого
# «продукт» в тексте. Только по заголовку и тегу: по всему тексту «project manager»
# и «аналитик» 24.09 пустили креативные агентства, SMM и ассистентов (51 вакансия
# на живых данных за 14 дней). Project Manager — только технический.
FIT_TITLE_ROLES = re.compile(
    r"\bcto\b|(?-i:\bСТО\b)|технич\w+\s+директор|chief\s+(?:technology|product)\s+officer|"
    r"\bcpo\b|product\s+director|директор\s+по\s+продукт\w*|менеджер\s+продукт\w*|"
    r"(?:technical|tech|it|software|ai)\s+project\s*manager|"
    r"project\s*manager\W{0,3}(?:[\w-]+\W{1,3}){0,2}?(?:software|ai|it|saas|разработ\w*)\b|"
    r"технич\w+\s+(?:менеджер|руководител)\w*\s+проект\w*|"
    r"systems?\s+analyst|\bba\s+analyst|системн\w+\s+аналитик\w*|"
    r"qa\s+(?:automation\s+)?engineer|quality\s+assurance\s+engineer|qa\s+automation", re.I)

# Product positions remain valid for this profile, but they no longer receive
# the same generic role bonus as an engineering vacancy.  The historical
# queue had product roles overrepresented because the word "product" was
# enough to make them look as strong as backend roles.
PRODUCT_ROLES = re.compile(
    r"(?:product\s*(?:manager|owner|lead|analyst)|technical\s*pm|"
    r"продакт|продуктов\w*\s+менеджер)", re.I)

# Роли, где он точно не кандидат (чтобы не тратить отклик).
# Госпортал «Работа России» отдаёт много бюджетных ролей, где слово
# «информатика» или «программист» есть, а работа — не инженерная.
BUDGET_ROLES = re.compile(
    r"(преподавател|учител|педагог|воспитател|методист|лаборант|"
    r"доцент|профессор|ассистент\s+кафедр|заведующ|ректор|декан|"
    r"научный\s+сотрудник|соискател\s+учен|аспирант|"
    r"библиотекар|делопроизводител|секретар|диспетчер|"
    r"электромонт|слесар|механик|монтажник|сварщик|токар|фрезеров|"
    r"техник[- ]|оператор\s+эвм|водител|кладовщик|груз|"
    r"инженер\s+по\s+охране|инженер[- ]констру|инженер[- ]механ|"
    r"инженер\s+по\s+(?:наладке|эксплуатац|снабжен|мет)|"
    r"специалист\s+по\s+кадр|бухгалтер|экономист|юрисконсульт|"
    r"ведущий\s+специалист\s+отдела|главный\s+специалист\s+отдела|"
    r"в\s+прочих\s+отраслях)", re.I)

MISFIT_ROLES = re.compile(
    r"\b(ios|android|swift|kotlin|frontend|front-end|react|vue|angular|"
    r"designer|дизайнер|php|\.net|c#|java\b|golang\b|rust\b|qa\s*manual|"
    r"unity|gamedev|3d|копирайтер|маркетолог|smm|sales|продаж)"
    # 24.09: инженерные роли вне профиля — 1С и железо (ПЛИС/FPGA). Без
    # фильтра «офис» «Программист 1С ERP» и «Инженер ПЛИС» проходили оценку.
    r"|\b1[сc]\b|1[сc][- :]|плис|\bfpga\b|\brtl\b|verilog|vhdl|схемотехн\w*", re.I)

# Не инженерные профессии. Мимо — только если в заголовке нет инженерного слова:
# «Python-разработчик HR-платформы» — наш случай (владелец делал HR-платформу).
NON_ENGINEERING_ROLES = re.compile(
    r"продюсер\w*|reels|таргетолог\w*|контент[- ]?(?:менеджер|мейкер)\w*|"
    r"\bhr\b|hrbp|рекрутер\w*|по\s+персоналу|кадров\w*|"
    r"\bfmcg\b|beauty|косметик\w*|дистрибуц\w*|мерчендайз\w*|"
    r"торгов\w+\s+представител\w*", re.I)
ENGINEERING_WORD = re.compile(
    r"разработчик\w*|developer|engineer|инженер\w*|программист\w*|devops|backend|"
    r"python|architect|архитектор\w*|tech\s*lead|team\s*lead|\bsre\b|data\s+engineer", re.I)

# Теги careered — надёжная категория роли. Эти = профнепригодно независимо от
# случайных совпадений терминов в тексте вакансии.
MISFIT_TAGS = {
    "ios", "android", "swift", "kotlin", "flutter", "react native",
    "frontend", "front-end", "react", "vue", "angular", "js", "javascript",
    "php", "c#", ".net", "java", "go", "golang", "rust", "ruby", "scala",
    "c / c++", "c/c++", "c++", "c", "1c", "unity", "gamedev", "game",
    "design", "designer", "ui/ux", "ux", "seo", "smm", "marketing",
    "sales", "hr", "copywriter", "sysadmin",
}


# Уровень позиции. Кандидат — Senior/Lead с 7 годами: junior/intern-вакансии
# это не «запасной вариант», а гарантированный отказ и потраченный контакт.
# Русские слова склоняются — \b на конце не работает («начинающ|его»),
# поэтому кириллические маркеры матчим по основе.
JUNIOR_RE = re.compile(
    r"(?:\b(?:junior|jun\.|intern|internship|trainee|entry[\s-]?level)\b"
    r"|стажёр|стажер|стажиров|начинающ|младш|без\s+опыта)", re.I)
MIDDLE_RE = re.compile(
    r"(?:\bmiddle\b|\bmid\b|\bmid-level\b|мидл|средний\s+уровень)", re.I)
SENIOR_RE = re.compile(
    r"(?:\b(?:senior|sr\.|lead|principal|staff|head\s+of|architect)\b"
    r"|ведущ|старш|главн|архитектор)", re.I)


@dataclass
class Score:
    total: float                         # 0..100
    fit_role: bool
    misfit_role: bool
    matched_skills: list = field(default_factory=list)   # [(term, level, weight)]
    forbidden_demands: list = field(default_factory=list)  # требуемое из never_claim
    jd_terms: list = field(default_factory=list)
    reason: str = ""
    is_junior: bool = False
    is_middle: bool = False
    work_format: str = workformat.UNKNOWN
    onsite_moscow: bool = False       # офис/гибрид в Москве без переезда — подходит

    @property
    def recommend(self) -> bool:
        return (self.total >= 45 and not self.forbidden_dominant
                and not self.misfit_role and not self.is_junior
                and not self.onsite_only)

    @property
    def onsite_only(self) -> bool:
        """Офис или релокация, которые владельцу не подходят.

        С 24.09 офис и гибрид в Москве подходят, переезд — нет. Молчание о
        формате отказом не считается — см. match/workformat.py.
        """
        return self.work_format == workformat.ONSITE and not self.onsite_moscow

    @property
    def forbidden_dominant(self) -> bool:
        """Ключевой стек вакансии — сплошь то, чего у кандидата нет."""
        return len(self.forbidden_demands) >= 3 and len(self.matched_skills) < 2


def score_job(title: str, tag: str, jd_text: str, profile: Profile | None = None,
              source: str = "") -> Score:
    p = profile or get_profile()
    jd_terms = _find_terms(jd_text or "") | _find_terms(tag or "")

    matched, forbidden = [], []
    skill_pts = 0.0
    for term in sorted(jd_terms):
        sk = next((s for s in p.skills if term in s.terms), None)
        if sk:
            w = LEVEL_WEIGHT.get(sk.level, 0.0)
            matched.append((term, sk.level, w))
            skill_pts += w
        elif term in p.forbidden_terms:
            forbidden.append(term)

    # Роль ищется по всему тексту: сужение до заголовка на 14 днях живых данных
    # (24.09) потеряло 147 годных ролей из 440 — «Engineering Manager», «Fullstack
    # Developer», «Cloud Engineer» в заголовке FIT_ROLES не узнаёт. Мусор режется
    # точнее — MISFIT_ROLES и NON_ENGINEERING_ROLES по заголовку.
    # Посты каналов часто теряют должность в заголовке («москва») и пишут её
    # первой строкой текста: «#москва  Chief Product Officer  Обязанности…».
    fit = (bool(FIT_ROLES.search(" ".join([title or "", tag or "", jd_text or ""])))
           or bool(FIT_TITLE_ROLES.search(" ".join([title or "", tag or "",
                                                     (jd_text or "")[:200]]))))
    tag_l = (tag or "").strip().lower()
    misfit = (tag_l in MISFIT_TAGS or bool(MISFIT_ROLES.search(title or ""))
              or bool(MISFIT_ROLES.search(tag or ""))
              or bool(BUDGET_ROLES.search(title or ""))
              or (bool(NON_ENGINEERING_ROLES.search(title or ""))
                  and not ENGINEERING_WORD.search(title or "")))

    # junior-позиция: ищем в заголовке и в первых строках описания, где обычно
    # стоит грейд. «Senior» в тексте перебивает — бывает «Junior/Senior» вилка.
    head = " ".join([title or "", (jd_text or "")[:400]])
    junior = bool(JUNIOR_RE.search(head)) and not bool(SENIOR_RE.search(head))
    # Middle-позиция: откликаемся, но резюме подаём укороченным — без
    # «7+ лет» и Tech Lead в заголовке, иначе выглядим переквалифицированными.
    middle = bool(MIDDLE_RE.search(head)) and not bool(SENIOR_RE.search(head))

    # ── баллы ──
    # навыки: до 60 (насыщение), роль: +25 fit / −30 misfit, штраф за forbidden
    skill_component = min(60.0, skill_pts * 12.0)
    product_role = bool(PRODUCT_ROLES.search(" ".join([title or "", tag or ""])))
    # Product is a supported family, but not an engineering-role bonus.  Keep
    # skill matches and the fixed base intact so a genuinely relevant product
    # vacancy can still pass; only remove the generic +25 role boost.
    role_component = (0.0 if product_role else (25.0 if fit else 0.0)) \
        - (30.0 if misfit else 0.0)
    forbidden_penalty = min(25.0, len(forbidden) * 8.0)
    total = max(0.0, min(100.0, skill_component + role_component + 15.0 - forbidden_penalty))
    fmt = workformat.detect(title, tag, jd_text, source=source)

    reason_bits = []
    if matched:
        reason_bits.append("совпало навыков: %d (%s)" % (
            len(matched), ", ".join(t for t, _, _ in matched[:6])))
    if forbidden:
        reason_bits.append("требуют вне профиля: %s" % ", ".join(forbidden[:5]))
    if misfit:
        reason_bits.append("роль не профильная")
    if fit:
        reason_bits.append("роль профильная")
    if product_role:
        reason_bits.append("product-роль без инженерного бонуса")
    if junior:
        reason_bits.append("junior-позиция при 7 годах опыта")
    moscow = fmt == workformat.ONSITE and workformat.onsite_ok(title, tag, jd_text)
    if fmt == workformat.ONSITE:
        reason_bits.append("офис/гибрид в Москве — подходит" if moscow
                           else "офис или релокация не в Москве")

    return Score(total=round(total, 1), fit_role=fit, misfit_role=misfit,
                 matched_skills=matched, forbidden_demands=forbidden,
                 jd_terms=sorted(jd_terms), reason="; ".join(reason_bits),
                 is_junior=junior, is_middle=middle, work_format=fmt,
                 onsite_moscow=moscow)
