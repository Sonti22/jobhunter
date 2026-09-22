"""SQLAlchemy-модели. Схема из §2 плана."""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Status(str, enum.Enum):
    """Состояния заявки. Переходы — только через applications.transition()."""
    DISCOVERED = "DISCOVERED"
    DUPLICATE = "DUPLICATE"
    SCORED = "SCORED"
    REJECTED_SCORE = "REJECTED_SCORE"
    HANDLE_MISSING = "HANDLE_MISSING"          # VIP/preview — контакта нет
    CONTENT_READY = "CONTENT_READY"
    GATE_FAILED = "GATE_FAILED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    SENDING = "SENDING"
    SENT = "SENT"
    SEND_FAILED = "SEND_FAILED"
    SEND_FAILED_AMBIGUOUS = "SEND_FAILED_AMBIGUOUS"
    HANDLE_DEAD = "HANDLE_DEAD"
    AWAITING_REPLY = "AWAITING_REPLY"
    FOLLOWUP_PENDING_APPROVAL = "FOLLOWUP_PENDING_APPROVAL"
    FOLLOWED_UP = "FOLLOWED_UP"
    NO_REPLY_CLOSED = "NO_REPLY_CLOSED"
    REPLIED = "REPLIED"
    IN_DIALOGUE = "IN_DIALOGUE"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    INTERVIEW_PROPOSED = "INTERVIEW_PROPOSED"
    INTERVIEW_CONFIRMED = "INTERVIEW_CONFIRMED"
    INTERVIEW_DONE = "INTERVIEW_DONE"
    OFFER = "OFFER"
    REJECTED_BY_EMPLOYER = "REJECTED_BY_EMPLOYER"
    WITHDRAWN = "WITHDRAWN"


TERMINAL = {
    Status.DUPLICATE, Status.REJECTED_SCORE, Status.HANDLE_DEAD,
    Status.NO_REPLY_CLOSED, Status.REJECTED_BY_EMPLOYER, Status.WITHDRAWN,
}

# Разрешённые переходы. Всё, чего здесь нет, — ошибка программиста.
ALLOWED_TRANSITIONS = {
    Status.DISCOVERED: {Status.DUPLICATE, Status.SCORED, Status.REJECTED_SCORE,
                        Status.HANDLE_MISSING, Status.CONTENT_READY,
                        Status.GATE_FAILED, Status.PENDING_APPROVAL,
                        Status.WITHDRAWN},
    Status.SCORED: {Status.REJECTED_SCORE, Status.CONTENT_READY,
                    Status.GATE_FAILED, Status.PENDING_APPROVAL, Status.WITHDRAWN},
    Status.CONTENT_READY: {Status.GATE_FAILED, Status.PENDING_APPROVAL,
                           Status.WITHDRAWN},
    Status.GATE_FAILED: {Status.DISCOVERED, Status.CONTENT_READY,
                         Status.PENDING_APPROVAL, Status.WITHDRAWN},
    Status.HANDLE_MISSING: {Status.DISCOVERED, Status.WITHDRAWN},
    Status.PENDING_APPROVAL: {Status.APPROVED, Status.WITHDRAWN,
                              Status.DISCOVERED, Status.GATE_FAILED},
    Status.APPROVED: {Status.SENDING, Status.PENDING_APPROVAL, Status.WITHDRAWN,
                      Status.REPLIED},
    Status.SENDING: {Status.SENT, Status.SEND_FAILED,
                     Status.SEND_FAILED_AMBIGUOUS, Status.HANDLE_DEAD,
                     Status.APPROVED},
    Status.SEND_FAILED: {Status.APPROVED, Status.HANDLE_DEAD, Status.WITHDRAWN},
    # После ambiguous-доставки возврат в очередь допустим только как
    # явное решение владельца — автомат не должен слать дубль вслепую.
    Status.SEND_FAILED_AMBIGUOUS: {Status.APPROVED, Status.WITHDRAWN},
    Status.SENT: {Status.AWAITING_REPLY, Status.FOLLOWED_UP, Status.REPLIED, Status.NEEDS_HUMAN},
    Status.AWAITING_REPLY: {Status.REPLIED, Status.FOLLOWUP_PENDING_APPROVAL,
                            Status.NO_REPLY_CLOSED, Status.NEEDS_HUMAN,
                            Status.WITHDRAWN},
    # APPROVED обязателен: отправители берут только его, и без этого
    # перехода подготовленное напоминание уходило в тупик — не отправлялось
    # и, не попав в LIVE, переставало опрашиваться на входящие.
    Status.FOLLOWUP_PENDING_APPROVAL: {Status.APPROVED, Status.FOLLOWED_UP,
                                       Status.NO_REPLY_CLOSED, Status.REPLIED,
                                       Status.WITHDRAWN},
    Status.FOLLOWED_UP: {Status.REPLIED, Status.NO_REPLY_CLOSED, Status.NEEDS_HUMAN},
    # Отвечают и через три недели — переход возможен даже из «закрыто».
    Status.NO_REPLY_CLOSED: {Status.REPLIED},
    Status.REPLIED: {Status.IN_DIALOGUE, Status.NEEDS_HUMAN,
                     Status.REJECTED_BY_EMPLOYER, Status.WITHDRAWN},
    Status.IN_DIALOGUE: {Status.NEEDS_HUMAN, Status.INTERVIEW_PROPOSED,
                         Status.INTERVIEW_CONFIRMED, Status.REJECTED_BY_EMPLOYER,
                         Status.WITHDRAWN},
    Status.NEEDS_HUMAN: {Status.IN_DIALOGUE, Status.INTERVIEW_PROPOSED,
                         Status.INTERVIEW_CONFIRMED, Status.OFFER,
                         Status.REJECTED_BY_EMPLOYER, Status.WITHDRAWN},
    Status.INTERVIEW_PROPOSED: {Status.INTERVIEW_CONFIRMED, Status.NEEDS_HUMAN,
                                Status.REJECTED_BY_EMPLOYER, Status.WITHDRAWN},
    # Перенос слота работодателем — обычное дело, поэтому обратный переход
    # в INTERVIEW_PROPOSED разрешён: приходит новое время, оно снова уходит
    # владельцу на подтверждение.
    Status.INTERVIEW_CONFIRMED: {Status.INTERVIEW_DONE, Status.NEEDS_HUMAN,
                                 Status.INTERVIEW_PROPOSED,
                                 Status.REJECTED_BY_EMPLOYER, Status.WITHDRAWN},
    Status.INTERVIEW_DONE: {Status.OFFER, Status.REJECTED_BY_EMPLOYER,
                            Status.NEEDS_HUMAN, Status.INTERVIEW_PROPOSED},
    Status.OFFER: {Status.WITHDRAWN, Status.REJECTED_BY_EMPLOYER},
}


