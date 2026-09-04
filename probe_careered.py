# -*- coding: utf-8 -*-
"""
ГЕЙТ 0 — пробник careered.io.

Единственный вопрос, на который отвечает этот скрипт:
    видит ли бесплатный залогиненный аккаунт настоящие Telegram-хендлы
    работодателей, или они замаскированы для всех, кроме VIP?

Анонимно каждая ссылка приходит как {"key":"telegram","value":"#"} при
"mode":"preview". Если под логином там же "#", вся Telegram-часть проекта
не имеет смысла и строить её не надо.

Скрипт ничего не пишет в БД, не ставит telethon, не логинится и не трогает
paywall. Только два GET-набора и сравнение.

Запуск:
    python probe_careered.py
    python probe_careered.py --opened-uuid <uuid, который ты открывал в браузере>
"""
import argparse
import base64
import hashlib
import json
import os
import socket
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx

BASE = "https://careered.io"
HOST = "careered.io"
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "probe_out"

# Поля, чьё присутствие означает лимит на просмотры и меняет всю архитектуру.
QUOTA_HINTS = (
    "views_left", "unlocks_remaining", "daily_limit", "limit_left", "quota",
    "subscription", "tariff", "plan", "credits", "is_vip", "vip", "premium",
    "unlocked", "access_level", "tier",
)

# ─────────────────────────────────────────────────────────────── .env ──


def load_env(path: Path) -> dict:
    """Минимальный парсер .env — ноль зависимостей.

    Значение читается до конца строки: Cookie содержит и '=' и ';'.
    """
    env = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


# ─────────────────────────────────────────────────────────────── TLS ──


def tls_probe() -> tuple:
    """Возвращает (notAfter_utc, spki_sha256_b64, expired: bool).

    Валидацию цепочки отключаем намеренно — сертификат протух, но SPKI
    листового сертификата пиннуем, чтобы не быть полностью открытыми для
    MITM с живой сессионной кукой в заголовке.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((HOST, 443), timeout=15) as sock:
        with ctx.wrap_socket(sock, server_hostname=HOST) as tls:
            der = tls.getpeercert(True)

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        cert = x509.load_der_x509_certificate(der)
        not_after = cert.not_valid_after_utc
        spki_der = cert.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        spki = base64.b64encode(hashlib.sha256(spki_der).digest()).decode()
    except ImportError:
        # Без cryptography пиннуем весь сертификат целиком — грубее, но работает.
        not_after = None
        spki = base64.b64encode(hashlib.sha256(der).digest()).decode()

    expired = bool(not_after and not_after < datetime.now(timezone.utc))
    return not_after, spki, expired


# ────────────────────────────────────────────────────────────── HTTP ──


def make_client(env: dict, auth: bool) -> httpx.Client:
    headers = {
        "User-Agent": env.get("CAREERED_UA", "Mozilla/5.0"),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru,en;q=0.9",
        "Referer": BASE + "/",
    }
    if auth:
        if env.get("CAREERED_COOKIE"):
            headers["Cookie"] = env["CAREERED_COOKIE"]
        if env.get("CAREERED_AUTH"):
            headers["Authorization"] = env["CAREERED_AUTH"]
    verify = env.get("CAREERED_INSECURE_TLS", "false").lower() != "true"
    return httpx.Client(base_url=BASE, headers=headers, verify=verify,
                        timeout=30.0, follow_redirects=False)


def get_json(client: httpx.Client, path: str, label: str) -> tuple:
    """Возвращает (status_code, parsed_json_or_None). Троттлинг 2с между запросами."""
    time.sleep(2.0)
    try:
        r = client.get(path)
    except Exception as exc:                       # сеть/TLS
        print("  ! %-28s ОШИБКА: %s" % (label, exc))
        return None, None
    body = None
    if r.headers.get("content-type", "").startswith("application/json"):
        try:
            body = r.json()
        except Exception:
            body = None
    print("  . %-28s HTTP %s  %s" % (label, r.status_code,
                                     "json" if body is not None else "не json"))
    return r.status_code, body


# ────────────────────────────────────────────────────── JSON-утилиты ──


def flatten(obj, prefix=""):
    """JSON → {'путь.через.точку': скалярное значение}. Списки индексируются."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, "%s.%s" % (prefix, k) if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, "%s[%d]" % (prefix, i)))
    else:
        out[prefix] = obj
    return out


