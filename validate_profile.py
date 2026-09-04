# -*- coding: utf-8 -*-
"""Проверка целостности profile.yaml.

Не гейт (тот проверяет сгенерированные документы), а санитарная проверка
самого источника правды: битые ссылки, конфликты уровней, завышенные годы.
Запускать после каждой правки profile.yaml.

    python validate_profile.py
"""
import sys
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
LEVELS = {"expert", "working", "familiar", "none"}


def months(start: str, end) -> int:
    """Длительность в месяцах. end=None означает 'по настоящее время'."""
    sy, sm = (int(x) for x in start.split("-"))
    if end:
        ey, em = (int(x) for x in end.split("-"))
    else:
        today = date.today()
        ey, em = today.year, today.month
    return (ey - sy) * 12 + (em - sm)


def main() -> int:
    profile = yaml.safe_load((ROOT / "profile.yaml").read_text(encoding="utf-8"))
    errors, warnings = [], []

    exp = profile["experience"]
    exp_ids = {e["id"] for e in exp}
    skills = profile["skills"]
    skill_ids = {s["id"] for s in skills}
    never = profile.get("never_claim", [])

    # 1. evidence_ids ссылаются на существующий опыт
    for s in skills:
        if s["level"] not in LEVELS:
            errors.append("skill %s: недопустимый level %r" % (s["id"], s["level"]))
        for ev in s.get("evidence_ids", []):
            if ev not in exp_ids:
                errors.append("skill %s: evidence_id %r не существует" % (s["id"], ev))
        if not s.get("evidence_ids"):
            warnings.append("skill %s: нет evidence_ids — нечем подтвердить" % s["id"])

    # 2. skills в буллетах существуют
    bullet_ids = set()
    for e in exp:
        for b in e.get("bullets", []):
            if b["id"] in bullet_ids:
                errors.append("дубль bullet id %r" % b["id"])
            bullet_ids.add(b["id"])
            for sk in b.get("skills", []):
                if sk not in skill_ids:
                    errors.append("bullet %s: навык %r не объявлен в skills"
                                  % (b["id"], sk))
            for m in b.get("metrics", []):
                if not all(k in m for k in ("value", "unit", "claim")):
                    errors.append("bullet %s: метрика без value/unit/claim" % b["id"])

    # 3. years навыка не больше суммы длительностей подтверждающего опыта
    exp_by_id = {e["id"]: e for e in exp}
    for s in skills:
        if not s.get("evidence_ids"):
            continue
        total = sum(months(exp_by_id[ev]["start"], exp_by_id[ev]["end"])
                    for ev in s["evidence_ids"])
        if s.get("years", 0) * 12 > total + 6:      # 6 мес допуск на округление
            errors.append("skill %s: заявлено %s лет, но подтверждающий опыт даёт "
                          "только %.1f года" % (s["id"], s["years"], total / 12))

    # 4. пересечение skills и never_claim — прямое противоречие
    never_terms = set()
    for n in never:
        never_terms.add(n["canonical"].lower())
        never_terms.update(a.lower() for a in n.get("aliases", []))
    for s in skills:
        terms = {s["canonical"].lower()} | {a.lower() for a in s.get("aliases", [])}
        clash = terms & never_terms
        if clash:
            errors.append("skill %s конфликтует с never_claim: %s"
                          % (s["id"], ", ".join(sorted(clash))))

    # 5. общий стаж совпадает с заявленным
    start = profile["meta"]["timeline_start"]
    real_years = months(start, None) / 12
    claimed = profile["claims"]["total_years_software"]
    if abs(real_years - claimed) > 1.0:
        errors.append("claims.total_years_software=%s, а по timeline_start=%s "
                      "выходит %.1f" % (claimed, start, real_years))

    # 6. непрерывность таймлайна
    ordered = sorted(exp, key=lambda e: e["start"])
    for prev, nxt in zip(ordered, ordered[1:]):
        if prev["end"] and prev["end"] > nxt["start"]:
            warnings.append("перекрытие: %s заканчивается %s, %s начинается %s"
                            % (prev["id"], prev["end"], nxt["id"], nxt["start"]))
        if prev["end"] and months(prev["end"], nxt["start"]) > 2:
            warnings.append("разрыв %d мес между %s и %s"
                            % (months(prev["end"], nxt["start"]), prev["id"], nxt["id"]))

    # ── отчёт ──
    print("profile.yaml")
    print("  опыт        : %d записей, %d буллетов" % (len(exp), len(bullet_ids)))
    print("  навыки      : %d (expert %d, working %d, familiar %d)"
          % (len(skills),
             sum(1 for s in skills if s["level"] == "expert"),
             sum(1 for s in skills if s["level"] == "working"),
             sum(1 for s in skills if s["level"] == "familiar")))
    print("  never_claim : %d терминов, %d синонимов"
          % (len(never), len(never_terms)))
    print("  общий стаж  : %.1f года от %s" % (real_years, start))

    for w in warnings:
        print("  ~ %s" % w)
    for e in errors:
        print("  ! %s" % e)

    print("\n%s" % ("ОШИБОК НЕТ" if not errors else "ОШИБОК: %d" % len(errors)))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
