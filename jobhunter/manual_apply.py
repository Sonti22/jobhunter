"""Вакансии для отклика вручную через форму компании.

ATS-фиды (Greenhouse, Lever, Ashby) дают лучшие вакансии в базе — напрямую от
работодателя. Но отклик там через форму, а не письмом, и автозаполнять её
нельзя: отсеивающие вопросы дают уверенно-неверные ответы и закрывают компанию
навсегда. Поэтому система делает всё, кроме последнего клика: оценивает,
готовит персональное резюме и отдаёт ссылку.

    python -m jobhunter.manual_apply            # топ-20 с резюме
    python -m jobhunter.manual_apply --top 40
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy import or_, select

from .config import get_settings
from .db import session_scope
from .match.scorer import score_job
from .models import Application, ContactKind, Job, Status, utcnow
from .tailor.render import render_cv
from .tailor.select import tailor

MIN_SCORE = 45.0


def rescore_all(verbose: bool = True) -> dict:
    """Оценивает вакансии без прямого контакта — их конвейер пропускает."""
    stats = {"scored": 0, "relevant": 0}
    with session_scope() as sess:
        rows = sess.scalars(
            select(Application).where(
                Application.status == Status.HANDLE_MISSING.value,
                Application.score == 0.0)).all()
        # Нерекомендованные обнуляются, поэтому по одному лишь score == 0
        # они попадают в выборку на КАЖДОМ прогоне и пересчитываются заново.
        # Отметка в разборе скоринга делает проход идемпотентным.
        ids = [a.id for a in rows
               if not (a.score_breakdown_json or {}).get("scored_at")]

    for aid in ids:
        with session_scope() as sess:
            a = sess.get(Application, aid)
            job = sess.get(Job, a.job_id)
            if not job or job.contact_kind == ContactKind.UNKNOWN.value:
                # Отметка обязательна и для пропущенных: без неё эти строки
                # (2201 штука) возвращаются в выборку на каждом прогоне и
                # холостой ход растёт вместе с базой.
                a.score_breakdown_json = {"skipped": "no_contact",
                                          "scored_at": utcnow().isoformat()}
                stats["skipped"] = stats.get("skipped", 0) + 1
                continue
            sc = score_job(job.title, job.tag, job.description_raw)
            # Не рекомендованные (junior, непрофильные, сплошь чужой стек)
            # обнуляем — иначе они всплывают в топе списка ручных откликов.
            a.score = sc.total if sc.recommend else 0.0
            a.score_breakdown_json = {"reason": sc.reason,
                                      "matched": [t for t, _, _ in sc.matched_skills],
                                      "recommend": sc.recommend,
                                      "scored_at": utcnow().isoformat()}
            stats["scored"] += 1
            if sc.recommend and sc.total >= MIN_SCORE:
                stats["relevant"] += 1
        if verbose and stats["scored"] % 100 == 0:
            print("  ... оценено %d" % stats["scored"])
    return stats


def build_cvs(top: int = 20, verbose: bool = True) -> list:
    """Готовит резюме под лучшие вакансии с ручным откликом."""
    out = []
    s = get_settings()
    with session_scope() as sess:
        rows = sess.scalars(
            select(Application).join(Job, Application.job_id == Job.id)
            .where(Application.status == Status.HANDLE_MISSING.value,
                   Application.score >= MIN_SCORE,
                   # Фильтр по источнику отрезал 88 greenhouse-вакансий,
                   # пришедших из careered и tg-каналов: важно наличие
                   # ссылки для отклика, а не имя источника.
                   Job.contact_url != "")
            .order_by(Application.score.desc()).limit(top)).all()
        ids = [(a.id, a.cv_path) for a in rows]

    for aid, existing in ids:
        with session_scope() as sess:
            a = sess.get(Application, aid)
            job = sess.get(Job, a.job_id)
            if existing:
                out.append(aid)
                continue
            res = tailor(job.title, job.tag, job.description_raw)
            if not res.ok:
                a.gate_failures_json = [{"rule": f.rule_id, "term": f.offending}
                                        for f in res.gate.hard]
                continue
            # Роль резюме в имени файла, компания — вторым куском: так рекрутёр
            # видит и позицию, и что файл собран под него, а не разослан веером.
            # Без компании — хеш uuid: у постов одного канала общий префикс,
            # и срез первых символов давал всем одно имя файла.
            import hashlib
            tail = (job.company_name or "").replace("/", "").replace(" ", "")[:20] \
                or hashlib.sha1((job.external_uuid or "").encode()).hexdigest()[:8]
            hint = "Hakobyan_%s_%s" % (res.cv_slug, tail)
            path, digest = render_cv(res.render, s.cv_out, filename_hint=hint,
                                     unique_seed=job.external_uuid)
            a.cv_path, a.cv_sha256, a.cv_lang = path, digest, res.lang
            a.gate_passed = True
            a.promoted_terms_json = res.gate.promoted_terms
            out.append(aid)
            if verbose:
                print("  %5.0f  %-34s %s" % (a.score, (job.company_name or "")[:34],
                                             (job.title or "")[:44]))
    return out


# Состояния ручного отклика. Живут в Application.outcome — см. комментарий
# у поля в models.py о том, почему не в статусе.
OUTCOME_NEW = ""
OUTCOME_APPLIED = "applied"
OUTCOME_NOT_FIT = "not_fit"
OUTCOME_SNOOZED = "snoozed"


def listing(top: int = 30, source: str = "", pending_only: bool = True) -> list:
    """Данные для очереди: что открыть, с каким резюме и в каком состоянии.

    source="" — все источники. Раньше показывались только ATS-вакансии, и из
    четырёх тысяч без прямого контакта владелец видел несколько сотен: у
    остальных ссылка на вакансию тоже есть, просто ведёт не на ATS.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = []
    with session_scope() as sess:
        q = (select(Application).join(Job, Application.job_id == Job.id)
             .where(Application.status == Status.HANDLE_MISSING.value,
                    Application.score >= MIN_SCORE,
                    (Job.is_closed.is_(False) | Job.is_closed.is_(None))))
        if source:
            q = q.where(Job.source.like(source + "%"))
        if pending_only:
            # Отложенное возвращается, когда срок вышел; обработанное —
            # никогда: повторно открывать ту же вакансию бессмысленно.
            q = q.where(Application.outcome.in_((OUTCOME_NEW, OUTCOME_SNOOZED)))
        apps = sess.scalars(
            q.order_by(Application.score.desc()).limit(top * 3)).all()
        for a in apps:
            if (pending_only and a.outcome == OUTCOME_SNOOZED
                    and a.snooze_until and a.snooze_until > now):
                continue
            j = sess.get(Job, a.job_id)
            # Без ссылки открывать нечего: строка в очереди, по которой
            # нельзя откликнуться, тратит время владельца впустую.
            if not (j.contact_url or "").strip():
                continue
            # Старше недели — тоже мимо: ручной отклик стоит минут владельца,
            # и тратить их на форму, где набор давно закрыт, обиднее всего.
            # Дата неизвестна (0/None) — не режем, часть источников её не даёт.
            if j.posted_at:
                age_days = (now - datetime.utcfromtimestamp(j.posted_at)).days
                if age_days > 14:
                    continue
            rows.append({
                "id": a.id, "score": a.score,
                "company": j.company_name or (j.source or "").split(":")[-1],
                "title": j.title or "",
                "tag": j.tag, "url": j.contact_url or "",
                "salary": j.salary_raw or "",
                "cv_path": a.cv_path or "",
                "source": j.source,
                "outcome": a.outcome or OUTCOME_NEW,
            })
            if len(rows) >= top:
                break
    return rows


