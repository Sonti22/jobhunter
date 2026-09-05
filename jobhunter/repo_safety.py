"""Reject tracked credentials and runtime data; never read their contents."""
from __future__ import annotations

import fnmatch
import subprocess
import sys
from pathlib import PurePosixPath

PRIVATE_NAMES = {
    "google_token.json", "google_client_secret.json", "tg_code.txt", "tg_2fa.txt",
    "tg_code_request.json", "proxy_backup.json", "stop_sending.flag", "sender.pid",
}
PRIVATE_DIRS = {"data", "backup", "out", "logs", "cv_out", "probe_out", ".venv", "venv"}


def private_path(name: str) -> bool:
    path = PurePosixPath(name.replace("\\", "/").lower())
    base = path.name
    return (
        base in PRIVATE_NAMES
        or (base.startswith(".env") and base != ".env.example")
        or any(fnmatch.fnmatchcase(base, p) for p in (
            "*.session", "*.session.*", "*.session-*", "*.db", "*.db.*", "*.db-*"))
        or any(p in PRIVATE_DIRS or p.startswith("backup_") for p in path.parts[:-1])
    )


def main() -> int:
    result = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True)
    names = result.stdout.decode("utf-8", "replace").split("\0")
    bad = sorted(n for n in names if n and private_path(n))
    if bad:
        print("Private files are tracked by Git (contents were not inspected):")
        for name in bad:
            print("  " + name)
        print("Remove these paths from the index, keeping local copies. Also review Git history.")
        return 1
    print("Git index: no credential/runtime paths tracked. History is not checked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
