"""Архив отправленного на диск: logs/sent/YYYY-MM-DD.md + JSONL.

Зачем дублировать то, что уже есть в БД и в Telegram. У БД и Telegram разные
режимы отказа, и оба чинятся не мгновенно: базу можно случайно пересоздать,
аккаунт — потерять доступ, сообщения — удалить с обеих сторон. Архив на диске
переживает и то, и другое, читается глазами без SQL и годится как
доказательство «я откликался туда-то тогда-то».

Два формата рядом: .md — чтобы прочитать за минуту, .jsonl — чтобы обработать
скриптом. Оба дописываются построчно, без перезаписи: параллельная отправка
не должна затирать чужие строки.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..config import get_settings


def _dir() -> Path:
    s = get_settings()
    p = Path(s.sent_archive_dir)
    if not p.is_absolute():
        from ..config import ROOT
        p = ROOT / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def record(app_id: int, channel: str, contact: str, text: str,
           job_title: str = "", company: str = "", score: float = 0.0,
           cv_path: str = "", kind: str = "cold", result: str = "ok") -> None:
    """Пишет отправленное сообщение в архив. Молча игнорирует сбои ФС."""
    s = get_settings()
    if not s.sent_archive_enabled:
        return
    now = datetime.now(timezone.utc)
    row = {
        "ts": now.isoformat(timespec="seconds"),
        "app_id": app_id, "kind": kind, "channel": channel,
        "contact": contact, "job": job_title, "company": company,
        "score": round(float(score or 0), 1), "result": result,
        "cv": Path(cv_path).name if cv_path else "",
        "text": (text or "").strip(),
    }
    try:
        day = now.strftime("%Y-%m-%d")
        base = _dir()
        with (base / (day + ".jsonl")).open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

        md = base / (day + ".md")
        header = "" if md.exists() else "# Отправлено %s\n\n" % day
        title = row["job"] or "—"
        if row["company"]:
            title += " · " + row["company"]
        with md.open("a", encoding="utf-8") as f:
            f.write(header)
            f.write("## %s · #%d · %s\n\n" % (now.strftime("%H:%M"), app_id, title))
            f.write("- канал: **%s** → `%s`%s\n"
                    % (channel, contact,
                       "  · резюме: `%s`" % row["cv"] if row["cv"] else ""))
            f.write("- скор: %.0f · тип: %s · результат: %s\n\n"
                    % (row["score"], kind, result))
            f.write("> " + row["text"].replace("\n", "\n> ") + "\n\n")
    except OSError:
        pass                    # архив полезен, но не критичен для отправки


def stats(days: int = 7) -> dict:
    """Сводка по архиву за последние дни — для дашборда и /status."""
    base = _dir()
    files = sorted(base.glob("*.jsonl"))[-days:]
    total, by_channel, by_result = 0, {}, {}
    for f in files:
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                total += 1
                by_channel[row.get("channel", "?")] = by_channel.get(
                    row.get("channel", "?"), 0) + 1
                by_result[row.get("result", "?")] = by_result.get(
                    row.get("result", "?"), 0) + 1
        except (OSError, ValueError):
            continue
    return {"total": total, "by_channel": by_channel, "by_result": by_result,
            "dir": str(base), "days": len(files)}
