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


def test_bot_tells_the_owner_when_autopilot_stalls_and_when_it_is_back(monkeypatch):
    """19.09: три часа простоя — владелец узнал, только спросив. Бот живёт отдельно и видит пульс."""
    from jobhunter.bot import watch
    sent = []
    monkeypatch.setattr(watch.notify, "push", lambda kind, text, **kw: sent.append((kind, text)))
    ages = {"autopilot": 30.0, "autopilot_tg": 120.0}
    monkeypatch.setattr(watch.health, "age", lambda name: ages[name])
    watch._state.update(down_since=0.0, checked=0.0)

    assert watch.check(now=1000.0) == "" and sent == []              # всё живо
    ages["autopilot_tg"] = 50 * 60.0                                  # очередь встала
    assert watch.check(now=1010.0) == ""                              # чаще раза в минуту не смотрим
    assert watch.check(now=1100.0) == "down"
    assert "телеграм-очередь молчит 50 мин" in sent[0][1]
    assert watch.check(now=1200.0) == "" and len(sent) == 1           # не повторяем каждую минуту
    ages["autopilot_tg"] = 20.0                                       # сторож перезапустил
    assert watch.check(now=1300.0) == "up" and "снова работает" in sent[1][1]
    ages["autopilot"] = float("inf")                                  # пульса нет вовсе — не паникуем
    assert watch.check(now=1400.0) == ""


def test_chain_after_ingest_runs_the_real_steps_and_survives_a_failing_one(tmp_path, monkeypatch):
    """19.09: вечерний сбор принёс 373 вакансии, а цепочка молча не сделала ничего — она звала
    задания планировщика, обёрнутые защитой «сегодня уже выполнялось»."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "chain.db"))
    import jobhunter.db as dbmod
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    dbmod._engine = None
    dbmod._Session = None
    try:
        from jobhunter import autopilot
        from jobhunter.models import RuntimeState
        calls = []

        def boom():
            calls.append("approve")
            raise RuntimeError("нет сети")
        out = autopilot.send_after_ingest(steps=(
            ("prepare", lambda: calls.append("prepare") or {"processed": 3}),
            ("approve", boom),
            ("email", lambda: calls.append("email") or {"ok": True})))
        assert calls == ["prepare", "approve", "email"]           # сбой одного шага не рвёт цепочку
        assert out["prepare"] == {"processed": 3} and out["approve"] == {"error": "RuntimeError"}
        with dbmod.session_scope() as sess:
            assert sess.get(RuntimeState, "task:after_ingest").status == "error"
        # по умолчанию — сами функции шагов, а не обёрнутые задания планировщика
        import inspect
        src = inspect.getsource(autopilot.send_after_ingest)
        assert "step_prepare" in src and "step_auto_approve" in src and "step_send_email" in src
    finally:
        if dbmod._engine is not None:
            dbmod._engine.dispose()
        dbmod._engine = None
        dbmod._Session = None
        get_settings.cache_clear()
