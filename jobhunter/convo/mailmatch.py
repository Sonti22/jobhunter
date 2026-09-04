"""Чьё это письмо: привязка входящего к заявке.

Функции здесь чистые — никакого IO и никакой сессии БД. Весь контекст
приходит готовым в MatchContext, собранным одним запросом в начале прохода.
Отсюда два следствия: правила проверяются юнит-тестами без сети, и легко
доказать главное свойство системы — что тело постороннего письма не
скачивается (need_body остаётся False).

Порядок правил — по убыванию надёжности. Первое сработавшее выигрывает, его
имя сохраняется в Message.match_rule: без этого разобрать ложную привязку
через месяц невозможно.

  msgid     наш Message-ID в In-Reply-To/References — переживает ответ с
            чужого адреса, но требует, чтобы мы сами его проставили
  plus      адрес suren6pro+jh42xSIG@ в To/Cc/Delivered-To
  alias     From совпал с адресом, с которого уже отвечали по этой заявке
  employer  From → работодатель → его живые заявки
  reftoken  plus-адрес найден в ТЕЛЕ (пересылка, «ответ» новым письмом)
  subject   уточнение, когда заявок к работодателю несколько
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..textutil import norm, similarity

# Автоматические письма: отвечать на них нельзя ни при каких условиях.
# Без этого фильтра автоответчик «я в отпуске» и наш бот переписываются
# между собой до исчерпания дневного лимита.
AUTO_LOCAL = re.compile(
    r"^(?:no-?reply|noreply|donotreply|mailer-daemon|postmaster|bounce|"
    r"notification|notifications|automated|auto-confirm)\b", re.I)
AUTO_SUBJECT = re.compile(
    r"(?:out\s+of\s+office|automatic\s+reply|autoreply|"
    r"авто-?ответ|в\s+отпуске|отсутству\w+\s+в\s+офисе|"
    r"undelivered|delivery\s+status|не\s+доставлено)", re.I)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
MSGID_RE = re.compile(r"<[^<>@\s]+@[^<>@\s]+>")
SUBJ_PREFIX = re.compile(
    r"^\s*(?:re|fwd?|ответ|пересылка|перенаправлено)\s*(?:\[\d+\])?\s*:\s*",
    re.I)


@dataclass
class MatchContext:
    """Снимок состояния БД на начало прохода."""
    by_msgid: dict = field(default_factory=dict)     # message-id -> app_id
    by_peer: dict = field(default_factory=dict)      # адрес -> app_id
    by_employer: dict = field(default_factory=dict)  # адрес -> [app_id]
    subjects: dict = field(default_factory=dict)     # app_id -> тема отклика
    known_domains: set = field(default_factory=set)


@dataclass
class Candidate:
    app_id: int = 0
    rule: str = ""
    need_body: bool = False       # можно ли скачивать тело
    ambiguous: list = field(default_factory=list)
    drop_reason: str = ""         # непусто — письмо отбрасываем молча

    @property
    def matched(self) -> bool:
        return bool(self.app_id)


def addr_of(raw: str) -> str:
    """Первый адрес из заголовка. «Имя <a@b>» → «a@b»."""
    m = EMAIL_RE.search(raw or "")
    return m.group(0).lower() if m else ""


def all_addrs(*values) -> list:
    out = []
    for v in values:
        out.extend(x.lower() for x in EMAIL_RE.findall(v or ""))
    return out


def norm_subject(raw: str) -> str:
    """Тема без префиксов Re:/Fwd:/Ответ: и любой их вложенности."""
    s = raw or ""
    for _ in range(5):
        stripped = SUBJ_PREFIX.sub("", s)
        if stripped == s:
            break
        s = stripped
    return s.strip()


def is_automated(headers: dict) -> str:
    """Причина отбросить письмо как автоматическое. Пусто — живое письмо."""
    auto = (headers.get("auto-submitted") or "").strip().lower()
    if auto and auto != "no":
        return "auto-submitted"
    prec = (headers.get("precedence") or "").strip().lower()
    if prec in ("bulk", "list", "junk", "auto_reply"):
        return "precedence:%s" % prec
    if headers.get("x-autoreply"):
        return "x-autoreply"
    # Рассылки: LinkedIn, HH, вакансионные дайджесты.
    if headers.get("list-id") or headers.get("list-unsubscribe"):
        return "рассылка"
    # Пустой обратный путь — отчёт о недоставке или автоответчик.
    if (headers.get("return-path") or "").strip() in ("<>", ""):
        if headers.get("return-path") is not None:
            return "return-path пуст"
    sender = addr_of(headers.get("from", ""))
    if sender and AUTO_LOCAL.match(sender.split("@")[0]):
        return "адрес автоматики"
    if AUTO_SUBJECT.search(headers.get("subject", "") or ""):
        return "тема автоответа"
    return ""


def match_by_headers(headers: dict, ctx: MatchContext,
                     parse_plus=None) -> Candidate:
    """Привязка по одним заголовкам. Тело при этом не скачано."""
    drop = is_automated(headers)
    if drop:
        return Candidate(drop_reason=drop)

    # R1: наш Message-ID в цепочке ответа. Проверяем ВЕСЬ References, а не
    # только In-Reply-To: при пересылке In-Reply-To указывает на письмо
    # пересылающего, а наш идентификатор остаётся в хвосте цепочки.
    chain = MSGID_RE.findall((headers.get("in-reply-to", "") or "") + " "
                             + (headers.get("references", "") or ""))
    for mid in reversed(chain):
        app_id = ctx.by_msgid.get(mid)
        if app_id:
            return Candidate(app_id, "msgid", True)

    # R2: plus-адрес в получателях.
    if parse_plus:
        for value in all_addrs(headers.get("to"), headers.get("cc"),
                               headers.get("delivered-to"),
                               headers.get("x-original-to")):
            app_id = parse_plus(value)
            if app_id:
                return Candidate(app_id, "plus", True)

    sender = addr_of(headers.get("from", ""))

    # R3: с этого адреса нам уже отвечали по конкретной заявке.
    if sender and sender in ctx.by_peer:
        return Candidate(ctx.by_peer[sender], "alias", True)

    # R4: адрес принадлежит работодателю, которому мы писали.
    apps = list(ctx.by_employer.get(sender, [])) if sender else []
    if len(apps) == 1:
        return Candidate(apps[0], "employer", True)
    if len(apps) > 1:
        # R6: несколько заявок в одну компанию — разбираем по теме.
        want = norm(norm_subject(headers.get("subject", "")))
        scored = [(similarity(want, norm(ctx.subjects.get(a, ""))), a)
                  for a in apps]
        scored.sort(reverse=True)
        if scored and scored[0][0] >= 0.85:
            return Candidate(scored[0][1], "employer+subject", True)
        # Гадать нельзя: цена ошибки — подтверждение интервью не по той
        # вакансии. Отдаём неоднозначность владельцу.
        return Candidate(0, "ambiguous", False, ambiguous=apps)

    # Тело качаем ещё в одном случае: домен знакомый, но адрес новый —
    # там может лежать plus-адрес в цитате (правило reftoken).
    domain = sender.split("@")[-1] if "@" in sender else ""
    if domain and domain in ctx.known_domains:
        return Candidate(0, "", True)
    return Candidate(0, "", False)


def match_by_body(body: str, ctx: MatchContext, parse_plus=None) -> Candidate:
    """Последняя попытка: plus-адрес в теле письма.

    Ищется в СЫРОМ тексте до очистки цитат — именно там он и живёт, когда
    письмо переслали или ответили «новым письмом» без заголовков.
    """
    if parse_plus:
        app_id = parse_plus(body or "")
        if app_id:
            return Candidate(app_id, "reftoken", True)
    return Candidate()
