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