def mark(app_id: int, outcome: str, snooze_days: int = 3) -> str:
    """Отметить ручной отклик. Возвращает короткий человеческий итог."""
    from datetime import timedelta

    if outcome not in (OUTCOME_APPLIED, OUTCOME_NOT_FIT, OUTCOME_SNOOZED):
        return "неизвестная отметка"
    with session_scope() as sess:
        a = sess.get(Application, app_id)
        if not a:
            return "заявка #%d не найдена" % app_id
        a.outcome = outcome
        a.applied_at = utcnow() if outcome == OUTCOME_APPLIED else a.applied_at
        a.snooze_until = (utcnow().replace(tzinfo=None)
                          + timedelta(days=snooze_days)
                          if outcome == OUTCOME_SNOOZED else None)
        job = sess.get(Job, a.job_id)
        title = (job.title or job.tag or "")[:40] if job else ""
    return {OUTCOME_APPLIED: "откликнулся: %s",
            OUTCOME_NOT_FIT: "не подходит: %s",
            OUTCOME_SNOOZED: "отложено: %s"}[outcome] % title


def unreachable_ids(older_than_days: int = 14) -> list:
    """Заявки, по которым откликнуться физически нечем.

    Ни ссылки, ни хендла — вакансия попала в базу из поста без единого
    способа связи. В очереди такие не показываются (фильтр в listing), но
    раздувают счётчик «без контакта» до 5517 и создают ощущение
    неразобранного завала, которого нет: достижимых из них 3239.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    out = []
    with session_scope() as sess:
        apps = sess.scalars(
            select(Application).join(Job, Application.job_id == Job.id)
            .where(Application.status == Status.HANDLE_MISSING.value,
                   Application.outcome == OUTCOME_NEW)).all()
        for a in apps:
            j = sess.get(Job, a.job_id)
            if (j.contact_url or "").strip() or (j.contact_handle or "").strip():
                continue
            # Свежие не трогаем: контакт мог не распознаться, но вакансия
            # ещё актуальна — вдруг recontact найдёт его вторым проходом.
            if j.posted_at:
                age = (now - datetime.utcfromtimestamp(j.posted_at)).days
                if age <= older_than_days:
                    continue
            out.append(a.id)
    return out


def sweep_unreachable(apply: bool = False, older_than_days: int = 14) -> dict:
    """Закрывает недостижимые заявки. По умолчанию — сухой прогон.

    WITHDRAWN, а не удаление: Job.external_uuid уникален, и дедуп при
    следующем сборе опирается на существование записи. Удалим — те же
    вакансии заведутся заново на ближайшем прогоне.
    """
    ids = unreachable_ids(older_than_days)
    if not apply:
        return {"found": len(ids), "closed": 0, "dry": True}
    closed = 0
    for app_id in ids:
        with session_scope() as sess:
            a = sess.get(Application, app_id)
            if a and a.advance(Status.WITHDRAWN, reason="нет канала отклика"):
                closed += 1
    return {"found": len(ids), "closed": closed, "dry": False}


def stats() -> dict:
    """Сводка по очереди ручных откликов."""
    from sqlalchemy import func

    with session_scope() as sess:
        rows = sess.execute(
            select(Application.outcome, func.count(Application.id))
            .where(Application.status == Status.HANDLE_MISSING.value)
            .group_by(Application.outcome)).all()
        ready = sess.scalar(
            select(func.count(Application.id))
            .where(Application.status == Status.HANDLE_MISSING.value,
                   Application.score >= MIN_SCORE,
                   Application.outcome == OUTCOME_NEW)) or 0
        # Достижимость считаем отдельно: «5517 без контакта» — цифра,
        # которая пугает и ничего не значит, потому что две трети из них
        # открыть можно, а треть нельзя вовсе.
        reachable = sess.scalar(
            select(func.count(Application.id))
            .select_from(Application).join(Job, Application.job_id == Job.id)
            .where(Application.status == Status.HANDLE_MISSING.value,
                   or_(Job.contact_url != "", Job.contact_handle != ""))) or 0
    counts = {(k or OUTCOME_NEW): v for k, v in rows}
    total = sum(counts.values())
    return {"total": total, "ready": ready,
            "reachable": reachable, "unreachable": total - reachable,
            "applied": counts.get(OUTCOME_APPLIED, 0),
            "not_fit": counts.get(OUTCOME_NOT_FIT, 0),
            "snoozed": counts.get(OUTCOME_SNOOZED, 0)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Вакансии для ручного отклика")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--no-cv", action="store_true", help="только оценить")
    args = ap.parse_args()

    print("Оценка вакансий без прямого контакта...")
    st = rescore_all()
    print("  оценено %d, релевантных %d\n" % (st["scored"], st["relevant"]))

    if not args.no_cv:
        print("Готовлю резюме под топ-%d:" % args.top)
        made = build_cvs(args.top)
        print("\n  резюме готово: %d" % len(made))

    print("\nЛучшие вакансии для отклика через форму компании:")
    print("%5s  %-24s %-40s %s" % ("скор", "компания", "вакансия", "ссылка"))
    print("-" * 110)
    for r in listing(args.top):
        print("%5.0f  %-24s %-40s %s" % (r["score"], r["company"][:24],
                                         r["title"][:40], r["url"][:40]))
    print("\nОткрыть дашборд: http://127.0.0.1:8765/manual")
    return 0


if __name__ == "__main__":
    sys.exit(main())
