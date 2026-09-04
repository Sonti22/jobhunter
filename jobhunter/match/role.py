"""Классификатор семейства роли по тексту вакансии.

Зачем отдельный модуль. Раньше роль выбиралась цепочкой if/elif в
select.py:_pick_headline — первое сработавшее регулярное выражение и побеждало.
Порядок проверки был DevOps → PM → ML → Tech Lead → Backend, поэтому любое
упоминание Kubernetes в обычной бэкенд-вакансии уводило резюме в DevOps-заголовок,
а вакансия дата-аналитика проваливалась в ветку «иначе → Backend Engineer».
Рекрутёр это видит: «На вакансию дата аналитика ваш профиль не подходит».

Здесь вместо первого совпадения — скор. Каждое семейство набирает очки по всем
своим маркерам, и побеждает набравшее больше. Заголовок вакансии весит втрое
против тела: в теле часто перечислен весь стек компании, а роль названа один раз
и именно в заголовке.

Семейство — это не заголовок резюме. Заголовок, порядок навыков и текст саммари
живут в presets.py; здесь только ответ на вопрос «кого ищут».
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Сколько знаков описания смотрим. Дальше начинается «о компании», ДМС и
# корпоративы — там маркеры ролей встречаются случайно и только шумят.
BODY_WINDOW = 800

W_TITLE, W_TAG, W_BODY = 3.0, 2.0, 1.0

# Отрыв победителя от второго места, ниже которого решение считается спорным
# и уточняется по совпадению навыков кандидата.
MARGIN = 0.20

# Минимальный балл, ниже которого роль считается нераспознанной. Без порога
# «Специалист службы поддержки» уезжал в backend на одном слабом совпадении
# (0.7 балла за слово «микросервис» где-то в теле) — и получал backend-резюме.
MIN_SCORE = 2.0


def _rx(*parts: str) -> re.Pattern:
    return re.compile("|".join(parts), re.I)


# Маркеры семейств. Вес — насколько однозначно маркер указывает на роль:
# 3.0 — прямое название роли, 1.5 — сильный признак, 0.7 — слабый намёк.
#
# Русские слова матчим по основе: \b на конце не работает со склонениями
# («аналитик|а», «разработчик|ом»).
ROLE_PATTERNS: dict[str, list[tuple[re.Pattern, float]]] = {
    "backend": [
        (_rx(r"back-?end", r"бэкенд", r"бекенд", r"server-?side"), 3.0),
        (_rx(r"python\s*(?:-|\s)?(?:разработ|developer|engineer)",
             r"разработчик\s+python", r"django\s*(?:developer|разработ)"), 3.0),
        (_rx(r"\bfastapi\b", r"\bdjango\b", r"\bflask\b", r"\basyncio\b",
             r"\bcelery\b", r"\bsqlalchemy\b"), 1.5),
        (_rx(r"rest\s*api", r"микросервис", r"microservice", r"\bgrpc\b"), 0.7),
    ],
    "devops": [
        (_rx(r"\bdevops\b", r"\bsre\b", r"site\s*reliability",
             r"platform\s*engineer", r"infrastructure\s*engineer",
             r"инженер\s+инфраструктур", r"системный\s+администратор",
             r"system\s*administrator", r"server\s*administrator",
             r"\bsysadmin\b", r"cloud\s*engineer"), 3.0),
        (_rx(r"\bci/?cd\b", r"gitlab\s*ci", r"\bjenkins\b", r"\bhelm\b",
             r"\bansible\b", r"\bterraform\b", r"\bargocd\b"), 1.5),
        (_rx(r"kubernetes", r"\bk8s\b", r"prometheus", r"grafana",
             r"эксплуатац", r"дежурств", r"on-?call", r"\bsla\b"), 0.7),
    ],
    "data_engineer": [
        (_rx(r"data\s*engineer", r"дата-?инженер", r"инженер\s+данных",
             r"data\s*platform", r"аналитик\s+кхд", r"\bdwh\b",
             r"хранилищ\s+данных"), 3.0),
        (_rx(r"\betl\b", r"\belt\b", r"airflow", r"clickhouse", r"greenplum",
             r"data\s*lake", r"\bdbt\b", r"spark"), 1.5),
        (_rx(r"kafka", r"витрин\s+данных", r"пайплайн\s+данных"), 0.7),
    ],
    "ml": [
        (_rx(r"\bml\s*engineer", r"machine\s*learning\s*engineer", r"\bmlops\b",
             r"data\s*scien", r"дата-?сайентист", r"\bai\s*engineer",
             r"computer\s*vision", r"\bcv\s*engineer", r"llm\s*engineer"), 3.0),
        (_rx(r"\bpytorch\b", r"tensorflow", r"\byolo", r"opencv", r"\bvllm\b",
             r"инференс", r"inference", r"\brag\b", r"эмбеддинг"), 1.5),
        (_rx(r"\bllm\b", r"нейросет", r"machine\s*learning", r"\bnlp\b"), 0.7),
    ],
    "product": [
        (_rx(r"product\s*manager", r"product\s*owner", r"продакт",
             r"продукт-?менеджер", r"менеджер\s+продукт", r"\bcpo\b",
             r"head\s+of\s+product", r"product\s*lead"), 3.0),
        (_rx(r"\broadmap\b", r"дорожн\w*\s+карт", r"продуктов\w*\s+метрик",
             r"\bdiscovery\b", r"customer\s*development", r"\bcustdev\b"), 1.5),
        (_rx(r"гипотез", r"backlog", r"бэклог", r"\bmvp\b", r"\bokr\b"), 0.7),
    ],
    "project": [
        (_rx(r"project\s*manager", r"проджект", r"руководител\w*\s+проект",
             r"delivery\s*manager", r"\bpmo\b", r"менеджер\s+проект"), 3.0),
        (_rx(r"сроки\s+проект", r"проектн\w*\s+документац", r"\bgantt\b",
             r"устав\s+проект"), 1.0),
    ],
    "analyst": [
        (_rx(r"систем\w*\s+аналитик", r"бизнес-?аналитик", r"system\s*analyst",
             r"business\s*analyst", r"аналитик\s+требован"), 3.0),
        (_rx(r"\bbpmn\b", r"\buml\b", r"постановк\w*\s+задач",
             r"техническ\w*\s+задани", r"\bтз\b", r"user\s*stor"), 1.5),
        (_rx(r"требован", r"openapi", r"swagger", r"интеграцион\w*\s+сценар"), 0.7),
    ],
    "qa": [
        (_rx(r"\bqa\b", r"\bsdet\b", r"тестировщик", r"test\s*engineer",
             r"\baqa\b", r"инженер\s+по\s+тестирован", r"quality\s*assurance"), 3.0),
        (_rx(r"автотест", r"\bpytest\b", r"selenium", r"playwright",
             r"нагрузочн\w*\s+тестирован", r"\blocust\b", r"тест-?кейс"), 1.5),
        (_rx(r"регресс", r"\bbug\b", r"дефект"), 0.7),
    ],
    "architect": [
        (_rx(r"архитектор", r"\barchitect\b", r"tech\s*lead", r"техлид",
             r"team\s*lead", r"тимлид", r"руководител\w*\s+разработ",
             r"engineering\s*manager", r"\bcto\b", r"staff\s*engineer"), 3.0),
        (_rx(r"проектирован\w*\s+систем", r"архитектурн", r"\badr\b",
             r"техническ\w*\s+долг"), 1.0),
    ],
    # Ниже — семейства, под которые резюме НЕ собирается. Классифицируем их
    # явно, чтобы отклик отклонялся с внятной причиной, а не собирался
    # backend-резюме под чужую вакансию.
    "frontend": [
        (_rx(r"front-?end", r"фронтенд", r"фронтэнд"), 3.0),
        (_rx(r"\breact\b", r"\bvue\b", r"\bangular\b", r"\bsvelte\b",
             r"next\.?js", r"typescript"), 1.5),
        (_rx(r"\bcss\b", r"\bscss\b", r"вёрстк", r"верстк"), 0.7),
    ],
    "mobile": [
        (_rx(r"\bios\b", r"\bandroid\b", r"мобильн\w*\s+разработ",
             r"\bflutter\b", r"react\s*native", r"\bswift\b",
             r"kotlin\s*(?:multiplatform|разработ)"), 3.0),
        (_rx(r"jetpack\s*compose", r"swiftui", r"app\s*store", r"google\s*play"), 1.0),
    ],
    "security": [
        (_rx(r"информационн\w*\s+безопасн", r"security\s*engineer", r"\bappsec\b",
             r"пентест", r"pentest", r"\bsoc\b\s*аналитик", r"\bdevsecops\b"), 3.0),
        (_rx(r"\bsiem\b", r"уязвимост", r"\bowasp\b", r"комплаенс\s+иб"), 1.0),
    ],
    "onec": [
        (_rx(r"\b1c\b", r"\b1с\b", r"битрикс", r"bitrix", r"\babap\b"), 3.0),
    ],
    # Не-инженерные роли. В корпусе их много: arbeitnow и The Muse — общие
    # job-борды, оттуда приезжают Account Executive, PR Associate и водители
    # Lyft. Классифицируем явно, чтобы отказ был с внятной причиной.
    "design": [
        (_rx(r"\bdesigner\b", r"дизайнер", r"\bux\b", r"\bui/ux\b",
             r"product\s*design", r"motion\s*design"), 3.0),
        (_rx(r"\bfigma\b", r"прототип\w*\s+интерфейс"), 1.0),
    ],
    "sales": [
        (_rx(r"account\s*executive", r"sales\s*(?:manager|representative|engineer)",
             r"business\s*development", r"менеджер\s+по\s+продаж", r"\bdeal\s*desk",
             r"pre-?sales", r"solutions?\s*engineer"), 3.0),
    ],
    "support": [
        (_rx(r"служб\w*\s+поддержк", r"customer\s*support", r"support\s*officer",
             r"техническ\w*\s+поддержк", r"help\s*desk", r"customer\s*success",
             r"специалист\s+поддержк"), 3.0),
    ],
    "nonit": [
        (_rx(r"\bhr\b\s*(?:manager|generalist)", r"рекрут", r"маркетолог",
             r"\bsmm\b", r"копирайт", r"content\s*manager", r"\bpr\s*&",
             r"communications?\s*associate", r"supply\s*chain", r"бухгалтер",
             r"логист", r"\bdriver\b", r"водител"), 3.0),
    ],
}

# Семейства, под которые у кандидата есть база. Всё остальное — отказ.
# Список согласован с пользователем: сильные роли плюс смежные, где база
# тоньше (QA, project manager, аналитик), но реально существует.
SUPPORTED = frozenset({"backend", "devops", "data_engineer", "ml", "product",
                       "project", "analyst", "qa", "architect"})

UNSUPPORTED_REASON = {
    "frontend": "нет фронтенд-опыта (React/Vue/Angular в never_claim)",
    "mobile": "нет мобильной разработки (iOS/Android/Flutter)",
    "security": "нет опыта в информационной безопасности",
    "onec": "нет опыта с 1С/Битрикс/ABAP",
    "design": "не дизайнер",
    "sales": "не продажи и не пресейл",
    "support": "не техподдержка",
    "nonit": "не инженерная роль",
    "unknown": "роль не распознана",
}


@dataclass
class RoleMatch:
    family: str
    score: float = 0.0
    runner_up: str = ""
    runner_up_score: float = 0.0
    all_scores: dict = field(default_factory=dict)

    @property
    def supported(self) -> bool:
        return self.family in SUPPORTED

    @property
    def confidence(self) -> float:
        """0..1. Насколько уверенно победитель оторвался от второго места."""
        if self.score <= 0:
            return 0.0
        if not self.runner_up_score:
            return 1.0
        return max(0.0, min(1.0, 1.0 - self.runner_up_score / self.score))

    @property
    def ambiguous(self) -> bool:
        return self.confidence < MARGIN

    def reason(self) -> str:
        if not self.supported:
            return UNSUPPORTED_REASON.get(self.family, "нет базы под роль")
        return ""


def _score_field(text: str, patterns: list, weight: float) -> float:
    if not text:
        return 0.0
    pts = 0.0
    for rx, w in patterns:
        if rx.search(text):
            pts += w * weight
    return pts


def classify(title: str, tag: str, jd_text: str,
             skill_hint: dict | None = None) -> RoleMatch:
    """Определяет семейство роли.

    skill_hint — необязательный словарь {family: балл совпадения навыков}.
    Используется только для разрешения спорных случаев: когда два семейства
    набрали почти поровну, выигрывает то, под которое у кандидата больше
    подтверждённого опыта. Само по себе оно роль не назначает — иначе любая
    вакансия уезжала бы в backend просто потому, что там больше всего навыков.
    """
    title = title or ""
    tag = tag or ""
    body = (jd_text or "")[:BODY_WINDOW]

    scores = {}
    for family, patterns in ROLE_PATTERNS.items():
        pts = (_score_field(title, patterns, W_TITLE)
               + _score_field(tag, patterns, W_TAG)
               + _score_field(body, patterns, W_BODY))
        if pts:
            scores[family] = round(pts, 2)

    if not scores:
        return RoleMatch(family="unknown", all_scores={})

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top, top_pts = ranked[0]
    second, second_pts = (ranked[1] if len(ranked) > 1 else ("", 0.0))

    # Слабый сигнал — это не роль, а случайное слово в теле вакансии.
    if top_pts < MIN_SCORE:
        return RoleMatch(family="unknown", score=top_pts, all_scores=scores)

    # Спорный случай — решаем по реальному опыту кандидата, а не по количеству
    # раз, которое слово встретилось в описании.
    if second and second_pts and (1.0 - second_pts / top_pts) < MARGIN and skill_hint:
        if skill_hint.get(second, 0) > skill_hint.get(top, 0):
            top, second = second, top
            top_pts, second_pts = second_pts, top_pts

    return RoleMatch(family=top, score=top_pts, runner_up=second,
                     runner_up_score=second_pts, all_scores=scores)
