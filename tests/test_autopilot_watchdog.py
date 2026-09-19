"""Сторож автопилота: зависший процесс должен выйти, чтобы Docker поднял его заново.

19.09 после обрыва сети вызов Telethon завис без таймаута; контейнер стал unhealthy,
но Docker такие не перезапускает — автопилот простоял три часа при живой сети.
"""
import pytest


def test_wedge_is_an_hour_of_silence_not_a_long_send_session():
    from jobhunter import autopilot
    assert not autopilot.wedged(25 * 60)              # сессия отправки держит пул до 25 минут
    assert not autopilot.wedged(40 * 60)              # порог healthcheck — ещё не зависание
    assert autopilot.wedged(61 * 60)
    assert not autopilot.wedged(float("inf"))         # пульса не было вовсе — не уходим в цикл


def test_watchdog_exits_only_when_wedged(monkeypatch):
    from jobhunter import autopilot

    class Exited(Exception):
        pass

    def fake_exit(code):
        raise Exited(code)
    monkeypatch.setattr(autopilot.os, "_exit", fake_exit)
    monkeypatch.setattr(autopilot.logging, "shutdown", lambda: None)

    monkeypatch.setattr(autopilot.health, "age", lambda name: 120.0)
    autopilot._watchdog()                              # всё живо — ничего не происходит

    seen = []
    monkeypatch.setattr(autopilot.health, "age", lambda name: seen.append(name) or 2 * 3600.0)
    with pytest.raises(Exited) as err:
        autopilot._watchdog()
    assert err.value.args == (1,) and seen == ["autopilot_tg"]
