"""Offline migration/read-screen probe on a temporary copy, never on the source.

Run in Docker with --network none and a read-only source mount. Accepts a SQLite
database or a verified jobhunter backup archive. Prints aggregates, not messages.
No Telegram client, send pipeline, LLM or account API is started.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tarfile
import tempfile
from contextlib import closing
from pathlib import Path

from .backup import check_sqlite, snapshot_sqlite, verify_archive


def fingerprints(path: Path) -> dict:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        result = {}
        for name in tables:
            quoted = '"' + name.replace('"', '""') + '"'
            digest, count = hashlib.sha256(), 0
            for row in conn.execute("SELECT * FROM " + quoted + " ORDER BY rowid"):
                digest.update(json.dumps(row, default=str, ensure_ascii=False).encode())
                digest.update(b"\n")
                count += 1
            result[name] = {"rows": count, "sha256": digest.hexdigest()}
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    source = parser.parse_args().source.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="jobhunter-release-") as directory:
        temp = Path(directory)
        target = temp / "jobhunter.db"
        if source.suffix == ".tgz":
            verify_archive(source)
            with tarfile.open(source, "r:gz") as archive:
                member = archive.extractfile("jobhunter.db")
                if member is None:
                    raise ValueError("Archive has no database")
                with member, target.open("xb") as output:
                    import shutil
                    shutil.copyfileobj(member, output)
        else:
            snapshot_sqlite(source, target)
        check_sqlite(target, main=True)
        before = fingerprints(target)
        os.environ.update(DB_PATH=str(target), LLM_ENABLED="false", GCAL_ENABLED="false",
                          BOT_ALLOWED_USER_IDS="1", TELEGRAM_BOT_TOKEN="",
                          HEARTBEAT_DIR=str(temp), OUT_DIR=str(temp), LOG_DIR=str(temp),
                          KILL_SWITCH_PATH=str(temp / "STOP"))
        from .config import get_settings
        get_settings.cache_clear()
        from .db import get_engine
        engine = get_engine()
        try:
            migrated = fingerprints(target)
            if any(migrated.get(name) != value for name, value in before.items()):
                raise AssertionError("Migration changed existing table data")
            from .models import ApplicationFeedback, OwnerPreference, ResultEvent
            added = {model.__tablename__ for model in (ApplicationFeedback, OwnerPreference, ResultEvent)}
            assert added <= migrated.keys()
            from fastapi.testclient import TestClient

            from . import dashboard, manual_telegram
            from .bot import screens
            from .web.server import app
            client = TestClient(app)
            routes = ("/ping", "/attention", "/reading", "/sending", "/results", "/outcomes",
                      "/manual-telegram", "/api/attention?limit=5", "/api/outcomes?limit=5",
                      "/api/reading", "/api/sending?limit=5")
            for route in routes:
                assert client.get(route).status_code == 200, route
            names = ["main", "work_tasks_0", "work_results_all_0", "work_reading",
                     "work_sending_0", "work_tracks", "work_feedback"]
            for item in dashboard.attention(limit=5)["items"]:
                names.append(f"work_task_{item['id']}")
            for name in names:
                body, markup = screens.render(name)
                assert body and len(body) <= 4096, name
                for row in markup.get("inline_keyboard", []):
                    for button in row:
                        assert len(button.get("callback_data", "").encode()) <= 64, name
            for track in manual_telegram.TRACKS:
                assert client.get("/manual-telegram", params={"track": track}).status_code == 200
            after = fingerprints(target)
            for table in ("applications", "jobs", "messages", "send_log", "owner_requests", *added):
                assert after[table] == migrated[table], "Read screen changed " + table
            check_sqlite(target, main=True)
            print(json.dumps({"ok": True, "applications": after["applications"]["rows"],
                              "migration_preserved_tables": len(before),
                              "new_tables_created": sorted(added - before.keys()),
                              "web_routes": len(routes) + len(manual_telegram.TRACKS),
                              "bot_screens": len(names), "history_unchanged": True,
                              "network_actions": 0}))
        finally:
            engine.dispose()


if __name__ == "__main__":
    main()
