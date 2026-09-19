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


def test_referral_channel_post_keeps_role_company_and_apply_bot(monkeypatch):
    """18.09: 77 постов «Рефералки в IT» потеряны — заголовком шла «Область и стек: Бэкенд»,
    компания не читалась, а кнопка отклика через реф-бота не попадала в контакты."""
    import jobhunter.ingest.tgchannels as tg

    monkeypatch.setattr(tg, "_verified_channels", lambda: [])
    monkeypatch.setattr(tg, "_discovered", lambda: [])
    html = (
        '<div data-post="refer_me_it/793"><time datetime="2026-09-17T07:00:00+00:00">'
        '<div class="tgme_widget_message_text">'
        "Область и стек: Бэкенд<br/><br/>Должность: Java Lead<br/><br/>Компания: Дом.РФ<br/><br/>"
        "Зарплатная вилка: 500к+<br/>Формат работы: Удаленка<br/>"
        "ЧЕМ ПРЕДСТОИТ ЗАНИМАТЬСЯ: руководить командой, backend-разработка на Spring Boot. "
        "НАШИ ПОЖЕЛАНИЯ К КАНДИДАТУ: опыт с Kafka. Предложка — "
        '<a href="https://t.me/refer_me_it_bot">@refer_me_it_bot</a><br/>'
        '<a href="https://telegram.me/refer_me_it_bot?start=vacancy_1ef77ef8-aa">Откликнуться на вакансию</a>'
        "</div></div>"
    )

    class Response:
        status_code = 200
        text = html

    src = tg.TelegramChannelSource(channels=["refer_me_it"], throttle=0)
    monkeypatch.setattr(src.http, "get", lambda *args, **kwargs: Response())
    try:
        job = list(src.iter_jobs(pages_per_channel=1))[0]
    finally:
        src.close()
    assert job.title == "Java Lead" and job.company == "Дом.РФ"
    assert job.contact_kind == "bot"
    assert job.contact_url == "https://t.me/refer_me_it_bot?start=vacancy_1ef77ef8-aa"
    assert job.raw["referral"] is True
    assert not job.has_direct_contact                     # автомат туда не пишет — только владелец


HABR_POST = (
    '<div data-post="progjob/501"><time datetime="2026-09-18T07:00:00+00:00">'
    '<div class="tgme_widget_message_text">'
    "Вакансия: Backend-разработчик (Python). Требования: Python, FastAPI, PostgreSQL. "
    "Мы предлагаем удалённую работу, зарплата от 300 000 ₽. Подписывайтесь: "
    '<a href="https://t.me/progjob">@progjob</a><br/>'
    '<a href="https://career.habr.com/vacancies/1000123">Откликнуться на Хабр Карьере</a>'
    "</div></div>")


def _scan(monkeypatch, channel, html):
    import jobhunter.ingest.tgchannels as tg
    monkeypatch.setattr(tg, "_verified_channels", lambda: [])
    monkeypatch.setattr(tg, "_discovered", lambda: [])

    class Response:
        status_code = 200
        text = html
    src = tg.TelegramChannelSource(channels=[channel], throttle=0)
    monkeypatch.setattr(src.http, "get", lambda *args, **kwargs: Response())
    try:
        return list(src.iter_jobs(pages_per_channel=1))
    finally:
        src.close()


def test_apply_on_site_link_is_a_contact_for_the_manual_queue(monkeypatch):
    """Проверка 19.09: progjob — 290 постов из 290 без контакта, хотя у каждого ссылка на
    career.habr.com. Вакансия закрывалась как недостижимая и даже не оценивалась."""
    job = _scan(monkeypatch, "progjob", HABR_POST)[0]
    assert job.contact_kind == "external_url"
    assert job.contact_url == "https://career.habr.com/vacancies/1000123"
    assert not job.has_direct_contact                      # автомат не пишет — только ручная очередь


def test_social_and_channel_links_are_not_apply_links():
    from jobhunter.ingest.tgchannels import apply_site_link
    assert apply_site_link(["https://t.me/progjob", "https://youtube.com/watch?v=1",
                            "https://telegra.ph/x"]) == ""
    assert apply_site_link(["https://yandex.ru/jobs/vacancies/123", "https://t.me/ya_jobs"]) \
        == "https://yandex.ru/jobs/vacancies/123"


def test_vacancy_closed_as_unreachable_comes_back_when_contact_is_found(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "revive.db"))
    import jobhunter.db as dbmod
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    dbmod._engine = None
    dbmod._Session = None
    try:
        from sqlalchemy import select

        from jobhunter.ingest.base import save_jobs
        from jobhunter.models import Application, Job
        job = _scan(monkeypatch, "progjob", HABR_POST)[0]
        old = job.__class__(**{**job.__dict__, "contact_kind": "unknown", "contact_url": "",
                               "all_links": []})
        assert save_jobs(iter([old]), verbose=False)["unreachable"] == 1
        stats = save_jobs(iter([job]), verbose=False)
        assert stats["revived"] == 1
        with dbmod.session_scope() as sess:
            row = sess.scalar(select(Job))
            app = sess.scalar(select(Application))
            assert row.contact_kind == "external_url" and row.contact_url.startswith("https://career.habr")
            assert app.status == "HANDLE_MISSING"
    finally:
        if dbmod._engine is not None:
            dbmod._engine.dispose()
        dbmod._engine = None
        dbmod._Session = None
        get_settings.cache_clear()


def test_bare_bot_link_is_not_an_apply_button():
    from jobhunter.ingest.tgchannels import apply_bot_link, post_fields
    assert apply_bot_link(["https://t.me/refer_me_it_bot", "https://t.me/somechannel"]) == ""
    assert apply_bot_link(["https://t.me/jobs_bot?start=v_12345"]) == "https://t.me/jobs_bot?start=v_12345"
    long = "Компания: " + "мы делаем продукт для банков и страховых компаний по всему миру уже десять лет"
    assert "company" not in post_fields(long)


def test_referral_vacancies_come_first_in_manual_cards():
    from jobhunter.bot.cards import manual_keyboard
    from jobhunter.manual_batch import _card
    row = {"id": 1, "title": "Java Lead", "company": "Дом.РФ", "score": 60.0, "salary": "",
           "source": "tg:refer_me_it", "cv_path": "", "referral": True}
    assert _card(row).startswith("🤝 Java Lead") and "реферальной ссылке" in _card(row)
    kb = manual_keyboard(1, "https://t.me/refer_me_it_bot?start=v", referral=True)
    assert kb["inline_keyboard"][0][0]["text"] == "🤝 Откликнуться через реферала"


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
