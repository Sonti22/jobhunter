"""Живые сообщения рекрутёров, на которых классификатор ошибся.

Не гипотетические примеры — тексты из боевой базы. Каждый из них стоил
одного заглохшего диалога: система пометила сообщение «не понял» или
«вежливость», рекрутёр не получил ответа, тред умер. Восемь ответов из
тринадцати были потеряны так.

Этот файл — регрессионный якорь. Если он краснеет, значит какая-то правка
классификатора вернула ровно ту ошибку, которая уже однажды обошлась
дорого.
"""
import pytest

from jobhunter.convo.classify import classify

# (текст из БД, ожидаемая метка, чем была раньше)
LIVE_CASES = [
    ("Добрый вечер! Присылайте резюме", "ask_cv", "unknown 0.00"),
    ("На связи!", "ack", "unknown 0.00"),
    ("спасибо за отклик, но вакансия уже не актуальна",
     "rejection", "ack 0.70 — отказ считался вежливостью"),
    ("вакансия пока на стопе, могу присылать другие?",
     "rejection", "unknown 0.00"),
    # Проверка 19.09: текущий классификатор всё ещё не понимал 21 из 47 входящих.
    ("Если готовы рассмотреть, могу запросить резюме?", "ask_cv", "unknown 0.00"),
    ("Добрый день! Открыта, могу попросить вас резюме?", "ask_cv", "unknown 0.00"),
    ("Просьба направить резюме на рассмотрение", "ask_cv", "unknown 0.00"),
    ("Уже закрыли позицию", "rejection", "unknown 0.00"),
    ("К сожалению, вакансию захолдили. Давайте оставаться на связи", "rejection", "rejection"),
    # рекрутёр уже выбрал человека, но просит резюме: решает владелец, не автоответ
    ("Здравствуйте! На данный момент уже выбрал кандидата. Но можете скинуть резюме!",
     "rejection", "unknown 0.00"),
    # бот написал соискателю, приняв его резюме за вакансию
    ("Здравствуйте, это не вакансия, это мое резюме)", "rejection", "unknown 0.00"),
    ("Стоп. Так вы тоже ищите работу?) я работу ищу, а не сотрудника.", "rejection", "unknown 0.00"),
]


@pytest.mark.parametrize("text,expected,was", LIVE_CASES,
                         ids=[c[0][:28] for c in LIVE_CASES])
def test_live_recruiter_message(text, expected, was):
    got = classify(text)
    assert got.label == expected, (
        "%r → %s (раньше: %s)" % (text, got.label, was))


def test_rejection_wording_variants():
    """Формулировки закрытия позиции — все в отказ, а не в «спасибо»."""
    for txt in ("Позиция закрыта, спасибо за интерес",
                "Вакансия заполнена",
                "Набор приостановлен до осени",
                "This role is no longer available"):
        assert classify(txt).label == "rejection", txt


def test_ack_still_narrow():
    """Расширение ACK не должно съедать осмысленные сообщения."""
    assert classify("Спасибо, посмотрим").label == "ack"
    # «спасибо» + вопрос о деньгах — это деньги, а не вежливость
    assert classify("Спасибо! Какие у вас зарплатные ожидания?").label == "money"


def test_send_cv_wording_variants():
    for txt in ("Присылайте резюме", "Пришлите резюме, пожалуйста",
                "Скиньте CV", "Please send your CV"):
        assert classify(txt).label == "ask_cv", txt


# Ложные срабатывания дней недели — реальные тексты из БД, из-за которых
# обе заявки в статусе «предложено интервью» оказались фальшивыми.
NOT_A_SLOT = [
    ("Набор на эту позицию мы приостановили, сроки не ясны", "«сроки» → среда"),
    ("Всё понятно, спасибо", "«всё» → воскресенье"),
    ("сбор документов займёт время", "«сбор» → суббота"),
    ("что вы думаете о вакансии", "«что» → четверг"),
    ("средство разработки", "«сред» → среда"),
    ("I am satisfied with monitoring setup", "«sat»/«mon» → суббота/понедельник"),
]


@pytest.mark.parametrize("text,why", NOT_A_SLOT,
                         ids=[c[0][:26] for c in NOT_A_SLOT])
def test_not_a_time_proposal(text, why):
    """Слово, начинающееся как сокращение дня, — не предложение времени."""
    assert classify(text).label != "slot_proposed", why


REAL_SLOTS = ["Давайте в среду в 15:00", "Можем в чт?", "How about Thursday?",
              "Завтра в 11 удобно?", "Let us meet on Mon. at 3pm",
              "в пятницу удобно"]


@pytest.mark.parametrize("text", REAL_SLOTS, ids=[t[:24] for t in REAL_SLOTS])
def test_real_time_proposal_still_matches(text):
    """Ужесточение не должно съесть настоящие предложения времени."""
    assert classify(text).label == "slot_proposed", text


def test_paused_hiring_is_rejection_not_interview():
    """«Набор приостановили» — отказ, а не приглашение на интервью."""
    it = classify("Набор на эту позицию мы сейчас приостановили, "
                  "рекомендую не рассчитывать на нас")
    assert it.label == "rejection", it.label


# ── Находки адверсарного ревью 30.08 ──

def test_version_numbers_are_not_time():
    """«Python 3.10» и «оплата 20.00» — не предложение времени."""
    for txt in ("у нас Python 3.10 и Django 4.2",
                "нужна версия 1.75 или новее",
                "оплата 20.00 в час на старте"):
        assert classify(txt).label != "slot_proposed", txt
    # а настоящее время — по-прежнему время
    assert classify("давайте в 15:00").label == "slot_proposed"
    assert classify("созвон в 15.30 удобно?").label == "slot_proposed"


def test_poluchilos_is_not_ack():
    """«всё получилось с другим кандидатом» — не вежливость с автоответом."""
    it = classify("у нас всё получилось с другим кандидатом")
    assert it.label != "ack", it.label
    # честные «получил/принял» остаются вежливостью
    assert classify("Получил, посмотрю").label == "ack"
    assert classify("Принял в работу").label == "ack"


def test_priostanov_needs_vacancy_context():
    """«приостановка проекта, продолжаем» — не отказ с закрытием заявки."""
    it = classify("приостановка проекта не связана с вами, продолжаем")
    assert it.label != "rejection", it.label
    # с контекстом вакансии — отказ как и раньше
    assert classify("вакансия приостановлена").label == "rejection"
    assert classify("Набор приостановлен до осени").label == "rejection"


def test_slot_beats_work_format():
    """Слот в ESCALATE_ALWAYS обязан побеждать автоответ о формате."""
    it = classify("можете работать в офисе? созвонимся в среду в 15:00")
    assert it.label == "slot_proposed", it.label


def test_weekday_abbrev_before_punctuation():
    """«в пт!» — реальный слот, знак препинания не должен его прятать."""
    for txt in ("созвонимся в пт!", "может в чт;", "в пт: обсудим детали",
                "(предлагаю в ср)"):
        assert classify(txt).label == "slot_proposed", txt
