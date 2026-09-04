"""Чтение публичной схемы формы отклика.

Подать заявку программно нельзя ни на одной из четырёх площадок: POST-ручка
везде требует ключ РАБОТОДАТЕЛЯ (Greenhouse — Job Board API key компании,
Lever — ключ Super Admin, Ashby — candidatesWrite, Workable — Bearer со
scope w_candidates). Пользоваться чужим ключом — несанкционированный
доступ, а headless-браузер прямо запрещён соглашением Greenhouse:
«use automated means, including spiders, robots, crawlers, or similar means
or processes to access or use the Services».

Поэтому подаёт заявку владелец. Система снимает с него всё остальное —
и вот тут Greenhouse неожиданно щедр: схему формы вместе со скрининговыми
вопросами он отдаёт публично, без авторизации, по `?questions=true`.
Проверено на живой вакансии: 15 вопросов, из них 8 обязательных, с полными
списками вариантов ответа.

Lever кастомные вопросы через API не публикует (сказано в их же доках),
Workable отдаёт только по токену — для них режим деградирует до «ссылка,
письмо и резюме», без предзаполнения.

Поправка по Ashby: его публичный Posting API принимает и подачу отклика
(application-form/submit, без ключа работодателя) — этим занимается
submit_ashby.py; здесь по-прежнему только чтение схемы.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

import httpx

# Поля, которые не заполняем никогда: самоидентификация по расе, полу,
# инвалидности и ветеранскому статусу. Это добровольные ответы о человеке,
# а не о его опыте — их даёт только сам человек.
NEVER_FILL_SECTIONS = ("demographic_questions", "compliance")

GREENHOUSE_URL = re.compile(
    r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)"
    r"(?:/jobs/|.*?gh_jid=)(\d+)", re.I)
ASHBY_URL = re.compile(r"jobs\.ashbyhq\.com/([\w%\-]+)/([\w-]+)", re.I)


@dataclass
class FormField:
    name: str                 # first_name | question_36101209002
    label: str
    type: str                 # input_text | input_file | multi_value_single_select
    required: bool = False
    values: list = field(default_factory=list)   # [(value, label), ...]

    @property
    def is_file(self) -> bool:
        return self.type == "input_file"

    @property
    def is_choice(self) -> bool:
        return bool(self.values)


@dataclass
class FormSpec:
    provider: str
    board: str
    job_id: str
    apply_url: str
    fields: list = field(default_factory=list)
    supported: bool = True
    note: str = ""

    def fingerprint(self) -> str:
        """Отпечаток схемы — чтобы не пересобирать пакет без причины.

        Считается по именам и типам полей, отсортированным: перестановка
        вопросов в форме не должна выглядеть как её изменение.
        """
        parts = sorted("%s:%s:%d" % (f.name, f.type, int(f.required))
                       for f in self.fields)
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]

    @property
    def required_fields(self) -> list:
        return [f for f in self.fields if f.required]


def form_ref_from_job(job) -> tuple | None:
    """(provider, board, job_id) из вакансии, или None.

    Смотрим и на external_uuid, и на ссылку: greenhouse-вакансии приходят
    не только из ats-источника, но и из careered и телеграм-каналов — по
    имени источника их не найти.
    """
    uid = (getattr(job, "external_uuid", "") or "")
    if uid.startswith("ats:"):
        parts = uid.split(":")
        if len(parts) >= 4 and parts[1] in ("greenhouse", "ashby"):
            return parts[1], parts[2], parts[3]

    url = (getattr(job, "contact_url", "") or "")
    m = GREENHOUSE_URL.search(url)
    if m:
        return "greenhouse", m.group(1).lower(), m.group(2)
    m = ASHBY_URL.search(url)
    if m:
        return "ashby", m.group(1).lower(), m.group(2)
    return None


def _greenhouse_form(board: str, job_id: str, http: httpx.Client,
                     timeout: float) -> FormSpec:
    url = ("https://boards-api.greenhouse.io/v1/boards/%s/jobs/%s"
           "?questions=true" % (board, job_id))
    r = http.get(url, timeout=timeout)
    r.raise_for_status()
    d = r.json()
    spec = FormSpec(provider="greenhouse", board=board, job_id=str(job_id),
                    apply_url=d.get("absolute_url", ""))
    for q in d.get("questions", []) or []:
        label = (q.get("label") or "").strip()
        required = bool(q.get("required"))
        fields = q.get("fields", []) or []
        # Вопрос с файловым полем — это вложение целиком: у Greenhouse
        # «Resume/CV» состоит из resume (файл) и resume_text (textarea),
        # и текстовый двойник просился у владельца отдельным вопросом.
        if any((f.get("type") or "") == "input_file" for f in fields):
            spec.fields.append(FormField(
                name=fields[0].get("name", ""), label=label,
                type="input_file", required=required))
            continue
        for f in fields:
            values = [(str(v.get("value")), str(v.get("label", "")))
                      for v in (f.get("values") or [])]
            spec.fields.append(FormField(
                name=f.get("name", ""), label=label,
                type=f.get("type", ""), required=required, values=values))
    return spec


def _ashby_form(board: str, job_id: str, http: httpx.Client,
                timeout: float) -> FormSpec:
    """Ashby: базовые поля есть, кастомные вопросы — не всегда."""
    url = ("https://api.ashbyhq.com/posting-api/job-board/%s?includeCompensation=true"
           % board)
    r = http.get(url, timeout=timeout)
    r.raise_for_status()
    job = next((j for j in (r.json().get("jobs") or [])
                if str(j.get("id")) == str(job_id)), None)
    spec = FormSpec(provider="ashby", board=board, job_id=str(job_id),
                    apply_url=(job or {}).get("jobUrl", ""),
                    note="кастомные вопросы Ashby публично не отдаёт")
    # Базовый набор, одинаковый у всех досок Ashby.
    for name, label, typ, req in (("_systemfield_name", "Full Name", "input_text", True),
                                  ("_systemfield_email", "Email", "input_text", True),
                                  ("_systemfield_phone", "Phone", "input_text", False),
                                  ("_systemfield_resume", "Resume", "input_file", True)):
        spec.fields.append(FormField(name=name, label=label, type=typ,
                                     required=req))
    return spec


def fetch_form(provider: str, board: str, job_id: str,
               http: httpx.Client | None = None,
               timeout: float | None = None) -> FormSpec:
    """Схема формы. Для lever/workable — supported=False с объяснением.

    timeout=None означает «таймаут чужого клиента»: бот передаёт клиент с
    коротким поводком 6 с, и явные 25 с здесь перебивали бы его — владелец
    смотрел бы на часики вместо ответа кнопки.
    """
    if provider in ("lever", "workable"):
        return FormSpec(provider=provider, board=board, job_id=str(job_id),
                        apply_url="", supported=False,
                        note=("%s не публикует схему формы — заполняется "
                              "вручную по ссылке" % provider))
    own = http is None
    if timeout is None:
        timeout = 25.0 if own else httpx.USE_CLIENT_DEFAULT
    http = http or httpx.Client(trust_env=False, follow_redirects=True,
                                headers={"User-Agent": "Mozilla/5.0"})
    try:
        if provider == "greenhouse":
            return _greenhouse_form(board, job_id, http, timeout)
        if provider == "ashby":
            return _ashby_form(board, job_id, http, timeout)
        return FormSpec(provider=provider, board=board, job_id=str(job_id),
                        apply_url="", supported=False,
                        note="провайдер неизвестен")
    finally:
        if own:
            http.close()
