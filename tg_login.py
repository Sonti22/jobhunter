# -*- coding: utf-8 -*-
"""Авторизация Telethon в два шага, без интерактивного терминала.

Почему в два шага. Раньше скрипт запрашивал код и тут же ждал файл с ним в
одном процессе: не успел вписать за отведённое время — процесс падал, а код
сгорал. Каждый повторный запуск дёргал send_code_request заново, и это прямой
путь к временной блокировке номера у Telegram (ровно так мы уже словили
«Sorry, too many tries» на my.telegram.org).

Теперь запрос кода и вход разделены, а phone_code_hash лежит на диске:

    python tg_login.py send      # запросить код (один раз!)
    <вписать код в tg_code.txt>
    python tg_login.py finish    # войти уже полученным кодом

Без аргументов скрипт решает сам: есть свежий запрос и файл с кодом — входит,
иначе запрашивает код. Секреты (код, пароль 2FA) вводит только владелец
аккаунта, в чат они не попадают; файл удаляется сразу после чтения.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

from jobhunter.config import get_settings

ROOT = Path(__file__).resolve().parent
CODE_FILE = ROOT / "tg_code.txt"
PASS_FILE = ROOT / "tg_2fa.txt"
PENDING = ROOT / "tg_code_request.json"

# Сколько живёт запрос кода. Telegram не публикует точный срок; 15 минут —
# консервативная оценка, дальше проще запросить новый, чем ловить
# PhoneCodeExpiredError.
PENDING_TTL = 900


def _read_secret(path: Path, what: str) -> str:
    value = path.read_text(encoding="utf-8-sig", errors="ignore").strip()
    path.unlink(missing_ok=True)             # секрет на диске не задерживается
    if not value:
        raise ValueError("файл %s пустой (ожидался %s)" % (path.name, what))
    return value


def _load_pending() -> dict | None:
    if not PENDING.exists():
        return None
    try:
        data = json.loads(PENDING.read_text(encoding="utf-8"))
    except Exception:
        return None
    if time.time() - data.get("ts", 0) > PENDING_TTL:
        return None
    return data


async def _client():
    from telethon import TelegramClient

    s = get_settings()
    if not s.tg_api_id or not s.telegram_api_hash:
        print("Нет TELEGRAM_API_ID / TELEGRAM_API_HASH в .env.")
        print("Получить: my.telegram.org → API development tools")
        return None, None
    if not s.telegram_phone:
        print("Нет TELEGRAM_PHONE в .env (формат +79991234567)")
        return None, None

    client = TelegramClient(s.telegram_session_path, s.tg_api_id, s.telegram_api_hash)
    client.flood_sleep_threshold = 60
    await client.connect()
    return client, s


async def do_send() -> int:
    client, s = await _client()
    if client is None:
        return 2
    if await client.is_user_authorized():
        me = await client.get_me()
        print("Уже авторизован: @%s (id %s) — код не нужен" % (me.username, me.id))
        await client.disconnect()
        return 0

    prev = _load_pending()
    if prev:
        left = int(PENDING_TTL - (time.time() - prev["ts"]))
        print("Код уже запрошен %d сек. назад, он ещё действует (~%d сек)."
              % (int(time.time() - prev["ts"]), left))
        print("Повторный запрос злит Telegram. Впиши код в %s и запусти:"
              % CODE_FILE.name)
        print("    python tg_login.py finish")
        await client.disconnect()
        return 0

    print("Запрашиваю код для %s ..." % s.telegram_phone)
    sent = await client.send_code_request(s.telegram_phone)
    PENDING.write_text(json.dumps({"phone": s.telegram_phone,
                                   "hash": sent.phone_code_hash,
                                   "ts": time.time()}), encoding="utf-8")
    await client.disconnect()

    print("Код отправлен в Telegram (не SMS) — служебный чат «Telegram».")
    print()
    print("=" * 68)
    print("1. Впиши код в файл:  %s" % CODE_FILE)
    print("2. Запусти:           python tg_login.py finish")
    print("   Время не поджимает: процесс не висит и код не сгорит.")
    print("=" * 68)
    return 0


async def do_finish() -> int:
    pending = _load_pending()
    if not pending:
        print("Нет свежего запроса кода. Сначала: python tg_login.py send")
        return 2

    # Код уже принят на прошлом запуске, остался только пароль 2FA.
    # Требовать код заново нельзя: он одноразовый и уже сгорел.
    await_password = bool(pending.get("await_password"))
    if not await_password and not CODE_FILE.exists():
        print("Нет файла %s — впиши туда код из Telegram." % CODE_FILE)
        return 2
    if await_password and not PASS_FILE.exists():
        print("Код уже принят, ждём только пароль 2FA.")
        print("Впиши пароль в %s и запусти finish ещё раз." % PASS_FILE)
        return 3

    client, s = await _client()
    if client is None:
        return 2
    if await client.is_user_authorized():
        me = await client.get_me()
        print("Уже авторизован: @%s (id %s)" % (me.username, me.id))
        PENDING.unlink(missing_ok=True)
        await client.disconnect()
        return 0

    try:
        if not await_password:
            code = _read_secret(CODE_FILE, "код подтверждения")
            print("Код принят (%d симв.), вхожу..." % len(code))
            await client.sign_in(phone=pending["phone"], code=code,
                                 phone_code_hash=pending["hash"])
        else:
            pwd = _read_secret(PASS_FILE, "пароль 2FA")
            print("Ввожу пароль 2FA...")
            await client.sign_in(password=pwd)
    except Exception as e:
        name = type(e).__name__
        if "SessionPasswordNeeded" in name:
            # Код верный, но включена 2FA. Запоминаем стадию: следующий
            # finish пойдёт сразу по ветке пароля, код больше не нужен.
            pending["await_password"] = True
            pending["ts"] = time.time()
            PENDING.write_text(json.dumps(pending), encoding="utf-8")
            print("Включена двухфакторная защита.")
            if PASS_FILE.exists():
                pwd = _read_secret(PASS_FILE, "пароль 2FA")
                print("Ввожу пароль 2FA...")
                try:
                    await client.sign_in(password=pwd)
                except Exception as e2:
                    print("Пароль не подошёл: %s" % type(e2).__name__)
                    print("Впиши верный пароль в %s и запусти finish."
                          % PASS_FILE.name)
                    await client.disconnect()
                    return 3
            else:
                print("Впиши пароль в %s и запусти finish ещё раз." % PASS_FILE)
                await client.disconnect()
                return 3
        elif "PasswordHashInvalid" in name:
            print("Пароль 2FA не подошёл. Впиши верный в %s и запусти finish."
                  % PASS_FILE.name)
            await client.disconnect()
            return 3
        else:
            print("Ошибка входа: %s: %s" % (name, e))
            if "Expired" in name or "expired" in str(e).lower():
                PENDING.unlink(missing_ok=True)
                print("Код просрочен — запроси новый: python tg_login.py send")
            await client.disconnect()
            return 1

    PENDING.unlink(missing_ok=True)
    me = await client.get_me()
    print()
    print("Авторизовано: @%s (id %s)" % (me.username, me.id))
    print("Сессия: %s  (в .gitignore — не передавать никому)"
          % s.telegram_session_path)
    if getattr(me, "restricted", False):
        print("ВНИМАНИЕ: аккаунт ограничен Telegram: %s"
              % getattr(me, "restriction_reason", ""))
    await client.disconnect()
    return 0


async def main() -> int:
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if cmd == "send":
        return await do_send()
    if cmd == "finish":
        return await do_finish()
    # Без аргументов — угадываем намерение: код или пароль на руках → входим,
    # иначе шлём запрос кода.
    p = _load_pending()
    if p and (CODE_FILE.exists()
              or (p.get("await_password") and PASS_FILE.exists())):
        return await do_finish()
    return await do_send()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
