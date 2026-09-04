"""Отправка откликов по email (SMTP).

Для вакансий с почтовым контактом — HN и часть Telegram-постов.
Резюме идёт вложением: в почте это норма и, в отличие от Telegram,
не является спам-сигналом.

Пароль приложения читается из .env (SMTP_APP_PASSWORD) и никуда не логируется.

    python -m jobhunter.outreach.mailer --dry-run
    python -m jobhunter.outreach.mailer --limit 10
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import random
import re
import smtplib
import ssl
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

from sqlalchemy import select

from ..config import get_settings
from ..db import session_scope
from ..models import Application, ContactKind, Employer, Job, Message, SendLog, Status, utcnow
from ..tailor.render import resolve_cv
from . import policy


def reply_to_addr(app_id: int) -> str:
    """Адрес вида suren6pro+jh42xa1b2c@gmail.com для ответов по заявке 42.

    Gmail доставляет письма на адрес с «плюсом» в тот же ящик, а суффикс
    возвращается к нам в заголовке To — и, что важнее, в процитированном
    тексте, когда рекрутёр пересылает письмо коллеге или отвечает с личного
    ящика. Это единственная привязка, переживающая потерю заголовков.

    Короткая подпись HMAC нужна, чтобы случайное письмо на +jh1x... не
    приклеилось к чужой заявке.
    """
    s = get_settings()
    user = s.smtp_user or ""
    if "@" not in user:
        return user
    local, domain = user.split("@", 1)
    secret = (s.mail_bind_secret or s.telegram_api_hash or "jobhunter").encode()
    sig = hmac.new(secret, str(app_id).encode(), hashlib.sha256).hexdigest()[:6]
    return "%s+jh%dx%s@%s" % (local, app_id, sig, domain)


def _valid_sigs(app_id: int) -> set:
    """Все подписи, под которыми могли уходить письма по этой заявке.

    Секрет подписи менялся: первые письма подписаны фолбэком (api_hash),
    новые — MAIL_BIND_SECRET. Ответ на старое письмо может прийти через
    недели, поэтому проверка принимает подписи всех секретов, а не только
    текущего — иначе смена секрета молча отвязала бы старые треды.
    """
    s = get_settings()
    secrets_ = [x for x in (s.mail_bind_secret, s.telegram_api_hash, "jobhunter") if x]
    return {hmac.new(sec.encode(), str(app_id).encode(),
                     hashlib.sha256).hexdigest()[:6]
            for sec in secrets_}


def parse_reply_to(text: str) -> int:
    """Номер заявки из plus-адреса, если подпись сходится. Иначе 0.

    На вход годится и заголовок, и целиком текст письма: в пересланном
    письме адрес живёт в цитате, и искать его надо там же.
    """
    for m in re.finditer(r"\+jh(\d+)x([0-9a-f]{6})", text or "", re.I):
        app_id = int(m.group(1))
        if m.group(2).lower() in _valid_sigs(app_id):
            return app_id
    return 0


@contextmanager
def smtp_session():
    """SMTP + STARTTLS + вход, всё под защитой.

    Раньше эти три шага стояли голыми: обрыв на любом из них ронял весь
    прогон автопилота, а не одно письмо.
    """
    s = get_settings()
    server = None
    try:
        server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30)
        server.starttls(context=ssl.create_default_context())
        server.login(s.smtp_user, s.smtp_app_password)
        yield server
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass


def build_message(*, to: str, subject: str, body: str, cv_path: str = "",
                  app_id: int = 0, in_reply_to: str = "",
                  references: list | None = None,
                  auto: bool = False, message_id: str = "") -> EmailMessage:
    """Готовое письмо с проставленным Message-ID.

    Message-ID ставим сами: иначе его генерирует Gmail уже после отправки, и
    узнать, на что именно ответил рекрутёр, будет не по чему.
    """
    s = get_settings()
    msg = EmailMessage()
    msg["From"] = "%s <%s>" % (s.smtp_from_name, s.smtp_user)
    msg["To"] = to
    msg["Subject"] = subject
    domain = (s.smtp_user or "localhost").rsplit("@", 1)[-1] or "localhost"
    msg["Message-ID"] = message_id or make_msgid(domain=domain)
    if app_id:
        msg["Reply-To"] = "%s <%s>" % (s.smtp_from_name, reply_to_addr(app_id))
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        refs = list(references or [])[-8:]
        if in_reply_to not in refs:
            refs.append(in_reply_to)
        msg["References"] = " ".join(refs)      # RFC 5322: через пробел
    if auto:
        # RFC 3834: корректный автоответчик на той стороне обязан
        # не отвечать на такое письмо. Дешёвая защита от петли.
        msg["Auto-Submitted"] = "auto-generated"

    footer = "\n\n—\n%s\n%s" % (s.smtp_from_name, reply_to_addr(app_id)
                                if app_id else s.smtp_user)
    msg.set_content(body + footer)
    # Через резолвер, а не прямой проверкой пути: путь записан в заявку при
    # подготовке и мог устареть при переезде проекта. Прямая проверка тихо
    # вернула бы False, и письмо ушло бы без резюме — отказ в ту сторону,
    # где о нём никто не узнает.
    real = resolve_cv(cv_path) if cv_path else ""
    if real:
        msg.add_attachment(Path(real).read_bytes(), maintype="application",
                           subtype="pdf", filename=Path(real).name)
    return msg


_PLACEHOLDER_ROLE = re.compile(
    r"^(?:текст\s+вакансии|vacancy\s+text|job\s+description|description)"
    r"\s*:?[\s-]*$", re.I)


def _subject_role(job: Job, lang: str) -> str:
    """Человеческое название роли для темы, даже если источник сломан."""
    generic_tags = {
        "python": "Backend Engineer" if lang == "en" else "Backend-разработчик",
        "backend": "Backend Engineer" if lang == "en" else "Backend-разработчик",
        "devops": "DevOps Engineer" if lang == "en" else "DevOps-инженер",
        "product": "Product Manager" if lang == "en" else "Продакт-менеджер",
        "ml": "ML Engineer" if lang == "en" else "ML-инженер",
        "data": "Data Engineer" if lang == "en" else "Data-инженер",
    }
    candidates = [job.title or "", job.tag or ""]
    for raw in candidates:
        role = raw.strip()
        # Часть источников сохраняет префикс буквально: «Текст вакансии:
        # Python Backend». Берём полезную часть, а не тащим мусор в тему.
        if re.match(r"^текст\s+вакансии\s*:", role, re.I):
            role = role.split(":", 1)[1].strip()
        if not role or _PLACEHOLDER_ROLE.match(role):
            continue
        if re.match(r"^(?:https?://|www\.)", role, re.I):
            continue
        if role.lower() in generic_tags:
            return generic_tags[role.lower()]
        if role:
            return role

    blob = " ".join([job.title or "", job.tag or "", job.description_raw or ""])
    if re.search(r"product\s*(?:manager|owner)|продакт|продуктов\w*\s+менедж", blob, re.I):
        return "Product Manager" if lang == "en" else "Продакт-менеджер"
    if re.search(r"devops|\bsre\b|kubernetes|terraform", blob, re.I):
        return "DevOps Engineer" if lang == "en" else "DevOps-инженер"
    if re.search(r"data\s*engineer|etl|airflow|\bdwh\b|аналитик", blob, re.I):
        return "Data Engineer" if lang == "en" else "Data-инженер"
    if re.search(r"python|backend|back-end|fastapi|django|разработчик|инженер", blob, re.I):
        return "Backend Engineer" if lang == "en" else "Backend-разработчик"
    return "Software Engineer" if lang == "en" else "Разработчик"


def _subject(job: Job, lang: str) -> str:
    role = _subject_role(job, lang)
    if lang == "en":
        return "Application: %s — Suren Hakobyan (7+ yrs, backend/tech lead)" % role[:70]
    return "Отклик: %s — Акопян Сурен (7+ лет, backend/tech lead)" % role[:70]


# Ящики, куда отклик слать бессмысленно и вредно: это не наём, а закупки,
# бухгалтерия, продажи, поддержка. Письмо туда — спам в чужой отдел.
BAD_MAILBOX = re.compile(
    r"^(zakupki|zakup|tender|buh|buhg|account|accounting|finance|"
    r"sales|shop|order|zakaz|opt|market|marketing|reklama|adv|"
    r"support|help|noreply|no-reply|admin|webmaster|postmaster|abuse|"
    r"secretar|priemnaya|office|ofis|director|general|"
    r"press|pr|legal|jurist|law)([._-]|\d|$)", re.I)

# Приоритетные — явно про наём.
GOOD_MAILBOX = re.compile(r"^(hr|job|jobs|career|careers|recruit|recruiting|"
                          r"vacancy|vacancies|resume|cv|talent|people|hiring)", re.I)


def _mailbox_ok(addr: str) -> bool:
    """Служебный ящик ловим и в середине: npo_buh@, ooo-zakupki@."""
    local = (addr or "").split("@")[0]
    if BAD_MAILBOX.match(local):
        return False
    for part in re.split(r"[._\-+]", local):
        if part and BAD_MAILBOX.match(part):
            return False
    return True


def _smtp_delivery_ambiguous(exc: Exception) -> bool:
    """Мог ли сервер принять письмо до того, как клиент получил ошибку."""
    # Отказ SMTP с кодом 4xx/5xx означает, что DATA не была принята. Для
    # сетевого обрыва, таймаута и неизвестной ошибки доказать это нельзя.
    if isinstance(exc, smtplib.SMTPResponseException):
        return False
    return True


def _smtp_retryable(exc: Exception) -> bool:
    """4xx SMTP-ответ не принял письмо и может быть повторён позже."""
    return (isinstance(exc, smtplib.SMTPResponseException)
            and 400 <= int(exc.smtp_code or 0) < 500)


def _stable_message_id(app_id: int, followup: bool = False) -> str:
    """Детерминированный RFC Message-ID для безопасной ручной сверки."""
    s = get_settings()
    domain = (s.smtp_user or "jobhunter.local").rsplit("@", 1)[-1]
    kind = "followup" if followup else "initial"
    return "<jobhunter-%s-%s@%s>" % (app_id, kind, domain)


# Публичные почтовики: домен НЕ означает компанию. Два рекрутёра с
# ящиками на gmail.com — разные работодатели, дедуп по домену молча
# вытеснял бы одного из них из каждой партии.
FREEMAIL = {
    "gmail.com", "googlemail.com", "yandex.ru", "ya.ru", "mail.ru",
    "bk.ru", "list.ru", "inbox.ru", "rambler.ru", "outlook.com",
    "hotmail.com", "live.com", "icloud.com", "proton.me", "protonmail.com",
    "yahoo.com", "tutanota.com", "gmx.com",
}

EMPLOYER_COOLDOWN_DAYS = 30


def pick_batch(limit: int) -> list:
    out, seen = [], set()
    cooldown_edge = (datetime.now(timezone.utc).replace(tzinfo=None)
                     - timedelta(days=EMPLOYER_COOLDOWN_DAYS))
    with session_scope() as sess:
        now = utcnow()
        rows = sess.scalars(
            select(Application)
            .where(((Application.status == Status.APPROVED.value) |
                     ((Application.status == Status.SEND_FAILED.value) &
                     Application.send_next_try_at.is_not(None) &
                     (Application.send_next_try_at <= now))))
            .order_by(Application.score.desc())).all()
        try:
            from ..report import source_preferences, source_priority_penalty
            source_rates = source_preferences(min_sent=5)
        except Exception:
            source_rates = {}

            def source_priority_penalty(source: str,
                                        preferences: dict | None = None
                                        ) -> float:
                return 0.0
        pairs = [(app, sess.get(Job, app.job_id)) for app in rows]
        pairs.sort(key=lambda pair: (
            (pair[0].score - source_priority_penalty(
                pair[1].source if pair[1] else "", source_rates)),
            pair[0].id), reverse=True)
        for app, job in pairs:
            if not job or job.is_closed or job.contact_kind != ContactKind.EMAIL.value:
                continue
            addr = (job.contact_url or "").replace("mailto:", "").strip()
            if not addr or "@" not in addr:
                continue
            if not _mailbox_ok(addr):    # закупки/бухгалтерия/поддержка — не наём
                continue
            domain = addr.split("@")[-1].lower()
            # Одна компания — один отклик за партию. Для корпоративного
            # домена компания = домен; для публичного почтовика — сам адрес.
            key = addr.lower() if domain in FREEMAIL else domain
            if key in seen:
                continue
            seen.add(key)
            emp = sess.get(Employer, app.employer_id) if app.employer_id else None
            if emp and (emp.do_not_contact or emp.last_inbound_at):
                continue
            # Кулдаун между ПРОГОНАМИ, а не только внутри партии: у sender
            # он есть, у почты не было — второй APPROVED к тому же
            # работодателю через день уходил бы повторным письмом.
            if emp and emp.last_contacted_at and emp.last_contacted_at > cooldown_edge:
                continue
            if app.send_next_try_at and app.send_next_try_at > utcnow():
                continue
            out.append({"app_id": app.id, "email": addr, "lang": app.cv_lang or "ru",
                        "title": job.title or job.tag, "score": app.score,
                        "company": job.company_name, "cv_path": app.cv_path,
                        "job_id": job.id, "employer_id": app.employer_id})
            if len(out) >= limit:
                break
    return out


def send_batch(limit: int, dry: bool) -> int:
    s = get_settings()
    # То же окно вежливости, что у Telegram-отправщика: ночное холодное
    # письмо — прямой сигнал спам-фильтру и раздражение живому человеку.
    if not dry:
        from zoneinfo import ZoneInfo
        hour_msk = datetime.now(ZoneInfo("Europe/Moscow")).hour
        if not policy.within_send_window(hour_msk):
            print("Вне окна 09-21 МСК (%02d:xx) — почта подождёт утра." % hour_msk)
            return 0
    batch = pick_batch(limit)
    if not batch:
        print("Нет одобренных заявок с email-контактом.")
        return 0

    print("Email-партия: %d%s" % (len(batch), "  [DRY-RUN]" if dry else ""))
    for it in batch:
        print("  %5.0f  %-34s %s" % (it["score"], it["email"], (it["title"] or "")[:40]))

    if not dry and (not s.smtp_user or not s.smtp_app_password):
        print("\nНет SMTP_USER / SMTP_APP_PASSWORD в .env.")
        print("Gmail → Аккаунт → Безопасность → Двухэтапная аутентификация → "
              "Пароли приложений. Вписать в .env самому.")
        return 2

    server = None
    if not dry:
        ctx = ssl.create_default_context()
        server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30)
        server.starttls(context=ctx)
        server.login(s.smtp_user, s.smtp_app_password)
        print("\nSMTP: подключено как %s\n" % s.smtp_user)

    rng = random.Random()
    ok = 0
    for i, it in enumerate(batch):
        if policy.kill_switch_active():
            print("СТОП: kill-switch")
            break
        with session_scope() as sess:
            app = sess.get(Application, it["app_id"])
            is_followup = bool(app.followup_body and not app.followup_sent_at)
            text = app.followup_body if is_followup else app.message_body
            cv_path, lang = app.cv_path, app.cv_lang or "ru"
            job = sess.get(Job, it["job_id"])
            subj = _subject(job, lang)

        if dry:
            print("  [dry-run] → %s | тема: %s | вложение: %s"
                  % (it["email"], subj[:52], Path(cv_path).name if cv_path else "нет"))
            ok += 1
            continue

        mid = _stable_message_id(it["app_id"], is_followup)
        msg = build_message(to=it["email"], subject=subj, body=text,
                            cv_path=cv_path, app_id=it["app_id"],
                            message_id=mid)
        # Lease и idempotency-ключ фиксируются ДО сетевого вызова. При падении
        # между SMTP и БД такая заявка будет остановлена как ambiguous, а не
        # отправлена повторно вслепую.
        with session_scope() as sess:
            a = sess.get(Application, it["app_id"])
            if not a or a.status not in (Status.APPROVED.value,
                                         Status.SEND_FAILED.value):
                continue
            now = utcnow()
            if a.status == Status.SEND_FAILED.value:
                a.transition(Status.APPROVED, reason="повтор после SMTP 4xx")
            a.transition(Status.SENDING)
            a.sending_lease_until = now + timedelta(seconds=180)
            a.send_channel = "email"
            a.send_idempotency_key = "email:%d:%s" % (a.id, mid)
            a.send_last_attempt_at = now
            a.send_next_try_at = None
            a.send_error_detail = ""
            a.send_attempts += 1
            if sess.scalar(select(Message).where(
                Message.application_id == a.id,
                Message.direction == "out",
                Message.email_message_id == mid).limit(1)) is None:
                sess.add(Message(application_id=a.id, direction="out", body=text,
                                 is_auto=True, email_message_id=mid,
                                 email_from=s.smtp_user, email_subject=subj))
        try:
            if server is None:
                raise RuntimeError("SMTP-сессия не открыта")
            server.send_message(msg)
        except Exception as e:
            with session_scope() as sess:
                a = sess.get(Application, it["app_id"])
                target = (Status.SEND_FAILED_AMBIGUOUS
                          if _smtp_delivery_ambiguous(e) else Status.SEND_FAILED)
                a.transition(target, reason=type(e).__name__)
                a.sending_lease_until = None
                a.send_error_class = type(e).__name__
                a.send_error_detail = str(e)[:500]
                a.send_next_try_at = (utcnow() + timedelta(minutes=15)
                                      if _smtp_retryable(e) else None)
                sess.add(SendLog(application_id=it["app_id"],
                                 result="ambiguous" if target == Status.SEND_FAILED_AMBIGUOUS
                                 else "error",
                                 error_class=type(e).__name__, peer_id=it["email"]))
            print("  ! %s: %s" % (it["email"], str(e)[:60]))
            continue

        with session_scope() as sess:
            a = sess.get(Application, it["app_id"])
            a.transition(Status.SENT)
            a.sending_lease_until = None
            a.send_next_try_at = None
            a.sent_at = utcnow()
            a.last_outbound_at = utcnow()
            a.transition(Status.AWAITING_REPLY)
            if is_followup:
                # Напоминание уже отправлено — второго не планируем: два
                # «напоминаю о себе» подряд читаются как спам.
                a.followup_sent_at = utcnow()
                a.followup_due_at = None
            else:
                a.followup_due_at = (datetime.now(timezone.utc)
                                     .replace(tzinfo=None) + timedelta(days=5))
            sess.add(SendLog(application_id=it["app_id"], result="ok",
                             peer_id=it["email"]))
            # Message-ID сохраняем: по нему потом находится ответ рекрутёра
            # через In-Reply-To/References — привязка, переживающая ответ с
            # другого адреса.
            mid = msg.get("Message-ID", "")
            outbound = sess.scalar(select(Message).where(
                Message.application_id == it["app_id"],
                Message.direction == "out",
                Message.email_message_id == mid).limit(1))
            if outbound is None:
                outbound = Message(application_id=it["app_id"], direction="out",
                                    body=text, email_message_id=mid)
                sess.add(outbound)
            outbound.body = text
            outbound.sent_at = utcnow()
            outbound.is_auto = True
            outbound.email_from = s.smtp_user
            outbound.email_subject = subj
            a = sess.get(Application, it["app_id"])
            refs = list(a.email_thread_refs or [])
            if mid and mid not in refs:
                refs.append(mid)
            a.email_thread_refs = refs[-10:]
            if it.get("employer_id"):
                emp = sess.get(Employer, it["employer_id"])
                if emp:
                    emp.last_contacted_at = utcnow()
                    emp.total_messages_sent += 1
        from . import archive
        archive.record(it["app_id"], "email", it["email"], text,
                       job_title=it.get("title", ""),
                       company=it.get("company", ""), score=it.get("score", 0),
                       cv_path=it.get("cv_path", ""), kind="cold")
        ok += 1
        print("  [%d/%d] %s — отправлено" % (i + 1, len(batch), it["email"]))
        if i < len(batch) - 1:
            time.sleep(rng.uniform(60, 180))

    if server:
        server.quit()
    print("\nИтог: отправлено %d из %d" % (ok, len(batch)))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Email-отправка откликов")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    s = get_settings()
    return send_batch(args.limit or s.email_daily_limit, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
