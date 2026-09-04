"""Автоответы на рутинные входящие + эскалация всего остального.

Автоматически отвечаем только на: просьбу прислать резюме, вопрос «когда
удобно созвониться», простое подтверждение. Всё прочее — техвопросы,
зарплата, оффер, непонятное — уходит Сурену как NEEDS_HUMAN (защёлка).
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..models import Application, Status
from .classify import (
    ABOUT,
    ACK,
    ASK_CALL,
    ASK_CV,
    CONFIDENCE_MIN,
    ESCALATE_ALWAYS,
    TECH_QUESTION,
    WORK_FORMAT,
    Intent,
    auto_ok_set,
    classify,
)

OWNER_TZ = "Europe/Moscow"
MAX_AUTO_REPLIES = 4          # дальше — только человек (защита от пинг-понга
# Техответы считаются отдельно и НЕ съедают общий запас: два техвопроса
# подряд иначе выжигали бы весь лимит треда, и на «когда созвонимся»
# ответить было бы уже нечем.
MAX_AUTO_TECH_REPLIES = 2

CV_REPLIES = [
    "Конечно, прикрепляю резюме. Если удобнее в другом формате — скажите.",
    "Отправляю резюме. Готов ответить на вопросы по опыту.",
    "Держите резюме. Если нужно — пришлю англоязычную версию.",
]
CALL_REPLIES = [
    "Давайте созвонимся. Мне удобно {slots}. Подойдёт какой-то из вариантов?",
    "Готов пообщаться. Свободен {slots} — скажите, что удобнее вам.",
    "С удовольствием. Могу {slots}. Или предложите своё время.",
]
ACK_REPLIES = [
    "Спасибо! Буду ждать обратной связи.",
    "Понял, спасибо. Если понадобится что-то ещё — пишите.",
]

# Английские зеркала: выбор набора идёт по языку заявки (app.cv_lang),
# он же определил язык письма и резюме — рекрутёр не должен получать
# русский автоответ на английский тред.
CV_REPLIES_EN = [
    "Sure, attaching my CV. Happy to answer any questions about my experience.",
    "Here is my CV. Let me know if you need it in a different format.",
]
CALL_REPLIES_EN = [
    "Happy to talk. I'm available {slots} — does any of these work for you?",
    "Sounds good. I'm free {slots}, or feel free to suggest another time.",
]
# Формат работы: та же формулировка, что в холодных письмах, — владелец
# рассматривает только удалённый формат, страна значения не имеет.
FORMAT_REPLIES = [
    "Рассматриваю только удалённый формат — страна и часовой пояс не важны, "
    "работал в распределённых командах. Если у вас есть удалённые позиции, "
    "с радостью обсужу.",
    "Работаю только удалённо, к переезду и офису не готов. Английский C1, "
    "с распределёнными командами опыт есть — если формат подойдёт, "
    "буду рад продолжить.",
]
FORMAT_REPLIES_EN = [
    "I'm looking for remote-only roles — any country and time zone work for "
    "me, I've worked in distributed teams. Happy to continue if you have "
    "remote openings.",
    "I work remotely only and am not considering relocation or office work. "
    "English C1, experience in distributed teams — glad to talk if that fits.",
]

ACK_REPLIES_EN = [
    "Thank you! Looking forward to hearing from you.",
    "Got it, thanks. Let me know if you need anything else.",
]


@dataclass
class ReplyPlan:
    should_reply: bool
    text: str = ""
    # needs_draft — текст обязан написать LLM (шаблона нет);
    # needs_review — перед отправкой нужна пройденная самопроверка.
    needs_draft: bool = False
    needs_review: bool = False
    attach_cv: bool = False
    escalate: bool = False
    reason: str = ""
    intent: str = ""
    # Строка предложенных слотов — время считает КОД, и LLM-вариант ответа
    # обязан содержать её дословно (проверяется в draft_routine_reply).
    slots_line: str = ""


def _slots(tz_name: str = OWNER_TZ, count: int = 3,
           busy: list | None = None) -> list:
    """Ближайшие СВОБОДНЫЕ рабочие слоты в часовом поясе владельца.

    Время считаем через zoneinfo на дату слота — фиксированный офсет
    ошибается на час при переходе на летнее время.

    busy — занятые интервалы (наивный UTC, см. convo/busy.py): другие
    интервью и календарь. Без параметра занятость подтягивается сама;
    пустой список = «всё свободно» (нужно тестам и офлайн-режиму).
    Раньше занятость не проверялась вовсе — два рекрутёра могли получить
    один и тот же слот, что гарантировало неявку на один из созвонов.
    """
    if busy is None:
        from .busy import busy_intervals
        try:
            busy = busy_intervals()
        except Exception:                            # noqa: BLE001
            busy = []                                # fail-open
    from .busy import is_free

    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    out, day = [], now + timedelta(days=1)
    guard = 0
    while len(out) < count and guard < 30:           # максимум месяц вперёд
        guard += 1
        if day.weekday() < 5:                       # будни
            for hour in (11, 16):
                if len(out) >= count:
                    break
                cand = day.replace(hour=hour, minute=0, second=0, microsecond=0)
                cand_utc = cand.astimezone(timezone.utc).replace(tzinfo=None)
                if is_free(cand_utc, busy=busy):
                    out.append(cand)
        day += timedelta(days=1)
    return out


def _fmt_slots(slots: list, tz_name: str = OWNER_TZ, lang: str = "ru") -> str:
    if lang == "en":
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        tpl = "%s %d.%02d at %02d:%02d (UTC%+d)"
    else:
        days = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
        tpl = "%s %d.%02d в %02d:%02d (UTC%+d)"
    parts = []
    for s in slots:
        off = s.utcoffset()
        off_h = int(off.total_seconds() // 3600) if off else 0
        parts.append(tpl % (days[s.weekday()], s.day, s.month,
                            s.hour, s.minute, off_h))
    return "; ".join(parts)


def plan_reply(app: Application, incoming_text: str,
               rng: random.Random | None = None,
               intent: Intent | None = None,
               history: list | None = None,
               bold: bool | None = None) -> ReplyPlan:
    """Что делать с входящим сообщением.

    intent — готовая классификация, если вызывающий уже её уточнил (engine
    может поднять unknown до рутины вторым, LLM-ярусом). Не передана —
    классифицируем сами, как раньше. history — [(direction, body), ...] для
    проверки пинг-понга вежливости.
    """
    rng = rng or random.Random()
    intent = intent or classify(incoming_text)

    # Стоп пинг-понга: «спасибо» в ответ на наше же вежливое закрытие не
    # требует третьей реплики. Без этого два вежливых автомата обменивались
    # благодарностями до лимита, и весь запас автоответов треда уходил в шум.
    if (intent.label == ACK and history
            and "?" not in (incoming_text or "")
            and len((incoming_text or "").strip()) < 120):
        last_out = next((b for d, b in reversed(history) if d == "out"), "")
        if last_out and classify(last_out).label == ACK:
            return ReplyPlan(False, escalate=False, intent=intent.label,
                             reason="обмен вежливостью завершён, молчим")

    # защёлка: если тред уже отдан человеку — автоматика молчит
    if app.status == Status.NEEDS_HUMAN.value:
        return ReplyPlan(False, escalate=True, intent=intent.label,
                         reason="тред уже у человека")

    if (app.auto_replies_count or 0) >= MAX_AUTO_REPLIES:
        return ReplyPlan(False, escalate=True, intent=intent.label,
                         reason="лимит автоответов в треде")

    from ..config import get_settings
    bold = get_settings().bold_autonomy if bold is None else bold
    allowed = auto_ok_set(bold)

    # Деньги, оффер и слоты — владельцу при любых настройках.
    if intent.label in ESCALATE_ALWAYS:
        return ReplyPlan(False, escalate=True, intent=intent.label,
                         reason="решает владелец: %s" % intent.label)

    if intent.label not in allowed or intent.confidence < CONFIDENCE_MIN:
        return ReplyPlan(False, escalate=True, intent=intent.label,
                         reason="интент %s (уверенность %.2f)"
                                % (intent.label, intent.confidence))

    en = (getattr(app, "cv_lang", "") or "ru") == "en"
    if intent.label == ASK_CV:
        return ReplyPlan(True, rng.choice(CV_REPLIES_EN if en else CV_REPLIES),
                         attach_cv=True, intent=intent.label)
    if intent.label == ASK_CALL:
        slots = _slots()
        line = _fmt_slots(slots, lang="en" if en else "ru")
        text = rng.choice(CALL_REPLIES_EN if en else CALL_REPLIES).format(slots=line)
        return ReplyPlan(True, text, intent=intent.label, slots_line=line)
    if intent.label == ACK:
        return ReplyPlan(True, rng.choice(ACK_REPLIES_EN if en else ACK_REPLIES),
                         intent=intent.label)
    if intent.label == WORK_FORMAT:
        # Ответ известен жёстко и одинаков всегда: только удалённый формат.
        # Шаблон, а не LLM — выдумывать тут нечего, а формулировка уже
        # выверена и стоит в каждом холодном письме.
        return ReplyPlan(True, rng.choice(FORMAT_REPLIES_EN if en
                                          else FORMAT_REPLIES),
                         intent=intent.label)
    if intent.label == ABOUT:
        # Выверенный владельцем текст, если он есть. Пустой faq больше не
        # означает эскалацию: рассказать о себе можно строго по фактам
        # профиля, и за этим следят гейт и самопроверка.
        from ..profile import get_profile
        about = get_profile().faq_answer(
            "about_me", lang="en" if en else "ru")
        if about:
            return ReplyPlan(True, about, intent=intent.label)
        return ReplyPlan(True, "", intent=intent.label, needs_draft=True,
                         needs_review=True,
                         reason="о себе — черновиком по фактам профиля")
    if intent.label == TECH_QUESTION:
        if (getattr(app, "auto_tech_replies_count", 0) or 0) >=                 MAX_AUTO_TECH_REPLIES:
            return ReplyPlan(False, escalate=True, intent=intent.label,
                             reason="лимит техответов в треде")
        # Текста здесь нет и быть не может: ответ на технический вопрос
        # пишет LLM по фактам профиля. Шаблонного отката нет — значит при
        # любом сбое проверки уходит карточка, а не «что-нибудь».
        return ReplyPlan(True, "", intent=intent.label, needs_draft=True,
                         needs_review=True,
                         reason="техвопрос — черновиком по фактам профиля")

    return ReplyPlan(False, escalate=True, intent=intent.label,
                     reason="нет шаблона")


def reply_delay_seconds(rng: random.Random | None = None) -> float:
    """Мгновенный ответ в 02:00 с машинной точностью — очевидный бот."""
    rng = rng or random.Random()
    return rng.uniform(40, 8 * 60)


def within_reply_window(tz_name: str = OWNER_TZ) -> bool:
    h = datetime.now(ZoneInfo(tz_name)).hour
    return 9 <= h < 21
