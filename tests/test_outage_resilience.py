# -*- coding: utf-8 -*-
"""Сетевой обрыв не должен стоить дня работы.

10.09 утром пропал DNS: доставка бота сожгла пять попыток на каждом из
17 уведомлений за секунды (цикл крутится раз в секунду), и утренняя
пачка ручных откликов умерла, не дойдя. Мёртвые строки при этом навсегда
занимали ключ дедупа — ту же карточку нельзя было переотправить. А в
саму пачку попали закрытая вакансия, дубль и «Публикатор: …» вместо
названия должности.
"""
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

OWNER = 5875908057


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "outage.db")
    os.environ["TELEGRAM_BOT_TOKEN"] = "test:token"
    os.environ["BOT_ALLOWED_USER_IDS"] = str(OWNER)
    os.environ["OWNER_CHANNEL"] = "bot"
    os.environ["LLM_ENABLED"] = "false"
    os.environ["GCAL_ENABLED"] = "false"
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    import jobhunter.db as dbmod
    dbmod._engine = None
    dbmod._Session = None
    yield dbmod
    get_settings.cache_clear()


def _rows(db, dedup):
    from jobhunter.models import BotOutbox
    with db.session_scope() as sess:
        return [(r.id, r.attempts, r.sent_at) for r in
                sess.query(BotOutbox).filter(BotOutbox.dedup_key == dedup).all()]


# ── доставка: сеть лежит — попытки не горят ──

def test_network_error_does_not_burn_attempts(db, monkeypatch):
    import httpx

    from jobhunter import notify
    from jobhunter.bot import outbox

    notify.push("error", "проверка обрыва", dedup="net-down-1")

    def down(*a, **kw):
        raise httpx.ConnectError("[Errno -3] Temporary failure in name resolution")

    monkeypatch.setattr(outbox.api, "send_message", down)
    monkeypatch.setattr(outbox.time, "sleep", lambda *_: None)
    for _ in range(10):                 # десять оборотов цикла доставки
        outbox.drain()
    (row_id, attempts, sent_at), = _rows(db, "net-down-1")
    assert sent_at is None
    assert attempts == 0, "обрыв сети не должен тратить попытки"


def test_network_failure_older_than_ttl_is_closed(db, monkeypatch):
    import httpx

    from jobhunter import notify
    from jobhunter.bot import outbox
    from jobhunter.models import BotOutbox

    notify.push("error", "старое", dedup="net-down-old")
    with db.session_scope() as sess:
        row = sess.query(BotOutbox).filter(BotOutbox.dedup_key == "net-down-old").one()
        row.created_at = (datetime.now(timezone.utc).replace(tzinfo=None)
                          - timedelta(hours=outbox.WAIT_TTL_HOURS + 1))

    monkeypatch.setattr(outbox.api, "send_message",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            httpx.ConnectError("Network is unreachable")))
    monkeypatch.setattr(outbox.time, "sleep", lambda *_: None)
    outbox.drain()
    (_, attempts, _), = _rows(db, "net-down-old")
    assert attempts == 1, "устаревшее закрывается, а не висит вечно"


def test_telegram_refusal_still_counts_as_attempt(db, monkeypatch):
    """Настоящий отказ API (400) — это не сеть, попытка тратится."""
    from jobhunter import notify
    from jobhunter.bot import outbox

    notify.push("error", "битое", dedup="bad-request")
    monkeypatch.setattr(outbox.api, "send_message",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            RuntimeError("sendMessage: HTTP 400 Bad Request")))
    monkeypatch.setattr(outbox.time, "sleep", lambda *_: None)
    outbox.drain()
    (_, attempts, _), = _rows(db, "bad-request")
    assert attempts == 1


# ── дедуп: мёртвая строка не держит ключ ──

def test_dead_row_does_not_block_resend(db):
    from jobhunter import notify
    from jobhunter.models import BotOutbox

    notify.push("manual_item", "карточка", dedup="manual:777")
    with db.session_scope() as sess:
        row = sess.query(BotOutbox).filter(BotOutbox.dedup_key == "manual:777").one()
        row.attempts = 5                # умерла на сетевом обрыве
    notify.push("manual_item", "карточка ещё раз", dedup="manual:777")
    rows = _rows(db, "manual:777")
    assert len(rows) == 2, "переотправка той же карточки обязана пройти"

    # а живой дубль по-прежнему схлопывается
    notify.push("manual_item", "дубль", dedup="manual:777")
    assert len(_rows(db, "manual:777")) == 2


# ── ручная пачка: закрытые, дубли, мусорные заголовки ──

def _job_app(db, title, company="", description="", score=90):
    from jobhunter.models import Application, Job, Status
    with db.session_scope() as sess:
        job = Job(external_uuid=str(uuid.uuid4()), source="careered",
                  title=title, company_name=company,
                  description_raw=description or title,
                  contact_url="https://example.com/job/%s" % uuid.uuid4().hex[:6],
                  posted_at=int(time.time()) - 3600)
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, status=Status.HANDLE_MISSING.value,
                          score=score, outcome="")
        sess.add(app)
        sess.flush()
        return app.id


def test_listing_skips_closed_dedups_and_cleans_titles(db):
    from jobhunter.manual_apply import listing

    closed = _job_app(db, "❌**ЗАКРЫТА**❌ **Системный аналитик Middle+**")
    dup1 = _job_app(db, "Senior Platform Engineer", company="Partnerize", score=92)
    dup2 = _job_app(db, "Senior Platform Engineer", company="Partnerize", score=92)
    meta = _job_app(db, "Публикатор: Margarita Ivanishcheva",
                    description="Публикатор: Margarita Ivanishcheva\n"
                                "Обсуждение:\nИнженер по данным в команду платформы")
    emoji = _job_app(db, "🔍 **Backend-разработчик")
    fun = _job_app(db, "Backend Engineer",
                   description="Join our fun-filled team, closed beta soon")

    rows = {r["id"]: r for r in listing(top=50)}
    assert closed not in rows, "закрытая автором вакансия не идёт в пачку"
    assert (dup1 in rows) != (dup2 in rows), "дубль схлопнут до одной карточки"
    assert rows[meta]["title"] == "Инженер по данным в команду платформы"
    assert rows[emoji]["title"] == "Backend-разработчик"
    assert fun in rows, "«fun-filled» и «closed beta» — не закрытие вакансии"
