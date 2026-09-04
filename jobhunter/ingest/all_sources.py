"""Единый прогон всех источников вакансий.

    python -m jobhunter.ingest.all_sources                # все источники
    python -m jobhunter.ingest.all_sources --only tg      # только Telegram
    python -m jobhunter.ingest.all_sources --careered-max 250
"""
import argparse
import sys

from .base import save_jobs
from .hn import HackerNewsSource
from .tgchannels import TelegramChannelSource, ingest_telegram


def _careered_jobs(max_jobs):
    """careered.io через существующий клиент, приведённый к RawJob."""
    from ..models import ContactKind
    from .base import RawJob
    from .careered import CareeredClient

    client = CareeredClient()
    who = client.verify_auth()          # канарейка: без токена ничего не пишем
    print("  careered: токен подтверждён (%s)" % who.get("mail"))
    for uuid, _entry in client.iter_ids(max_jobs=max_jobs):
        try:
            rec = client.detail(uuid)
        except Exception as exc:
            print("  ! careered %s: %s" % (uuid[:8], str(exc)[:60]))
            continue
        email = ""
        if rec.contact_kind == ContactKind.EMAIL.value:
            email = (rec.contact_url or "").replace("mailto:", "")
        yield RawJob(
            source="careered", external_uuid=uuid,
            title=rec.title, company=rec.company, tag=rec.tag,
            content=rec.content, mode=rec.mode, posted_at=rec.posted_at,
            contact_kind=rec.contact_kind, contact_handle=rec.contact_handle,
            contact_url=rec.contact_url, contact_email=email,
            all_links=rec.links, raw={"tag": rec.tag},
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest всех источников вакансий")
    ap.add_argument("--only", choices=["careered", "tg", "hn", "ats", "boards", "jobapis"], default=None)
    ap.add_argument("--careered-max", type=int, default=None,
                    help="лимит вакансий careered (по умолчанию вся лента)")
    ap.add_argument("--tg-pages", type=int, default=3,
                    help="страниц назад по каждому Telegram-каналу")
    ap.add_argument("--hn-threads", type=int, default=1)
    args = ap.parse_args()

    totals = {}

    if args.only in (None, "careered"):
        print("\n[careered.io]")
        try:
            totals["careered"] = save_jobs(_careered_jobs(args.careered_max))
        except Exception as exc:
            print("  ОШИБКА careered: %s" % str(exc)[:160])
            totals["careered"] = {"errors": 1}

    if args.only in (None, "tg"):
        src = TelegramChannelSource(throttle=0)
        print("\n[Telegram-каналы] %s" % ", ".join("@" + c for c in src.channels))
        src.close()
        totals["telegram"] = ingest_telegram(pages_per_channel=args.tg_pages)

    if args.only in (None, "hn"):
        print("\n[HN Who is hiring]")
        src = HackerNewsSource()
        totals["hn"] = save_jobs(src.iter_jobs(threads=args.hn_threads))

    if args.only in (None, "ats"):
        from .ats import ATSSource
        print("\n[ATS-фиды компаний] Greenhouse / Lever / Ashby")
        src = ATSSource()
        totals["ats"] = save_jobs(src.iter_jobs())

    if args.only in (None, "boards"):
        from .boards import SOURCES, BoardsSource
        print("\n[Job-борды] %s" % ", ".join(SOURCES))
        totals["boards"] = save_jobs(BoardsSource().iter_jobs())

    if args.only in (None, "jobapis"):
        from .jobapis import SOURCES as API_SOURCES
        print("\n[API-источники] %s" % ", ".join(API_SOURCES))
        for api_name, api_cls in API_SOURCES.items():
            try:
                totals[api_name] = save_jobs(api_cls().iter_jobs())
            except Exception as e:
                print("  ! %s: %s" % (api_name, str(e)[:80]))

    print("\n" + "=" * 62)
    print("%-12s %7s %7s %7s %9s" % ("источник", "видел", "новых", "дублей", "контактов"))
    print("-" * 62)
    grand = {"seen": 0, "new": 0, "dupes": 0, "with_contact": 0}
    for name, st in totals.items():
        print("%-12s %7d %7d %7d %9d" % (name, st.get("seen", 0), st.get("new", 0),
                                         st.get("dupes", 0), st.get("with_contact", 0)))
        for k in grand:
            grand[k] += st.get(k, 0)
    print("-" * 62)
    print("%-12s %7d %7d %7d %9d" % ("ИТОГО", grand["seen"], grand["new"],
                                     grand["dupes"], grand["with_contact"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
