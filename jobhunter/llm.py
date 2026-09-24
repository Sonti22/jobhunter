"""Клиент бесплатных LLM API с фолбэком между провайдерами.

Роль LLM в системе: писать ЖИВОЙ текст (саммари резюме, сопроводительное)
из фактов, которые ей дают. Придумывать факты она не может — всё, что она
вернёт, проходит тот же анти-фабрикация гейт. Не прошло → берём шаблон.

То есть LLM отвечает за стиль, гейт — за правду. Один без другого не работает:
шаблон без LLM звучит роботом, LLM без гейта уверенно сочиняет опыт.

Провайдеры (по убыванию приоритета), все с бесплатным тиром:
  gemini      1500 запросов/день, лучшее качество на русском
  groq        1000/день, самый быстрый
  openrouter  50/день без депозита, много моделей
  cerebras    быстрый, Llama

Ключи — в .env. Нет ключей → всё работает на шаблонах, просто суше.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from .config import get_settings


@dataclass
class LLMResult:
    text: str
    provider: str = ""
    ok: bool = True
    error: str = ""


class LLMUnavailable(RuntimeError):
    pass


# ── провайдеры ───────────────────────────────────────────────────────────

def _gemini(prompt: str, key: str, model: str, timeout: float) -> str:
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "%s:generateContent" % model)
    r = httpx.post(url, params={"key": key}, timeout=timeout, trust_env=False,
                   json={"contents": [{"parts": [{"text": prompt}]}],
                         "generationConfig": {"temperature": 0.7,
                                              "maxOutputTokens": 700}})
    r.raise_for_status()
    d = r.json()
    return d["candidates"][0]["content"]["parts"][0]["text"].strip()


def _openai_compatible(prompt: str, key: str, model: str, base: str,
                       timeout: float) -> str:
    r = httpx.post(base + "/chat/completions", timeout=timeout, trust_env=False,
                   headers={"Authorization": "Bearer " + key,
                            "Content-Type": "application/json"},
                   json={"model": model,
                         "messages": [{"role": "user", "content": prompt}],
                         "temperature": 0.7, "max_tokens": 700})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


# Порядок = приоритет. Groq первым: он единственный отвечает быстро с
# российского IP. Gemini последним — Google блокирует регион
# (FAILED_PRECONDITION: User location is not supported), ключ пригодится
# только с VPN, выходящим в ЕС/США.
PROVIDERS = [
    ("groq", lambda p, k, s, t: _openai_compatible(
        p, k, s.llm_groq_model, "https://api.groq.com/openai/v1", t)),
    ("openrouter", lambda p, k, s, t: _openai_compatible(
        p, k, s.llm_openrouter_model, "https://openrouter.ai/api/v1", t)),
    ("gemini", lambda p, k, s, t: _gemini(p, k, s.llm_gemini_model, t)),
    ("cerebras", lambda p, k, s, t: _openai_compatible(
        p, k, s.llm_cerebras_model, "https://api.cerebras.ai/v1", t)),
    # Mistral La Plateforme: самый большой бесплатный объём среди всех —
    # миллиард токенов в месяц. Ограничение 2 запроса/мин, поэтому он
    # запасной, а не первый: при штормах провайдеры выше умирают быстрее,
    # чем этот успеет отвечать.
    ("mistral", lambda p, k, s, t: _openai_compatible(
        p, k, s.llm_mistral_model, "https://api.mistral.ai/v1", t)),
    # GitHub Models: бесплатно по обычному GitHub-токену (PAT с правом
    # models:read), под капотом Azure — модели уровня GPT-4o.
    ("github", lambda p, k, s, t: _openai_compatible(
        p, k, s.llm_github_model,
        "https://models.github.ai/inference", t)),
]


def _key_for(provider: str, s) -> str:
    return {
        "gemini": s.gemini_api_key,
        "groq": s.groq_api_key,
        "openrouter": s.openrouter_api_key,
        "cerebras": s.cerebras_api_key,
        "mistral": s.mistral_api_key,
        "github": s.github_models_token,
    }.get(provider, "")


def available() -> list:
    """Провайдеры, для которых задан ключ."""
    s = get_settings()
    allow = {x.strip() for x in (s.llm_providers or "").split(",") if x.strip()}
    return [name for name, _ in PROVIDERS
            if _key_for(name, s) and (not allow or name in allow)]


_PII_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,24}\b", re.I)
# Кандидаты в телефоны: цифры с разделителями (включая точки и юникодные
# дефисы — «8.999.123.45.67» раньше уходил в API нередактированным).
_PII_PHONE = re.compile(r"(?<!\w)\+?\d[\d ().\-‐-―]{8,}\d(?!\w)")
# Группировка тысяч «300 000 - 350 000»: зарплатная вилка, не телефон.
_THOUSANDS = re.compile(r"^\d{1,3}([ .]\d{3})+([ .\-‐-―]+"
                        r"\d{1,3}([ .]\d{3})+)?$")


def _phone_like(m: "re.Match") -> str:
    """Телефон против зарплатной вилки и прочих больших чисел.

    Регэкс, съедавший «300 000 - 350 000», ломал ответы о деньгах: черновик
    просит модель назвать цифру дословно, а цифры в промпте уже нет.
    """
    tok = m.group(0)
    if _THOUSANDS.match(tok.strip()):
        return tok                      # группы тысяч — деньги, не телефон
    digits = re.sub(r"\D", "", tok)
    if not 10 <= len(digits) <= 15:
        return tok
    return "<PHONE>"


def redact_pii(prompt: str) -> str:
    """Убрать email/телефон из prompt перед отправкой во внешний API."""
    clean = _PII_EMAIL.sub("<EMAIL>", prompt or "")
    return _PII_PHONE.sub(_phone_like, clean)


def generate(prompt: str, timeout: float = 30.0) -> LLMResult:
    """Первый работающий провайдер. Без ключей — LLMResult(ok=False)."""
    s = get_settings()
    allow = {x.strip() for x in (s.llm_providers or "").split(",") if x.strip()}
    request_prompt = redact_pii(prompt) if s.llm_redact_pii else prompt
    order = [p for p in PROVIDERS
             if _key_for(p[0], s) and (not allow or p[0] in allow)]
    if not order:
        return LLMResult("", ok=False, error="нет ключей LLM в .env")

    errors = []
    for name, fn in order:
        try:
            text = fn(request_prompt, _key_for(name, s), s, timeout)
            if text:
                return LLMResult(text, provider=name)
            errors.append("%s: пустой ответ" % name)
        except httpx.HTTPStatusError as e:
            errors.append("%s: HTTP %s" % (name, e.response.status_code))
        except Exception as e:
            errors.append("%s: %s" % (name, type(e).__name__))
    # В файл логов, а не только в возвращаемое значение: вызывающие печатают
    # ошибку в stdout контейнера, и «все провайдеры лежат» не оставляло
    # следа в autopilot.log — историю инцидента было не восстановить.
    import logging
    logging.getLogger("llm").warning("все провайдеры не ответили: %s",
                                     "; ".join(errors))
    return LLMResult("", ok=False, error="; ".join(errors))


# ── промпты ──────────────────────────────────────────────────────────────

FACT_RULES = """
ЖЁСТКИЕ ПРАВИЛА (нарушение = текст будет отклонён автоматической проверкой):
1. Используй ТОЛЬКО факты из блока ФАКТЫ. Ничего не додумывай.
2. НЕ упоминай технологии, которых нет в списке РАЗРЕШЁННЫЕ ТЕХНОЛОГИИ.
3. НИКОГДА не упоминай: {forbidden}
4. Не выдумывай числа: проценты, метрики, размеры команд, сроки.
5. Не заявляй уровень выше, чем указан (не пиши «эксперт», если стоит «работал с»).
6. Пиши по-русски, если вакансия на русском; по-английски, если на английском.
7. Без воды, без «динамичный», «стрессоустойчивый», без восклицаний.
8. НЕ цитируй требования про СТАЖ («опыт от 5 лет», «3+ years experience»)
   и не подтверждай их. Цитируй только требования про ЗАДАЧИ и ТЕХНОЛОГИИ.
   Иначе получается ложное заявление о годах, которых нет.
