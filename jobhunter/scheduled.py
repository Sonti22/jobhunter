"""Restart-safe, mutually exclusive daily task execution."""
from __future__ import annotations

import re
from datetime import datetime
from functools import wraps
from pathlib import Path

from .locking import FileLock, LockBusy
from .observability import record


def has_errors(result) -> bool:
    if not isinstance(result, dict):
        return False
    return bool(result.get("error") or result.get("errors") or result.get("scan_errors")) or any(
        has_errors(v) for v in result.values() if isinstance(v, dict))


def wrap_marked(fn, job_id: str, directory: Path):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", job_id):
        raise ValueError("Invalid scheduled task id")
    done = directory / ("last_%s.txt" % job_id)
    started = directory / ("start_%s.txt" % job_id)

    @wraps(fn)
    def run():
        try:
            lock = FileLock(directory / ("job-%s.lock" % job_id))
            lock.__enter__()
        except LockBusy:
            return {"blocked": "задача уже выполняется"}
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            try:
                if done.read_text(encoding="utf-8").strip() == today:
                    return None
            except FileNotFoundError:
                pass
            # A stale start marker is diagnostic only. The OS lock, not an
            # arbitrary two-hour timeout, proves another worker is alive.
            started.write_text(datetime.now().isoformat(), encoding="utf-8")
            try:
                record("task:" + job_id, "running")
                result = fn()
                failed = has_errors(result)
                blocked = isinstance(result, dict) and bool(result.get("blocked"))
                record("task:" + job_id, "error" if failed else "blocked" if blocked else "ok",
                       details=result if isinstance(result, dict) else {"result": result})
                if not failed and not blocked:
                    done.write_text(today, encoding="utf-8")
                return result
            except Exception as exc:
                record("task:" + job_id, "error", error=type(exc).__name__)
                raise
            finally:
                started.unlink(missing_ok=True)
        finally:
            lock.__exit__(None, None, None)
    return run
