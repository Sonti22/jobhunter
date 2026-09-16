"""Зарубежные email-вакансии не теряются на артефактах разбора.

Замер 16.09: из 172 свежих зарубежных заявок с email отправляемых было ноль.
Скорер отсеивал HN с заголовками «NY, USA» и «https://coder.com/» как «роль
не распознана», WWR терял адрес (в contact_url лежала страница борда) или
брал privacy@ из подвала, HN-тред протухал к 22-му числу, а remote-борды
получали «удалёнка не упомянута» за слово relocation.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from jobhunter.ingest.base import RawJob, _contact_url, extract_email, is_hiring_mailbox
from jobhunter.ingest.hn import _company_role
from jobhunter.match import workformat


@pytest.mark.parametrize("line,company,role", [
    ("Ours Privacy | Senior Platform Engineer | Remote (US) | Full-time | https://oursprivacy.com/careers",
     "Ours Privacy", "Senior Platform Engineer"),
    ("Cider Consulting | NY, USA | REMOTE (US-based only)", "Cider Consulting", ""),
    ("MWI Animal Health | Remote (US only) | Senior SWE, Senior DevOps",
     "MWI Animal Health", "Senior SWE, Senior DevOps"),
    ("Coder | https://coder.com/ | Multiple roles | Multiple locations | Full-time",
     "Coder", "Multiple roles"),
    ("Trax | https://www.traxtech.com/ | Onsite Cebu PH, Remote US are possible | Full-time | Architect",
     "Trax", "Architect"),
    ("Estuary | US-Based / Remote | Full-time – https://estuary.dev/", "Estuary", ""),
    ("Rerun | Remote | Stockholm, Sweden | Full-time", "Rerun", ""),
    ("VictoriaMetrics | Remote | EMEA, North America | Hiring", "VictoriaMetrics", ""),
    ("Purple Candor LLC — AI Engineer — Remote / US (Sub-contractor- 6 month+)",
     "Purple Candor LLC", "AI Engineer"),
    ("Software Engineer — Remote (US Only)", "", "Software Engineer"),
    ("Turquoise|Senior Performance Engineer|FT| Remote USA | 172-195",
     "Turquoise", "Senior Performance Engineer"),
])
def test_hn_header_finds_role_not_location(line, company, role):
    assert _company_role(line) == (company, role)


@pytest.mark.parametrize("addr", [
    "privacy@acme.com", "security@acme.com", "candidateaccommodations@acme.com",
    "gdpr@acme.com", "legal-team@acme.com", "no-reply@acme.com",
])
def test_footer_mailboxes_are_not_hiring_contacts(addr):
    assert not is_hiring_mailbox(addr)
    from jobhunter.outreach.mailer import _mailbox_ok
    assert not _mailbox_ok(addr)


def test_extract_email_skips_footer_and_keeps_real_contact():
    body = ("Send your CV to jobs@acme.com.\n\nWe respect your privacy: privacy@acme.com. "
            "Need an accommodation? candidateaccommodations@acme.com")
    assert extract_email(body) == "jobs@acme.com"
    assert extract_email("Questions: privacy@acme.com") == ""


def test_board_email_job_stores_address_not_board_page():
    rj = RawJob(source="wwr", external_uuid="wwr:1", contact_kind="email",
                contact_email="jobs@acme.com",
                contact_url="https://weworkremotely.com/remote-jobs/acme-backend")
    assert _contact_url(rj) == "jobs@acme.com"
    link_only = RawJob(source="wwr", external_uuid="wwr:2", contact_kind="external_url",
                       contact_url="https://weworkremotely.com/remote-jobs/acme")
    assert _contact_url(link_only) == "https://weworkremotely.com/remote-jobs/acme"


def _job(source, days):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(source=source, is_closed=False, title="Backend Engineer",
                           description_raw="We are hiring a Python backend engineer. Remote.",
                           posted_at=int((now - timedelta(days=days)).timestamp()))


def test_hn_thread_lives_the_whole_month(monkeypatch):
    monkeypatch.setenv("MAX_VACANCY_AGE_DAYS", "21")
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    try:
        from jobhunter.outreach.eligibility import vacancy_problem
        assert vacancy_problem(_job("hn", 30)).allowed
        assert vacancy_problem(_job("hn", 40)).code == "stale"
        assert vacancy_problem(_job("wwr", 30)).code == "stale"
    finally:
        get_settings.cache_clear()


def test_remote_only_board_is_remote_despite_relocation_word():
    text = "Senior Python Engineer. Benefits: relocation package optional, hybrid team offsites."
    assert workformat.detect(text) == workformat.ONSITE
    assert workformat.detect(text, source="wwr") == workformat.REMOTE
    # явное «no remote» сильнее источника
    assert workformat.detect("Remote: no. Office in Berlin.", source="wwr") == workformat.ONSITE
    # общий борд по-прежнему решает по тексту
    assert workformat.detect(text, source="arbeitnow") == workformat.ONSITE
