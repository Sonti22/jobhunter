"""Очистка входящего письма от цитат, подписей и подвалов.

Главный тест здесь — `test_quoted_slots_do_not_leak_to_classifier`. Он
проверяет не качество текста, а корректность системы: без очистки наш
собственный автоответ с предложенными слотами, процитированный в ответе
рекрутёра, приводит к тому, что бот подтверждает живому человеку интервью,
которого никто не назначал.
"""
from jobhunter.convo.classify import ACK, SLOT_PROPOSED, classify
from jobhunter.textutil import clean_email_body, html_to_text, strip_quoted_reply, strip_signature

# Реальная форма ответа: две строки нового текста плюс цитата нашего письма,
# в котором стоят предложенные нами слоты.
REPLY_WITH_QUOTED_SLOTS = """Спасибо, посмотрю и вернусь с ответом.

вт, 26 авг. 2026 г. в 10:12, Suren Hakobyan <suren6pro@gmail.com> написал:
> Здравствуйте! Готов созвониться. Мне удобно:
> пн 26.08 в 11:00 (UTC+3); ср 28.08 в 16:00 (UTC+3).
> Подскажите, какое время подойдёт вам?
"""

OUTLOOK_REPLY = """Добрый день! Вакансия ещё актуальна.

От: Suren Hakobyan <suren6pro@gmail.com>
Отправлено: вторник, 26 августа 2026 г. 10:12
Кому: HR <hr@company.ru>
Тема: Отклик: Python Backend
Дата: 26.08.2026

Здравствуйте! Пишу по вакансии, зарплата от 300 000 руб.
"""

WITH_DISCLAIMER = """Спасибо за отклик, передам команде.

С уважением,
Мария Иванова
HR-менеджер

Настоящее сообщение является конфиденциальным. Если вы не являетесь его
адресатом, объясните отправителю ошибку и удалите письмо.
"""


def test_quoted_slots_do_not_leak_to_classifier():
    """Слоты из цитаты не должны читаться как предложение времени.

    Без очистки: classify видит «11:00» в цитате нашего же письма → ставит
    SLOT_PROPOSED → карточка владельцу → подтверждение несуществующего
    интервью реальному рекрутёру плюс событие в календаре.
    """
    assert classify(REPLY_WITH_QUOTED_SLOTS).label == SLOT_PROPOSED, (
        "фикстура должна воспроизводить проблему, иначе тест бесполезен")

    clean = clean_email_body(REPLY_WITH_QUOTED_SLOTS)
    assert "11:00" not in clean and "16:00" not in clean
    assert "Спасибо" in clean
    assert classify(clean).label == ACK


def test_outlook_header_block_cut():
    clean = clean_email_body(OUTLOOK_REPLY)
    assert "актуальна" in clean
    assert "Отправлено:" not in clean and "300 000" not in clean


def test_disclaimer_and_signature_cut():
    clean = clean_email_body(WITH_DISCLAIMER)
    assert "передам команде" in clean
    assert "конфиденциальным" not in clean
    assert "HR-менеджер" not in clean


def test_short_reply_survives():
    """«Спасибо!» — валидный ответ, а не мусор: резать его нечем и незачем."""
    assert clean_email_body("Спасибо!") == "Спасибо!"
    assert clean_email_body("Ок, принято") == "Ок, принято"


def test_pure_quote_returns_empty():
    """Письмо из одной цитаты: нового текста нет, классифицировать нечего."""
    assert clean_email_body("> старый текст\n> и ещё строка\n") == ""


def test_html_quote_dropped():
    html = ("<div>Здравствуйте, готовы обсудить.</div>"
            "<blockquote>пн 26.08 в 11:00 (UTC+3)</blockquote>")
    clean = clean_email_body(html, is_html=True)
    assert "готовы обсудить" in clean
    assert "11:00" not in clean


def test_html_gmail_quote_container_dropped():
    html = ('<div>Ответ по существу.</div>'
            '<div class="gmail_quote">цитата с 16:00 внутри</div>')
    assert "16:00" not in clean_email_body(html, is_html=True)


def test_broken_html_does_not_raise():
    assert "текст" in html_to_text("<div><span>текст</div></b><<>")


def test_strip_functions_are_independent():
    """Функции должны работать по отдельности — их зовут и порознь."""
    assert "wrote:" not in strip_quoted_reply(
        "новый текст\nOn Tue, 25 Aug 2026 at 10:12, X <a@b> wrote:\n> старое")
    assert "Best regards" not in strip_signature(
        "первая строка\nвторая строка\nBest regards\nJohn")


def test_invisible_characters_removed():
    """Zero-width пробелы рвут границы слов в регулярках классификатора."""
    assert clean_email_body("зар​плата\xa0обсудим") == "зарплата обсудим"
