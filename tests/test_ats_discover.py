"""Автопополнение реестра ATS-досок из собранных ссылок."""
import os
import uuid

import pytest


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "ats.db")
    os.environ["LLM_ENABLED"] = "false"
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    import jobhunter.db as dbmod
    dbmod._engine = None
    dbmod._Session = None
    yield dbmod
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def clean(db):
    from sqlalchemy import delete

    from jobhunter.models import Application, AtsCandidate, Job
    with db.session_scope() as sess:
        for m in (Application, Job, AtsCandidate):
            sess.execute(delete(m))
    yield


def _job(db, desc="", url=""):
    from jobhunter.models import Job
    with db.session_scope() as sess:
        j = Job(external_uuid=str(uuid.uuid4()), source="hn", title="X",
                contact_url=url, description_raw=desc)
        sess.add(j)
        sess.flush()


def test_token_extraction_all_providers(db):
    from jobhunter.ingest.ats_discover import _extract
    text = ("apply: https://boards.greenhouse.io/livekit/jobs/1 or "
            "https://jobs.lever.co/railway/abc and "
            "https://jobs.ashbyhq.com/checkly?src=x plus "
            "https://apply.workable.com/seeq/j/123 but not "
            "https://greenhouse.io/blog/post and not "
            "https://boards.greenhouse.io/embed/foo")
    got = set(_extract(text))
    assert ("greenhouse", "livekit") in got
    assert ("lever", "railway") in got
    assert ("ashby", "checkly") in got
    assert ("workable", "seeq") in got
    assert not any(t in ("blog", "embed") for _, t in got)


def test_harvest_is_idempotent(db):
    from sqlalchemy import select

    from jobhunter.ingest.ats_discover import harvest
    from jobhunter.models import AtsCandidate

    _job(db, desc="see https://jobs.lever.co/railway/1")
    s1 = harvest()
    assert s1.get("new") == 1
    s2 = harvest()
    assert not s2.get("new")
    with db.session_scope() as sess:
        assert len(sess.scalars(select(AtsCandidate)).all()) == 1


def test_registry_tokens_are_not_candidates(db):
    """Уже покрытые реестром доски не предлагаются повторно."""
    from jobhunter.ingest.ats_discover import harvest
    _job(db, desc="https://boards.greenhouse.io/gitlab/jobs/1")  # в REGISTRY
    assert not harvest().get("new")


def test_only_enabled_boards_are_merged(db):
    from jobhunter.ingest.ats import REGISTRY, ATSSource
    from jobhunter.models import AtsCandidate

    with db.session_scope() as sess:
        sess.add(AtsCandidate(provider="lever", token="offboard",
                              passed=True, enabled=False))
        sess.add(AtsCandidate(provider="lever", token="onboard",
                              company_name="On", passed=True, enabled=True))
        # дубль уже существующей записи реестра не должен задваиваться
        sess.add(AtsCandidate(provider="greenhouse", token="gitlab",
                              enabled=True))

    src = ATSSource()
    pairs = [(p, t) for p, t, _ in src.registry]
    assert ("lever", "onboard") in pairs
    assert ("lever", "offboard") not in pairs
    assert pairs.count(("greenhouse", "gitlab")) == 1
    assert len(src.registry) == len(REGISTRY) + 1
