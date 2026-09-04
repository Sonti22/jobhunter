"""report.intents_daily: раскладка решений классификатора."""
import os
import uuid
from datetime import datetime, timezone

import pytest


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "ri.db")
    os.environ["TELEGRAM_BOT_TOKEN"] = "test:token"
    os.environ["LLM_ENABLED"] = "false"
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    import jobhunter.db as dbmod
    dbmod._engine = None
    dbmod._Session = None
    yield dbmod
    get_settings.cache_clear()


def test_intents_grouped_and_corrections_counted(db):
    from jobhunter.models import Application, Job, Message
    from jobhunter.report import intents_daily

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with db.session_scope() as sess:
        job = Job(external_uuid=str(uuid.uuid4()), source="tg:t", title="X")
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, score=1, status="REPLIED")
        sess.add(app)
        sess.flush()
        rows = [
            ("ask_cv", "", now), ("ask_cv", "", now),
            ("rejection", "slot_proposed", now),   # поправка LLM
            ("rejection", "rejection", now),       # согласие — не поправка
        ]
        for reg, llm, ts in rows:
            sess.add(Message(application_id=app.id, direction="in",
                             body="t", received_at=ts,
                             classifier_label=reg, llm_label=llm))
    out = intents_daily(days=1)
    day = next(iter(out["days"].values()))
    assert day["ask_cv"] == 2 and day["rejection"] == 2
    assert out["llm_corrections"] == 1
