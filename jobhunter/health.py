"""Пульс процессов: файлы живости для healthcheck контейнеров.

Проверять «процесс жив» через pgrep недостаточно: планировщик может висеть
живым процессом, не выполняя заданий, а именно это и есть типичный отказ.
Поэтому каждый долгоживущий процесс трогает свой файл, а healthcheck смотрит
на его свежесть.

Файлы лежат рядом с базой (в контейнере — в томе), чтобы healthcheck внутри
контейнера видел ровно то, что пишет его собственный процесс.
"""
from __future__ import annotations

import time
from pathlib import Path

from .config import get_settings


def _dir() -> Path:
    s = get_settings()
    p = Path(s.heartbeat_dir)
    if not p.is_absolute():
        from .config import ROOT
        p = ROOT / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def beat(name: str) -> None:
    """Отметить, что процесс жив. Сбои файловой системы не критичны."""
    try:
        (_dir() / ("%s.beat" % name)).write_text(str(int(time.time())),
                                                 encoding="utf-8")
    except OSError:
        pass


def age(name: str) -> float:
    """Сколько секунд назад процесс отмечался. inf — не отмечался вовсе."""
    try:
        return time.time() - (_dir() / ("%s.beat" % name)).stat().st_mtime
    except OSError:
        return float("inf")


def check(name: str, max_age: int) -> bool:
    """Живость для healthcheck: отметка свежее max_age секунд."""
    return age(name) <= max_age


def ages() -> dict:
    """Возрасты всех отметок — для экрана статуса в боте."""
    out = {}
    try:
        for f in _dir().glob("*.beat"):
            out[f.stem] = round(time.time() - f.stat().st_mtime, 1)
    except OSError:
        pass
    return out


def human(seconds: float) -> str:
    """«40 с назад», «5 мин назад», «нет отметки»."""
    if seconds == float("inf"):
        return "нет отметки"
    if seconds < 90:
        return "%d с назад" % int(seconds)
    if seconds < 5400:
        return "%d мин назад" % int(seconds // 60)
    return "%d ч назад" % int(seconds // 3600)
