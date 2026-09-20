"""Google Calendar: подтверждённое интервью уезжает в календарь владельца.

Авторизация — OAuth для настольных приложений, один раз:

    python -m jobhunter.schedule.gcal --login

Откроется браузер, Google спросит доступ к календарю, токен ляжет в
google_token.json рядом с проектом (в .gitignore). Дальше библиотека сама
обновляет его по refresh_token.

Область доступа намеренно узкая — calendar.events: читать и писать события,
но не управлять календарями и не трогать остальной аккаунт.

Приглашения участникам НЕ рассылаются: адрес рекрутёра в событии — это
письмо от вашего имени человеку, который его не ждал. Ссылку на встречу
отправляет сам бот текстом в тот же диалог, где договорились.

Библиотеки ставятся отдельно:
    pip install google-api-python-client google-auth-oauthlib
Их нет — модуль честно говорит об этом, и остаётся .ics-файл.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import ROOT, get_settings

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


class GCalUnavailable(RuntimeError):
    """Нет библиотек, нет client_secret или пользователь не авторизовался."""


def _path(raw: str) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


def available() -> tuple:
    """(готов, причина). Проверяет библиотеки, секрет и токен — без сети."""
    s = get_settings()
    if not s.gcal_enabled:
        return False, "GCAL_ENABLED=false"
    try:
        import google.oauth2.credentials  # noqa: F401
        import googleapiclient.discovery  # noqa: F401
    except ImportError:
        return False, ("нет библиотек: pip install google-api-python-client "
                       "google-auth-oauthlib")
    if not _path(s.google_token_path).is_file():
        if not _path(s.google_client_secret_path).is_file():
            # Путь называем как есть, но подсказываем порядок: вход делается
            # на хосте (в контейнере нет браузера), а сюда приезжает только
            # готовый токен — client_id и client_secret лежат внутри него.
            return False, ("нет токена (%s). Порядок: скачать OAuth-клиент из "
                           "Google Cloud Console в корень проекта, на хосте "
                           "выполнить python -m jobhunter.schedule.gcal --login, "
                           "затем скопировать google_token.json в том"
                           % s.google_token_path)
        return False, "не авторизован: python -m jobhunter.schedule.gcal --login"
    return True, "готов"


def _credentials(interactive: bool = False):
    """Учётные данные — из общего входа Google (календарь и почта живут на одном токене)."""
    from .. import googleauth
    try:
        return googleauth.credentials(interactive, need=() if interactive else (googleauth.CALENDAR,))
    except googleauth.GoogleUnavailable as e:
        raise GCalUnavailable(str(e)) from e


def _service(interactive: bool = False):
    from googleapiclient.discovery import build
    return build("calendar", "v3", credentials=_credentials(interactive),
                 cache_discovery=False)


def freebusy(start_utc: datetime, end_utc: datetime) -> list:
    """Занятые интервалы календаря: [(start, end), ...] наивным UTC.

    Для сверки предлагаемых рекрутёрам слотов (convo/busy.py). Через
    events.list, а не freebusy.query: выданный токен несёт узкий scope
    calendar.events, которому freebusy-эндпоинт недоступен, — а требовать
    от владельца повторную авторизацию ради него не стоит.

    Ошибки (нет токена, сеть) летят как есть — вызывающий обязан быть
    fail-open: ответы рекрутёрам не должны зависеть от доступности Google.
    """
    ok, why = available()
    if not ok:
        raise GCalUnavailable(why)
    svc = _service()
    resp = svc.events().list(
        calendarId=get_settings().gcal_calendar_id,
        timeMin=start_utc.replace(tzinfo=timezone.utc).isoformat(),
        timeMax=end_utc.replace(tzinfo=timezone.utc).isoformat(),
        singleEvents=True, orderBy="startTime", maxResults=100).execute()
    out = []
    for ev in resp.get("items", []):
        if ev.get("status") == "cancelled" or ev.get("transparency") == "transparent":
            continue                     # отменённое и «свободен» не занимают
        try:
            s_raw = ev["start"].get("dateTime")
            e_raw = ev["end"].get("dateTime")
            if not (s_raw and e_raw):
                continue                 # события «на весь день» не блокируют
            s = datetime.fromisoformat(s_raw.replace("Z", "+00:00"))
            e = datetime.fromisoformat(e_raw.replace("Z", "+00:00"))
            out.append((s.astimezone(timezone.utc).replace(tzinfo=None),
                        e.astimezone(timezone.utc).replace(tzinfo=None)))
        except (KeyError, ValueError):
            continue
    return out


def upsert_event(summary: str, start_utc: datetime, duration_min: int = 60,
                 description: str = "", event_id: str = "",
                 tz_name: str = "Europe/Moscow") -> dict:
    """Создаёт или обновляет событие. Возвращает {id, link, meet}.

    Время передаётся в UTC с явным смещением: «плавающее» локальное время в
    календаре уезжает при смене пояса устройства.
    """
    s = get_settings()
    ok, why = available()
    if not ok:
        raise GCalUnavailable(why)

    if start_utc.tzinfo is None:
        start_utc = start_utc.replace(tzinfo=timezone.utc)
    end_utc = start_utc + timedelta(minutes=max(15, duration_min))

    body = {
        "summary": summary[:200],
        "description": description[:7000],
        "start": {"dateTime": start_utc.isoformat(), "timeZone": "UTC"},
        "end": {"dateTime": end_utc.isoformat(), "timeZone": "UTC"},
        "reminders": {"useDefault": False,
                      "overrides": [{"method": "popup", "minutes": 60},
                                    {"method": "popup", "minutes": 24 * 60}]},
        "source": {"title": "jobhunter", "url": "https://t.me"},
    }
    params = {"calendarId": s.gcal_calendar_id}
    if s.gcal_create_meet and not event_id:
        body["conferenceData"] = {
            "createRequest": {"requestId": "jh-%d" % int(start_utc.timestamp()),
                              "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
        params["conferenceDataVersion"] = 1

    svc = _service()
    events = svc.events()
    if event_id:
        ev = events.patch(eventId=event_id, body=body, **params).execute()
    else:
        ev = events.insert(body=body, **params).execute()

    meet = ""
    for ep in (ev.get("conferenceData", {}) or {}).get("entryPoints", []) or []:
        if ep.get("entryPointType") == "video":
            meet = ep.get("uri", "")
            break
    return {"id": ev.get("id", ""), "link": ev.get("htmlLink", ""), "meet": meet}


def delete_event(event_id: str) -> bool:
    s = get_settings()
    ok, _ = available()
    if not (ok and event_id):
        return False
    try:
        _service().events().delete(calendarId=s.gcal_calendar_id,
                                   eventId=event_id).execute()
        return True
    except Exception:
        return False


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Google Calendar для jobhunter")
    ap.add_argument("--login", action="store_true", help="разовая авторизация")
    ap.add_argument("--test", action="store_true",
                    help="создать тестовое событие через час и удалить его")
    args = ap.parse_args()

    if args.login:
        try:
            _credentials(interactive=True)
        except GCalUnavailable as e:
            print("Не вышло: %s" % e)
            return 2
        print("Готово. Токен: %s" % _path(get_settings().google_token_path))
        return 0

    ok, why = available()
    print("Google Calendar: %s (%s)" % ("готов" if ok else "не готов", why))
    if ok and args.test:
        ev = upsert_event("jobhunter — проверка",
                          datetime.now(timezone.utc) + timedelta(hours=1), 30,
                          "Тестовое событие, можно удалять.")
        print("создано: %s" % ev["link"])
        print("удалено: %s" % delete_event(ev["id"]))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
