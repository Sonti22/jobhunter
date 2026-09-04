"""Прогон ingest: лента → детали → дедуп → БД.

Пишем только mode:full (контакт есть). preview → отдельная заявка HANDLE_MISSING
для статистики, но без контакта.

Дедуп три ключа: external_uuid, (handle_norm,title_norm), description_hash.
"""
import argparse
import sys

from sqlalchemy import select

from ..db import session_scope
from ..models import Application, Employer, Job, Status
from ..textutil import norm_hash
from .careered import AuthLostError, CareeredClient, title_norm


def _dedupe_hit(sess, uuid, handle_norm, title_n, desc_hash) -> str:
    if sess.scalar(select(Job).where(Job.external_uuid == uuid)):
        return "external_uuid"
    if desc_hash and sess.scalar(select(Job).where(Job.description_hash == desc_hash)):
        return "description_hash"
    if handle_norm and title_n:
        hit = sess.scalar(select(Job).where(
            Job.contact_handle_norm == handle_norm, Job.title_norm == title_n))
        if hit:
            return "handle+title"
    return ""


def ingest(max_jobs: int | None, verbose: bool = True) -> dict:
    client = CareeredClient()
    who = client.verify_auth()                     # канарейка ДО записи
    if verbose:
        print("токен подтверждён: %s" % who.get("mail"))

    stats = {"seen": 0, "full": 0, "preview": 0, "dupes": 0, "new_jobs": 0,
             "new_apps": 0, "handle_missing": 0}

    stats["errors"] = 0
    for uuid, _entry in client.iter_ids(max_jobs=max_jobs):
        stats["seen"] += 1
        try:
            rec = client.detail(uuid)
        except Exception as exc:                    # один сбой не рушит прогон
            stats["errors"] += 1
            if verbose:
                print("  ! ошибка детали %s: %s" % (uuid[:8], str(exc)[:60]))
            continue
        if rec.mode == "full":
            stats["full"] += 1
        elif rec.mode == "preview":
            stats["preview"] += 1

        handle_norm = (rec.contact_handle or "").lower()
        title_n = title_norm(rec.title)
        desc_hash = norm_hash(rec.content)

        with session_scope() as sess:
            hit = _dedupe_hit(sess, uuid, handle_norm, title_n, desc_hash)
            if hit:
                stats["dupes"] += 1
                if verbose:
                    print("  dup(%s) %s" % (hit, rec.title[:50]))
                continue

            job = Job(
                external_uuid=uuid, source="careered",
                title=rec.title, title_norm=title_n,
                company_name=rec.company, tag=rec.tag,
                description_raw=rec.content, description_hash=desc_hash,
                remote=True, mode=rec.mode,
                contact_kind=rec.contact_kind,
                contact_handle=rec.contact_handle,
                contact_handle_norm=handle_norm,
                contact_url=rec.contact_url,
                all_links_json=rec.links,
                posted_at=rec.posted_at,
                auth_fingerprint=client.auth_fingerprint,
                raw_json={"tag": rec.tag},
            )
            sess.add(job)
            sess.flush()
            stats["new_jobs"] += 1

            employer_id = None
            if rec.has_telegram_user:
                emp = sess.scalar(select(Employer).where(
                    Employer.handle_norm == handle_norm))
                if not emp:
                    emp = Employer(handle_norm=handle_norm,
                                   handle_kind=rec.contact_kind,
                                   display_name=rec.company)
                    sess.add(emp)
                    sess.flush()
                emp.total_jobs_seen += 1
                employer_id = emp.id

            if rec.mode == "full" and rec.has_telegram_user:
                status = Status.DISCOVERED.value
                stats["new_apps"] += 1
            else:
                status = Status.HANDLE_MISSING.value
                stats["handle_missing"] += 1

            sess.add(Application(job_id=job.id, employer_id=employer_id, status=status))

        if verbose and stats["seen"] % 10 == 0:
            print("  ... %d просмотрено, %d новых" % (stats["seen"], stats["new_jobs"]))

    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest careered.io")
    ap.add_argument("--max", type=int, default=None, help="ограничить число вакансий")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    try:
        stats = ingest(max_jobs=args.max, verbose=not args.quiet)
    except AuthLostError as e:
        print("AUTH LOST: %s" % e)
        return 2
    print("\nИтог ingest:")
    for k, v in stats.items():
        print("  %-16s %d" % (k, v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
