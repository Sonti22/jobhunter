"""Одобрение партии откликов перед отправкой.

    python -m jobhunter.outreach.approve --list          # что готово
    python -m jobhunter.outreach.approve --show 42       # письмо + резюме заявки
    python -m jobhunter.outreach.approve --all           # одобрить всё готовое
    python -m jobhunter.outreach.approve --ids 42,43,44  # одобрить выборочно
    python -m jobhunter.outreach.approve --reject 42     # снять с отправки
"""
import argparse
import sys

from sqlalchemy import select

from ..db import session_scope
from ..models import Application, Batch, ContactKind, Job, Status, utcnow


def _ready(sess):
    return sess.scalars(
        select(Application)
        .where(Application.status == Status.PENDING_APPROVAL.value)
        .order_by(Application.score.desc())).all()


def cmd_list() -> int:
    with session_scope() as sess:
        apps = _ready(sess)
        if not apps:
            print("Нет заявок, готовых к отправке.")
            return 0
        print("Готовы к отправке: %d\n" % len(apps))
        print("%4s %5s %-15s %-34s %-24s %s"
              % ("id", "скор", "тег", "вакансия", "контакт", "канал"))
        print("-" * 104)
        for a in apps:
            j = sess.get(Job, a.job_id)
            if j.contact_kind == ContactKind.USER_HANDLE.value:
                contact, chan = "@" + j.contact_handle, "telegram"
            elif j.contact_kind == ContactKind.EMAIL.value:
                contact, chan = (j.contact_url or "")[:24], "email"
            else:
                contact, chan = "—", "нет канала"
            print("%4d %5.0f %-15s %-34s %-24s %s"
                  % (a.id, a.score, (j.tag or "")[:15], (j.title or "")[:34],
                     contact, chan))
        print("\nОдобрить всё:      python -m jobhunter.outreach.approve --all")
        print("Посмотреть письмо: python -m jobhunter.outreach.approve --show <id>")
    return 0


def cmd_show(app_id: int) -> int:
    with session_scope() as sess:
        a = sess.get(Application, app_id)
        if not a:
            print("Нет заявки %d" % app_id)
            return 1
        j = sess.get(Job, a.job_id)
        print("=" * 76)
        print("Заявка %d | статус %s | скор %.0f" % (a.id, a.status, a.score))
        print("Вакансия : %s" % (j.title or "(без названия)"))
        print("Тег      : %s   Источник: %s" % (j.tag, j.source))
        print("Контакт  : %s (%s)" % (j.contact_handle or j.contact_url, j.contact_kind))
        print("Резюме   : %s (%s)" % (a.cv_path, a.cv_lang))
        if a.promoted_terms_json:
            print("Термины работодателя, поднятые в резюме: %s"
                  % ", ".join(a.promoted_terms_json))
        print("Похожесть письма на недавние: %.2f (порог 0.75)" % a.message_similarity_max)
        print("-" * 76)
        print(a.message_body)
        print("=" * 76)
    return 0


def cmd_approve(ids=None, take_all=False) -> int:
    with session_scope() as sess:
        apps = _ready(sess)
        if ids:
            apps = [a for a in apps if a.id in ids]
        elif not take_all:
            print("Укажи --all или --ids")
            return 1
        if not apps:
            print("Нечего одобрять.")
            return 0
        batch = Batch(planned_count=len(apps), approved_at=utcnow(),
                      approved_count=len(apps))
        sess.add(batch)
        sess.flush()
        n = 0
        for a in apps:
            try:
                a.transition(Status.APPROVED)
                a.approved_at = utcnow()
                a.batch_id = batch.id
                n += 1
            except Exception as e:
                print("  ! заявка %d: %s" % (a.id, e))
        print("Одобрено: %d (партия #%d)" % (n, batch.id))
        print("\nОтправка:")
        print("  проверка : python -m jobhunter.outreach.sender --dry-run")
        print("  боевая   : python -m jobhunter.outreach.sender")
    return 0


def cmd_reject(app_id: int) -> int:
    with session_scope() as sess:
        a = sess.get(Application, app_id)
        if not a:
            print("Нет заявки %d" % app_id)
            return 1
        a.transition(Status.WITHDRAWN, reason="снято вручную")
        print("Заявка %d снята с отправки." % app_id)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Одобрение партии откликов")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--show", type=int)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--ids", type=str)
    ap.add_argument("--reject", type=int)
    args = ap.parse_args()

    if args.show:
        return cmd_show(args.show)
    if args.reject:
        return cmd_reject(args.reject)
    if args.all or args.ids:
        ids = {int(x) for x in args.ids.split(",")} if args.ids else None
        return cmd_approve(ids=ids, take_all=args.all)
    return cmd_list()


if __name__ == "__main__":
    sys.exit(main())
