# -*- coding: utf-8 -*-
"""Демонстрация движка подгонки на реальных вакансиях из БД.

    python demo_tailor.py            # топ-скор вакансии из БД
    python demo_tailor.py --uuid X   # конкретная вакансия
"""
import argparse
import sys

from sqlalchemy import select

from jobhunter.config import get_settings
from jobhunter.db import session_scope
from jobhunter.models import Job
from jobhunter.match.scorer import score_job
from jobhunter.tailor.render import render_cv
from jobhunter.tailor.select import tailor


def run(uuid=None, top=3):
    with session_scope() as s:
        jobs = s.scalars(select(Job).where(Job.mode == "full")).all()
        rows = []
        for j in jobs:
            sc = score_job(j.title, j.tag, j.description_raw)
            rows.append((sc.total, j.external_uuid, j.title, j.tag, j.description_raw, j.company_name))
        rows.sort(key=lambda r: -r[0])

    print("Скоринг вакансий в БД (mode:full):")
    for total, uid, title, tag, _desc, _co in rows[:12]:
        print("  %5.1f  [%-14s] %s" % (total, tag[:14], (title or "(no title)")[:48]))

    targets = [r for r in rows if r[1] == uuid] if uuid else rows[:top]
    out = get_settings().cv_out
    print("\nПодгонка резюме:")
    for total, uid, title, tag, desc, co in targets:
        res = tailor(title, tag, desc)
        hint = "Hakobyan_%s_%s" % ((tag or "role").replace("/", "").replace(" ", ""), uid[:8])
        if res.ok:
            path, digest = render_cv(res.render, out, filename_hint=hint, unique_seed=uid)
            print("\n  [%s] %s" % (tag, (title or "(no title)")[:50]))
            print("    score=%.1f lang=%s bullets=%d" % (total, res.lang,
                  sum(len(j["bullets"]) for j in res.render["jobs"])))
            print("    headline: %s" % res.render["headline"])
            if res.gate.promoted_terms:
                print("    promoted (термины работодателя): %s" % ", ".join(res.gate.promoted_terms))
            print("    match: %s" % res.score.reason)
            print("    PDF: %s  sha256=%s" % (path, digest[:12]))
        else:
            print("\n  [%s] %s — ГЕЙТ НЕ ПРОШЁЛ:" % (tag, (title or "")[:40]))
            for f in res.gate.hard:
                print("      ! %s: %s (%s)" % (f.rule_id, f.offending, f.detail))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--uuid", default=None)
    ap.add_argument("--top", type=int, default=3)
    args = ap.parse_args()
    run(uuid=args.uuid, top=args.top)
    sys.exit(0)
