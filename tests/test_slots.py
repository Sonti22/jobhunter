"""Разбор времени из сообщений рекрутёров.

Кейсы взяты из реальных формулировок: «давайте завтра в 15:00», «удобно в
чт 28.08?», плюс ловушки — график работы, зарплата, срок опыта.
"""
from datetime import datetime, timedelta, timezone

import pytest

from jobhunter.convo.slots import OWNER_TZ, detect_tz, fmt, parse_owner_time, parse_slots, tzinfo_of

# Понедельник, 24 августа 2026, 10:00 по Москве.
NOW = datetime(2026, 8, 24, 7, 0, tzinfo=timezone.utc)


def local(s):
    return s.dt_utc.astimezone(tzinfo_of(s.tz))


def test_tomorrow_with_time():
    slots = parse_slots("Давайте созвонимся завтра в 15:00", now=NOW)
    assert len(slots) == 1
    d = local(slots[0])
    assert (d.day, d.month, d.hour, d.minute) == (25, 8, 15, 0)
    assert slots[0].confidence > 0.8


def test_explicit_date_and_time():
    slots = parse_slots("Удобно 28.08 в 11:30?", now=NOW)
    d = local(slots[0])
    assert (d.day, d.month, d.hour, d.minute) == (28, 8, 11, 30)


def test_month_name_ru():
    slots = parse_slots("Предлагаю 3 сентября в 14:00, интервью на час", now=NOW)
    d = local(slots[0])
    assert (d.day, d.month, d.hour) == (3, 9, 14)


def test_weekday_moves_to_future():
    # Сегодня понедельник; «в понедельник» — значит следующий.
    slots = parse_slots("Давайте в понедельник в 12:00 созвонимся", now=NOW)
    d = local(slots[0])
    assert d.weekday() == 0 and d.day == 31


def test_timezone_named():
    slots = parse_slots("Завтра в 15:00 по Киеву удобно?", now=NOW)
    assert slots[0].tz == "Europe/Kyiv"
    # Летом Киев тоже UTC+3, поэтому проверяем на Берлине: 15:00 CEST = 16:00 МСК
    slots = parse_slots("Завтра в 15:00 по Берлину удобно?", now=NOW)
    assert slots[0].tz == "Europe/Berlin"
    msk = slots[0].dt_utc.astimezone(tzinfo_of(OWNER_TZ))
    assert msk.hour == 16


def test_utc_offset():
    assert detect_tz("созвон завтра 10:00 UTC+2") == "UTC+2"
    slots = parse_slots("созвон завтра 10:00 UTC+2", now=NOW)
    assert slots[0].dt_utc.hour == 8


def test_english():
    slots = parse_slots("Can we have a call tomorrow at 3pm CET?", now=NOW)
    d = local(slots[0])
    assert (d.day, d.hour) == (25, 15)
    assert slots[0].tz == "Europe/Berlin"


def test_two_options():
    slots = parse_slots("Могу завтра в 11:00 или в 16:00, как удобнее?", now=NOW)
    hours = sorted(local(s).hour for s in slots)
    assert hours == [11, 16]


def test_workday_schedule_is_not_a_slot():
    slots = parse_slots("График работы с 10 до 19, офис в центре, зарплата 250 000",
                        now=NOW)
    assert slots == []


def test_experience_years_is_not_a_slot():
    slots = parse_slots("Требуется опыт от 3 лет, знание Python и PostgreSQL",
                        now=NOW)
    assert slots == []


def test_salary_is_not_a_slot():
    slots = parse_slots("Вилка 250 000 - 300 000 рублей на руки", now=NOW)
    assert slots == []


def test_past_time_ignored():
    # 08:00 уже прошло (сейчас 10:00 мск), без даты — переносим на завтра
    slots = parse_slots("Давайте созвонимся в 8:00", now=NOW)
    assert slots and local(slots[0]).day == 25


def test_date_without_time_flagged():
    slots = parse_slots("Давайте в четверг созвонимся, интервью", now=NOW)
    assert slots and slots[0].has_time is False


def test_owner_command_time():
    dt = parse_owner_time("29.08 16:00", now=NOW)
    assert dt is not None
    d = dt.astimezone(tzinfo_of(OWNER_TZ))
    assert (d.day, d.month, d.hour) == (29, 8, 16)


def test_owner_command_iso():
    dt = parse_owner_time("2026-09-01 09:30", now=NOW)
    d = dt.astimezone(tzinfo_of(OWNER_TZ))
    assert (d.day, d.month, d.hour, d.minute) == (1, 9, 9, 30)


def test_owner_command_garbage():
    assert parse_owner_time("когда-нибудь потом", now=NOW) is None


def test_fmt_readable():
    s = fmt(datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))
    assert "28.08" in s and "15:00" in s and "UTC+3" in s


@pytest.mark.parametrize("text", [
    "", "   ", "Спасибо, посмотрим ваше резюме и вернёмся",
])
def test_no_time_no_slots(text):
    assert parse_slots(text, now=NOW) == []


def test_far_future_rejected():
    slots = parse_slots("Вернёмся к вам 01.03 в 10:00", now=NOW)
    assert slots == []          # больше 60 дней вперёд


def test_dedup_same_slot():
    slots = parse_slots("Завтра в 15:00. Повторю: завтра в 15:00, ок?", now=NOW)
    assert len(slots) == 1


def test_range_short_is_a_slot():
    # «с 11 до 12» — это встреча на час, а не режим работы
    slots = parse_slots("Готовы пообщаться завтра с 11 до 12", now=NOW)
    assert slots and local(slots[0]).hour == 11


def test_slot_json_roundtrip():
    s = parse_slots("завтра в 15:00", now=NOW)[0]
    j = s.to_json()
    assert j["tz"] == OWNER_TZ and j["has_time"] is True
    assert datetime.fromisoformat(j["utc"]) - NOW < timedelta(days=2)