def masked_paths(flat: dict) -> list:
    """Пути, где значение — литерал '#', то есть замаскированный сервером контакт."""
    return sorted(p for p, v in flat.items() if v == "#")


def quota_hits(blob) -> list:
    """Ищет в сыром JSON признаки лимита на просмотры."""
    text = json.dumps(blob, ensure_ascii=False).lower()
    return [h for h in QUOTA_HINTS if h in text]


def save(name: str, blob) -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / name).write_text(json.dumps(blob, ensure_ascii=False, indent=2),
                            encoding="utf-8")


# ─────────────────────────────────────────────────────────── main ──


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--opened-uuid", default=None,
                    help="uuid вакансии, которую ты ТОЧНО открывал в браузере "
                         "под своим логином — проверяем, не разблокируется ли "
                         "хендл только после просмотра")
    ap.add_argument("--sample", type=int, default=5,
                    help="сколько вакансий сверять детально (по умолчанию 5)")
    args = ap.parse_args()

    env = load_env(ROOT / ".env")
    if not env.get("CAREERED_COOKIE") and not env.get("CAREERED_AUTH"):
        print("СТОП: в .env нет ни CAREERED_COOKIE, ни CAREERED_AUTH.")
        print("Инструкция по захвату — в .env.example, занимает 2 минуты.")
        return 2

    print("=" * 78)
    print("ГЕЙТ 0 — пробник careered.io")
    print("=" * 78)

    # ── TLS ──────────────────────────────────────────────────────────
    print("\n[1/6] TLS")
    not_after, spki, expired = tls_probe()
    print("  notAfter : %s" % (not_after or "не определено"))
    print("  SPKI     : %s" % spki)
    if expired:
        print("  ! Сертификат ПРОСРОЧЕН. Идём с verify=False + пиннингом SPKI.")
        print("    Только из доверенной сети: в заголовке живая сессионная кука.")
    pin = env.get("CAREERED_SPKI_PIN", "").strip()
    if pin and pin != spki:
        print("  !! SPKI НЕ СОВПАЛ С ЗАКРЕПЛЁННЫМ.")
        print("     Ожидали: %s" % pin)
        print("     Получили: %s" % spki)
        print("     Либо продлили сертификат, либо MITM. Разберись до продолжения.")
        return 3
    if not pin:
        print("  -> впиши в .env: CAREERED_SPKI_PIN=%s" % spki)

    anon = make_client(env, auth=False)
    auth = make_client(env, auth=True)
    query = env.get("CAREERED_QUERY", "links_type=all&links=telegram&remote=true")
    list_path = "/api/jobs?%s&offset=0" % query

    # ── список ───────────────────────────────────────────────────────
    print("\n[2/6] Список вакансий: /api/jobs?%s" % query)
    _, anon_list = get_json(anon, list_path, "список анонимно")
    _, auth_list = get_json(auth, list_path, "список под логином")
    if not anon_list or not auth_list:
        print("СТОП: список не получен. Проверь токен и доступность сайта.")
        return 4
    save("list_anon.json", anon_list)
    save("list_auth.json", auth_list)

    a_entries = anon_list.get("entries", []) or []
    u_entries = auth_list.get("entries", []) or []
    print("  total анонимно     : %s" % anon_list.get("total"))
    print("  total под логином  : %s" % auth_list.get("total"))
    if anon_list.get("total") != auth_list.get("total"):
        print("  ! Разное количество — под логином видно другой набор вакансий.")

    a_modes = {e.get("mode") for e in a_entries}
    u_modes = {e.get("mode") for e in u_entries}
    print("  mode в списке анонимно    : %s" % (a_modes or "поля нет"))
    print("  mode в списке под логином : %s" % (u_modes or "поля нет"))

    # ── квоты и тариф ────────────────────────────────────────────────
    print("\n[3/6] Признаки лимита на просмотры")
    hits = quota_hits(auth_list)
    print("  в ответе списка: %s" % (", ".join(hits) if hits else "не найдено"))
    for p in ("/api/me", "/api/user", "/api/users/me", "/api/profile",
              "/api/subscription", "/api/users/me/features"):
        code, blob = get_json(auth, p, p)
        if code == 200 and blob is not None:
            save("me_%s.json" % p.strip("/").replace("/", "_"), blob)
            h = quota_hits(blob)
            print("      -> найдено: %s" % (", ".join(h) if h else "ничего из списка"))

    # ── детали ───────────────────────────────────────────────────────
    a_ids = [e.get("id") or e.get("uuid") for e in a_entries]
    u_ids = [e.get("id") or e.get("uuid") for e in u_entries]
    common = [i for i in a_ids if i and i in set(u_ids)][:args.sample]
    if args.opened_uuid and args.opened_uuid not in common:
        common.append(args.opened_uuid)

    print("\n[4/6] Детали %d вакансий: анонимно против логина" % len(common))
    unmasked = 0
    checked = 0
    per_job = []
    for uuid in common:
        _, a_det = get_json(anon, "/api/jobs/%s" % uuid, "anon  %s" % uuid[:8])
        _, u_det = get_json(auth, "/api/jobs/%s" % uuid, "auth  %s" % uuid[:8])
        if not a_det or not u_det:
            continue
        save("job_%s_anon.json" % uuid[:8], a_det)
        save("job_%s_auth.json" % uuid[:8], u_det)

        a_flat, u_flat = flatten(a_det), flatten(u_det)
        m_paths = masked_paths(a_flat)
        revealed = [p for p in m_paths if u_flat.get(p) not in ("#", None)]
        checked += 1
        if revealed:
            unmasked += 1
        per_job.append({
            "uuid": uuid,
            "mode_anon": a_det.get("mode"),
            "mode_auth": u_det.get("mode"),
            "masked_paths": len(m_paths),
            "revealed_paths": len(revealed),
            "revealed_examples": {p: u_flat[p] for p in revealed[:3]},
            "extra_keys": sorted(set(u_flat) - set(a_flat))[:20],
            "is_opened_in_browser": uuid == args.opened_uuid,
        })
        print("      mode: %s -> %s | замаскировано %d, раскрыто %d"
              % (a_det.get("mode"), u_det.get("mode"), len(m_paths), len(revealed)))
        for p in revealed[:3]:
            print("        %s = %r" % (p, u_flat[p]))

    save("summary.json", per_job)

    # ── вердикт ──────────────────────────────────────────────────────
    print("\n[5/6] Итог")
    print("  вакансий проверено      : %d" % checked)
    print("  с раскрытыми контактами : %d" % unmasked)
    all_extra = sorted({k for j in per_job for k in j["extra_keys"]})
    if all_extra:
        print("  поля, которых нет анонимно: %s" % ", ".join(all_extra[:15]))

    print("\n[6/6] Вердикт")
    if checked == 0:
        print("  НЕ ОПРЕДЕЛЕНО — детали не получены.")
        verdict = "unknown"
    elif unmasked == checked:
        print("  ИСХОД 1: бесплатный аккаунт видит контакты. ЗЕЛЁНЫЙ СВЕТ.")
        print("  Дальше: харвест 218 вакансий (1 req/2s), потом фазы 1-7 плана.")
        verdict = "green"
    elif unmasked == 0:
        print("  ИСХОД 2: под логином контакты ВСЁ ЕЩЁ замаскированы.")
        print("  Telethon-слой НЕ строим. Порядок действий из плана:")
        print("    a) искать отдельный unlock/apply POST во вкладке Network")
        print("    b) проверить внутренний отклик на площадке (риск бана исчезает)")
        print("    c) узнать цену VIP — это бизнес-решение, не инженерное")
        print("    d) перенацелить пайплайн на источник с открытыми контактами")
        verdict = "masked"
    else:
        print("  ИСХОД 3: раскрыто %d из %d — доступ ЧАСТИЧНЫЙ." % (unmasked, checked))
        print("  Ищем правило: возраст вакансии / тег / зарплата / факт просмотра.")
        print("  Пересобираем вокруг дефицита: скоринг важнее анти-бан машинерии.")
        verdict = "partial"

    if args.opened_uuid:
        op = next((j for j in per_job if j["is_opened_in_browser"]), None)
        if op:
            print("\n  Просмотренная в браузере вакансия: раскрыто %d путей."
                  % op["revealed_paths"])
            print("  Если раскрыта только она — разблокировка идёт отдельным "
                  "запросом, его надо найти в Network.")

    print("\n  Сырые ответы для разбора: %s" % OUT)
    save("verdict.json", {"verdict": verdict, "checked": checked,
                          "unmasked": unmasked, "jobs": per_job})
    return 0


if __name__ == "__main__":
    sys.exit(main())
