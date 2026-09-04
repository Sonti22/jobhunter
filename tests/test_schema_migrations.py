"""Схема модели и список миграций не должны расходиться.

Колонка, добавленная в модель, но забытая в `_ADDED_COLUMNS`, появляется
только на пустой базе: `create_all()` создаёт таблицы целиком, а рабочая
база с историей откликов их уже имеет и молча остаётся без нового поля.
Обнаруживается это через недели, когда код обращается к полю, которого в
проде нет. Тест ловит расхождение сразу.
"""
import sqlalchemy as sa

from jobhunter import db as dbmod
from jobhunter.models import Base

# Колонки, которых нет в _ADDED_COLUMNS осознанно: они существуют с самого
# первого выпуска, и база без них невозможна.
BASELINE = {
    "applications": {
        "id", "job_id", "employer_id", "status", "created_at", "updated_at",
        "score", "score_breakdown_json", "reject_reason", "cv_path",
        "cv_sha256", "cv_lang", "message_body", "message_body_norm_hash",
        "message_similarity_max", "message_skeleton_id", "gate_passed",
        "gate_failures_json", "promoted_terms_json", "batch_id", "approved_at",
        "sending_lease_until", "worker_pid", "telegram_random_id", "sent_at",
        "telegram_msg_id", "send_error_class", "send_attempts",
        "alternate_job_ids_json", "followup_due_at", "followup_sent_at",
        "first_reply_at", "last_inbound_at", "last_outbound_at",
        "auto_replies_count", "needs_human_reason", "interview_at_utc",
        "interview_tz", "ics_path",
    },
    "campaign_state": {
        "id", "quota_ceiling", "consecutive_clean_days", "peerflood_total",
        "manual_only", "started_at",
    },
    "messages": {
        "id", "application_id", "direction", "telegram_msg_id", "body",
        "body_hash", "sent_at", "received_at", "is_auto", "classifier_label",
        "classifier_confidence", "escalated",
    },
    "owner_requests": {
        "id", "application_id", "kind", "question", "payload_json",
        "created_at", "sent_at", "owner_msg_id", "expires_at", "answered_at",
        "decision", "decision_note", "applied_at", "apply_error",
    },
}


def test_added_columns_covers_model():
    """Каждая колонка модели либо базовая, либо перечислена в миграциях."""
    missing = {}
    for table, baseline in BASELINE.items():
        model = Base.metadata.tables[table]
        migrated = {name for name, _ddl in dbmod._ADDED_COLUMNS.get(table, [])}
        gap = {c.name for c in model.columns} - baseline - migrated
        if gap:
            missing[table] = sorted(gap)
    assert not missing, (
        "колонки есть в модели, но не поедут в рабочую базу — "
        "допиши их в jobhunter/db.py::_ADDED_COLUMNS: %s" % missing)


def test_added_indexes_reference_existing_columns():
    """Индекс без колонки — тихо не создастся, и поиск пойдёт полным сканом."""
    for name, table, col in dbmod._ADDED_INDEXES:
        assert table in Base.metadata.tables, "нет таблицы %s (индекс %s)" % (table, name)
        cols = {c.name for c in Base.metadata.tables[table].columns}
        assert col in cols, "нет колонки %s.%s для индекса %s" % (table, col, name)


def test_migrate_is_idempotent(tmp_path, monkeypatch):
    """Повторный прогон миграций не должен падать: контейнеры перезапускаются."""
    from jobhunter.config import get_settings

    monkeypatch.setenv("DB_PATH", str(tmp_path / "m.db"))
    get_settings.cache_clear()
    monkeypatch.setattr(dbmod, "_engine", None)
    monkeypatch.setattr(dbmod, "_Session", None)
    try:
        engine = sa.create_engine("sqlite:///" + str(tmp_path / "m.db"))
        Base.metadata.create_all(engine)
        dbmod._migrate(engine)
        dbmod._migrate(engine)          # второй раз — не должно бросать
        with engine.begin() as conn:
            names = {r[1] for r in conn.execute(
                sa.text("PRAGMA index_list(messages)"))}
        assert "ix_messages_email_message_id" in names
    finally:
        get_settings.cache_clear()
