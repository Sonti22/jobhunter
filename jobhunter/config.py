"""Конфигурация из .env. Паттерн pydantic-settings + lru_cache."""
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # careered.io
    careered_access_token: str = Field(default="")
    careered_auth: str = Field(default="")
    careered_ua: str = Field(
        default="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    )
    careered_query: str = Field(default="links_type=all&links=telegram&remote=true")
    careered_insecure_tls: bool = Field(default=False)
    careered_spki_pin: str = Field(default="")

    # Отбор вакансий.
    # Возраст поста: четыре ответа рекрутёров из семи на первой партии были
    # «вакансия закрыта» / «она тоже старая». Каналы держат ленту месяцами,
    # ATS-фиды отдают всё открытое. 21 день — компромисс: живые вакансии
    # закрываются в среднем за 3-4 недели, а более жёсткий порог отрезал бы
    # источники, где дата известна только с точностью до дня публикации.
    max_vacancy_age_days: int = Field(default=21)

    # Базовое резюме. Если путь задан — к отклику прикладывается ИМЕННО этот
    # файл, а сгенерированное под вакансию не отправляется (но всё равно
    # собирается: на нём держится гейт и часть скоринга). Нужно, когда владелец
    # хочет рассылать своё «основное» резюме без вариаций.
    # Пусто — работает обычная подгонка под роль.
    base_cv_path: str = Field(default="")

    # отправка
    kill_switch_path: str = Field(default="STOP_SENDING.flag")
    daily_cold_limit: int = Field(default=30)
    send_cv_with_first_message: bool = Field(default=True)
    dry_run: bool = Field(default=False)

    # Telegram (MTProto, личный аккаунт).
    # str, а не int: пустое значение в .env до заполнения ключей не должно
    # ронять весь конфиг — до Telegram-фазы система работает и без него.
    telegram_api_id: str = Field(default="")
    telegram_api_hash: str = Field(default="")
    telegram_phone: str = Field(default="")
    telegram_session_path: str = Field(default="jobhunter.session")
    # Публичная лента t.me: читаем несколько страниц за проход и повторяем
    # проходы в течение дня. Это не история всего канала, но существенно
    # уменьшает окно, в котором новая вакансия может быть пропущена.
    telegram_ingest_pages: int = Field(default=3)
    telegram_ingest_throttle: float = Field(default=2.0)

    # LLM (бесплатные тиры). Без ключей система работает на шаблонах.
    gemini_api_key: str = Field(default="")
    groq_api_key: str = Field(default="")
    openrouter_api_key: str = Field(default="")
    cerebras_api_key: str = Field(default="")
    mistral_api_key: str = Field(default="")
    github_models_token: str = Field(default="")
    # Модели проверены живыми 2026-08-24; списки бесплатных тиров плавают,
    # при HTTP 404 от провайдера — спросить у него /models и обновить тут.
    llm_gemini_model: str = Field(default="gemini-2.5-flash")
    llm_groq_model: str = Field(default="openai/gpt-oss-120b")
    llm_openrouter_model: str = Field(default="z-ai/glm-5.2:free")
    llm_cerebras_model: str = Field(default="llama-3.3-70b")
    llm_mistral_model: str = Field(default="mistral-small-latest")
    llm_github_model: str = Field(default="openai/gpt-4o-mini")
    llm_enabled: bool = Field(default=True)
    # PII не уходит во внешнюю модель по умолчанию; провайдеры можно
    # ограничить явным allowlist через запятую.
    llm_redact_pii: bool = Field(default=True)
    llm_providers: str = Field(default="")
    # Второе мнение LLM для опасных меток классификатора (отказ, unknown).
    # Подчинён llm_enabled; выключение возвращает чисто-regex поведение,
    # но закрытие по отказу тогда невозможно вовсе — только карточка.
    llm_classify_enabled: bool = Field(default=True)
    # Ночной входящий ждёт утра в очереди; провисевший дольше этого — уже не
    # «ответим сами», а карточка владельцу: контекст устарел.
    night_queue_max_age_hours: int = Field(default=20)
    # Оживление застрявших диалогов: сколько ждать решения владельца,
    # прежде чем переклассифицировать и ответить самим.
    # Подготовка анкет для ручного отклика (jobhunter/apply/).
    # Смелая автономность: бот сам отвечает на техвопросы и вопрос о
    # формате работы. Деньги, оффер и слоты остаются владельцу всегда.
    bold_autonomy: bool = Field(default=False)
    apply_prep_daily_limit: int = Field(default=25)
    apply_form_fetch_throttle: float = Field(default=1.5)
    # Дневной потолок автоподачи в ATS (apply/submit_ashby.py). Отдельный от
    # телеграмной квоты policy: та защищает аккаунт Telegram от анти-спама,
    # здесь же — просто разумный темп откликов через формы.
    ats_daily_limit: int = Field(default=5)
    revive_enabled: bool = Field(default=True)
    revive_after_hours: int = Field(default=18)
    revive_max_attempts: int = Field(default=2)
    # Рутинные автоответы через LLM (с гейтом и откатом на шаблоны).
    # false — чистые шаблоны, как до этой функции.
    llm_auto_reply_enabled: bool = Field(default=True)
    # Сверка предлагаемых слотов с Google Calendar (free/busy). Fail-open:
    # ошибка календаря не останавливает ответы, просто без его данных.
    gcal_freebusy_enabled: bool = Field(default=True)
    # Буфер вокруг интервью: слот впритык к другому созвону — тоже занят.
    interview_busy_buffer_min: int = Field(default=30)

    # ── Куда складывать следы работы бота ───────────────────────────────
    # Отдельная папка-вкладка в Telegram со всеми диалогами, куда бот писал.
    # «Избранное» остаётся каналом управления (карточки на решение), а
    # переписка по вакансиям живёт отдельно и не мешает командам.
    tg_folder_enabled: bool = Field(default=True)
    tg_folder_name: str = Field(default="Работа")
    # Копия каждого отправленного сообщения на диск: Telegram может удалить
    # сообщение, аккаунт может уйти в ограничение, а история откликов нужна
    # хотя бы для того, чтобы не написать одному рекрутёру дважды разное.
    sent_archive_enabled: bool = Field(default=True)
    sent_archive_dir: str = Field(default=str(ROOT / "logs" / "sent"))

    # ── Чтение почты (IMAP) ─────────────────────────────────────────────
    # Отдельных учётных данных не нужно: пароль приложения Gmail выдаётся на
    # аккаунт, а не на протокол, поэтому SMTP_APP_PASSWORD работает и здесь.
    imap_enabled: bool = Field(default=True)
    imap_host: str = Field(default="imap.gmail.com")
    imap_port: int = Field(default=993)
    # INBOX не локализуется; «Вся почта» у Gmail зависит от языка интерфейса
    # ([Gmail]/All Mail против [Gmail]/Вся почта), и хардкодить её нельзя.
    imap_folder: str = Field(default="INBOX")
    imap_max_fetch: int = Field(default=200)
    # Автоответы по почте — отдельный рубильник от общего auto_reply_enabled:
    # читать письма можно захотеть раньше, чем доверить боту отвечать на них.
    email_auto_reply_enabled: bool = Field(default=True)
    # Не отвечать, если сами писали недавно: петля из двух автоответчиков
    # сходится к этому интервалу на итерацию и гасится лимитом ответов.
    email_reply_min_gap_min: int = Field(default=30)
    # Соль для подписи в plus-адресе (suren6pro+jh42xABC123@gmail.com).
    # Пусто — подпись считается от api-hash, лишь бы не была предсказуемой.
    mail_bind_secret: str = Field(default="")

    # ── Переписка (входящие) ────────────────────────────────────────────
    # Автоматика отвечает только на рутину (см. convo/classify.py). Всё
    # остальное уходит владельцу. false — не отвечать вообще, только читать
    # и складывать в NEEDS_HUMAN.
    auto_reply_enabled: bool = Field(default=True)
    inbox_lookback_days: int = Field(default=45)
    # Сколько диалогов просматривать за один опрос. Верхняя граница нужна,
    # чтобы опрос не превращался в обход всего списка чатов аккаунта.
    inbox_max_threads: int = Field(default=120)

    # ── Согласование с владельцем ───────────────────────────────────────
    # Канал связи с владельцем — «Избранное» (Saved Messages) собственного
    # аккаунта: не нужен ни бот от BotFather, ни второй токен, и переписка
    # не видна никому. Владелец отвечает командами (/ok, /no, /time, /say).
    owner_tz: str = Field(default="Europe/Moscow")
    owner_confirm_required: bool = Field(default=True)
    # Сколько часов ждать решения владельца, прежде чем признать карточку
    # протухшей. Слот на завтра, подтверждённый через сутки, — хуже, чем
    # честное «не успели».
    owner_decision_ttl_hours: int = Field(default=12)

    # ── Интервью и календарь ────────────────────────────────────────────
    interview_duration_min: int = Field(default=60)
    gcal_enabled: bool = Field(default=True)
    google_client_secret_path: str = Field(default="google_client_secret.json")
    google_token_path: str = Field(default="google_token.json")
    gcal_calendar_id: str = Field(default="primary")
    # Google Meet создаётся вместе с событием. Ссылка уходит рекрутёру в
    # подтверждении — не нужно ждать, пока он пришлёт свою.
    gcal_create_meet: bool = Field(default=True)

    # ── Автопоиск каналов ───────────────────────────────────────────────
    discover_enabled: bool = Field(default=True)
    # Потолок на прогон: t.me читается как обычным посетителем, 1 запрос в
    # 2 секунды (см. оговорку по ToS в README). 60 проверок ≈ 2 минуты.
    discover_max_checks: int = Field(default=60)
    discover_min_fresh7: int = Field(default=3)
    discover_min_contacts: int = Field(default=2)

    # Email
    smtp_host: str = Field(default="smtp.gmail.com")
    # 465 — TLS с момента подключения; остальные порты — обязательный STARTTLS.
    smtp_port: int = Field(default=587)
    smtp_user: str = Field(default="")
    smtp_app_password: str = Field(default="")
    smtp_from_name: str = Field(default="Suren Hakobyan")
    # Цель прогрева. Фактический потолок дня — policy.email_daily_cap: старт с
    # email_warmup_start и +email_warmup_step за каждый день без отбивок.
    email_daily_limit: int = Field(default=80)
    email_warmup_start: int = Field(default=40)
    email_warmup_step: int = Field(default=5)
    # Отбивки до запуска прогрева не учитывались — те дни «чистыми» не считаем.
    email_warmup_since: str = Field(default="2026-09-17")
    # Больше стольких отбивок за день — почта стоит до завтра.
    email_bounce_stop: int = Field(default=3)

    # ── Reddit: официальный Data API, приложение типа «script» ─────────────
    # Анонимный доступ закрыт (403/429). Без id/secret источник пропускается.
    reddit_client_id: str = Field(default="")
    reddit_client_secret: str = Field(default="")
    reddit_subreddits: str = Field(
        default="forhire,remotepython,pythonjobs,devopsjobs,MachineLearningJobs")

    # ── Пути ────────────────────────────────────────────────────────────
    # В контейнере переопределяются переменными окружения: база и сессия
    # уезжают в том Docker, а всё, что владелец открывает глазами, — в
    # смонтированную папку проекта.
    db_path: str = Field(default=str(ROOT / "jobhunter.db"))
    cv_out: str = Field(default=str(ROOT / "cv_out"))
    profile_path: str = Field(default=str(ROOT / "profile.yaml"))
    lexicon_path: str = Field(default=str(ROOT / "lexicon" / "tech_terms.txt"))
    # Каталог видимых результатов: interviews.ics, INTERVIEWS.md.
    out_dir: str = Field(default=str(ROOT))
    # Логи. Раньше считались как «рядом с базой» — в контейнере это увело бы
    # их в том, где их не видно.
    log_dir: str = Field(default=str(ROOT / "logs"))
    # Файлы живости процессов для healthcheck контейнеров.
    heartbeat_dir: str = Field(default=str(ROOT / "logs"))

    # ── Telegram-бот владельца (BotFather) ──────────────────────────────
    # Второй, отдельный от личного аккаунта канал: статистика, кнопки,
    # уведомления. Писать рекрутёрам он не может и не должен — это делает
    # только процесс, владеющий jobhunter.session.
    telegram_bot_token: str = Field(default="")
    # Кто имеет право нажимать кнопки: id через запятую. ПУСТО = бот не
    # выполняет ничего. Бот управляет отправкой от лица владельца, поэтому
    # открытым по умолчанию быть не может.
    bot_allowed_user_ids: str = Field(default="")
    # Куда уходят карточки на решение: bot | saved | both.
    owner_channel: str = Field(default="saved")

    # Дашборд: в контейнере слушать 0.0.0.0, наружу машины порт всё равно
    # публикуется только на 127.0.0.1.
    web_host: str = Field(default="127.0.0.1")
    web_port: int = Field(default=8765)

    @property
    def base_url(self) -> str:
        return "https://careered.io"

    @property
    def auth_header(self) -> str:
        """Готовое значение заголовка Authorization."""
        if self.careered_auth:
            return self.careered_auth
        if self.careered_access_token:
            return "Bearer " + self.careered_access_token
        return ""

    @property
    def tg_api_id(self) -> int:
        """api_id числом; 0 означает «ключи ещё не заданы»."""
        try:
            return int(str(self.telegram_api_id).strip() or 0)
        except ValueError:
            return 0

    @property
    def kill_switch(self) -> Path:
        p = Path(self.kill_switch_path)
        return p if p.is_absolute() else ROOT / p

    @property
    def bot_owner_ids(self) -> set:
        """Кому разрешено управлять ботом. Пустое множество = никому."""
        raw = (self.bot_allowed_user_ids or "").replace(" ", "")
        return {int(x) for x in raw.split(",") if x.isdigit()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
