"""Календарь интервью (.ics) + INTERVIEWS.md с планом подготовки.

Время всегда хранится в UTC и пишется в .ics как UTC (суффикс Z) — плавающее
локальное время в календаре уезжает. Для человека показываем оба пояса,
пересчитывая через zoneinfo НА ДАТУ слота: фиксированный офсет ошибается на
час при переходе на летнее время.

План подготовки = требования вакансии минус профиль: ровно те темы, по которым
кандидата спросят, а подтверждения в резюме нет.

    python -m jobhunter.schedule.ics
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..config import ROOT, get_settings
from ..db import session_scope
from ..models import Application, Job, Status
from ..profile import get_profile
from ..tailor.gate import _find_terms

OWNER_TZ = "Europe/Moscow"


def _out_dir() -> Path:
    """Каталог видимых результатов.

    Функция, а не константа модуля: константа вычислилась бы один раз при
    импорте от корня проекта и не подхватила бы путь, заданный окружением
    контейнера, — файлы молча оседали бы внутри образа.
    """
    p = Path(get_settings().out_dir)
    if not p.is_absolute():
        p = ROOT / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def ics_path() -> Path:
    return _out_dir() / "interviews.ics"


def md_path() -> Path:
    return _out_dir() / "INTERVIEWS.md"

# Материалы под темы, которых нет в профиле.
PREP_HINTS = {
    "csharp": "C# — синтаксис и экосистема .NET, чтобы читать чужой код: learn.microsoft.com/dotnet/csharp",
    "dotnet": ".NET — устройство runtime, DI, минимальные API: learn.microsoft.com/aspnet/core",
    "github actions": "GitHub Actions — синтаксис workflow, отличия от GitLab CI: docs.github.com/actions",
    "kubernetes": "Kubernetes — Deployment/Service/Ingress, отладка подов",
    "terraform": "Terraform — state, модули, plan/apply",
    "react": "React — хуки, состояние, рендер-цикл (для разговора с фронтендом)",
    "java": "Java/Spring — базовая модель для кросс-стековых обсуждений",
    "golang": "Go — горутины, каналы, стандартная библиотека",
    "amplitude": "Продуктовая аналитика — воронки, когорты, retention",
    "a/b testing": "A/B-тесты — дизайн эксперимента, значимость, подводные камни",
    "okr": "OKR — постановка целей и метрик продукта",
    "jtbd": "JTBD — интервью и формулировка job story",
}

DEFAULT_PREP = [
    "Перечитать своё резюме под эту вакансию (лежит в cv_out) — вопросы будут по нему",
    "Подготовить 2-3 истории по STAR: задача → что сделал → результат",
    "Свой вопрос работодателю: как устроен процесс принятия продуктовых решений",
]


def _fmt_dt_utc(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _both_zones(dt_utc: datetime, other_tz: str | None = None) -> str:
    """«чт 7 авг, 15:00 Москва (UTC+3) = 14:00 Берлин (UTC+2)»."""
    days = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    months = ["", "янв", "фев", "мар", "апр", "мая", "июн",
              "июл", "авг", "сен", "окт", "ноя", "дек"]
    own = dt_utc.astimezone(ZoneInfo(OWNER_TZ))
    off = int((own.utcoffset() or timezone.utc.utcoffset(own)).total_seconds() // 3600)
    s = "%s %d %s, %02d:%02d (UTC%+d)" % (days[own.weekday()], own.day,
                                          months[own.month], own.hour, own.minute, off)
    if other_tz and other_tz != OWNER_TZ:
        try:
            oth = dt_utc.astimezone(ZoneInfo(other_tz))
            ooff = int((oth.utcoffset()).total_seconds() // 3600)
            s += " = %02d:%02d %s (UTC%+d)" % (oth.hour, oth.minute, other_tz, ooff)
        except Exception:
            pass
    return s


def prep_plan(job: Job) -> list:
    """Чего в профиле нет, а вакансия просит — то и учить."""
    p = get_profile()
    jd_terms = _find_terms(job.description_raw or "") | _find_terms(job.tag or "")
    gaps = [t for t in sorted(jd_terms) if t not in p.allowed_terms]
    items = []
    for t in gaps[:6]:
        items.append(PREP_HINTS.get(t, "%s — разобраться на уровне разговора, "
                                        "честно сказать про отсутствие опыта" % t))
    return items + DEFAULT_PREP


def build() -> tuple:
    """Собирает .ics и INTERVIEWS.md. Возвращает (кол-во, путь_ics, путь_md)."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             "PRODID:-//jobhunter//interviews//RU", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "X-WR-CALNAME:Интервью"]
    md = ["# Интервью и подготовка", "",
          "Файл собирается автоматически: `python -m jobhunter.schedule.ics`", ""]
    count = 0

    with session_scope() as sess:
        apps = sess.scalars(
            select(Application)
            .where(Application.status.in_([Status.INTERVIEW_CONFIRMED.value,
                                           Status.INTERVIEW_PROPOSED.value,
                                           Status.INTERVIEW_DONE.value]))
            .order_by(Application.interview_at_utc)).all()
        for a in apps:
            if not a.interview_at_utc:
                continue
            job = sess.get(Job, a.job_id)
            dt = a.interview_at_utc.replace(tzinfo=timezone.utc)
            end = dt.replace(hour=(dt.hour + 1) % 24)
            title = job.title or job.tag or "Интервью"
            contact = ("@" + job.contact_handle) if job.contact_handle else job.contact_url
            prep = prep_plan(job)

            lines += [
                "BEGIN:VEVENT",
                "UID:jobhunter-%d@local" % a.id,
                "DTSTAMP:%s" % _fmt_dt_utc(datetime.now(timezone.utc)),
                "DTSTART:%s" % _fmt_dt_utc(dt),
                "DTEND:%s" % _fmt_dt_utc(end),
                "SUMMARY:Интервью — %s" % title[:70],
                "DESCRIPTION:%s\\n\\nПодготовка:\\n%s"
                % (contact, "\\n".join("- " + x for x in prep)),
                "BEGIN:VALARM", "TRIGGER:-PT1H", "ACTION:DISPLAY",
                "DESCRIPTION:Интервью через час", "END:VALARM",
                "BEGIN:VALARM", "TRIGGER:-P1D", "ACTION:DISPLAY",
                "DESCRIPTION:Интервью завтра", "END:VALARM",
                "END:VEVENT",
            ]
            md += ["## %s" % title, "",
                   "- **Когда:** %s" % _both_zones(dt, a.interview_tz or None),
                   "- **С кем:** %s" % contact,
                   "- **Источник:** %s   **Скор:** %.0f" % (job.source, a.score),
                   "- **Резюме:** `%s`" % (a.cv_path or "—"), "",
                   "**Подготовка:**", ""]
            md += ["- [ ] %s" % x for x in prep]
            md += [""]
            count += 1

    lines.append("END:VCALENDAR")
    ics, mdp = ics_path(), md_path()
    ics.write_text("\r\n".join(lines), encoding="utf-8")
    if count == 0:
        md += ["_Подтверждённых интервью пока нет._", "",
               "Появятся здесь автоматически, когда работодатель подтвердит слот.", ""]
    mdp.write_text("\n".join(md), encoding="utf-8")
    return count, ics, mdp


def main() -> int:
    n, ics, md = build()
    print("Интервью в календаре: %d" % n)
    print("  %s" % ics)
    print("  %s" % md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
