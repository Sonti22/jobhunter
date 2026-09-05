"""Consistent per-SQLite snapshots and validated archives. Standard library only.

May run as a standalone, read-only bind-mounted script in the existing image;
never imports configuration, opens Telegram, or sends messages.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

MANIFEST = "backup-manifest.json"
REQUIRED_TABLES = {"jobs", "applications", "messages", "bot_outbox", "schema_migrations"}
MAX_BYTES = 2 * 1024 ** 3
SQLITE_HEADER = b"SQLite format 3\x00"


def _readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _is_sqlite(path: Path) -> bool:
    with path.open("rb") as stream:
        return stream.read(16) == SQLITE_HEADER


def check_sqlite(path: Path, *, main: bool = False) -> dict:
    with closing(_readonly(path)) as conn:
        integrity = [r[0] for r in conn.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise ValueError("SQLite integrity check failed: " + path.name)
        result = {"integrity": "ok"}
        if main:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if not REQUIRED_TABLES <= tables:
                raise ValueError("Missing jobhunter tables: " + ", ".join(sorted(REQUIRED_TABLES - tables)))
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("Foreign key violations: " + path.name)
            result["applications"] = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
        return result


def snapshot_sqlite(source: Path, target: Path, *, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout

    def progress(_status, _remaining, _total):
        if time.monotonic() > deadline:
            raise TimeoutError("SQLite snapshot timed out: " + source.name)

    with closing(_readonly(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst, pages=256, progress=progress, sleep=0.05)
        dst.execute("PRAGMA journal_mode=DELETE")
    check_sqlite(target, main=source.name == "jobhunter.db")


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _members(archive: Path, destination: Path) -> dict[str, Path]:
    """Extract only bounded, regular files, never links or traversal paths."""
    files: dict[str, Path] = {}
    total = 0
    with tarfile.open(archive, "r:gz") as tar:
        for index, member in enumerate(tar):
            if index >= 10000:
                raise ValueError("Too many archive entries")
            name = PurePosixPath(member.name)
            if (name.is_absolute() or ".." in name.parts or "\\" in member.name
                    or ":" in member.name):
                raise ValueError("Unsafe archive path")
            if member.isdir():
                continue
            if not member.isfile() or not name.parts:
                raise ValueError("Archive contains a link or non-regular file")
            key = name.as_posix()
            if key in files:
                raise ValueError("Duplicate archive entry: " + key)
            total += member.size
            if member.size < 0 or total > MAX_BYTES:
                raise ValueError("Archive exceeds extraction size limit")
            path = destination.joinpath(*name.parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            extracted = tar.extractfile(member)
            if extracted is None:
                raise ValueError("Archive member has no contents")
            with closing(extracted) as src, path.open("xb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            files[key] = path
    return files


def verify_archive(archive: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="jobhunter-verify-") as temp:
        files = _members(archive, Path(temp))
        if "jobhunter.db" not in files:
            raise ValueError("Archive has no root jobhunter.db")
        manifest = None
        if MANIFEST in files:
            manifest = json.loads(files[MANIFEST].read_text(encoding="utf-8"))
            if manifest.get("format") != "jobhunter-backup-v1":
                raise ValueError("Unsupported backup manifest")
            expected = manifest.get("files", {})
            if set(expected) != set(files) - {MANIFEST}:
                raise ValueError("Archive contents differ from manifest")
            for name, info in expected.items():
                if files[name].stat().st_size != info["size"] or _hash(files[name]) != info["sha256"]:
                    raise ValueError("Backup checksum mismatch: " + name)
        checks = {}
        for name, path in files.items():
            if name in ("jobhunter.db", "jobhunter.session") or _is_sqlite(path):
                checks[name] = check_sqlite(path, main=name == "jobhunter.db")
            elif Path(name).name in ("google_token.json", "google_client_secret.json"):
                if not isinstance(json.loads(path.read_text(encoding="utf-8")), dict):
                    raise ValueError("Invalid credential JSON structure")
        return {"ok": True, "files": len(files), "manifest": manifest is not None,
                "databases": checks, "applications": checks["jobhunter.db"]["applications"]}


def create_archive(source: Path, target: Path) -> dict:
    source, target = source.resolve(), target.resolve()
    if source == target or source in target.parents:
        raise ValueError("Backup destination must be outside source directory")
    if not source.is_dir() or not (source / "jobhunter.db").is_file():
        raise ValueError("Source has no jobhunter.db")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive publication lock; an existing output is never overwritten.
    lock = target.with_name(target.name + ".lock")
    with lock.open("x"):
        pass
    part = target.with_name(target.name + ".part-" + uuid.uuid4().hex)
    try:
        if target.exists():
            raise FileExistsError("Backup already exists: " + target.name)
        with tempfile.TemporaryDirectory(prefix="jobhunter-snapshot-") as temp:
            stage = Path(temp)
            manifest: dict = {"format": "jobhunter-backup-v1",
                        "created_at": datetime.now(timezone.utc).isoformat(), "files": {}}
            for path in sorted(source.rglob("*")):
                if path.is_symlink():
                    raise ValueError("Symlinks are not allowed in backup source")
                if not path.is_file():
                    continue
                name = path.relative_to(source).as_posix()
                # Snapshots replace SQLite plus its journals. Process leases and
                # heartbeats must not masquerade as running processes on restore.
                if (path.name.endswith(("-wal", "-shm", "-journal", ".pid", ".beat", ".lock"))
                        or path.name.startswith("start_")):
                    continue
                if name == MANIFEST:
                    raise ValueError("Source contains reserved backup manifest")
                dest = stage / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                if name in ("jobhunter.db", "jobhunter.session") or _is_sqlite(path):
                    snapshot_sqlite(path, dest)
                else:
                    before = path.stat()
                    shutil.copyfile(path, dest)
                    after = path.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise OSError("Source changed during copy; retry backup: " + name)
                manifest["files"][name] = {"size": dest.stat().st_size, "sha256": _hash(dest)}
            (stage / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            with part.open("xb") as stream:
                with tarfile.open(fileobj=stream, mode="w:gz") as tar:
                    for path in sorted(stage.rglob("*")):
                        if path.is_file():
                            tar.add(path, arcname=path.relative_to(stage).as_posix(), recursive=False)
                stream.flush()
                os.fsync(stream.fileno())
            result = verify_archive(part)
            if target.exists():
                raise FileExistsError("Backup already exists: " + target.name)
            os.replace(part, target)
            return dict(result, archive=target.name, bytes=target.stat().st_size)
    finally:
        part.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--source", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = (create_archive(args.source, args.output) if args.command == "create"
                  else verify_archive(args.archive))
    except (OSError, ValueError, sqlite3.Error, tarfile.TarError) as exc:
        print("Backup failed: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
