"""Операционные команды: проверка, экспорт и безопасное восстановление БД.

Примеры:
    python -m jobhunter.ops check
    python -m jobhunter.ops export --path out/export.json
    python -m jobhunter.ops restore --source backup.db --target restored.db
    python -m jobhunter.ops close-stale --days 45 --apply
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from .config import get_settings
from .db import session_scope
from .models import Application, Employer, Job, Message, SendLog, Status, utcnow


def _json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _row(obj) -> dict:
    return {column.name: _json_value(getattr(obj, column.name))
            for column in obj.__table__.columns}


def check_database(path: str | None = None) -> dict:
    """Проверить integrity_check и наличие ключевых таблиц."""
    db_path = str(path or get_settings().db_path)
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"jobs", "applications", "messages", "bot_outbox",
                    "schema_migrations"}
        return {"path": db_path, "integrity": integrity,
                "tables": len(tables),
                "missing_tables": sorted(required - tables),
                "ok": integrity == "ok" and required <= tables}
    finally:
        conn.close()


def export_json(path: str, include_messages: bool = True) -> Path:
    """Экспортировать рабочие данные без секретов и Telegram session-файлов."""
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with session_scope() as sess:
        data = {
            "format": "jobhunter-export-v1",
            "created_at": datetime.utcnow().isoformat() + "Z",
            "jobs": [_row(x) for x in sess.scalars(select(Job)).all()],
            "applications": [_row(x) for x in sess.scalars(select(Application)).all()],
            "employers": [_row(x) for x in sess.scalars(select(Employer)).all()],
            "send_log": [_row(x) for x in sess.scalars(select(SendLog)).all()],
        }
        if include_messages:
            data["messages"] = [_row(x) for x in sess.scalars(select(Message)).all()]
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    return target


def restore_sqlite(source: str, target: str) -> dict:
    """Восстановить SQLite через backup API только после integrity_check."""
    source_path = Path(source).resolve()
    target_path = Path(target).resolve()
    if source_path == target_path:
        raise ValueError("source и target должны различаться")
    check = check_database(str(source_path))
    if not check["ok"]:
        raise ValueError("резервная копия не прошла проверку: %s" % check)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(source_path), uri=False)
    dst = sqlite3.connect(str(target_path), uri=False)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()
    return check_database(str(target_path))


def close_stale(days: int = 45) -> int:
    from .ingest.base import close_stale_jobs
    return close_stale_jobs(days)


def repair_historical_sent(dry: bool = True) -> dict:
    """Вернуть уже отправленные заявки из ошибочного APPROVED в LIVE.

    Это миграция данных, а не общий requeue: фильтр одновременно требует
    sent_at, поэтому письмо не будет отправлено повторно. REJECTED_BY_EMPLOYER
    не меняем — отказ остаётся терминальным состоянием.
    """
    stats = {"found": 0, "repaired": 0, "replied": 0}
    with session_scope() as sess:
        ids = [a.id for a in sess.scalars(
            select(Application).where(
                Application.status == Status.APPROVED.value,
                Application.sent_at.is_not(None)))]

    for app_id in ids:
        with session_scope() as sess:
            app = sess.get(Application, app_id)
            if (app is None or app.status != Status.APPROVED.value
                    or app.sent_at is None):
                continue
            stats["found"] += 1
            target = (Status.REPLIED if app.first_reply_at is not None
                      else Status.AWAITING_REPLY)
            if dry:
                stats["replied" if target == Status.REPLIED else "pending"] = \
                    stats.get("replied" if target == Status.REPLIED else "pending", 0) + 1
                continue
            app.transition(target, reason="repair:sent-state")
            app.sending_lease_until = None
            app.worker_pid = None
            app.send_next_try_at = None
            app.updated_at = utcnow()
            stats["repaired"] += 1
            if target == Status.REPLIED:
                stats["replied"] += 1
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="jobhunter operations")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="проверить текущую SQLite БД")
    check.add_argument("--path", help="путь к SQLite-файлу; по умолчанию DB_PATH")
    exp = sub.add_parser("export", help="экспортировать данные в JSON")
    exp.add_argument("--path", required=True)
    exp.add_argument("--no-messages", action="store_true")
    restore = sub.add_parser("restore", help="проверить и восстановить SQLite")
    restore.add_argument("--source", required=True)
    restore.add_argument("--target", required=True)
    stale = sub.add_parser("close-stale", help="закрыть давно не встречавшиеся вакансии")
    stale.add_argument("--days", type=int, default=45)
    stale.add_argument("--apply", action="store_true",
                       help="применить; без флага только показать намерение")
    sent = sub.add_parser("repair-sent",
                          help="вернуть уже отправленные заявки в LIVE")
    sent.add_argument("--apply", action="store_true",
                      help="применить; без флага только показать")
    args = parser.parse_args(argv)

    if args.command == "check":
        result = check_database(args.path)
    elif args.command == "export":
        result = {"path": str(export_json(args.path, not args.no_messages))}
    elif args.command == "restore":
        result = restore_sqlite(args.source, args.target)
    elif args.command == "close-stale":
        if not args.apply:
            result = {"dry_run": True, "days": args.days,
                      "message": "добавь --apply для изменения БД"}
        else:
            result = {"closed": close_stale(args.days)}
    else:
        result = repair_historical_sent(dry=not args.apply)
        result["dry_run"] = not args.apply
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok", True) else 2


if __name__ == "__main__":
    sys.exit(main())
