# -*- coding: utf-8 -*-
"""Кто автор поста: работодатель (вакансия) или соискатель (резюме).

Зачем отдельный модуль: в каналах вроде @devops_jobs вакансии и резюме
идут вперемешку, у обоих есть контакт, и наивный сбор берёт оба. Система
уже написала четырём соискателям, приняв их резюме за вакансии, — для
чужого человека это спам, для нашего аккаунта — сигнал антиспаму Telegram.
Проверка нужна на ВСЕХ путях: при сборе, перед подготовкой письма и в
последний момент перед отправкой — источники разные, а ошибка одна.

Принцип — взвешенные маркеры обеих сторон, при равенстве побеждает
«соискатель»: не написать лишний раз дешевле, чем написать не тому.
Явная метка публикатора (#резюме / #вакансия, первое слово поста) весит
больше любой фразы в теле: это самоописание автора, а не догадка.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

SEEKER = "seeker"
VACANCY = "vacancy"
OTHER = "other"

_HEAD = 1500          # маркеры тела ищем в начале: дальше идут стеки и ссылки

# ── соискатель ──
_S_TAG = re.compile(
    r"#(?:резюме|resume|cv|ищу_работу|ищуработу|поиск_работы|opentowork|"
    r"open_to_work|открыт_к_предложениям|lookingforjob|looking_for_job)\b", re.I)
# «[For Hire]» — самоописание автора на Reddit, той же силы, что «Резюме»
# первым словом.
_S_FIRST = re.compile(r"^\W{0,3}(?:(?:резюме|resume|cv)\b|for\s+hire\s*\])", re.I)
_S_STRONG = re.compile(
    r"(ищу\s+(?:работу|проект|команду|вакансию|позицию|удал[её]нк|подработк)|"
    r"в\s+(?:активном\s+)?поиске\s+(?:работы|проекта|вакансии)|"
    r"рассматриваю\s+(?:предложения|вакансии|офферы|варианты)|"
    r"открыт[а]?\s+(?:к|для)\s+(?:предложени|новы[хм]\s+возможност)|"
    r"open\s+to\s+(?:work|new\s+opportunit|offers)|"
    r"looking\s+for\s+(?:a\s+)?(?:new\s+)?(?:job|work|role|position|opportunit|remote\s+work)|"
    r"seeking\s+(?:a\s+)?(?:new\s+)?(?:job|role|position|opportunit)|"
    r"обо\s+мне\s*[:\n]|о\s+себе\s*[:\n]|мо[йё]\s+(?:опыт|стек|навыки|резюме|портфолио)\s*[:\n]|"
    r"желаема[яе]\s+(?:должност|зарплат|зп|позици)|ожидани[яе]\s+по\s+(?:зп|зарплат|деньг)|"
    r"готов[а]?\s+(?:к\s+)?(?:рассмотреть|выйти|приступить)\s+|"
    r"about\s+me\s*[:\n]|my\s+(?:experience|stack|skills)\s*[:\n]|"
    r"ключевой\s+опыт\s*[:\n]|формат\s+работы,\s+который\s+ищу|"
    # Шаблон HN «Who wants to be hired»: «Location: … / Remote: Yes / Willing
    # to relocate: No / Technologies: … / Résumé/CV: …». Такие посты попадали
    # в ветку найма, и соискателям из Бразилии и Чарльстона были готовы письма.
    r"willing\s+to\s+relocate\s*:|r[ée]sum[ée]\s*/\s*cv\s*:|^\s*technologies\s*:)", re.I | re.M)
# «#ищу» без уточнения — слабый: работодатели вешают его в смысле «ищем»
_S_WEAK = re.compile(r"#ищу\b|\bя\s+(?:python|backend|бэкенд|devops|разработчик|"
                     r"программист|инженер|аналитик|тестировщик)\b", re.I)

# ── работодатель ──
_V_TAG = re.compile(r"#(?:вакансия|вакансии|vacancy|hiring|нанимаем|ищем)\b", re.I)
_V_FIRST = re.compile(r"^\W{0,3}(?:вакансия|vacancy|hiring|job\s+opening|"
                      r"открыта\s+вакансия|ищем|требуется)\b", re.I)
# Глаголы найма — автор ищет ЧЕЛОВЕКА. Это сильнее любой метки: «#резюме
# … Ищем DevOps-инженера в проект» — вакансия с ошибочным тегом.
_V_HIRE = re.compile(
    r"(мы\s+ищем|ищем\s+(?:в\s+команду|разработ|специал|инженер|аналит|"
    r"тестиров|devops|python|backend|frontend|менеджер|дизайн|senior|middle|junior|"
    r"сетев|сисадмин|администратор)|"
    # «Ищу разработчика для парсера» — работодатель в первом лице; объект
    # поиска отличает его от соискателя («ищу работу / проект / позицию»).
    r"ищу\s+(?:разработ|программист|специал|инженер|аналит|тестиров|дизайн|"
    r"devops|python|backend|frontend|человека|исполнител|фрилансер|подрядчик)|"
    r"\bтребу[её]тся\b|\bтребуются\b|\bнанимаем\b|в\s+команду\s+(?:нужен|нужна|ищем|требуется)|"
    r"we\s+(?:are\s+)?(?:hiring|looking\s+for)|we're\s+(?:hiring|looking)|join\s+our\s+team|"
    r"присылай(?:те)?\s+(?:своё\s+|ваше\s+)?резюме|send\s+(?:your\s+)?(?:cv|resume)|"
    r"откликнуться|отклик\s+на\s+ваканси|ждём\s+(?:ваши|твои)\s+резюме)", re.I)
# Структура объявления — есть и в вакансиях, и в резюме («мои требования
# к работе»), поэтому весит меньше и сама по себе резюме не перевешивает.
_V_STRUCT = re.compile(
    r"(требовани[яе]\s*[:\n]|обязанност[ие]\s*[:\n]|мы\s+предлагаем|условия\s*[:\n]|"
    r"что\s+(?:нужно|предстоит)\s+делать|задачи\s*[:\n]|"
    r"responsibilities\s*[:\n]|requirements\s*[:\n]|what\s+you(?:'ll|\s+will)\s+do)", re.I)


@dataclass
class PostKind:
    kind: str
    seeker: float
    vacancy: float
    reasons: list = field(default_factory=list)

    @property
    def is_seeker(self) -> bool:
        return self.kind == SEEKER


def classify_post(text: str) -> PostKind:
    """Веса: первое слово 4, явная метка 3, глагол найма 2, фраза соискателя
    1.5, секция объявления 1, слабый признак 0.5.

    Ядро решения — не арифметика, а два правила поверх неё:
      1. Метка соискателя БЕЗ его фраз — только подсказка: «#резюме … Ищем
         DevOps-инженера» — вакансия с чужим тегом (живой случай #9290).
      2. При настоящем конфликте (и фразы соискателя, и глаголы найма)
         ничья — соискатель: не написать дешевле, чем написать не тому.
    """
    t = (text or "").strip()
    head = t[:_HEAD]
    s = v = 0.0
    why: list = []

    s_first = bool(_S_FIRST.match(t))
    s_tag = bool(_S_TAG.search(t))
    s_phr = len(_S_STRONG.findall(head))
    if s_first:
        s += 4; why.append("пост начинается с «резюме»")
    if s_tag:
        s += 3; why.append("тег соискателя")
    if s_phr:
        # Вес равен глаголу найма: «ищу работу» против одного «требуется» —
        # ничья, а ничья по правилу 2 — соискатель.
        s += 2.0 * min(s_phr, 3); why.append("фразы соискателя ×%d" % s_phr)
    if _S_WEAK.search(head):
        s += 0.5; why.append("слабый признак соискателя")

    v_first = bool(_V_FIRST.match(t))
    v_hire = len(_V_HIRE.findall(head))
    v_struct = len(_V_STRUCT.findall(head))
    if v_first:
        v += 4; why.append("пост начинается с «вакансия»")
    if _V_TAG.search(t):
        v += 3; why.append("тег вакансии")
    if v_hire:
        v += 2.0 * min(v_hire, 3); why.append("глаголы найма ×%d" % v_hire)
    if v_struct:
        v += 1.0 * min(v_struct, 3); why.append("секции объявления ×%d" % v_struct)

    if s == 0 and v == 0:
        return PostKind(OTHER, s, v, why)
    if s == 0:
        return PostKind(VACANCY, s, v, why)
    seeker_core = s_first or s_phr > 0
    if not seeker_core:
        # Только метка/слабый признак. Найм подтверждён глаголом или первым
        # словом — вакансия; иначе метка остаётся последним словом автора.
        if v_first or v_hire:
            return PostKind(VACANCY, s, v, why + ["найм подтверждён, метка — ошибка автора"])
        return PostKind(SEEKER, s, v, why)
    if s >= v:
        return PostKind(SEEKER, s, v, why)
    return PostKind(VACANCY, s, v, why)


def is_seeker_post(text: str) -> bool:
    """Автор ищет работу, а не предлагает её. Таким не пишем никогда."""
    return classify_post(text).is_seeker
