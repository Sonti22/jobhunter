"""Проверки покрытия и наблюдаемости Telegram-сборщика."""

def test_short_explicit_vacancy_is_not_lost():
    from jobhunter.ingest.tgchannels import _is_vacancy

    assert _is_vacancy("Вакансия Python developer, @hr_company")
    assert not _is_vacancy("Подпишись на канал @hr_company и жди новости")


def test_channel_scan_records_success(monkeypatch):
    import jobhunter.ingest.tgchannels as tg

    monkeypatch.setattr(tg, "_verified_channels", lambda: [])
    monkeypatch.setattr(tg, "_discovered", lambda: [])
    html = (
        '<div data-post="demo/123">'
        '<time datetime="2026-09-04T07:00:00+00:00">'
        '<div class="tgme_widget_message_text">'
        "Вакансия Python developer. Требования: Python, FastAPI. "
        "Мы предлагаем удалённую работу и зарплату 250 000 ₽. @hr_company"
        "</div></div>"
    )

    class Response:
        status_code = 200
        text = html

    src = tg.TelegramChannelSource(channels=["demo"], throttle=0)
    monkeypatch.setattr(src.http, "get", lambda *args, **kwargs: Response())
    try:
        jobs = list(src.iter_jobs(pages_per_channel=1))
        assert len(jobs) == 1
        assert jobs[0].external_uuid == "tg:demo/123"
        assert src.scan_stats["demo"]["status"] == "ok"
        assert src.scan_stats["demo"]["posts"] == 1
        assert src.scan_stats["demo"]["vacancies"] == 1
        assert src.scan_stats["demo"]["contacts"] == 1
    finally:
        src.close()


def test_channel_scan_exposes_http_error(monkeypatch):
    import jobhunter.ingest.tgchannels as tg

    monkeypatch.setattr(tg, "_verified_channels", lambda: [])
    monkeypatch.setattr(tg, "_discovered", lambda: [])
    monkeypatch.setattr(tg.time, "sleep", lambda *_args: None)

    src = tg.TelegramChannelSource(channels=["demo"], throttle=0)
    monkeypatch.setattr(src.http, "get",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            RuntimeError("offline")))
    try:
        assert list(src.iter_jobs(pages_per_channel=1)) == []
        stat = src.scan_stats["demo"]
        assert stat["status"] == "error"
        assert stat["error"] == "RuntimeError"
    finally:
        src.close()


def test_scan_summary_counts_partial_channels():
    from jobhunter.ingest.tgchannels import telegram_scan_summary

    summary = telegram_scan_summary({
        "ok": {"username": "ok", "status": "ok", "pages": 3,
               "posts": 30, "vacancies": 10, "contacts": 4},
        "bad": {"username": "bad", "status": "partial", "pages": 1,
                "posts": 10, "vacancies": 2, "contacts": 1},
        "not_run": {"username": "not_run", "status": "pending"},
    })
    assert summary["scan_channels"] == 2
    assert summary["scan_pages"] == 4
    assert summary["scan_vacancies"] == 12
    assert summary["scan_errors"] == 1
    assert summary["scan_error_channels"] == ["bad"]