class TransitionError(RuntimeError):
    """Недопустимый переход состояния заявки."""


class ContactKind(str, enum.Enum):
    USER_HANDLE = "user_handle"      # t.me/<user> — единственный, куда шлём авто
    BOT = "bot"
    CHANNEL = "channel"
    GROUP_INVITE = "group_invite"
    PHONE_LINK = "phone_link"        # t.me/+79... — НИКОГДА не резолвить авто
    EXTERNAL_URL = "external_url"
    EMAIL = "email"
    UNKNOWN = "unknown"


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    external_uuid: Mapped[str] = mapped_column(String, unique=True, index=True)
    source: Mapped[str] = mapped_column(String, default="careered")
    title: Mapped[str] = mapped_column(String, default="")
    title_norm: Mapped[str] = mapped_column(String, default="", index=True)
    company_name: Mapped[str] = mapped_column(String, default="")
    tag: Mapped[str] = mapped_column(String, default="")
    description_raw: Mapped[str] = mapped_column(Text, default="")
    description_hash: Mapped[str] = mapped_column(String, default="", index=True)
    salary_raw: Mapped[str] = mapped_column(String, default="")
    remote: Mapped[bool] = mapped_column(Boolean, default=True)
    # контакт
    mode: Mapped[str] = mapped_column(String, default="")          # full | preview
    contact_kind: Mapped[str] = mapped_column(String, default=ContactKind.UNKNOWN.value)
    contact_handle: Mapped[str] = mapped_column(String, default="")       # i_schanti
    contact_handle_norm: Mapped[str] = mapped_column(String, default="", index=True)
    contact_url: Mapped[str] = mapped_column(String, default="")          # https://t.me/...
    all_links_json: Mapped[list] = mapped_column(JSON, default=list)
    posted_at: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    auth_fingerprint: Mapped[str] = mapped_column(String, default="")     # хеш токена
    raw_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # Последний успешный проход источника. Не смешиваем с posted_at: вакансия
    # может быть старой, но всё ещё жить в ATS/feed.
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow,
                                                   onupdate=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    applications: Mapped[list[Application]] = relationship(back_populates="job")


class Employer(Base):
    __tablename__ = "employers"
    id: Mapped[int] = mapped_column(primary_key=True)
    handle_norm: Mapped[str] = mapped_column(String, unique=True, index=True)
    handle_kind: Mapped[str] = mapped_column(String, default=ContactKind.USER_HANDLE.value)
    display_name: Mapped[str] = mapped_column(String, default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_contacted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_messages_sent: Mapped[int] = mapped_column(Integer, default=0)
    total_jobs_seen: Mapped[int] = mapped_column(Integer, default=0)
    do_not_contact: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str] = mapped_column(Text, default="")


