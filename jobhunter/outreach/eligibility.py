"""Единые причины допуска: используются отправщиками и экраном ожидания."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..config import get_settings
from ..models import ContactKind, Status, utcnow


@dataclass(frozen=True)
class Eligibility:
    code: str = "ready"
    reason: str = "Готово к отправке"
    next_try_at: datetime | None = None

    @property
    def allowed(self) -> bool:
        return self.code == "ready"


def _utc_naive(value):
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def vacancy_problem(job, now=None) -> Eligibility:
    if job is None:
        return Eligibility("missing_job", "Вакансия не найдена")
    if job.is_closed:
        return Eligibility("closed", "Вакансия закрыта")
    from ..ingest.postkind import is_seeker_post
    from ..ingest.tgchannels import _is_vacancy, is_candidate_post
    content = job.description_raw or ""
    if is_candidate_post(content):
        return Eligibility("candidate", "Это резюме соискателя, а не предложение работы")
    # Второй, независимый классификатор — по меткам публикатора и заголовку
    # (#резюме в хвосте, «Senior DevOps-инженер … #резюме»). Эта функция —
    # общая точка подготовки и обоих отправщиков: пост соискателя не дойдёт
    # ни до письма, ни до отправки, каким бы источником он ни пришёл.
    if is_seeker_post((job.title or "") + "\n" + content):
        return Eligibility("candidate", "Автор поста сам ищет работу — писать ему нельзя")
    if job.source.startswith("tg:") and content and not _is_vacancy(content):
        return Eligibility("not_vacancy", "В публикации нет подтверждённого объявления о найме")
    if job.posted_at:
        now = now or utcnow()
        timestamp = _utc_naive(now).replace(tzinfo=timezone.utc).timestamp()
        age = max(0, int((timestamp - int(job.posted_at)) // 86400))
        days = get_settings().max_vacancy_age_days
        if age > days:
            return Eligibility("stale", "Вакансии %d дней, допустимо не больше %d" % (age, days))
    return Eligibility()


def is_followup(app) -> bool:
    return bool(app.sent_at and app.followup_body and not app.followup_sent_at)


def check(app, job, employer=None, *, now=None, sending: bool = False,
          manual: bool = False) -> Eligibility:
    now = _utc_naive(now or utcnow())
    if app is None:
        return Eligibility("missing_application", "Заявка не найдена")
    if (getattr(app, "outcome", "") or "").startswith("manual_tg_") and not manual:
        return Eligibility("manual_owner", "Передано владельцу: отправка только вручную")
    allowed_statuses: tuple[str, ...] = (Status.APPROVED.value, Status.SEND_FAILED.value)
    if sending:
        allowed_statuses += (Status.SENDING.value,)
    if app.status not in allowed_statuses:
        reasons = {Status.PENDING_APPROVAL.value: "Отклик ожидает одобрения",
                   Status.FOLLOWUP_PENDING_APPROVAL.value: "Напоминание ожидает одобрения",
                   Status.SENDING.value: "Отправка выполняется; повтор заблокирован",
                   Status.SEND_FAILED_AMBIGUOUS.value: "Доставка не подтверждена — нужна ручная проверка"}
        return Eligibility("status", reasons.get(app.status, "Статус заявки: %s" % app.status))
    if app.status == Status.SEND_FAILED.value and not app.send_next_try_at:
        return Eligibility("send_failed", "Ошибка отправки требует проверки")
    problem = vacancy_problem(job, now)
    if not problem.allowed:
        return problem
    if not app.gate_passed:
        return Eligibility("gate", "Проверка достоверности отклика не пройдена")
    if job.contact_kind not in (ContactKind.USER_HANDLE.value, ContactKind.EMAIL.value):
        return Eligibility("contact_kind", "Нет прямого контакта рекрутёра")
    if job.contact_kind == ContactKind.USER_HANDLE.value and not job.contact_handle:
        return Eligibility("contact_missing", "Не указан Telegram-контакт")
    if job.contact_kind == ContactKind.EMAIL.value and "@" not in (job.contact_url or ""):
        return Eligibility("contact_missing", "Не указан email-контакт")
    if job.contact_kind == ContactKind.EMAIL.value:
        from .mailer import _mailbox_ok
        if not _mailbox_ok((job.contact_url or "").replace("mailto:", "").strip()):
            return Eligibility("contact_filtered", "Служебный адрес не предназначен для откликов")
    if app.first_reply_at or app.last_inbound_at or (employer and employer.last_inbound_at):
        return Eligibility("already_replied", "Рекрутёр уже ответил — продолжение в диалогах")
    if employer and employer.do_not_contact:
        return Eligibility("do_not_contact", "Контакт запрещён владельцем или политикой отправки")
    followup = is_followup(app)
    if app.sent_at and not followup:
        return Eligibility("already_sent", "Отклик уже отправлен")
    if app.send_next_try_at and _utc_naive(app.send_next_try_at) > now:
        return Eligibility("retry", "Повтор ожидает назначенного времени", app.send_next_try_at)
    if followup:
        due = _utc_naive(app.sent_at) + timedelta(hours=72)
        if due > now:
            return Eligibility("followup_wait", "Напоминание разрешено через 72 часа", due)
    elif employer and employer.last_contacted_at:
        due = _utc_naive(employer.last_contacted_at) + timedelta(days=30)
        if due > now:
            return Eligibility("cooldown", "Этому работодателю уже писали за последние 30 дней", due)
    return Eligibility()
