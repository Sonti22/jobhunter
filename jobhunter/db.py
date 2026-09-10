"""Движок БД: SQLite в WAL, timeout=30 (см. риск database-is-locked)."""
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import cast

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base

_engine = None
_Session = None
SCHEMA_VERSION = 3


def _init():
    global _engine, _Session
    if _engine is not None:
        return
    s = get_settings()
    _engine = create_engine(
        "sqlite:///" + s.db_path,
        connect_args={"timeout": 30, "check_same_thread": False},
        future=True,
    )

    @event.listens_for(_engine, "connect")
    def _pragmas(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA foreign_keys=ON")
        # Контрольная точка каждые ~1000 страниц журнала. С несколькими
        # процессами WAL растёт быстрее, чем сводится сам: любой открытый
        # читатель откладывает checkpoint.
        cur.execute("PRAGMA wal_autocheckpoint=1000")
        cur.close()

    Base.metadata.create_all(_engine)
    _migrate(_engine)
    _Session = sessionmaker(bind=_engine, class_=Session, expire_on_commit=False)


# Новые колонки, добавленные после первого запуска. create_all() создаёт
# только отсутствующие ТАБЛИЦЫ и молча игнорирует новые колонки в старых —
# без этого шага база с историей откликов падала бы на «no such column».
# Тип и DEFAULT пишутся как в SQL: SQLite умеет ADD COLUMN только с
# константным дефолтом, поэтому NULL-время добавляем без него.
_ADDED_COLUMNS = {
    "jobs": [
        ("last_seen_at", "DATETIME"),
        ("closed_at", "DATETIME"),
        ("is_closed", "INTEGER DEFAULT 0"),
    ],
    "applications": [
        ("revive_attempts", "INTEGER DEFAULT 0"),
        ("auto_tech_replies_count", "INTEGER DEFAULT 0"),
        ("apply_packet_json", "JSON"),
        ("apply_prepared_at", "DATETIME"),
        ("apply_form_hash", "VARCHAR DEFAULT ''"),
        ("revived_at", "DATETIME"),
        ("interview_duration_min", "INTEGER DEFAULT 60"),
        ("interview_slots_json", "JSON"),
        ("gcal_event_id", "VARCHAR DEFAULT ''"),
        ("gcal_link", "VARCHAR DEFAULT ''"),
        ("last_inbound_msg_id", "INTEGER DEFAULT 0"),
        ("review_note", "VARCHAR DEFAULT ''"),
        ("followup_body", "TEXT DEFAULT ''"),
        ("outcome", "VARCHAR DEFAULT ''"),
        ("applied_at", "DATETIME"),
        ("snooze_until", "DATETIME"),
        ("email_peer", "VARCHAR DEFAULT ''"),
        ("email_thread_refs", "JSON"),
        ("telegram_file_random_id", "INTEGER"),
        ("telegram_followup_random_id", "INTEGER"),
        ("send_channel", "VARCHAR DEFAULT ''"),
        ("send_idempotency_key", "VARCHAR DEFAULT ''"),
        ("send_last_attempt_at", "DATETIME"),
        ("send_next_try_at", "DATETIME"),
        ("send_error_detail", "VARCHAR DEFAULT ''"),
        ("cv_template_version", "VARCHAR DEFAULT ''"),
        ("message_prompt_version", "VARCHAR DEFAULT ''"),
        ("llm_provider", "VARCHAR DEFAULT ''"),
    ],
    "campaign_state": [
        ("owner_last_seen_msg_id", "INTEGER DEFAULT 0"),
        ("imap_uidvalidity", "INTEGER DEFAULT 0"),
        ("imap_last_uid", "INTEGER DEFAULT 0"),
    ],
    "owner_requests": [
        ("next_try_at", "DATETIME"),
        ("decision_arg", "TEXT DEFAULT ''"),
        ("owner_chat_id", "BIGINT DEFAULT 0"),
        ("channel", "VARCHAR DEFAULT ''"),
        ("decided_by", "VARCHAR DEFAULT ''"),
        ("attempts", "INTEGER DEFAULT 0"),
    ],
    "messages": [
        ("email_uid", "INTEGER DEFAULT 0"),
        ("email_message_id", "VARCHAR DEFAULT ''"),
        ("email_in_reply_to", "VARCHAR DEFAULT ''"),
        ("email_from", "VARCHAR DEFAULT ''"),
        ("email_subject", "VARCHAR DEFAULT ''"),
        ("match_rule", "VARCHAR DEFAULT ''"),
        ("llm_label", "VARCHAR DEFAULT ''"),
        ("llm_confidence", "FLOAT DEFAULT 0"),
        ("llm_attempts", "INTEGER DEFAULT 0"),
        ("llm_next_try_at", "DATETIME"),
        ("llm_error", "VARCHAR DEFAULT ''"),
        ("processing_pending", "INTEGER DEFAULT 0"),
        ("processing_error", "VARCHAR DEFAULT ''"),
    ],
    "bot_tasks": [
        ("status", "VARCHAR DEFAULT 'pending'"),
        ("claimed_at", "DATETIME"),
        ("attempts", "INTEGER DEFAULT 0"),
        ("next_try_at", "DATETIME"),
        ("last_error", "VARCHAR DEFAULT ''"),
        ("finished_at", "DATETIME"),
    ],
    "bot_outbox": [
        ("claimed_at", "DATETIME"),
    ],
    "telegram_channel_stats": [
        ("oldest_post_id", "INTEGER DEFAULT 0"),
        ("newest_post_id", "INTEGER DEFAULT 0"),
        ("history_complete", "INTEGER DEFAULT 0"),
        ("rejected_posts", "INTEGER DEFAULT 0"),
    ],
}

# Индексы для колонок, добавленных задним числом. ALTER TABLE ADD COLUMN
# индекс не создаёт, а create_all() отрабатывает только на пустой базе —
# без этого списка index=True в модели остаётся декорацией, и поиск ответа
# по Message-ID пойдёт полным сканом таблицы сообщений.
_ADDED_INDEXES = [
    ("ix_messages_email_message_id", "messages", "email_message_id"),
    ("ix_applications_email_peer", "applications", "email_peer"),
    ("ix_applications_outcome", "applications", "outcome"),
    ("ix_jobs_last_seen_at", "jobs", "last_seen_at"),
    ("ix_jobs_is_closed", "jobs", "is_closed"),
    ("ix_applications_send_next_try_at", "applications", "send_next_try_at"),
    ("ix_bot_tasks_status", "bot_tasks", "status"),
    ("ix_bot_outbox_claimed_at", "bot_outbox", "claimed_at"),
]


def _ensure_schema_meta(conn) -> None:
    """Создать журнал версий даже для старой базы без Alembic.

    Исторические поля по-прежнему проверяются идемпотентно ниже, а журнал
    делает дальнейшие изменения наблюдаемыми и пригодными для аудита.
    """
    from sqlalchemy import text as _sql
    conn.execute(_sql(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at DATETIME NOT NULL, note VARCHAR DEFAULT '')"))
    conn.execute(_sql(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at, note) "
        "VALUES (1, :now, 'legacy incremental schema')"),
                 {"now": datetime.now(timezone.utc).replace(tzinfo=None)})


def _migrate(engine) -> None:
    """Досоздание колонок. Безопасно при одновременном старте процессов.

    Три процесса (автопилот, бот, дашборд) стартуют почти одновременно и все
    читают PRAGMA table_info. Без блокировки все трое увидят отсутствие
    колонки и все выполнят ALTER — двое упадут на «duplicate column name».
    BEGIN IMMEDIATE делает проверку и запись одной эксклюзивной транзакцией,
    а перехват ошибки страхует на случай рестарта в неудачный момент:
    операция идемпотентна по смыслу, значит и по поведению должна быть.
    """
    from sqlalchemy import text as _sql
    from sqlalchemy.exc import OperationalError

    # Нельзя использовать engine.begin(): он начинает обычную транзакцию на
    # первом SQL, после чего BEGIN IMMEDIATE превращается в «transaction
    # within transaction». Сначала берём SQLite-лок, потом читаем/меняем схему.
    with engine.connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            _ensure_schema_meta(conn)
            for table, cols in _ADDED_COLUMNS.items():
                have = {r[1] for r in conn.execute(_sql("PRAGMA table_info(%s)" % table))}
                if not have:                  # таблицы ещё нет — create_all создаст
                    continue
                for name, ddl in cols:
                    if name in have:
                        continue
                    try:
                        conn.execute(_sql("ALTER TABLE %s ADD COLUMN %s %s"
                                          % (table, name, ddl)))
                    except OperationalError as e:
                        if "duplicate column" not in str(e).lower():
                            raise
            for name, table, col in _ADDED_INDEXES:
                have = {r[1] for r in conn.execute(_sql("PRAGMA table_info(%s)" % table))}
                if col not in have:
                    continue                  # колонки ещё нет — нечего индексировать
                conn.execute(_sql("CREATE INDEX IF NOT EXISTS %s ON %s (%s)"
                                  % (name, table, col)))

            if "last_seen_at" in {r[1] for r in conn.execute(
                    _sql("PRAGMA table_info(jobs)"))}:
                # Для старых записей fetched_at — единственный доступный
                # baseline. Новые проходы дальше поддерживают last_seen_at.
                conn.execute(_sql(
                    "UPDATE jobs SET last_seen_at = fetched_at "
                    "WHERE last_seen_at IS NULL"))

            # Select-then-insert в notify.push не защищает от двух процессов,
            # поэтому дедупликация закреплена на уровне SQLite. Пустые ключи
            # разрешены многократно, непустые — только один раз.
            have_outbox = {r[1] for r in conn.execute(
                _sql("PRAGMA table_info(bot_outbox)"))}
            if "dedup_key" in have_outbox:
                # Дедуп — только среди НЕотправленных: он защищает от шторма
                # одинаковых уведомлений в очереди. Прежний индекс покрывал и
                # доставленные строки, и повторное событие с тем же ключом
                # (второй cv_missing через неделю) не уведомляло уже никогда.
                # Мёртвые строки (5 попыток, не доставлены) из индекса тоже
                # выведены: иначе такая строка навсегда занимала ключ, и ту
                # же карточку нельзя было переотправить никогда.
                conn.execute(_sql(
                    "DROP INDEX IF EXISTS ux_bot_outbox_dedup_key"))
                conn.execute(_sql(
                    "DROP INDEX IF EXISTS ux_bot_outbox_dedup_pending"))
                conn.execute(_sql(
                    "DELETE FROM bot_outbox WHERE dedup_key <> '' "
                    "AND sent_at IS NULL AND attempts < 5 AND id NOT IN ("
                    "SELECT MIN(id) FROM bot_outbox WHERE dedup_key <> '' "
                    "AND sent_at IS NULL AND attempts < 5 GROUP BY dedup_key)"))
                conn.execute(_sql(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_bot_outbox_dedup_live "
                    "ON bot_outbox (dedup_key) "
                    "WHERE dedup_key <> '' AND sent_at IS NULL AND attempts < 5"))

            conn.execute(_sql(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at, note) "
                "VALUES (:version, :now, :note)"),
                         {"version": SCHEMA_VERSION,
                          "now": datetime.now(timezone.utc).replace(tzinfo=None),
                          "note": "reliable delivery, durable queues and sync metadata"})
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def get_engine():
    _init()
    return cast(Engine, _engine)


@contextmanager
def session_scope():
    _init()
    s = cast(sessionmaker, _Session)()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def main() -> int:
    """CLI для one-shot сервиса миграций в docker-compose.

        python -m jobhunter.db --migrate

    Отдельный сервис, отрабатывающий до старта остальных, — самый надёжный
    способ не устраивать гонку схемы между тремя контейнерами.
    """
    import argparse

    ap = argparse.ArgumentParser(description="Схема БД jobhunter")
    ap.add_argument("--migrate", action="store_true",
                    help="создать таблицы и досоздать колонки")
    args = ap.parse_args()

    _init()
    if args.migrate:
        from sqlalchemy import text as _sql
        with cast(Engine, _engine).begin() as conn:
            tables = [r[0] for r in conn.execute(_sql(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "ORDER BY name"))]
        print("Схема готова: %s" % get_settings().db_path)
        print("Таблиц: %d — %s" % (len(tables), ", ".join(tables)))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
