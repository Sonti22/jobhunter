"""Вторая пара глаз для опасных решений классификатора.

Регэксный классификатор быстр и предсказуем, но не видит контекста. Его
цена ошибки неравномерна: перепутать «спасибо» с «получили» безвредно, а
принять «в четверг не получится, давайте в пятницу» за отказ — значит
терминально закрыть заявку, по которой рекрутёр только что предложил
перенос. Такая ошибка уже была найдена на живых паттернах: «к сожалению»
и «не подходит» дают отказу уверенность 0.9 при любом продолжении фразы.

Поэтому LLM подключается ровно в двух случаях — regex сказал «отказ» или
«не понял» — и только как проверяющий, не как решающий. Решение принимает
детерминированный код в engine.py, и закрыть заявку он может лишь при
согласии ОБОИХ ярусов. Любой сбой модели (выключена, таймаут, мусор в
ответе) возвращает None, и engine трактует это как «не подтверждено» —
заявка уходит человеку, а не в терминал. Fail-open в сторону владельца.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..llm import generate
from . import classify as C

# Метки, которые модель имеет право вернуть. Всё вне списка — мусор,
# приравнивается к сбою.
_ALLOWED = {C.ASK_CV, C.ASK_CALL, C.SLOT_PROPOSED, C.ACK, C.TECH_QUESTION,
            C.MONEY, C.OFFER, C.REJECTION, C.UNKNOWN}


@dataclass
class Verified:
    label: str
    confidence: float
    reason: str = ""


PROMPT = """Ты — ассистент, разбирающий переписку кандидата с рекрутёром.
Определи, что рекрутёр имел в виду в ПОСЛЕДНЕМ сообщении.

ПЕРЕПИСКА ДО ЭТОГО:
{history}

ПОСЛЕДНЕЕ СООБЩЕНИЕ РЕКРУТЁРА:
---
{text}
---

Метки (выбери РОВНО одну):
- rejection      — окончательный отказ кандидату по этой вакансии
- slot_proposed  — предлагает или переносит время созвона/интервью
- ask_cv         — просит прислать резюме
- ask_call       — предлагает созвониться, время не названо
- ack            — вежливое подтверждение без вопроса («спасибо, посмотрим»)
- tech_question  — содержательный вопрос об опыте или технологиях
- money          — вопрос о зарплатных ожиданиях
- offer          — предложение о работе
- unknown        — ничего из перечисленного

ВАЖНО про rejection: «в четверг не получится», «к сожалению, придётся
перенести» — это НЕ отказ, это перенос (slot_proposed или ask_call).
Отказ — только когда по смыслу закрыта сама вакансия для кандидата:
«выбрали другого», «не готовы продолжать», «ваш профиль не подходит».
Сомневаешься между rejection и чем-то ещё — выбирай что-то ещё.

Ответь СТРОГО JSON без текста вокруг:
{{"label": "...", "confidence": 0.0-1.0, "reason": "одна фраза"}}"""


def _parse(raw: str) -> dict:
    """JSON из ответа модели — она любит обрамлять его ``` и текстом."""
    text = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.M)
    m = re.search(r"\{.*\}", text.strip(), re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except ValueError:
        return {}


def verify_intent(text: str, history: list, regex_label: str,
                  timeout: float = 20.0) -> Verified | None:
    """Второе мнение по сообщению. None — мнения нет, решает человек.

    history — [(direction, body), ...], как в draft_reply.
    Таймаут короче обычного: вызов живёт в однопоточном tg-пуле, где за ним
    в очереди стоит вся отправка.
    """
    from ..config import get_settings
    s = get_settings()
    if not (s.llm_enabled and s.llm_classify_enabled):
        return None
    if not (text or "").strip():
        return None

    from .draft import _history_block
    prompt = PROMPT.format(history=_history_block(history) or "(переписки не было)",
                           text=text.strip()[:1500])
    res = generate(prompt, timeout=timeout)
    if not res.ok or not res.text:
        return None

    data = _parse(res.text)
    label = str(data.get("label", "")).strip().lower()
    if label not in _ALLOWED:
        return None
    try:
        conf = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        return None
    return Verified(label, conf, str(data.get("reason", ""))[:200])
