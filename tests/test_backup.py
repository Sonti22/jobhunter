"""Backup regressions: WAL, corrupt archives, unsafe paths and failed publication."""
import io
import json
import sqlite3
import tarfile
from contextlib import closing

import pytest

from jobhunter import backup


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source"
    path.mkdir()
    with closing(sqlite3.connect(path / "jobhunter.db")) as conn:
        for name in backup.REQUIRED_TABLES:
            conn.execute('CREATE TABLE "%s" (id INTEGER PRIMARY KEY)' % name)
        conn.execute("INSERT INTO applications VALUES (1)")
        conn.commit()
    return path


def archive_of(path, entries):
    with tarfile.open(path, "w:gz") as tar:
        for name, content in entries:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return path


def test_snapshot_includes_committed_wal_not_open_transaction(source, tmp_path):
    with closing(sqlite3.connect(source / "jobhunter.db")) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO applications VALUES (2)")
        writer.commit()
        writer.execute("INSERT INTO applications VALUES (3)")
        before = (source / "jobhunter.db-wal").read_bytes()
        result = backup.create_archive(source, tmp_path / "backup.tgz")
        assert result["applications"] == 2
        assert (source / "jobhunter.db-wal").read_bytes() == before
        assert writer.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 3
        writer.rollback()


def test_archive_contains_session_and_manifest_but_not_process_locks(source, tmp_path):
    with closing(sqlite3.connect(source / "jobhunter.session")) as conn:
        conn.execute("CREATE TABLE sessions (id INTEGER)")
    for name in ("sender.pid", "autopilot.beat", "start_tg1.txt"):
        (source / name).write_text("old process")
    (source / "google_token.json").write_text('{"token":"fake-test-token"}')
    (source / "STOP_SENDING.flag").write_text("stop")
    result = backup.create_archive(source, tmp_path / "backup.tgz")
    assert result["manifest"] and "jobhunter.session" in result["databases"]
    with tarfile.open(tmp_path / "backup.tgz") as tar:
        names = set(tar.getnames())
    assert {"jobhunter.db", "jobhunter.session", "google_token.json", "STOP_SENDING.flag",
            backup.MANIFEST} <= names
    assert not {"sender.pid", "autopilot.beat", "start_tg1.txt"} & names


def test_existing_backup_is_not_overwritten(source, tmp_path):
    target = tmp_path / "existing.tgz"
    target.write_bytes(b"keep me")
    with pytest.raises(FileExistsError):
        backup.create_archive(source, target)
    assert target.read_bytes() == b"keep me"
    assert not list(tmp_path.glob("*.lock"))


def test_failed_verification_does_not_publish_or_delete_old_backup(source, tmp_path, monkeypatch):
    old = tmp_path / "old.tgz"
    old.write_bytes(b"keep me")
    monkeypatch.setattr(backup, "verify_archive", lambda *a: (_ for _ in ()).throw(ValueError("broken")))
    with pytest.raises(ValueError, match="broken"):
        backup.create_archive(source, tmp_path / "new.tgz")
    assert not (tmp_path / "new.tgz").exists()
    assert old.read_bytes() == b"keep me"
    assert not list(tmp_path.glob("*.part-*"))
    assert not list(tmp_path.glob("*.lock"))


def test_existing_publication_lock_is_not_removed(source, tmp_path):
    lock = tmp_path / "backup.tgz.lock"
    lock.write_text("another worker")
    with pytest.raises(FileExistsError):
        backup.create_archive(source, tmp_path / "backup.tgz")
    assert lock.read_text() == "another worker"


def test_output_inside_source_is_rejected(source):
    with pytest.raises(ValueError, match="outside"):
        backup.create_archive(source, source / "backup.tgz")


def test_missing_source_does_not_create_a_database(tmp_path):
    with pytest.raises(ValueError, match="no jobhunter.db"):
        backup.create_archive(tmp_path / "missing", tmp_path / "backup.tgz")
    assert not (tmp_path / "missing").exists()


