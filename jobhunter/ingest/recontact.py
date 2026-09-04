"""Второй проход по контактам: достать то, что уже собрано, но потеряно.

Вакансия помечается «без прямого контакта» в момент сбора. Но контакт часто
лежит прямо в тексте поста, а извлекатель до него не добрался: у Telegram
парсер срезал HTML раньше, чем прочитал ссылки, у HN контакт стоит в теле
письма, у части бордов — в описании.

Итог: 293 заявки из четырёх тысяч «без контакта» на самом деле адресуемы.
Это впятеро больше всего, что отправлено за историю проекта, и не требует ни
одного нового запроса наружу — данные уже в базе.

Извлекатели берутся те же, что при сборе (ingest/base): они знают про
денилисты каналов, кросс-промо семейства, хендлы внутри почтовых адресов и
хвосты «по рекламе @x». Писать здесь свои регулярки значило бы завести
вторую, менее умную копию тех же правил.

    python -m jobhunter.ingest.recontact --dry     # посмотреть, что нашлось
    python -m jobhunter.ingest.recontact           # вернуть в работу
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter

from sqlalchemy import select

from ..db import session_scope
from ..models import Application, ContactKind, Employer, Job, Status, utcnow

# Ящики, куда отклик слать бессмысленно: это не наём. Список тот же, что у
# почтового отправителя, — держать два разных значило бы однажды написать в
# бухгалтерию из одного места и не написать из другого.
from ..outreach.mailer import _mailbox_ok
from .base import extract_email, extract_telegram_handle
from .tgchannels import CROSS_PROMO


def _channel_deny(job: Job) -> set:
    """Хендлы, которые не являются контактом работодателя.

    Канал-источник подписывает каждый пост своим именем, а кросс-промо
    семейство — соседними. Принять их за рекрутёра значит написать
    администратору канала вместо компании.
    """
    deny = {d.lower() for d in CROSS_PROMO}
    src = (job.source or "")
    if ":" in src:
        deny.add(src.split(":", 1)[1].lower())
    return deny


# Источники, где описание пишет человек и контакт в нём настоящий: посты
# каналов, объявления careered, треды HN.
#
# ATS и борды сюда НЕ входят намеренно. Их описания — маркетинговый текст,
# где «@Company» это упоминание компании, а не хендл, а «learn more@host» —
# обрывок ссылки. Проверка на живых данных дала ровно это: @Ramp вместо
# рекрутёра и more@grafana.com вместо адреса найма. Писать по такому
# контакту — та же ошибка, что писать в бухгалтерию, только незаметнее.
# То же решение уже записано в ingest/ats.py, где email захардкожен пустым.
TRUSTED_SOURCES = ("tg:", "careered", "hn")


def _trusted(job: Job) -> bool:
    src = (job.source or "").lower()
    return any(src.startswith(p) for p in TRUSTED_SOURCES)


# Слова, рядом с которыми в объявлении стоит настоящий контакт: «резюме
# присылайте», «писать сюда», «по вопросам». Проверка по контексту нужна,
# потому что голая регулярка вытаскивает обрывки текста: на живых данных
# она дала more@jnj.com из «learn more», us@www.abbott из адреса сайта и
# @Ramp из названия компании.
CONTACT_HINT = re.compile(
    r"(?:резюме|отклик\w*|откликн\w+|присыла\w+|пиш\w+|писать|связ\w+|"
    r"контакт\w*|вопрос\w*|cv\b|apply|send|contact|reach\s+out|dm\b|"
    r"telegram|телеграм|тг\b|почт\w*|email|e-mail|hr\b|рекрут\w*|"
    # Значок конверта или указующей руки перед хендлом — такой же явный
    # признак контакта, как слово. В постах их ставят вместо слов.
    r"[📩📨📧✉👉📝])",
    re.I)

# Признаки, что рядом с хендлом не контакт, а реклама канала. Проверяются
# ПОСЛЕ положительных: «Больше вакансий в Python: @forpython» иначе прошло бы
# по слову «вакансий», и отклик уехал бы администратору чужого канала.
PROMO_HINT = re.compile(
    r"(?:больше\s+вакансий|ещё\s+вакансии|подпис\w+|наш\s+канал|"
    r"другие\s+вакансии|все\s+вакансии|канал\w*\s*:|more\s+jobs|"
    r"subscribe|join\s+us|наши\s+каналы)", re.I)

# Ящик, по имени которого видно, что он для найма.
HIRING_MAILBOX = re.compile(
    r"^(hr|job|jobs|career|careers|recruit\w*|vacanc\w*|resume|cv|talent|"
    r"people|hiring|work|rabota|team)([._\-+]|\d|$)", re.I)

# Домен, у которого последняя часть похожа на настоящую зону, а сам он не
# начинается с www: «us@www.abbott» — это разобранный по кускам адрес сайта.
VALID_DOMAIN = re.compile(r"^(?!www\.)[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}$")


def _near_hint(text: str, needle: str, window: int = 120) -> bool:
    """Есть ли рядом с найденным контактом слово, объясняющее, зачем он."""
    # Проверяем ВСЕ вхождения, а не первое: хендл рекрутёра часто встречается
    # дважды — сначала в шапке поста, потом внизу с «пишите сюда». По первому
    # вхождению признака нет, и настоящий контакт терялся.
    start = 0
    found_any = False
    while True:
        pos = text.find(needle, start)
        if pos < 0:
            break
        start = pos + 1
        found_any = True
        ctx = text[max(0, pos - window):pos + len(needle)]
        if PROMO_HINT.search(ctx):
            continue
        if CONTACT_HINT.search(ctx):
            return True
    return False if found_any else False


def find_contact(job: Job) -> tuple:
    """(вид, значение) или ("", "") — контакта в тексте нет."""
    if not _trusted(job):
        return "", ""
    text = job.description_raw or ""
    if not text.strip():
        return "", ""

    handle = extract_telegram_handle(text, denylist=_channel_deny(job))
    if handle and _near_hint(text, handle):
        return ContactKind.USER_HANDLE.value, handle

    email = extract_email(text)
    if email and _mailbox_ok(email):
        local, _, domain = email.partition("@")
        # Либо имя ящика само говорит о найме, либо рядом в тексте
        # объясняется, зачем этот адрес. Просто «слово@домен» посреди
        # описания — почти всегда обрывок.
        if VALID_DOMAIN.match(domain) and (HIRING_MAILBOX.match(local)
                                           or _near_hint(text, email)):
            return ContactKind.EMAIL.value, email
    return "", ""


def run(dry: bool = True, limit: int = 0) -> dict:
    """Возвращает заявки с найденным контактом обратно в работу."""
    stats = Counter()
    with session_scope() as sess:
        rows = sess.scalars(
            select(Application)
            .where(Application.status == Status.HANDLE_MISSING.value)
            .order_by(Application.score.desc())).all()
        ids = [a.id for a in rows]
    if limit:
        ids = ids[:limit]

    for app_id in ids:
        with session_scope() as sess:
            app = sess.get(Application, app_id)
            job = sess.get(Job, app.job_id)
            if not job:
                continue
            kind, value = find_contact(job)
            stats["checked"] += 1
            if not kind:
                continue
            stats[kind] += 1
            if dry:
                continue

            if kind == ContactKind.USER_HANDLE.value:
                job.contact_kind = kind
                job.contact_handle = value
                job.contact_handle_norm = value.lower()
                job.contact_url = "https://t.me/%s" % value
                key = value.lower()
            else:
                job.contact_kind = kind
                job.contact_url = value
                key = value.lower()

            # Работодатель по тому же ключу, что и при обычном сборе, иначе
            # защиты «не писать дважды» и «не холодить ответившего» его не
            # увидят.
            emp = sess.scalars(
                select(Employer).where(Employer.handle_norm == key)).first()
            if emp is None:
                emp = Employer(handle_norm=key, handle_kind=kind,
                               display_name=job.company_name or "")
                sess.add(emp)
                sess.flush()
            app.employer_id = emp.id
            # DISCOVERED — вход в обычный конвейер: скоринг, резюме, письмо,
            # гейт. Отдельного пути для «восстановленных» нет и не нужно.
            app.advance(Status.DISCOVERED, reason="контакт найден в тексте")
            app.updated_at = utcnow()
            stats["restored"] += 1
    return dict(stats)


def main() -> int:
    ap = argparse.ArgumentParser(description="Поиск контактов в собранных текстах")
    ap.add_argument("--dry", action="store_true", help="только показать")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    stats = run(dry=args.dry, limit=args.limit)
    print("проверено: %d" % stats.get("checked", 0))
    print("нашлось telegram: %d" % stats.get(ContactKind.USER_HANDLE.value, 0))
    print("нашлось email: %d" % stats.get(ContactKind.EMAIL.value, 0))
    if args.dry:
        print("\n(сухой прогон, база не менялась)")
    else:
        print("вернулось в работу: %d" % stats.get("restored", 0))
        print("\nДальше: python -m jobhunter.pipeline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