class Application(Base):
    __tablename__ = "applications"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"))
    employer_id: Mapped[int | None] = mapped_column(ForeignKey("employers.id"), nullable=True)
    status: Mapped[str] = mapped_column(String, default=Status.DISCOVERED.value, index=True)

    score: Mapped[float] = mapped_column(Float, default=0.0)
    score_breakdown_json: Mapped[dict] = mapped_column(JSON, default=dict)
    reject_reason: Mapped[str] = mapped_column(String, default="")

    # резюме и письмо
    cv_path: Mapped[str] = mapped_column(String, default="")
    cv_sha256: Mapped[str] = mapped_column(String, default="")
    cv_lang: Mapped[str] = mapped_column(String, default="ru")
    message_body: Mapped[str] = mapped_column(Text, default="")
    message_body_norm_hash: Mapped[str] = mapped_column(String, default="", index=True)
    message_similarity_max: Mapped[float] = mapped_column(Float, default=0.0)
    message_skeleton_id: Mapped[str] = mapped_column(String, default="")
    cv_template_version: Mapped[str] = mapped_column(String, default="")
    message_prompt_version: Mapped[str] = mapped_column(String, default="")
    llm_provider: Mapped[str] = mapped_column(String, default="")

    # гейт
    gate_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    gate_failures_json: Mapped[list] = mapped_column(JSON, default=list)
    promoted_terms_json: Mapped[list] = mapped_column(JSON, default=list)

    # отправка
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sending_lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    worker_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    telegram_random_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    telegram_file_random_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    telegram_followup_random_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    send_channel: Mapped[str] = mapped_column(String, default="")
    send_idempotency_key: Mapped[str] = mapped_column(String, default="", index=True)
    send_last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    send_next_try_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    send_error_detail: Mapped[str] = mapped_column(String, default="")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    telegram_msg_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    send_error_class: Mapped[str] = mapped_column(String, default="")
    send_attempts: Mapped[int] = mapped_column(Integer, default=0)
    alternate_job_ids_json: Mapped[list] = mapped_column(JSON, default=list)

    # переписка
    # Текст напоминания живёт ОТДЕЛЬНО от исходного письма: затирать
    # message_body значит потерять то, что реально отправили рекрутёру —
    # а именно оно нужно владельцу при разборе и аналитике шаблонов.
    followup_body: Mapped[str] = mapped_column(Text, default="")
    followup_due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    followup_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_reply_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    auto_replies_count: Mapped[int] = mapped_column(Integer, default=0)
    needs_human_reason: Mapped[str] = mapped_column(String, default="")
    # Сколько раз пытались вернуть заглохший тред в оборот (convo/revive.py).
    revive_attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Техответы считаются отдельно от общего лимита автоответов.
    auto_tech_replies_count: Mapped[int] = mapped_column(Integer, default=0)
    # Пакет для ручного отклика: ответы, письмо, отпечаток формы.
    apply_packet_json: Mapped[dict] = mapped_column(JSON, default=dict)
    apply_prepared_at: Mapped[datetime | None] = mapped_column(DateTime,
                                                               nullable=True)
    apply_form_hash: Mapped[str] = mapped_column(String, default="")
    revived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # интервью
    interview_at_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    interview_tz: Mapped[str] = mapped_column(String, default="")
    interview_duration_min: Mapped[int] = mapped_column(Integer, default=60)
    ics_path: Mapped[str] = mapped_column(String, default="")
    # Слоты, которые предложил работодатель: [{"utc": iso, "raw": "...", "tz": "..."}]
    # Хранится до решения владельца — карточка подтверждения строится отсюда.
    interview_slots_json: Mapped[list] = mapped_column(JSON, default=list)
    gcal_event_id: Mapped[str] = mapped_column(String, default="")
    gcal_link: Mapped[str] = mapped_column(String, default="")
    # Последнее прочитанное входящее: инкрементальный опрос диалога.
    last_inbound_msg_id: Mapped[int] = mapped_column(Integer, default=0)
    # Адрес, с которого нам реально ответили по этой заявке. Рекрутёры часто
    # пишут с личного ящика вместо hr@ — отвечать надо туда, иначе разговор
    # раздваивается. Заполняется при первой же успешной привязке.
    # Почему самопроверка забраковала письмо. Непусто — заявка не идёт
    # в автоотправку и ждёт решения владельца.
    review_note: Mapped[str] = mapped_column(String, default="")
    email_peer: Mapped[str] = mapped_column(String, default="", index=True)
    # Хвост Message-ID переписки для заголовка References: без него Outlook
    # на той стороне не склеит письма в один тред.
    email_thread_refs: Mapped[list] = mapped_column(JSON, default=list)

    # ── Ручной отклик (вакансии без прямого контакта) ──
    # Состояние живёт ЗДЕСЬ, а не в статусе, намеренно: у HANDLE_MISSING в
    # графе переходов всего два выхода, и втаскивать ручные отклики в машину
    # состояний значит переписать граф и сломать смысл воронки, где
    # «отправлено» означает «система отправила сама».
    # "" — не смотрели | applied — откликнулся | not_fit — не подходит
    # | snoozed — вернуться позже
    outcome: Mapped[str] = mapped_column(String, default="", index=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    snooze_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    job: Mapped[Job] = relationship(back_populates="applications")

    def transition(self, to: Status, gate_result=None, reason: str = "") -> None:
        """Единственная законная точка смены статуса.

        Ключевая защита: попасть в PENDING_APPROVAL можно ТОЛЬКО с прошедшим
        анти-фабрикация гейтом. Без этого отправка могла бы обойти проверку.
        """
        cur = Status(self.status)
        if cur == to:
            return
        allowed = ALLOWED_TRANSITIONS.get(cur, set())
        # Разовая миграция старой версии отклика: письмо уже ушло, но
        # исторический repair_queue оставил запись в APPROVED. Это единственный
        # разрешённый прямой путь из APPROVED в живой диалог и требует явной
        # причины, чтобы обычный код не мог обойти отправитель.
        historical_sent_repair = (
            reason.startswith("repair:sent-state")
            and cur == Status.APPROVED
            and to in (Status.AWAITING_REPLY, Status.REPLIED,
                       Status.IN_DIALOGUE))
        if to not in allowed and not historical_sent_repair:
            raise TransitionError("%s → %s запрещён (заявка %s)"
                                  % (cur.value, to.value, self.id))
        if to == Status.PENDING_APPROVAL:
            # Либо гейт пройден прямо сейчас, либо был пройден раньше и это
            # откат одобрения (APPROVED → очередь) — перепроверять нечего.
            fresh_ok = gate_result is not None and getattr(gate_result, "passed", False)
            if not fresh_ok and not self.gate_passed:
                raise TransitionError(
                    "заявка %s: PENDING_APPROVAL только с пройденным гейтом" % self.id)
        if to == Status.APPROVED and not self.gate_passed:
            raise TransitionError("заявка %s: APPROVED без gate_passed" % self.id)
        self.status = to.value
        if reason:
            self.reject_reason = reason

    def advance(self, to: Status, reason: str = "") -> bool:
        """Перевод в целевой статус через промежуточные, если нужно.

        Возвращает False, если законного пути нет — вызывающий код сам
        решает, что это значит. Прямой transition() при этом остаётся
        единственным местом, где статус меняется.
        """
        cur = Status(self.status)
        if cur == to:
            return True
        path = transition_path(cur, to)
        if not path:
            return False
        for step in path:
            self.transition(step, reason=reason if step == to else "")
        return True


# Порядок обхода при поиске пути: обход должен быть предсказуемым, а
# промежуточные состояния — «естественными». Иначе путь к INTERVIEW_CONFIRMED
# может пролечь через NEEDS_HUMAN и заявка окажется помеченной как
# требующая человека, хотя человек её только что и подтвердил.
_PATH_RANK = {}


def _init_path_rank():
    order = [Status.REPLIED, Status.IN_DIALOGUE, Status.INTERVIEW_PROPOSED,
             Status.INTERVIEW_CONFIRMED, Status.INTERVIEW_DONE, Status.OFFER,
             Status.SENT, Status.AWAITING_REPLY, Status.CONTENT_READY,
             Status.PENDING_APPROVAL, Status.APPROVED, Status.SENDING]
    for i, st in enumerate(order):
        _PATH_RANK[st] = i
    for st in (Status.NEEDS_HUMAN, Status.FOLLOWUP_PENDING_APPROVAL,
               Status.FOLLOWED_UP, Status.NO_REPLY_CLOSED):
        _PATH_RANK[st] = 80
    for st in TERMINAL:
        _PATH_RANK[st] = 99


_init_path_rank()


def transition_path(cur: Status, to: Status, max_len: int = 4) -> list:
    """Кратчайшая цепочка разрешённых переходов cur → to. Пусто — пути нет.

    Нужна там, где событие перепрыгивает через промежуточные состояния:
    рекрутёр в первом же ответе называет время, и заявка должна пройти
    AWAITING_REPLY → REPLIED → IN_DIALOGUE → INTERVIEW_PROPOSED. Писать эти
    цепочки руками в каждом месте — верный способ однажды ошибиться и
    получить TransitionError в проде.
    """
    if cur == to:
        return []

    def bfs(intermediate_ok) -> list:
        seen = {cur}
        queue: list[tuple[Status, list[Status]]] = [(cur, [])]
        while queue:
            node, path = queue.pop(0)
            if len(path) >= max_len:
                continue
            for nxt in sorted(ALLOWED_TRANSITIONS.get(node, set()),
                              key=lambda st: (_PATH_RANK.get(st, 50), st.value)):
                if nxt in seen:
                    continue
                if nxt == to:
                    return path + [nxt]
                seen.add(nxt)
                if intermediate_ok(nxt):
                    queue.append((nxt, path + [nxt]))
        return []

    # Сначала ищем путь только через «естественные» состояния: NEEDS_HUMAN и
    # терминальные не должны мелькать транзитом, даже если через них короче.
    # Не нашли — разрешаем всё, кроме терминальных (из них выхода нет).
    return (bfs(lambda st: _PATH_RANK.get(st, 50) < 80)
            or bfs(lambda st: st not in TERMINAL))


class ApplicationFeedback(Base):
    """Owner feedback does not replace the durable manual-outreach outcome."""
    __tablename__ = "application_feedback"
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    reason: Mapped[str] = mapped_column(String, default="unspecified")
    actor_id: Mapped[int] = mapped_column(BigInteger, default=0)
    action_key: Mapped[str] = mapped_column(String, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ResultEvent(Base):
    """Observed milestones, not synthetic intermediate state transitions."""
    __tablename__ = "result_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    source: Mapped[str] = mapped_column(String)
    event_key: Mapped[str] = mapped_column(String, unique=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    details_json: Mapped[dict] = mapped_column(JSON, default=dict)


class OwnerPreference(Base):
    """Persist the owner's selected track without replacing an issued card."""
    __tablename__ = "owner_preferences"
    owner_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    outreach_track: Mapped[str] = mapped_column(String, default="all")


class Batch(Base):
    __tablename__ = "batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    planned_count: Mapped[int] = mapped_column(Integer, default=0)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    approved_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    aborted_reason: Mapped[str] = mapped_column(String, default="")
    day_quota_at_creation: Mapped[int] = mapped_column(Integer, default=0)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    direction: Mapped[str] = mapped_column(String)                 # out | in
    telegram_msg_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    body: Mapped[str] = mapped_column(Text, default="")
    body_hash: Mapped[str] = mapped_column(String, default="", index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_auto: Mapped[bool] = mapped_column(Boolean, default=False)
    classifier_label: Mapped[str] = mapped_column(String, default="")
    classifier_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    # Второе мнение LLM (convo/verify.py). Пишется только для опасных меток
    # (отказ, unknown); расхождение с classifier_label — метрика качества
    # регэксов: по ней видно, какие паттерны чинить.
    llm_label: Mapped[str] = mapped_column(String, default="")
    llm_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    # Сбой LLM — это «спросить позже», а не «мнения нет никогда». Без этих
    # трёх полей восемь диалогов навсегда остались с llm_label='' после
    # часа, когда все провайдеры разом отдавали 429.
    llm_attempts: Mapped[int] = mapped_column(Integer, default=0)
    llm_next_try_at: Mapped[datetime | None] = mapped_column(DateTime,
                                                             nullable=True)
    llm_error: Mapped[str] = mapped_column(String, default="")
    escalated: Mapped[bool] = mapped_column(Boolean, default=False)
    # Сохранение входящего и принятие решения — разные операции. Сбой между
    # ними остаётся виден владельцу; старые сообщения не запускаются заново.
    processing_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    processing_error: Mapped[str] = mapped_column(String, default="")
    # ── Почтовый канал ──
    # UID письма в папке IMAP; 0 у исходящих и у телеграмных.
    email_uid: Mapped[int] = mapped_column(Integer, default=0)
    # Message-ID: свой у исходящего, чужой у входящего. По нему потом
    # находится ответ через In-Reply-To/References — единственный способ
    # привязки, переживающий ответ с другого адреса.
    email_message_id: Mapped[str] = mapped_column(String, default="", index=True)
    email_in_reply_to: Mapped[str] = mapped_column(String, default="")
    # Кто написал НА САМОМ ДЕЛЕ: рекрутёр часто отвечает с личного ящика,
    # и отвечать надо туда же, а не на исходный hr@.
    email_from: Mapped[str] = mapped_column(String, default="")
    email_subject: Mapped[str] = mapped_column(String, default="")
    # Каким правилом привязали письмо к заявке. Без этого поля разобрать
    # ложную привязку через месяц невозможно.
    match_rule: Mapped[str] = mapped_column(String, default="")


class SendLog(Base):
    __tablename__ = "send_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    batch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    result: Mapped[str] = mapped_column(String, default="")        # ok | flood | peerflood | error
    error_class: Mapped[str] = mapped_column(String, default="")
    error_seconds: Mapped[int] = mapped_column(Integer, default=0)
    peer_id: Mapped[str] = mapped_column(String, default="")


class AccountHealth(Base):
    __tablename__ = "account_health"
    id: Mapped[int] = mapped_column(primary_key=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    is_restricted: Mapped[bool] = mapped_column(Boolean, default=False)
    restriction_reason: Mapped[str] = mapped_column(String, default="")
    spambot_raw: Mapped[str] = mapped_column(Text, default="")
    spambot_verdict: Mapped[str] = mapped_column(String, default="")
    premium: Mapped[bool] = mapped_column(Boolean, default=False)
    user_id: Mapped[str] = mapped_column(String, default="")
    oldest_auth_date: Mapped[str] = mapped_column(String, default="")
    dialogs_count: Mapped[int] = mapped_column(Integer, default=0)
    contacts_count: Mapped[int] = mapped_column(Integer, default=0)


class DailyQuota(Base):
    __tablename__ = "daily_quota"
    date: Mapped[str] = mapped_column(String, primary_key=True)     # YYYY-MM-DD
    planned_cap: Mapped[int] = mapped_column(Integer, default=0)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    resolve_count: Mapped[int] = mapped_column(Integer, default=0)
    peerflood_count: Mapped[int] = mapped_column(Integer, default=0)
    floodwait_total_seconds: Mapped[int] = mapped_column(Integer, default=0)
    clean_day: Mapped[bool] = mapped_column(Boolean, default=True)
    ramp_stage: Mapped[int] = mapped_column(Integer, default=1)


class SendLock(Base):
    __tablename__ = "send_lock"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scope: Mapped[str] = mapped_column(String, default="")          # cold_only | all
    reason: Mapped[str] = mapped_column(String, default="")
    set_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    set_by: Mapped[str] = mapped_column(String, default="")


class BotState(Base):
    """Состояние бота-пульта. Одна строка, id=1.

    Отдельно от CampaignState намеренно: там owner_last_seen_msg_id — водяной
    знак «Избранного» в пространстве id сообщений MTProto, а update_id у Bot
    API совсем другое и обычно на порядки больше. Смешать их значит либо
    навсегда проглотить команды в «Избранном», либо переисполнить старые.
    """
    __tablename__ = "bot_state"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    updates_offset: Mapped[int] = mapped_column(Integer, default=0)
    last_update_id: Mapped[int] = mapped_column(Integer, default=0)
    # Модель «один экран»: бот правит одно сообщение вместо ленты сводок.
    screen_chat_id: Mapped[int] = mapped_column(BigInteger, default=0)
    screen_msg_id: Mapped[int] = mapped_column(Integer, default=0)
    # Ожидание свободного ввода после кнопки «Другое время» / «Свой текст».
    awaiting_kind: Mapped[str] = mapped_column(String, default="")
    awaiting_req_id: Mapped[int] = mapped_column(Integer, default=0)
    awaiting_text: Mapped[str] = mapped_column(Text, default="")
    awaiting_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BotTask(Base):
    """Тяжёлая задача от кнопки бота (анкета, письмо, сводка почты).

    В базе, а не в памяти процесса: offset апдейта подтверждается сразу после
    разбора, Telegram повтора не пришлёт — задача в списке в памяти при
    рестарте (деплой, падение) пропадала бы молча, вместе с нажатием
    владельца.
    """
    __tablename__ = "bot_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, default=0)
    task: Mapped[str] = mapped_column(String, default="")
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # pending → running → done/failed. Задача не удаляется до завершения:
    # падение бота теперь приводит к повтору после истечения lease.
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_try_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(String, default="")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PendingReply(Base):
    """Ответы, отложенные до утра.

    Ночью автоматика молчит (окно 09:00-21:00), но водяной знак входящих уже
    сдвинут — раньше это значило, что ответ не уйдёт никогда: сообщение
    прочитано, а повторно его никто не подаст. Теперь ночное входящее ложится
    сюда, и утренний проход перепринимает решение целиком — свежим
    plan_reply, со свежим статусом, защёлкой NEEDS_HUMAN и лимитом
    автоответов. Храним ВХОДЯЩИЙ текст, а не сочинённый ответ: за ночь
    контекст мог измениться, и решение восьмичасовой давности доверия
    не заслуживает.
    """
    __tablename__ = "pending_replies"
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id"), index=True)
    incoming_text: Mapped[str] = mapped_column(Text, default="")
    body_hash: Mapped[str] = mapped_column(String, default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(String, default="")


class BotOutbox(Base):
    """Исходящая очередь уведомлений: автопилот пишет, бот доставляет.

    Автопилот не знает токена бота и знать не должен: чем меньше процессов
    держат ключ, дающий управление отправкой, тем лучше. Общая шина — та же
    SQLite, что и всё остальное.

    dedup_key с UNIQUE спасает от шторма одинаковых сообщений: PeerFlood
    проверяется каждые 20 минут, и без ключа чат заполнился бы сотней
    идентичных предупреждений за день.
    """
    __tablename__ = "bot_outbox"
    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, default=0)   # 0 = всем из белого списка
    kind: Mapped[str] = mapped_column(String, default="", index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    markup_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # Заполнено — правим существующее сообщение вместо отправки нового.
    target_msg_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    owner_request_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dedup_key: Mapped[str] = mapped_column(String, default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(String, default="")
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ChannelCandidate(Base):
    """Канал, найденный автопоиском: что нашли, чем измерили, взяли ли в сбор.

    Отдельная таблица, а не список в коде: решение о канале принимается по
    измерениям (свежесть, доля постов с контактом), эти измерения полезно
    хранить и перепроверять, а плохие каналы — помнить, чтобы не проверять
    их заново на каждом прогоне.
    """
    __tablename__ = "channel_candidates"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True, index=True)
    title: Mapped[str] = mapped_column(String, default="")
    found_via: Mapped[str] = mapped_column(String, default="")
    subscribers: Mapped[int] = mapped_column(Integer, default=0)
    posts_seen: Mapped[int] = mapped_column(Integer, default=0)
    fresh_7d: Mapped[int] = mapped_column(Integer, default=0)
    posts_with_contact: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    jobs_found: Mapped[int] = mapped_column(Integer, default=0)


class TelegramChannelStat(Base):
    """Последний результат чтения каждого канала Telegram.

    Вакансии хранятся в ``jobs``, но до этого не было следа для каналов, где
    сборщик получил пустую страницу или сетевую ошибку. Из-за этого «канал
    молчит» и «канал не прочитался» выглядели одинаково. Эта таблица хранит
    короткий операционный след без копирования содержимого постов.
    """
    __tablename__ = "telegram_channel_stats"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True, index=True)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(String, default="never")
    last_error: Mapped[str] = mapped_column(String, default="")
    last_pages: Mapped[int] = mapped_column(Integer, default=0)
    last_posts: Mapped[int] = mapped_column(Integer, default=0)
    last_vacancies: Mapped[int] = mapped_column(Integer, default=0)
    last_contacts: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    total_scans: Mapped[int] = mapped_column(Integer, default=0)
    oldest_post_id: Mapped[int] = mapped_column(Integer, default=0)
    newest_post_id: Mapped[int] = mapped_column(Integer, default=0)
    history_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    rejected_posts: Mapped[int] = mapped_column(Integer, default=0)


class RuntimeState(Base):
    """Последний проход источника или следующее выполнение задания."""
    __tablename__ = "runtime_state"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String, default="never")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str] = mapped_column(String, default="")
    details_json: Mapped[dict] = mapped_column(JSON, default=dict)


class ApplyAnswer(Base):
    """Ответ владельца на скрининговый вопрос — чтобы спросить один раз.

    Формы разных компаний спрашивают одно и то же дословно: «требуется ли
    спонсорство визы», «страна проживания», «работали ли вы у нас раньше».
    Ключ — нормализованный хеш вопроса, поэтому мелкие расхождения в
    пунктуации не заводят дубль.
    """
    __tablename__ = "apply_answers"
    id: Mapped[int] = mapped_column(primary_key=True)
    question_key: Mapped[str] = mapped_column(String, unique=True, index=True)
    label: Mapped[str] = mapped_column(String, default="")
    provider: Mapped[str] = mapped_column(String, default="")
    field_type: Mapped[str] = mapped_column(String, default="")
    answer_value: Mapped[str] = mapped_column(Text, default="")
    lang: Mapped[str] = mapped_column(String, default="en")
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AtsCandidate(Base):
    """ATS-доска компании, найденная в ссылках собранных вакансий.

    Тот же паттерн, что у ChannelCandidate: кандидат → проверка живым
    запросом → явное включение → подмешивание в сбор. Реестр ATS до этой
    таблицы пополнялся только руками, при том что в уже собранных вакансиях
    лежали ссылки на десятки непокрытых досок (greenhouse/lever/ashby/
    workable) — их вакансии просто проходили мимо.
    """
    __tablename__ = "ats_candidates"
    __table_args__ = (UniqueConstraint("provider", "token"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String, index=True)
    token: Mapped[str] = mapped_column(String, index=True)
    company_name: Mapped[str] = mapped_column(String, default="")
    found_via: Mapped[str] = mapped_column(String, default="")
    jobs_seen: Mapped[int] = mapped_column(Integer, default=0)
    relevant_jobs: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class HandleCache(Base):
    __tablename__ = "handle_cache"
    handle_norm: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, default="")
    access_hash: Mapped[str] = mapped_column(String, default="")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolve_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(String, default="")


class CampaignState(Base):
    """Одна строка на кампанию: глобальный потолок, счётчики чистых дней."""
    __tablename__ = "campaign_state"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    quota_ceiling: Mapped[int] = mapped_column(Integer, default=15)
    consecutive_clean_days: Mapped[int] = mapped_column(Integer, default=0)
    peerflood_total: Mapped[int] = mapped_column(Integer, default=0)
    manual_only: Mapped[bool] = mapped_column(Boolean, default=False)
    # Момент последнего ХОЛОДНОГО сообщения. Темп холодных задан паузой между
    # ними, а не дневным счётчиком (решение владельца 23.09), и пауза обязана
    # переживать перезапуск: счётчик в памяти процесса обнулялся бы при каждом
    # вызове планировщика, и первое сообщение каждого прогона уходило бы сразу.
    last_cold_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # id последнего разобранного сообщения из «Избранного» — чтобы не
    # обрабатывать одну и ту же команду владельца дважды.
    owner_last_seen_msg_id: Mapped[int] = mapped_column(Integer, default=0)
    # Водяной знак почтового ящика. UID уникален только внутри текущего
    # uidvalidity: сменилось — старые номера указывают на другие письма,
    # и знак надо сбрасывать, иначе часть почты будет пропущена молча.
    imap_uidvalidity: Mapped[int] = mapped_column(Integer, default=0)
    imap_last_uid: Mapped[int] = mapped_column(Integer, default=0)


class OwnerRequestKind(str, enum.Enum):
    SLOT_CONFIRM = "slot_confirm"      # рекрутёр предложил время — подтвердить?
    NEEDS_HUMAN = "needs_human"        # автоматика не берётся отвечать
    DIGEST = "digest"                  # сводка, ответа не требует


class OwnerRequest(Base):
    """Вопрос владельцу и его решение.

    Ни одно интервью не подтверждается и ни один нешаблонный ответ не уходит
    работодателю, пока здесь не появится decision. Это и есть требование
    «предварительно уточнив у меня»: карточка уходит в «Избранное», владелец
    отвечает командой, решение применяется.
    """
    __tablename__ = "owner_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String, default=OwnerRequestKind.SLOT_CONFIRM.value)
    question: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    owner_msg_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # "" пока не ответил | ok | no | time | say | skip | expired
    decision: Mapped[str] = mapped_column(String, default="", index=True)
    decision_note: Mapped[str] = mapped_column(Text, default="")
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    apply_error: Mapped[str] = mapped_column(String, default="")
    # Машинный аргумент решения: индекс слота, строка времени, текст ответа.
    # decision_note остаётся человекочитаемым примечанием.
    decision_arg: Mapped[str] = mapped_column(Text, default="")
    # Куда ушла карточка и где её править по результату исполнения.
    owner_chat_id: Mapped[int] = mapped_column(BigInteger, default=0)
    channel: Mapped[str] = mapped_column(String, default="")      # saved | bot
    decided_by: Mapped[str] = mapped_column(String, default="")   # bot:<uid> | saved
    # Счётчик попыток исполнения: карточка, роняющая исполнитель, не должна
    # уходить в бесконечный ретрай и блокировать очередь.
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_try_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ChannelStat(Base):
    """Канал-источник вакансий и его измеренное качество.

    Нужен, чтобы автопоиск каналов был воспроизводимым: почему канал попал
    в работу, когда последний раз проверялся, сколько из него пришло вакансий
    с контактом. Мёртвые каналы отключаются автоматически, а не руками.
    """
    __tablename__ = "channel_stats"
    username: Mapped[str] = mapped_column(String, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    discovered_from: Mapped[str] = mapped_column(String, default="")
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    posts: Mapped[int] = mapped_column(Integer, default=0)
    fresh7: Mapped[int] = mapped_column(Integer, default=0)
    contacts: Mapped[int] = mapped_column(Integer, default=0)
    vacancy_posts: Mapped[int] = mapped_column(Integer, default=0)
    resume_posts: Mapped[int] = mapped_column(Integer, default=0)
    stack_hits: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    reject_reason: Mapped[str] = mapped_column(String, default="")
    jobs_ingested: Mapped[int] = mapped_column(Integer, default=0)
