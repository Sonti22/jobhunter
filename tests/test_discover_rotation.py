# -*- coding: utf-8 -*-
"""Автопоиск каналов не должен выдыхаться.

06.09: проверок по дням 152, 105, 6, 2, 1, 0 — фиксированные seed-каналы
и запросы возвращали одно и то же, а отказ был навсегда. Плюс 18 каналов
прошли проверку без --apply и две недели не собирались.
"""
import os
import random
from datetime import timedelta

import pytest


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "disc.db")
    os.environ["LLM_ENABLED"] = "false"
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    import jobhunter.db as dbmod
    dbmod._engine = None
    dbmod._Session = None
    yield dbmod
    get_settings.cache_clear()


def test_seeds_rotate_between_runs():
    from jobhunter.ingest.discover import pick_seeds

    pool = ["ch%02d" % i for i in range(120)]
    a = pick_seeds(pool, 15, random.Random(1))
    b = pick_seeds(pool, 15, random.Random(2))
    assert len(a) == len(b) == 15
    assert set(a) != set(b), "одинаковые seed'ы каждый день — автопоиск выдыхается"
    assert pick_seeds(["A", "a", "b"], 15) == ["a", "b"], "дедуп без регистра"


def test_recheck_picks_only_stale_soft_rejections(db, monkeypatch):
    from jobhunter.ingest import discover
    from jobhunter.models import ChannelCandidate, utcnow

    old = utcnow() - timedelta(days=20)
    with db.session_scope() as sess:
        sess.add_all([
            ChannelCandidate(username="dead_old", passed=False, enabled=False,
                             reason="мёртвый: 0 постов за неделю", checked_at=old),
            ChannelCandidate(username="junk_old", passed=False, enabled=False,
                             reason="мусорная тематика", checked_at=old),
            ChannelCandidate(username="dead_new", passed=False, enabled=False,
                             reason="мёртвый: 1 постов за неделю", checked_at=utcnow()),
        ])

    seen = []

    def fake_eval(username, http, known=None):
        seen.append(username)
        return {"username": username, "ok": True, "reason": "20 постов, 9 за неделю, 5 с контактом",
                "posts": 20, "fresh7": 9, "with_contact": 5, "subscribers": 0, "title": username}

    monkeypatch.setattr(discover, "evaluate", fake_eval)
    monkeypatch.setattr(discover.time, "sleep", lambda *_: None)
    st = discover.recheck_rejected(days=14, limit=10, apply=True)
    assert seen == ["dead_old"], seen
    assert st["revived"] == 1 and st["added"] >= 1
    with db.session_scope() as sess:
        row = sess.query(ChannelCandidate).filter_by(username="dead_old").one()
        assert row.passed and row.enabled, "оживший канал обязан попасть в сбор"


def test_apply_enables_previously_passed_channels(db):
    from jobhunter.ingest.discover import _apply_to_registry
    from jobhunter.models import ChannelCandidate

    with db.session_scope() as sess:
        sess.add(ChannelCandidate(username="passed_but_off", passed=True,
                                  enabled=False, reason="ок"))
    n = _apply_to_registry([])
    assert n >= 1
    with db.session_scope() as sess:
        assert sess.query(ChannelCandidate).filter_by(
            username="passed_but_off").one().enabled