def test_missing_database_check_never_creates_file(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        backup.check_sqlite(path)
    assert not path.exists()


@pytest.mark.parametrize("name", ["../outside", "/outside", "C:/outside", "..\\outside"])
def test_archive_traversal_rejected(tmp_path, name):
    archive = archive_of(tmp_path / "bad.tgz", [(name, b"no")])
    with pytest.raises(ValueError, match="Unsafe"):
        backup.verify_archive(archive)


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_archive_links_and_special_files_rejected(tmp_path, kind):
    path = tmp_path / "link.tgz"
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo("link")
        info.type, info.linkname = kind, "/etc/passwd"
        tar.addfile(info)
    with pytest.raises(ValueError, match="non-regular"):
        backup.verify_archive(path)


def test_duplicate_members_rejected(tmp_path):
    path = archive_of(tmp_path / "dup.tgz", [("file", b"a"), ("./file", b"b")])
    with pytest.raises(ValueError, match="Duplicate"):
        backup.verify_archive(path)


def test_extraction_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "MAX_BYTES", 2)
    path = archive_of(tmp_path / "large.tgz", [("file", b"123")])
    with pytest.raises(ValueError, match="size limit"):
        backup.verify_archive(path)


def test_legacy_archive_with_wal_is_supported(source, tmp_path):
    with closing(sqlite3.connect(source / "jobhunter.db")) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("INSERT INTO applications VALUES (2)")
        conn.commit()
        path = tmp_path / "legacy.tgz"
        with tarfile.open(path, "w:gz") as tar:
            tar.add(source, arcname=".")
        result = backup.verify_archive(path)
        assert result["applications"] == 2 and not result["manifest"]


@pytest.mark.parametrize("mutation", ["checksum", "extra", "missing", "format"])
def test_manifest_detects_tampering(source, tmp_path, mutation):
    path = tmp_path / "good.tgz"
    backup.create_archive(source, path)
    with tarfile.open(path) as tar:
        data = {m.name: tar.extractfile(m).read() for m in tar if m.isfile()}
    if mutation == "checksum":
        data["jobhunter.db"] += b"bad"
    elif mutation == "extra":
        data["extra"] = b"bad"
    elif mutation == "missing":
        del data["jobhunter.db"]
    else:
        manifest = json.loads(data[backup.MANIFEST])
        manifest["format"] = "unknown"
        data[backup.MANIFEST] = json.dumps(manifest).encode()
    bad = archive_of(tmp_path / "bad.tgz", list(data.items()))
    with pytest.raises(ValueError):
        backup.verify_archive(bad)


def test_empty_valid_database_is_not_mistaken_for_corruption(source, tmp_path):
    with closing(sqlite3.connect(source / "jobhunter.db")) as conn:
        conn.execute("DELETE FROM applications")
        conn.commit()
    assert backup.create_archive(source, tmp_path / "empty.tgz")["applications"] == 0


def test_corrupt_database_fails_without_publishing(source, tmp_path):
    (source / "jobhunter.db").write_bytes(b"not sqlite")
    assert backup.main(["create", "--source", str(source), "--output", str(tmp_path / "bad.tgz")]) == 1
    assert not (tmp_path / "bad.tgz").exists()


def test_snapshot_timeout(source, tmp_path):
    with pytest.raises(TimeoutError):
        backup.snapshot_sqlite(source / "jobhunter.db", tmp_path / "timed-out.db", timeout=-1)


def test_verify_cli(source, tmp_path, capsys):
    path = tmp_path / "backup.tgz"
    backup.create_archive(source, path)
    assert backup.main(["verify", "--archive", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]


@pytest.mark.parametrize("path", [
    ".env", ".env.local", "google_token.json", "GOOGLE_CLIENT_SECRET.JSON",
    "jobhunter.session", "jobhunter.session-journal", "jobhunter.session.moved",
    "jobhunter.db", "jobhunter.db.moved-wal", "logs/sent/letter.md",
    "backup/copy.tgz", "backup_old/private.json", "out/cv.pdf", "tg_2fa.txt",
])
def test_repository_guard_rejects_private_paths(path):
    from jobhunter.repo_safety import private_path
    assert private_path(path)


@pytest.mark.parametrize("path", [
    ".env.example", "jobhunter/backup.py", "tests/test_mail_inbox.py",
    "README.md", "profile.yaml", "scripts/backup_volume.ps1",
])
def test_repository_guard_allows_source_and_templates(path):
    from jobhunter.repo_safety import private_path
    assert not private_path(path)
