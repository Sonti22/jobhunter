"""Классификатор входящих сообщений от работодателя.

Принцип — fail-closed: всё, в чём классификатор не уверен, уходит человеку.
Автоматически отвечаем только на узкий белый список рутины; техвопросы,
деньги и офферы — всегда к Сурену.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ── интенты ──
ASK_CV = "ask_cv"                 # «пришлите резюме»
ASK_CALL = "ask_call"             # «когда удобно созвониться»
SLOT_PROPOSED = "slot_proposed"   # предлагают конкретное время
ACK = "ack"                       # «спасибо, посмотрим»
ABOUT = "about"                   # «расскажите о себе»
TECH_QUESTION = "tech_question"   # технический вопрос → человек
MONEY = "money"                   # зарплата/ставка → человек
OFFER = "offer"                   # оффер → человек
REJECTION = "rejection"           # отказ
WORK_FORMAT = "work_format"       # «готовы в офис / переехать?» — ответ из профиля
UNKNOWN = "unknown"               # → человек

# ABOUT здесь условно: plan_reply отвечает сам ТОЛЬКО при заполненном
# faq.about_me в профиле, иначе эскалирует — см. reply.py.
AUTO_OK = {ASK_CV, ASK_CALL, ACK, ABOUT}

# Смелый режим (bold_autonomy): бот сам отвечает и на техвопросы, и на
# вопрос о формате работы. Оба ответа строятся ТОЛЬКО из фактов профиля и
# проходят гейт правды плюс самопроверку; при недоступной LLM автоответа
# нет — уходит карточка (fail-closed).
AUTO_OK_BOLD = AUTO_OK | {TECH_QUESTION, WORK_FORMAT}

# Деньги, оффер и предложенное время не автоматизируются НИКОГДА, при
# любых настройках. Прямое решение владельца, закреплено тестом.
ESCALATE_ALWAYS = {MONEY, OFFER, SLOT_PROPOSED}
ESCALATE = {TECH_QUESTION, MONEY, OFFER, UNKNOWN, SLOT_PROPOSED, WORK_FORMAT}


def auto_ok_set(bold: bool = False) -> set:
    return AUTO_OK_BOLD if bold else AUTO_OK

CONFIDENCE_MIN = 0.62

# Порог, с которого отказ вообще МОЖЕТ закрыть заявку (второй ярус — LLM —
# обязателен всё равно, см. engine). Отдельная константа, а не CONFIDENCE_MIN:
# закрытие необратимо, и его порог не должен ездить вместе с порогом рутины.
REJECTION_CLOSE_MIN = 0.9

_PATTERNS = [
    # \b перед rate — иначе «опыт» и «работали» ловятся как деньги
    # «rate» — только в денежном контексте: голое \brate\b превращало
    # техвопрос «how would you design a rate limiter» в разговор о деньгах.
    (MONEY, 0.95, r"(зарплат|вилк|оклад|ставк|доход|компенсац|salary|"
                  r"(?:hourly|day|daily|monthly|your)\s+rate\b|"
                  r"\brate\s+(?:expectation|range)|"
                  r"compensation|сколько\s+(?:вы\s+)?(?:хотите|ожидае)|"
                  r"ожидания\s+по\s+деньгам|\bnet\s+(?:salary|pay|income)\b|"
                  r"\bgross\b|на\s+руки|"
                  r"\bдене[гж]|финансов\w*\s+ожидан)"),
    (OFFER, 0.95, r"(оффер|offer|предлагаем\s+вам|готовы\s+сделать\s+предложение|"
                  r"выходить\s+на\s+работу|job\s+offer|мы\s+вас\s+берём)"),
    # EN-формулировки отказа собраны с реальных писем: «we went with another
    # candidate» без слова unfortunately проходило мимо, и заявка вечно
    # ждала ответа.
    (REJECTION, 0.9, r"(к сожалению|не готовы продолж|отказ|не подход|"
                     r"не\s+актуальн\w*|(?:вакансия|позиция|ваканси\w+)\s+"
                     r"(?:уже\s+)?(?:закрыт\w*|заполнен\w*)|"
                     # «приостанов» только рядом со словом про вакансию/набор:
                     # голая форма ловила «приостановка проекта не связана с
                     # вами, продолжаем» как отказ 0.90 — ровно порог
                     # необратимого закрытия.
                     r"на\s+стопе|на\s+паузе|"
                     r"(?:ваканси\w+|позици\w+|найм\w*|наём|рол[ьи]|hiring)"
                     r"[^.!?]{0,40}приостанов\w*|"
                     r"приостанов\w*[^.!?]{0,40}(?:ваканси\w+|позици\w+|найм\w*|наём)|"
                     r"набор\s+(?:на\s+[^.!?]{0,30})?(?:закрыт|приостанов)\w*|"
                     r"не\s+рассчитыва\w+\s+на\s+нас|"
                     r"no\s+longer\s+(?:open|available|active)|"
                     r"выбрали другого|unfortunately|not a fit|not moving forward|"
                     r"went\s+with\s+(?:another|other)\s+candidate|"
                     r"regret\s+to\s+inform|"
                     r"(?:position|role)\s+has\s+been\s+filled|"
                     r"(?:decided|chosen)\s+to\s+(?:move\s+forward|proceed)\s+with\s+(?:another|other)|"
                     r"will\s+not\s+be\s+moving\s+forward|"
                     r"pursue\s+other\s+candidates)"),
    (TECH_QUESTION, 0.85, r"(расскажите,?\s+как|как\s+бы\s+вы|тестовое|test\s*task|"
                          r"take-?home\s+(?:test|task|assignment)|"
                          r"coding\s+(?:challenge|test|assignment)|"
                          r"technical\s+assessment|"
                          r"how\s+(?:would|did)\s+you\s+(?:design|build|handle|approach)|"
                          r"walk\s+(?:me|us)\s+through|"
                          r"какой\s+опыт\s+(?:у\s+вас\s+)?(?:с|в)\b|"
                          r"работали\s+ли\s+вы\s+с|есть\s+ли\s+опыт|"
                          r"владеете\s+ли|знаете\s+ли\s+вы|"
                          r"почему\s+вы|объясните|"
                          r"\?\s*$.*(архитектур|алгоритм|sql|python|api|базе))"),
    # «Готовы работать в офисе?» — ответ известен и одинаков всегда:
    # владелец рассматривает только удалённый формат. Держим ПОСЛЕ
    # tech_question (там «как бы вы...») и ДО about.
    (WORK_FORMAT, 0.85, r"(готов\w*\s+(?:ли\s+)?(?:вы\s+)?(?:к\s+)?"
                        r"(?:переезд\w*|релокац\w*|работать\s+в\s+офис\w*)|"
                        r"мож(?:ете|ешь)\s+работать\s+в\s+офис\w*|"
                        r"рассматрива\w+\s+(?:ли\s+)?(?:вы\s+)?"
                        r"(?:офис|переезд|релокац)\w*|"
                        r"(?:у\s+нас\s+)?(?:только\s+)?офисн\w+\s+формат|"
                        r"are\s+you\s+(?:open\s+to|willing\s+to)\s+"
                        r"(?:relocat\w+|work\s+(?:from|in)\s+(?:the\s+)?office)|"
                        r"willing\s+to\s+relocate|"
                        r"can\s+you\s+work\s+(?:from|in)\s+(?:the\s+)?office)"),
    # До tech_question в списке нельзя: «расскажите, как вы строили API» —
    # это техвопрос, и он должен матчиться раньше. Здесь только «о себе».
    (ABOUT, 0.85, r"(расскажите\s+(?:немного\s+)?о\s+себе|"
                  r"о\s+(?:вашем|своём|своем)\s+опыте\s+в\s+целом|"
                  r"пару\s+слов\s+о\s+себе|"
                  r"tell\s+(?:me|us)\s+about\s+yourself)"),
    # Дни недели — только целыми словами. Хвост \w* после сокращения
    # превращал в предложение времени половину переписки: «**ср**оки» →
    # среда, «**чт**о» → четверг, «**сб**ор» → суббота, «**вс**ё» →
    # воскресенье, «**sat**isfied» → суббота, «**mon**itoring» →
    # понедельник. Из-за этого обе заявки «предложено интервью» оказались
    # ложными: одна из них — прямой отказ, принятый за приглашение.
    # Время с точкой — только после «в/к»: голое \d.\d{2} превращало
    # «Python 3.10», «версия 1.75» и «оплата 20.00» в предложение времени,
    # а engine безусловно двигал заявку в INTERVIEW_PROPOSED.
    (SLOT_PROPOSED, 0.8, r"(\b\d{1,2}:\d{2}\b|"
                         r"(?<=[вк]\s)\d{1,2}\.\d{2}\b|"
                         r"\b(?:пн|вт|ср|чт|пт|сб|вс)\.?(?=[\s,.;:!?)»]|$)|"
                         r"\b(?:понедельник\w{0,2}|вторник\w{0,2}|"
                         r"сред[ауые]|четверг\w{0,2}|пятниц[ауые]|"
                         r"суббот[ауые]|воскресень[еяю])\b|"
                         r"\b(?:завтра|послезавтра|сегодня)\b|"
                         r"\b(?:monday|tuesday|wednesday|thursday|friday|"
                         r"saturday|sunday)\b|"
                         r"\b(?:mon|tue|wed|thu|fri|sat|sun)\.|"
                         r"\b\d{1,2}\s*(?:am|pm)\b|"
                         r"\btomorrow\b|\bnext\s+week\b)"),
    # «Пришлите, пожалуйста, ваше резюме» — между глаголом и словом «резюме»
    # бывает вводное слово, поэтому допускаем произвольную вставку.
    (ASK_CV, 0.9, r"((?:пришлите|присыла\w+|присыл|скиньте|отправьте|вышлите|скинь|пришли|"
                  r"поделитесь|шарьте)[^.!?\n]{0,40}"
                  r"(?:резюме|cv\b|си-?ви)|"
                  r"(?:можно|есть|дайте|нужно|интересует)[^.!?\n]{0,25}резюме|"
                  r"резюме[^.!?\n]{0,20}(?:пришл|отправ|скин|есть\?)|"
                  r"send[^.!?\n]{0,30}(?:cv|resume)|share[^.!?\n]{0,20}(?:cv|resume)|"
                  r"(?:your|the)\s+(?:cv|resume)\s+please)"),
    (ASK_CALL, 0.8, r"(когда\s+(?:вам\s+)?удобно|давайте\s+созвон|"
                    r"готовы\s+пообщаться|назначим\s+звонок|"
                    r"можем\s+созвониться|schedule\s+a\s+call|"
                    r"when\s+are\s+you\s+available|интервью|\binterview\b|"
                    r"(?:book|set\s+up|hop\s+on|jump\s+on)[^.!?\n]{0,20}call|"
                    r"are\s+you\s+(?:free|available)|quick\s+(?:call|chat))"),
    # «принял/получил» с границей слова: без неё «у нас всё получилось с
    # другим кандидатом» (мягкий отказ) уходило в вежливость с автоответом.
    (ACK, 0.7, r"(спасибо|благодар|принял(?:а|и)?\b|получил(?:а|и)?\b|посмотрим|"
               r"на\s+связи|добро\b|хорошо,?\s+жду|"
               r"передам|thanks|thank you|received|noted)"),
]


@dataclass
class Intent:
    label: str
    confidence: float
    matched: str = ""

    @property
    def auto_reply_ok(self) -> bool:
        return self.label in AUTO_OK and self.confidence >= CONFIDENCE_MIN

    @property
    def needs_human(self) -> bool:
        return not self.auto_reply_ok


def classify(text: str) -> Intent:
    """Первое совпадение по приоритету списка. Ничего не совпало → человек."""
    t = (text or "").strip().lower()
    if not t:
        return Intent(UNKNOWN, 0.0)

    hits = []
    for label, conf, pat in _PATTERNS:
        m = re.search(pat, t, re.I | re.M)
        if m:
            hits.append((label, conf, m.group(0)[:60]))

    if not hits:
        return Intent(UNKNOWN, 0.0)

    # Эскалирующие интенты всегда побеждают: если в сообщении есть и «пришлите
    # резюме», и вопрос про зарплату — отвечает человек.
    labels_hit = {label for label, _, _ in hits}
    for label, conf, matched in hits:
        # SLOT_PROPOSED здесь обязателен: он в ESCALATE_ALWAYS, но по max(conf)
        # проигрывал work_format 0.85 — «можете в офис? созвонимся в среду в
        # 15:00» уходило в автоответ про формат, и предложенное время исчезало
        # без карточки.
        if label in (MONEY, OFFER, TECH_QUESTION, REJECTION, SLOT_PROPOSED):
            # «К сожалению, в четверг не получится, давайте в пятницу»:
            # маркеры отказа рядом с днём недели/временем — почти всегда
            # перенос, а не отказ. Уверенность режем вдвое, чтобы такое
            # никогда не добиралось до терминального закрытия и уходило
            # человеку (LLM-ярус может поднять обратно только явный отказ).
            if label == REJECTION and SLOT_PROPOSED in labels_hit:
                return Intent(REJECTION, conf * 0.5, matched)
            return Intent(label, conf, matched)

    label, conf, matched = max(hits, key=lambda h: h[1])
    # длинное сообщение с вопросительным знаком — почти всегда содержательный
    # вопрос, а не рутина; не рискуем
    if len(t) > 400 and "?" in t and label in AUTO_OK:
        return Intent(UNKNOWN, 0.4, matched)
    return Intent(label, conf, matched)