9. Числа лет бери ТОЛЬКО из блока ФАКТЫ. Если в ФАКТАХ «Django (working, 3 г.)»,
   писать «5 лет Django» нельзя ни при каких требованиях вакансии.
"""


def prompt_message(role: str, jd_excerpt: str, facts: str, allowed: str,
                   forbidden: str, source: str, max_chars: int = 400,
                   safe_quote: str = "", lang: str = "ru") -> str:
    # Цитату отбирает код, а не модель: она уже проверена на подтверждаемость
    # профилем (не про стаж, не про тип компании, содержит знакомый термин).
    # Пусто — значит подтверждать в этой вакансии нечего, и письмо обязано
    # обойтись без кавычек вовсе.
    if safe_quote:
        quote_rule = ("- третья фраза: процитируй ДОСЛОВНО и ТОЛЬКО эту строку "
                      "требования в кавычках «%s» и скажи, что с этим работал. "
                      "Любые другие цитаты запрещены." % safe_quote)
    else:
        quote_rule = ("- НЕ цитируй требования вакансии вообще: подтверждать в "
                      "ней нечего. Кавычек в тексте быть не должно.")
    # Инструкции остаются русскими для обоих языков (модель им следует), но
    # для EN язык ТЕКСТА задаётся жёстко и первой строкой формата: мягкую
    # просьбу в середине промпта модели регулярно игнорируют.
    lang_rule = ("- весь текст сообщения — СТРОГО ПО-АНГЛИЙСКИ, ни одного "
                 "русского слова; условие кандидата формулируй как "
                 "remote-only (any country), English C2\n"
                 if lang == "en" else "")
    return f"""Ты помогаешь инженеру написать короткое сопроводительное сообщение
