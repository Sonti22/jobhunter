"""Какие задания в каком пуле планировщика.

Однопоточный tg-пул нужен только шагам, открывающим Telethon: два MTProto-клиента
на одной сессии — это разлогин аккаунта. 24.09 дневной сбор каналов (обычный HTTP к
t.me/s/…) занимал этот пул 30+ минут: входящие и ответы рекрутёрам стояли в очереди,
пульс autopilot_tg молчал, и сторож через час перезапустил бы процесс посреди сбора.
"""
from types import SimpleNamespace

import pytest


@pytest.fixture()
def jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "pools.db"))
    monkeypatch.setenv("LLM_ENABLED", "false")
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    import jobhunter.db as dbmod
    dbmod._engine = None
    dbmod._Session = None
    import apscheduler.schedulers.blocking as blocking

    registered = {}

    class FakeScheduler:
        def __init__(self, **kw):
            pass

        def add_job(self, fn, trigger=None, **kw):
            job = SimpleNamespace(id=kw.get("id"), func=fn, trigger=trigger,
                                  next_run_time=None, kw=kw,
                                  modify=lambda **m: None)
            registered[kw.get("id")] = job
            return job

        def get_job(self, job_id):
            return registered.get(job_id)

        def get_jobs(self):
            return list(registered.values())

        def start(self):
            raise SystemExit

    monkeypatch.setattr(blocking, "BlockingScheduler", FakeScheduler)
    from jobhunter import autopilot
    autopilot.run_daemon()
    yield registered
    dbmod._engine = None
    dbmod._Session = None
    get_settings.cache_clear()


def _pool(job):
    return job.kw.get("executor", "default")


def test_telethon_steps_share_the_single_tg_worker(jobs):
    for job_id in ("discover", "spambot", "tg1", "tg2", "inbox", "decisions"):
        assert _pool(jobs[job_id]) == "tg", job_id


def test_channel_ingest_does_not_hold_the_tg_worker(jobs):
    for job_id in ("ingest_tg_midday", "ingest_tg_evening"):
        assert _pool(jobs[job_id]) != "tg", job_id
