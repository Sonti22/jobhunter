"""Общий контракт источника вакансий + запись в БД.

Каждый источник (careered, Telegram-каналы, HN) отдаёт поток JobRecord;
сохранение, дедуп и создание Application — общий код здесь.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import select

from ..db import session_scope
from ..models import Application, ContactKind, Employer, Job, Status, utcnow
from ..textutil import norm_hash, norm_keep_digits

# t.me/<handle>, @handle, tg://resolve?domain=<handle>
_TME = re.compile(r"(?:https?://)?t(?:elegram)?\.me/(?P<h>[^/?#\s\)\]]+)", re.I)
_AT = re.compile(r"(?<![\w@/])@([A-Za-z][A-Za-z0-9_]{3,31})\b")
# Точка после TLD разрешена — «пришлите на hr@acme.com.» частый случай;
# мусорные домены отсекает _valid_email по длине TLD.
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?"
                    r"\.[A-Za-z]{2,24}(?![A-Za-z0-9])")
# Обфускация из HN: "name [at] company [dot] com". Разделители ТОЛЬКО в скобках
# или отдельными словами — иначе «cross-platform. For» читается как адрес.
#
# Два паттерна вместо одного. Комбинация «at-слово + обычная точка» была
# самой опасной дырой экстрактора: «Learn more at jobhunter.io» давал
# more@jobhunter.io, «Apply at acme.com» — Apply@acme.com, и по этим
# сфабрикованным адресам реально уходили письма (найдено аудитом, проверено
# исполнением). Правило: хотя бы один из разделителей обязан быть
# «настоящей» обфускацией — в скобках или словом dot. Голое «слово at слово
# точка tld» — это просто английская фраза.
_OBF_AT = r"(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\})"
_OBF_DOT = r"(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\s+dot\s+)"
_EMAIL_OBF = re.compile(
    # ветка 1: at в скобках — точка в домене допустима
    r"([A-Za-z0-9._%+-]+)\s*" + _OBF_AT + r"\s*"
    r"([A-Za-z0-9-]+(?:\s*" + _OBF_DOT + r"\s*[A-Za-z0-9-]+|\.[A-Za-z0-9-]+)*)"
    r"\s*(?:" + _OBF_DOT + r"|\.)\s*([A-Za-z]{2,12})\b"
    r"|"
    # ветка 2: at словом (или «собака») — тогда точки обязаны быть словом dot
    r"([A-Za-z0-9._%+-]+)\s+(?:at|собака)\s+"
    r"([A-Za-z0-9-]+(?:\s*" + _OBF_DOT + r"\s*[A-Za-z0-9-]+)*)"
    r"\s*" + _OBF_DOT + r"\s*([A-Za-z]{2,12})\b", re.I)

# TLD длиннее 6 букв встречается редко — принимаем только известные.
_LONG_TLD_OK = {
    "health", "online", "agency", "capital", "digital", "systems", "network",
    "ventures", "software", "solutions", "technology", "engineering", "consulting",
    "community", "academy", "finance", "institute", "international", "management",
}


def _valid_email(addr: str) -> bool:
    """Отсекает адреса, собранные из случайных слов текста."""
    if not addr or addr.count("@") != 1:
        return False
    local, _, domain = addr.partition("@")
    if not local or "." not in domain:
        return False
    raw_tld = domain.rsplit(".", 1)[-1]
    tld = raw_tld.lower()
    if not tld.isalpha() or len(tld) < 2:
        return False
    # «Cogram@scale.We» — TLD с заглавной = начало следующего слова, не домен.
    # Реальные TLD пишут строчными (или целиком капсом в CAPS-тексте).
    if raw_tld[0].isupper() and not raw_tld.isupper():
        return False
    return len(tld) <= 6 or tld in _LONG_TLD_OK

# Юзернеймы, которые НЕ являются контактом работодателя.
HANDLE_STOPLIST = {
    "share", "joinchat", "addstickers", "proxy", "socks", "iv", "s",
    "telegram", "durov", "channel", "c",
    # Почтовые домены: голый regex «@слово» вытаскивает @gmail из адреса
    # ivan@gmail.com и записывает его как контакт рекрутёра.
    "gmail", "yandex", "mail", "outlook", "hotmail", "icloud", "yahoo",
    "list", "bk", "inbox", "rambler", "protonmail", "proton", "ya",
    "company", "example", "domain",
}
# Служебные/рекламные хвосты каналов — не контакт вакансии.
CHANNEL_ADMIN_HINT = re.compile(
    r"(реклам|сотрудничеств|прайс|разместить|admin|админ|по вопросам размещ)", re.I)


def parse_ts(value) -> int:
    """Дата публикации из чего угодно → unix-время. Не разобрали — 0.

    Источники отдают дату по-разному: Greenhouse и Ashby — ISO-8601 со сдвигом,
    Lever — миллисекунды эпохи, Workable — просто «2026-01-15», Telegram —
    ISO из атрибута <time datetime>. Разбираем всё одним местом, потому что
    молча потерянная дата стоит дорого: без неё фильтр свежести не работает,
    и отклики уходят в вакансии, закрытые месяц назад.
    """
    if not value:
        return 0
    if isinstance(value, (int, float)):
        n = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        n = int(value.strip())
    else:
        s = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y"):
                try:
                    dt = datetime.strptime(s[:10], fmt)
                    break
                except ValueError:
                    continue
            else:
                return 0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        n = int(dt.timestamp())

    # Миллисекунды (Lever) — приводим к секундам. Порог: 1e11 секунд это 5138 год.
    if n > 100_000_000_000:
        n //= 1000
    # Отсекаем мусор: до 2000 года и «из будущего» дальше суток.
    now = int(datetime.now(timezone.utc).timestamp())
    if n < 946_684_800 or n > now + 86_400:
        return 0
    return n


@dataclass
class RawJob:
    """Сырая вакансия из любого источника, до записи в БД."""
    source: str
    external_uuid: str
    title: str = ""
    company: str = ""
    tag: str = ""
    content: str = ""
    mode: str = "full"
    posted_at: int = 0
    salary_raw: str = ""
    contact_kind: str = ContactKind.UNKNOWN.value
    contact_handle: str = ""
    contact_url: str = ""
    contact_email: str = ""
    all_links: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def has_direct_contact(self) -> bool:
        return bool(
            (self.contact_kind == ContactKind.USER_HANDLE.value and self.contact_handle)
            or self.contact_email
        )


class SourceAdapter(Protocol):
    name: str

    def iter_jobs(self, limit: int | None = None) -> Iterator[RawJob]:
        ...


def _usable_handle(h: str) -> bool:
    """Живой человек, а не бот и не служебный юзернейм.

    Боты не читают отклики — писать им бессмысленно, а массовые сообщения
    ботам-агрегаторам ещё и выглядят как спам.
    """
    if not h or h.lower() in HANDLE_STOPLIST:
        return False
    if h.lower().endswith("bot"):
        return False
    return bool(re.match(r"^[A-Za-z][A-Za-z0-9_]{3,31}$", h))


def extract_telegram_handle(text: str, denylist=None) -> str:
    """Первый пригодный @handle из текста поста. '' если нет.

    denylist — хендлы самого канала и его кросс-промо семейства: они есть
    почти в каждом посте и не являются контактом работодателя.
    """
    deny = {d.lower() for d in (denylist or ())}
    for m in _TME.finditer(text or ""):
        h = m.group("h").strip(".,;:!?)")
        if h.startswith("+") or h.lower() in deny:
            continue
        if _usable_handle(h):
            return h
    for m in _AT.finditer(text or ""):
        h = m.group(1)
        if h.lower() in deny or not _usable_handle(h):
            continue
        # хендл внутри email (ivan@gmail.com) — не контакт
        if m.start() > 0 and (text or "")[m.start() - 1].isalnum():
            continue
        # хвост «по рекламе @x» — не контакт вакансии
        ctx = (text or "")[max(0, m.start() - 60): m.start()]
        if CHANNEL_ADMIN_HINT.search(ctx):
            continue
        return h
    return ""


# Ящики из подвала вакансии, а не для откликов: политика данных, безопасность,
# доступность собеседований, юристы. В WWR-вакансиях они стоят почти всегда, и
# 11 из 32 «почтовых» вакансий WWR на деле были privacy@/security@/
# candidateaccommodations@ — писать туда резюме бессмысленно и вредно.
NON_HIRING_MAILBOX = re.compile(
    r"privacy|security|accommodat|compliance|gdpr|\bdpo\b|dataprotection|"
    r"data[._-]protection|trust|abuse|legal|ethics|whistleblow|noreply|no-reply|"
    r"donotreply|unsubscribe|dsar", re.I)


# Заглушка вместо адреса: «first.last@grafana.com» в профиле GitHub — человек показывает
# ФОРМАТ адреса, пряча настоящий. 20.09 прямое письмо ушло ровно на такую строку. Письмо на
# заглушку — гарантированная отбивка, а отбивки останавливают прогрев ящика.
PLACEHOLDER_MAILBOX = re.compile(
    r"^(?:first[._-]?(?:name)?[._-]?last(?:name)?|f[._-]?last(?:name)?|name[._-]?surname|"
    r"(?:your|my)[._-]?(?:name|email|mail)|(?:user)?name|user|someone|somebody|you|"
    r"example|sample|test|foo|bar|john[._-]?(?:doe|smith)|jane[._-]?doe|ivan[._-]?ivanov|"
    r"x{2,}|_+|\.+)$", re.I)
_PLACEHOLDER_DOMAIN = re.compile(r"^(?:example\.(?:com|org|net)|domain\.com|company\.com|"
                                 r"email\.com|yourcompany\.com|test\.com)$", re.I)


def is_placeholder_email(addr: str) -> bool:
    local, _, domain = (addr or "").strip().lower().replace("mailto:", "").partition("@")
    return bool(PLACEHOLDER_MAILBOX.match(local) or _PLACEHOLDER_DOMAIN.match(domain))


def is_hiring_mailbox(addr: str) -> bool:
    if is_placeholder_email(addr):
        return False
    return not NON_HIRING_MAILBOX.search((addr or "").split("@")[0])


def extract_email(text: str) -> str:
    """Email для отклика, включая обфусцированный вид HN ('a [at] b [dot] com')."""
    for m in _EMAIL.finditer(text or ""):
        addr = m.group(0).rstrip(".,;:")
        if _valid_email(addr) and is_hiring_mailbox(addr):
            return addr
    for m in _EMAIL_OBF.finditer(text or ""):
        # Ветка 1 (at в скобках) — группы 1-3, ветка 2 (at словом) — 4-6.
        local, raw_domain, tld = (m.group(1), m.group(2), m.group(3)) \
            if m.group(1) else (m.group(4), m.group(5), m.group(6))
        domain = re.sub(r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\s+dot\s+)\s*", ".",
                        raw_domain, flags=re.I).strip()
        addr = "%s@%s.%s" % (local, domain, tld)
        if _valid_email(addr) and is_hiring_mailbox(addr):
            return addr
    return ""


def _contact_url(rj: RawJob) -> str:
    """Для почтовой вакансии контакт — сам адрес, а не страница борда.

    Борды отдают и ссылку на вакансию, и email из текста. Раньше ссылка
    побеждала: у вакансии contact_kind=email, а в contact_url — страница WWR,
    и отправщик видел «email не указан» (11 одобренных WWR 16.09). Ссылка при
    этом не теряется — она в all_links.
    """
    if rj.contact_kind == ContactKind.EMAIL.value and rj.contact_email:
        return rj.contact_email
    return rj.contact_url or rj.contact_email


def save_jobs(jobs: Iterator[RawJob], verbose: bool = True) -> dict:
    """Дедуп + запись Job/Employer/Application. Общая для всех источников."""
    stats = {"seen": 0, "new": 0, "dupes": 0, "with_contact": 0,
             "handle_missing": 0, "updated": 0, "errors": 0}
    for rj in jobs:
        stats["seen"] += 1
        try:
            title_n = norm_keep_digits(rj.title)
            company_n = norm_keep_digits(rj.company)
            desc_hash = norm_hash(rj.content)
            handle_norm = (rj.contact_handle or "").lower()

            with session_scope() as sess:
                # три независимых ключа дедупа
                dup = sess.scalar(select(Job).where(Job.external_uuid == rj.external_uuid))
                if not dup and desc_hash and company_n:
                    # Один и тот же текст часто публикуют разные компании.
                    # Совпадение description_hash допустимо только внутри
                    # той же компании и роли.
                    for candidate in sess.scalars(select(Job).where(
                            Job.description_hash == desc_hash)).all():
                        if (norm_keep_digits(candidate.company_name) == company_n
                                and (not title_n or candidate.title_norm == title_n)):
                            dup = candidate
                            break
                if not dup and handle_norm and title_n:
                    dup = sess.scalar(select(Job).where(
                        Job.contact_handle_norm == handle_norm,
                        Job.title_norm == title_n))
                if dup:
                    now = utcnow()
                    dup.last_seen_at = now
                    dup.fetched_at = now
                    if rj.content and len(rj.content) >= len(dup.description_raw or ""):
                        dup.description_raw = rj.content
                        dup.description_hash = desc_hash
                    for attr, value in (
                        ("title", rj.title), ("title_norm", title_n),
                        ("company_name", rj.company), ("tag", rj.tag),
                        ("salary_raw", rj.salary_raw),
                        ("contact_handle", rj.contact_handle),
                        ("contact_handle_norm", handle_norm),
                        ("contact_url", _contact_url(rj)),
                        ("posted_at", rj.posted_at), ("raw_json", rj.raw),
                    ):
                        if value not in (None, "", 0):
                            setattr(dup, attr, value)
                    if rj.all_links:
                        dup.all_links_json = rj.all_links
                    # Контакт появился там, где его «не было»: разборщик научился читать
                    # кнопку бота или ссылку на сайт. Вакансия была закрыта как недостижимая
                    # не владельцем, а дефектом — возвращаем её в работу.
                    unknown = ContactKind.UNKNOWN.value
                    if dup.contact_kind == unknown and rj.contact_kind != unknown:
                        dup.contact_kind = rj.contact_kind
                        app = sess.scalar(select(Application).where(Application.job_id == dup.id))
                        if (app is not None and app.status == Status.WITHDRAWN.value
                                and not app.sent_at and not app.approved_at
                                and not (app.outcome or "")):
                            app.status = (Status.DISCOVERED.value if rj.has_direct_contact
                                          else Status.HANDLE_MISSING.value)
                            app.score, app.score_breakdown_json = 0.0, {}
                            app.review_note = "контакт найден при повторном сборе"
                            stats["revived"] = stats.get("revived", 0) + 1
                    if dup.is_closed:
                        dup.is_closed = False
                        dup.closed_at = None
                    stats["dupes"] += 1
                    stats["updated"] += 1
                    continue

                job = Job(
                    external_uuid=rj.external_uuid, source=rj.source,
                    title=rj.title, title_norm=title_n,
                    company_name=rj.company, tag=rj.tag,
                    description_raw=rj.content, description_hash=desc_hash,
                    salary_raw=rj.salary_raw, remote=True, mode=rj.mode,
                    contact_kind=rj.contact_kind,
                    contact_handle=rj.contact_handle,
                    contact_handle_norm=handle_norm,
                    contact_url=_contact_url(rj),
                    all_links_json=rj.all_links,
                    posted_at=rj.posted_at, raw_json=rj.raw,
                    last_seen_at=utcnow(),
                )
                sess.add(job)
                sess.flush()
                stats["new"] += 1

                employer_id = None
                key = handle_norm or (rj.contact_email or "").lower()
                if rj.has_direct_contact and key:
                    emp = sess.scalar(select(Employer).where(Employer.handle_norm == key))
                    if not emp:
                        emp = Employer(handle_norm=key, handle_kind=rj.contact_kind,
                                       display_name=rj.company or rj.tag)
                        sess.add(emp)
                        sess.flush()
                    emp.total_jobs_seen += 1
                    employer_id = emp.id

                if rj.has_direct_contact:
                    status = Status.DISCOVERED.value
                    stats["with_contact"] += 1
                elif (job.contact_url or "").strip() or                         (job.contact_handle or "").strip():
                    # Прямого контакта нет, но есть куда пойти руками —
                    # это ручная очередь.
                    status = Status.HANDLE_MISSING.value
                    stats["handle_missing"] += 1
                else:
                    # Ни ссылки, ни хендла: откликнуться нечем ни автомату,
                    # ни человеку. Раньше такие копились в ручной очереди —
                    # 2278 штук за месяц, и каждая раздувала счётчик,
                    # который владелец видит в боте.
                    status = Status.WITHDRAWN.value
                    stats["unreachable"] = stats.get("unreachable", 0) + 1
                sess.add(Application(job_id=job.id, employer_id=employer_id,
                                     status=status))
        except Exception as exc:
            stats["errors"] += 1
            if verbose:
                print("  ! %s: %s" % (rj.external_uuid[:20], str(exc)[:70]))
    return stats


def close_stale_jobs(age_days: int = 45) -> int:
    """Закрыть вакансии, которые давно не встречались ни в одном источнике.

    Источник может переиздать запись с новым external_uuid, поэтому статус
    закрывается по last_seen_at, а при новом появлении save_jobs его открывает
    обратно. Уже отправленные заявки не переписываются: закрываются только
    незавершённые/неотправленные состояния.
    """
    from datetime import timedelta

    now = utcnow()
    cutoff = now - timedelta(days=max(1, age_days))
    withdrawable = {
        Status.DISCOVERED.value, Status.SCORED.value,
        Status.CONTENT_READY.value, Status.GATE_FAILED.value,
        Status.PENDING_APPROVAL.value, Status.APPROVED.value,
        Status.SEND_FAILED.value, Status.SEND_FAILED_AMBIGUOUS.value,
        Status.HANDLE_MISSING.value,
    }
    closed = 0
    with session_scope() as sess:
        jobs = sess.scalars(select(Job).where(
            Job.is_closed.is_(False), Job.last_seen_at.is_not(None),
            Job.last_seen_at < cutoff)).all()
        for job in jobs:
            job.is_closed = True
            job.closed_at = now
            for app in list(job.applications):
                if app.status in withdrawable:
                    app.transition(Status.WITHDRAWN,
                                   reason="вакансия не встречалась %d дней" % age_days)
            closed += 1
    return closed
