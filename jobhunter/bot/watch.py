"""Бот присматривает за автопилотом и говорит владельцу, когда тот встал.

19.09 автопилот простоял три часа после обрыва сети, и владелец узнал об этом, только
спросив. Сам автопилот в такой момент сообщить ничего не может, а бот живёт в отдельном
контейнере и видит его пульс через общий том.
"""
from __future__ import annotations

import time

from .. import health, notify

# Основной пульс бьётся раз в минуту; телеграм-очередь легально молчит до ~25 минут.
LIMITS = {"autopilot": 10 * 60, "autopilot_tg": 45 * 60}
CHECK_EVERY = 60.0

_state = {"down_since": 0.0, "checked": 0.0}


def stalled() -> tuple:
    """(какой пульс молчит, сколько секунд) — или ("", 0.0), если всё живо."""
    for name, limit in LIMITS.items():
        age = health.age(name)
        if limit < age < float("inf"):
            return name, age
    return "", 0.0


def check(now: float | None = None) -> str:
    """'down' | 'up' | '' — что сообщили владельцу на этом проходе."""
    now = time.time() if now is None else now
    if now - _state["checked"] < CHECK_EVERY:
        return ""
    _state["checked"] = now
    name, age = stalled()
    if name and not _state["down_since"]:
        _state["down_since"] = now - age
        what = "телеграм-очередь" if name == "autopilot_tg" else "планировщик"
        notify.push_once("autopilot_down",
                    "🛑 Автопилот стоит: %s молчит %d мин. Шаги дня не выполняются — ни сбор, "
                    "ни отправка, ни разбор ответов.\nЧаще всего это сеть или VPN. Сторож сам "
                    "перезапустит его в течение часа; быстрее — docker compose restart autopilot"
                    % (what, age // 60),
                    dedup="autopilot_down:%s" % time.strftime("%Y-%m-%d-%H", time.gmtime(now)))
        return "down"
    if not name and _state["down_since"]:
        idle = int((now - _state["down_since"]) // 60)
        _state["down_since"] = 0.0
        notify.push_once("autopilot_up",
                    "✅ Автопилот снова работает, простой был около %d мин. Пропущенные шаги дня "
                    "он догоняет сам." % idle,
                    dedup="autopilot_up:%s" % time.strftime("%Y-%m-%d-%H-%M", time.gmtime(now)))
        return "up"
    return ""
