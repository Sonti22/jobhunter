"""Напоминания по не ответившим — с жёсткими лимитами.

Расписание +72ч / +7д / стоп выведено из данных, а не из вкуса: по замерам
LinkedIn 65% ответов приходят в первые 24 часа и 90% за неделю. После второго
касания ждать нечего, а третье и последующие — прямой путь к жалобе на спам.

    python -m jobhunter.outreach.followup --list
    python -m jobhunter.outreach.followup --prepare      # готовит тексты
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..db import session_scope
from ..models import Application, Job, Status
from ..textutil import max_similarity

FIRST_AFTER_HOURS = 72
SECOND_AFTER_DAYS = 7
# Ровно одно напоминание. Ветка второго была сломана насквозь: mailer
# опознаёт follow-up по пустому followup_sent_at, у второго оно уже занято
# первым — и рекрутёру повторно уходило ИСХОДНОЕ холодное письмо с
# перезаводом таймера, вечный цикл дублей (найдено аудитом). Чинить второе
# касание незачем: два «напоминаю о себе» подряд — это спам.
MAX_FOLLOWUPS = 1
STALE_JOB_DAYS = 30          # вакансия старше — не догоняем, там уже никого нет

TEMPLATES_1 = [
    "Здравствуйте! Поднимаю своё сообщение по «{role}» — вакансия ещё актуальна?",
    "Добрый день! Уточню по «{role}»: рассматриваете ещё кандидатов?",
    "Привет! Напомню о себе по «{role}». Если позиция закрыта — так и скажите, не буду отвлекать.",
]
# Английские зеркала: напоминание по EN-треду обязано быть на английском.
TEMPLATES_1_EN = [
    "Hi! Following up on my message about “{role}” — is the position still open?",
    "Hello! Just checking in about “{role}”: are you still reviewing candidates?",
    "Hi! A quick nudge about “{role}”. If the role is closed, just let me know.",
]


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def due(sess) -> list:
    """Заявки, которым пора напомнить."""
    out = []
    rows = sess.scalars(
        select(Application).where(
            Application.status.in_([Status.AWAITING_REPLY.value,
                                    Status.FOLLOWED_UP.value]))).all()
    now = _now()
    for a in rows:
        if a.first_reply_at or a.last_inbound_at:
            continue                                  # ответили — не трогаем
        n = 0
        if a.followup_sent_at:
            n = 1
        if n >= MAX_FOLLOWUPS:
            continue
        if not a.sent_at:
            continue
        job = sess.get(Job, a.job_id)
        # вакансия протухла — догонять бессмысленно
        if job and job.posted_at:
            age = (now - datetime.utcfromtimestamp(job.posted_at)).days
            if age > STALE_JOB_DAYS:
                continue
        if now - a.sent_at >= timedelta(hours=FIRST_AFTER_HOURS):
            out.append((a, job, 1))
    return out


def prepare(dry: bool = True) -> dict:
    """Готовит тексты напоминаний. Отправляет их обычный sender после аппрува."""
    rng = random.Random()
    stats = {"due": 0, "prepared": 0, "closed": 0}
    with session_scope() as sess:
        items = due(sess)
        stats["due"] = len(items)
        corpus = [a.message_body for a in sess.scalars(
            select(Application).where(Application.message_body != "")
            .order_by(Application.id.desc()).limit(30)).all() if a]
        for a, job, n in items:
            role = (job.title or job.tag or "вакансия")[:60]
            tpl = rng.choice(TEMPLATES_1_EN if (a.cv_lang or "ru") == "en"
                             else TEMPLATES_1)
            text = tpl.format(role=role)
            if max_similarity(text, corpus) > 0.85:
                continue
            if dry:
                print("  [%d-е] %s → @%s: %s"
                      % (n, role[:34], job.contact_handle or job.contact_url, text))
            else:
                a.followup_body = text
                a.transition(Status.FOLLOWUP_PENDING_APPROVAL)
            stats["prepared"] += 1

        # добиваем «мертвяк»: две попытки и неделя тишины — закрываем
        for a in sess.scalars(select(Application).where(
                Application.status == Status.FOLLOWED_UP.value)).all():
            if a.followup_sent_at and not a.first_reply_at and \
                    _now() - a.followup_sent_at >= timedelta(days=SECOND_AFTER_DAYS):
                if not dry:
                    a.transition(Status.NO_REPLY_CLOSED, reason="нет ответа после 2 касаний")
                stats["closed"] += 1
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Напоминания по не ответившим")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--prepare", action="store_true")
    args = ap.parse_args()
    stats = prepare(dry=not args.prepare)
    print("\nК напоминанию: %d, подготовлено: %d, закрыто без ответа: %d"
          % (stats["due"], stats["prepared"], stats["closed"]))
    if not args.prepare and stats["due"]:
        print("Записать в очередь: python -m jobhunter.outreach.followup --prepare")
    return 0


if __name__ == "__main__":
    sys.exit(main())