рекрутёру в Telegram по конкретной вакансии.

ВАКАНСИЯ: {role}
ОТКУДА: {source}
ФРАГМЕНТ ОПИСАНИЯ:
{jd_excerpt[:1200]}

ФАКТЫ О КАНДИДАТЕ (только это можно использовать):
{facts}

РАЗРЕШЁННЫЕ ТЕХНОЛОГИИ: {allowed}

{FACT_RULES.format(forbidden=forbidden)}

ФОРМАТ (соблюдай точно):
{lang_rule}- длина 280-{max_chars} знаков. Короче 280 — плохо, это выглядит отпиской.
- первая фраза: по какой вакансии пишешь и откуда узнал
- вторая: конкретный опыт под ЭТУ вакансию — назови 3-4 технологии из
  РАЗРЕШЁННЫХ, которые реально требуются в описании выше
{quote_rule}
- предпоследняя: ОБЯЗАТЕЛЬНО скажи формат кандидата — удалённо, гибрид или
  офис в Москве (к переезду не готов) — и английский C2. Это условие
  кандидата, без него письмо бессмысленно.
- последняя: вопрос об актуальности или предложение созвониться
- пиши от первого лица, по-деловому, без «я заинтересовался» и «хотел бы узнать»
- никаких ссылок, эмодзи, форматирования, списков

Верни ТОЛЬКО текст сообщения, без пояснений и кавычек."""


def prompt_summary(role: str, jd_excerpt: str, facts: str, allowed: str,
                   forbidden: str, lang: str = "ru") -> str:
    lang_line = ("Пиши по-русски." if lang == "ru" else "Write in English.")
    return f"""Напиши раздел «О себе» для резюме под конкретную вакансию.

ВАКАНСИЯ: {role}
ФРАГМЕНТ ОПИСАНИЯ:
{jd_excerpt[:1200]}

ФАКТЫ О КАНДИДАТЕ (только это можно использовать):
{facts}

РАЗРЕШЁННЫЕ ТЕХНОЛОГИИ: {allowed}

{FACT_RULES.format(forbidden=forbidden)}

ФОРМАТ: 3-4 предложения, 350-550 знаков. {lang_line}
Начни с сути опыта, затем — чем именно полезен на ЭТОЙ позиции.
Верни ТОЛЬКО текст, без заголовка и кавычек."""
