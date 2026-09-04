"""Два предохранителя рассылки.

Окно вежливости: паузы между сессиями отправщика достигают двух часов, и
прогон, начатый днём, доползал бы до ночи — ночной холодный DM это жалоба.
Функция окна была написана, но не вызывалась нигде.

Дедуп каналов: username в Telegram регистронезависим, а реестр складывается
из трёх источников — «Remoteit» из каталога и «remoteit» из автопоиска
скрейпились как два разных канала.
"""

import pytest


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "sw.db"))
    monkeypatch.setenv("LLM_ENABLED", "false")
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    import jobhunter.db as dbmod
    dbmod._engine = None
    dbmod._Session = None
    yield
    get_settings.cache_clear()


def test_channel_registry_dedup_ignores_case(monkeypatch):
    monkeypatch.setattr("jobhunter.ingest.tgchannels._verified_channels",
                        lambda: ["Remoteit", "python_jobs"])
    monkeypatch.setattr("jobhunter.ingest.tgchannels._discovered",
                        lambda: ["remoteit", "PYTHON_JOBS", "devjobs"])
    from jobhunter.ingest.tgchannels import TelegramChannelSource
    src = TelegramChannelSource()
    lowered = [c.lower() for c in src.channels]
    assert len(lowered) == len(set(lowered)), "дубли по регистру в реестре"
    assert "remoteit" in lowered and "devjobs" in lowered


def test_night_stops_the_batch(monkeypatch):
    """Вне окна 09-21 прогон завершается, send_one не вызывается."""
    import asyncio

    from jobhunter.outreach import policy, sender

    assert not policy.within_send_window(23)
    assert policy.within_send_window(12)

    sent = []

    async def no_send(client, item, rng, dry):
        sent.append(item)
        return "ok"

    class _FakeDT:
        @staticmethod
        def now(tz=None):
            import datetime as _d
            return _d.datetime(2026, 8, 26, 23, 30, tzinfo=tz)

    monkeypatch.setattr(sender, "send_one", no_send)
    monkeypatch.setattr(sender, "datetime", _FakeDT)
    monkeypatch.setattr(sender, "pick_batch",
                        lambda limit: [{"app_id": 1, "handle": "hr_x",
                                        "score": 70.0, "title": "X"}])
    monkeypatch.setattr(sender.policy, "session_plan",
                        lambda n, rng=None: [n])
    # dry=False, client=None: до сети дойти не должны — окно закрыто раньше
    rc = asyncio.run(sender.run(5, dry=False))
    assert sent == [], "ночью send_one вызываться не должен"
    assert rc == 0
