"""Самопроверка текста перед отправкой человеку.

Зачем это отдельно от гейта. Гейт (tailor/gate.py) отвечает на вопрос
«не выдумано ли», сверяя термины, числа и годы с профилем. Он ничего не
знает о том, читается ли текст: письмо может быть безупречно правдивым и
при этом бессвязным, не отвечающим на вопрос или состоящим из общих слов.
Такое письмо тратит единственную попытку — рекрутёр не отвечает, и второго
шанса по этой вакансии не будет.

Поэтому второй проход: модель перечитывает собственный текст глазами
получателя и выносит вердикт. Важно, что проверяющий промпт не знает, что
текст написан ею же, — иначе оценка съезжает в «всё хорошо».

Правило безопасности: улучшенный вариант, если он предложен, — такой же
подозреваемый, как исходный. Он обязан пройти тот же анти-фабрикация гейт.
Иначе самопроверка превратилась бы в дыру, через которую выдумки попадают
в письмо в обход единственной защиты.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..llm import generate

# Сколько раз просить переписать, прежде чем звать владельца.
MAX_ATTEMPTS = 2


@dataclass
class Verdict:
    ok: bool = True
    reason: str = ""
    improved: str = ""
    checked: bool = False        # была ли проверка выполнена вообще

    @property
    def needs_owner(self) -> bool:
        return self.checked and not self.ok


PROMPT = """Ты — опытный IT-рекрутёр. Тебе пришло сообщение от кандидата.
Оцени его так, как оценил бы в реальной работе: ответишь ты на такое или нет.

ВАКАНСИЯ, ПО КОТОРОЙ ПИШУТ: {role}
ФРАГМЕНТ ОПИСАНИЯ ВАКАНСИИ:
{jd}

{context}

СООБЩЕНИЕ КАНДИДАТА:
---
{text}
---

Забракуй сообщение, если верно хотя бы одно:
- непонятно, о чём оно, или мысль обрывается
- не отвечает на заданный вопрос (если был вопрос)
- состоит из общих слов без конкретики по этой вакансии
- звучит как шаблон, разосланный веером
- есть противоречия внутри текста
- тон неуместный: развязный, заискивающий, требовательный
- обрезано на середине фразы, сломанное форматирование, мусорные символы

НЕ бракуй за:
- краткость, если мысль закончена
- сухой деловой тон
- отсутствие эмодзи и восклицаний
- то, что кандидат чего-то не умеет и честно об этом пишет

Ответь СТРОГО в формате JSON, без пояснений вокруг:
{{"ok": true|false, "reason": "одна фраза почему", "improved": ""}}

Если бракуешь — в поле improved напиши исправленный вариант того же
сообщения. Правила для него жёсткие: те же факты, что в оригинале, ни одного
нового навыка, числа, компании или года. Только формулировки. Если исправить,
не добавляя фактов, невозможно — оставь improved пустым."""


def _parse(raw: str) -> dict:
    """JSON из ответа модели. Модели любят обрамлять его текстом и ```."""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except ValueError:
        return {}


def review(text: str, role: str = "", jd_text: str = "",
           context: str = "", timeout: float = 25.0) -> Verdict:
    """Вердикт по одному тексту. Модель недоступна — проверки не было."""
    from ..config import get_settings

    if not (text or "").strip():
        return Verdict(False, "пустой текст", checked=True)
    if not get_settings().llm_enabled:
        # Без модели проверять нечем. Возвращаем «не проверено», а не «плохо»:
        # иначе выключенная LLM остановила бы всю рассылку.
        return Verdict(True, "LLM выключена", checked=False)

    prompt = PROMPT.format(role=role or "не указана",
                           jd=(jd_text or "")[:900],
                           context=context or "",
                           text=text)
    res = generate(prompt, timeout=timeout)
    if not res.ok or not res.text:
        return Verdict(True, res.error or "модель не ответила", checked=False)

    data = _parse(res.text)
    if not data:
        # Не разобрали ответ — это не приговор тексту.
        return Verdict(True, "не разобрал ответ проверяющего", checked=False)

    ok = bool(data.get("ok"))
    return Verdict(ok=ok, reason=str(data.get("reason", ""))[:200],
                   improved=str(data.get("improved", "")).strip()[:1200],
                   checked=True)


def review_with_retry(text: str, *, role: str = "", jd_text: str = "",
                      context: str = "", gate_check=None) -> tuple:
    """Проверить и, если нужно, переписать. Возвращает (текст, Verdict).

    gate_check — функция, проверяющая переписанный вариант на выдумки.
    Возвращает True, если вариант чист. Без неё улучшенный текст не
    принимается вовсе: пропустить его мимо анти-фабрикация проверки значит
    открыть обход единственной защиты от вранья в письме.
    """
    current = text
    last = Verdict(checked=False)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        last = review(current, role=role, jd_text=jd_text, context=context)
        if last.ok or not last.checked:
            return current, last
        if not last.improved:
            break
        if gate_check is not None and not gate_check(last.improved):
            # Переписанный вариант добавил фактов — берём предыдущий и идём
            # к владельцу: гейт важнее гладкости.
            last.reason = (last.reason + "; правка не прошла гейт").strip("; ")
            break
        current = last.improved
        if attempt == MAX_ATTEMPTS:
            last = review(current, role=role, jd_text=jd_text, context=context)
    return current, last
