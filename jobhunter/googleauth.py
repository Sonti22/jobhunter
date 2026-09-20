"""Один вход в Google на всё: календарь интервью и почта через Gmail API.

Почему Gmail API, а не SMTP/IMAP: это обычный HTTPS на порт 443. VPN владельца режет
почтовые порты (465, 587, 993), и почта вставала на часы; HTTPS он не трогает.

Разрешения запрашиваются разом, чтобы владелец проходил вход один раз:
  calendar.events  подтверждённое интервью уезжает в календарь;
  gmail.send       отправка откликов;
  gmail.readonly   чтение ответов рекрутёров (только чтение: пометить, удалить или
                   переместить письмо бот не может).

Подводный камень, найденный 20.09: пока проект Google Cloud в статусе «Тестирование»,
токен живёт 7 дней. Токен календаря от 26.08 умер с invalid_grant, и никто не заметил.
Проект нужно перевести в статус «В работе» — тогда токен бессрочный.

    python -m jobhunter.googleauth --login    # на хосте: откроется браузер
    python -m jobhunter.googleauth --check    # что разрешено и жив ли токен
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import ROOT, get_settings

CALENDAR = "https://www.googleapis.com/auth/calendar.events"
GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"
GMAIL_READ = "https://www.googleapis.com/auth/gmail.readonly"
SCOPES = [CALENDAR, GMAIL_SEND, GMAIL_READ]


class GoogleUnavailable(RuntimeError):
    """Нет библиотек, нет client_secret, нет токена или он отозван."""


def _path(raw: str) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


def _direct_request():
    """Транспорт для обмена и обновления токена — напрямую, мимо системного прокси.

    requests берёт прокси из настроек Windows. У владельца там остаётся локальный SOCKS-порт
    VPN (socks=127.0.0.1:10808) даже при выключенном VPN, и обмен кода на токен падал с
    SOCKSHTTPSConnectionPool (20.09). Весь остальной проект ходит с trust_env=False по той же
    причине; Google доступен напрямую.
    """
    import requests
    from google.auth.transport.requests import Request
    session = requests.Session()
    session.trust_env = False
    return Request(session=session)


def granted() -> set:
    """Какие разрешения реально лежат в токене: владелец мог снять галочку на экране входа."""
    import json
    try:
        data = json.loads(_path(get_settings().google_token_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    scopes = data.get("scopes") or []
    return set(scopes.split() if isinstance(scopes, str) else scopes)


def credentials(interactive: bool = False, need: tuple = ()):
    """Учётные данные из токена; при interactive — полный вход через браузер.

    need — разрешения, без которых вызывающему делать нечего (например, GMAIL_SEND).
    """
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials

    s = get_settings()
    token_path = _path(s.google_token_path)
    creds = None
    if token_path.is_file() and not interactive:
        have = granted()
        missing = [x for x in need if x not in have]
        if missing:
            raise GoogleUnavailable("в токене нет разрешения %s — нужен повторный вход"
                                    % missing[0].rsplit("/", 1)[-1])
        # Загружаем ровно с теми разрешениями, что выданы: запрос большего при обновлении
        # токена Google отклоняет целиком.
        creds = Credentials.from_authorized_user_file(str(token_path), sorted(have) or SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(_direct_request())
        except RefreshError as e:
            raise GoogleUnavailable("токен Google отозван или истёк (%s) — нужен повторный вход: "
                                    "python -m jobhunter.googleauth --login" % str(e)[:60]) from e
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds
    if not interactive:
        raise GoogleUnavailable("нет действующего токена Google "
                                "(python -m jobhunter.googleauth --login)")

    from google_auth_oauthlib.flow import InstalledAppFlow
    secret = _path(s.google_client_secret_path)
    if not secret.is_file():
        raise GoogleUnavailable("нет файла %s" % secret)
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    flow.oauth2session.trust_env = False        # обмен кода на токен — тоже мимо системного прокси
    # offline + consent: Google выдаёт refresh_token только при явном согласии
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    token_path.write_text(creds.to_json(), encoding="utf-8")
    try:
        token_path.chmod(0o600)
    except OSError:
        pass
    return creds


def check() -> dict:
    """Жив ли токен и что им разрешено. Адрес ящика возвращается с маской."""
    have = granted()
    out = {"token": bool(have), "calendar": CALENDAR in have, "gmail_send": GMAIL_SEND in have,
           "gmail_read": GMAIL_READ in have, "alive": False, "mailbox": ""}
    if not have:
        return out
    try:
        creds = credentials()
        out["alive"] = True
        if GMAIL_READ in have:
            from googleapiclient.discovery import build
            profile = build("gmail", "v1", credentials=creds, cache_discovery=False) \
                .users().getProfile(userId="me").execute()
            addr = profile.get("emailAddress", "")
            out["mailbox"] = addr[:1] + "***" + addr[addr.find("@"):] if "@" in addr else ""
    except Exception as e:                                  # noqa: BLE001
        out["error"] = "%s: %s" % (type(e).__name__, str(e)[:120])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Вход в Google: календарь и почта")
    ap.add_argument("--login", action="store_true", help="открыть браузер и выдать разрешения")
    ap.add_argument("--check", action="store_true", help="что разрешено и жив ли токен")
    args = ap.parse_args()
    if args.login:
        credentials(interactive=True)
        print("Токен сохранён: %s" % _path(get_settings().google_token_path))
    state = check()
    human = {"calendar": "календарь", "gmail_send": "отправка писем", "gmail_read": "чтение ответов"}
    print("Токен: %s, %s" % ("есть" if state["token"] else "нет",
                             "живой" if state["alive"] else "НЕ работает"))
    for key, name in human.items():
        print("  %-16s %s" % (name, "разрешено" if state[key] else "НЕТ"))
    if state.get("mailbox"):
        print("  ящик: %s" % state["mailbox"])
    if state.get("error"):
        print("  ошибка: %s" % state["error"])
    return 0 if state["alive"] and all(state[k] for k in human) else 1


if __name__ == "__main__":
    sys.exit(main())
