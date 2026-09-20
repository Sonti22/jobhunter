"""График «сколько сообщений уходит по дням» на странице статистики (просьба владельца 20.09)."""
import random
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "chart.db"))
    monkeypatch.setenv("OWNER_TZ", "Europe/Moscow")
    import jobhunter.db as dbmod
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    dbmod._engine = None
    dbmod._Session = None
    yield dbmod
    if dbmod._engine is not None:
        dbmod._engine.dispose()
    dbmod._engine = None
    dbmod._Session = None
    get_settings.cache_clear()


def _send(db, source, peer, result="ok", hours_ago=1.0, reply=False, applied=False):
    from jobhunter.models import Application, Job, Message, SendLog
    at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours_ago)
    with db.session_scope() as sess:
        job = Job(external_uuid=str(random.random()), source=source, title="Backend Engineer")
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, status="AWAITING_REPLY", applied_at=at if applied else None)
        sess.add(app)
        sess.flush()
        if peer:
            sess.add(SendLog(application_id=app.id, result=result, peer_id=peer, attempted_at=at))
        if reply:
            sess.add(Message(application_id=app.id, direction="in", body="ok", received_at=at))


def test_activity_series_splits_channels_and_keeps_empty_days(db):
    from jobhunter import report
    _send(db, "hn", "jobs@acme.io", reply=True)
    _send(db, "direct:exec", "cto@kong.com")
    _send(db, "tg:python", "@anna_hr")
    _send(db, "hn", "x@y.io", result="bounce")
    _send(db, "ats:greenhouse", "", applied=True)
    _send(db, "hn", "old@acme.io", hours_ago=24 * 40)             # старше окна — мимо
    rows = report.activity_series(30)
    assert len(rows) == 30 and rows[0]["date"] < rows[-1]["date"]
    recent = [r for r in rows[-2:] if r["sent"]]                  # «час назад» мог выпасть на вчера
    total = {k: sum(r[k] for r in recent) for k in ("mail", "direct", "tg", "failed", "manual", "replies", "sent")}
    assert total == {"mail": 1, "direct": 1, "tg": 1, "failed": 1, "manual": 1, "replies": 1, "sent": 3}
    assert sum(r["sent"] for r in rows[:-2]) == 0                 # дни простоя в ряду есть, и они нулевые


def test_chart_is_readable_without_colour_and_without_the_chart(db):
    from jobhunter import report
    from jobhunter.web import charts
    _send(db, "hn", "jobs@acme.io")
    _send(db, "direct:exec", "cto@kong.com")
    rows = report.activity_series(14)
    svg = charts.bars(rows, charts.SERIES, label="Отправлено по дням")
    assert svg.startswith("<svg") and "role='img'" in svg and "aria-label='Отправлено по дням'" in svg
    assert svg.count("<title>") == 14                             # подсказка у каждого дня
    assert "Отклики почтой: 1" in svg and "Прямые письма: 1" in svg
    assert all(name in charts.legend(charts.SERIES) for _, name, _ in charts.SERIES)
    assert "<details>" in charts.table(rows) and charts.table(rows).count("<tr>") == 15
    assert charts.bars([], charts.SERIES) == "<p class='muted'>ещё нет данных</p>"


def test_stats_page_shows_the_daily_block(db):
    from fastapi.testclient import TestClient

    from jobhunter.web.server import app
    _send(db, "hn", "jobs@acme.io")
    page = TestClient(app).get("/stats")
    assert page.status_code == 200
    assert "Отправка по дням" in page.text and "ушло сегодня" in page.text and "<svg" in page.text
