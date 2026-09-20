"""Резервные копии по расписанию.

Проверка 20.09: backup.py существовал, но автопилот его ни разу не вызывал — 21 тысяча
вакансий, вся переписка и сессия Telegram жили без единой копии.
"""
from datetime import datetime, timedelta

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data, out = tmp_path / "data", tmp_path / "out"
    data.mkdir()
    out.mkdir()
    monkeypatch.setenv("DB_PATH", str(data / "jobhunter.db"))
    monkeypatch.setenv("OUT_DIR", str(out))
    import jobhunter.db as dbmod
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    dbmod._engine = None
    dbmod._Session = None
    dbmod.init_db() if hasattr(dbmod, "init_db") else None
    with dbmod.session_scope() as sess:                      # создаёт схему и непустую базу
        from jobhunter.models import Application, Job
        job = Job(external_uuid="j1", source="hn", title="Backend Engineer")
        sess.add(job)
        sess.flush()
        sess.add(Application(job_id=job.id, status="APPROVED"))
    yield out
    if dbmod._engine is not None:
        dbmod._engine.dispose()
    dbmod._engine = None
    dbmod._Session = None
    get_settings.cache_clear()


def test_backup_is_created_verified_and_not_repeated(env):
    from jobhunter import autopilot
    from jobhunter.backup import verify_archive
    first = autopilot.step_backup()
    assert first.get("archive", "").startswith("jobhunter-") and first["applications"] == 1
    archive = env / "backups" / first["archive"]
    assert archive.is_file() and verify_archive(archive)["ok"]      # копию можно восстановить
    assert "skipped" in autopilot.step_backup()                     # вторая за день не нужна
    assert len(list((env / "backups").glob("*.tar.gz"))) == 1


def test_rotation_keeps_a_week_of_dailies_and_four_sundays(tmp_path):
    from jobhunter import autopilot
    folder = tmp_path / "backups"
    folder.mkdir()
    day0 = datetime(2026, 9, 20)                                    # воскресенье
    for i in range(40):
        (folder / ("jobhunter-%s.tar.gz" % (day0 - timedelta(days=i)).strftime("%Y-%m-%d"))).write_bytes(b"x")
    (folder / "jobhunter-before-upgrade.tar.gz").write_bytes(b"manual")     # сделана руками
    (folder / "notes.txt").write_text("x")
    removed = autopilot.prune_backups(folder)
    left = sorted(p.name for p in folder.glob("jobhunter-2026*.tar.gz"))
    assert len(left) == 7 + 3                  # 7 последних дней (в них одно воскресенье) + ещё 3 воскресенья
    assert "jobhunter-2026-09-20.tar.gz" in left and "jobhunter-2026-08-30.tar.gz" in left
    assert len(removed) == 30
    assert (folder / "jobhunter-before-upgrade.tar.gz").exists() and (folder / "notes.txt").exists()


def test_failed_backup_tells_the_owner(env, monkeypatch):
    from jobhunter import autopilot, backup
    sent = []
    monkeypatch.setattr("jobhunter.notify.push_once", lambda kind, text, **kw: sent.append((kind, text)) or True)

    def boom(*a, **kw):
        raise OSError("диск полон")
    monkeypatch.setattr(backup, "create_archive", boom)
    assert autopilot.step_backup() == {"error": "OSError"}
    assert sent and sent[0][0] == "backup_failed" and "диск полон" in sent[0][1]
