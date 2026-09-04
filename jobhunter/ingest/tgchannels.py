"""Источник: публичные Telegram-каналы с вакансиями.

Читаем публичное веб-превью https://t.me/s/<channel> — обычный
server-rendered HTML, без логина и без Bot API. robots.txt на t.me
отсутствует (404), доступ анонимный.

Ценность: в отличие от job-бордов, в теле поста часто прямо указан
@handle рекрутёра — то есть готовый адрес для отклика.
"""
from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
from sqlalchemy import select

from ..config import get_settings
from ..db import session_scope
from ..models import ContactKind, TelegramChannelStat, utcnow
from .base import RawJob, extract_email, extract_telegram_handle, parse_ts

# Каналы: (username, ярлык, ожидаемая доля прямых контактов)
#
# Список проверен 2026-08-25: каждый канал открыт через t.me/s/<name>, у всех
# есть посты за последнюю неделю и вакансии по профилю. Порядок — по темам:
# python, backend, devops, data, общие IT, релокация.
#
# «Ожидаемая доля прямых контактов» — что чаще стоит в посте: @handle
# рекрутёра (высокая) или ссылка на форму агрегатора (низкая). Каналы с
# низкой долей всё равно полезны: они дают объём, а вакансии без контакта
# уходят в список для ручного отклика.
CHANNELS = [
    ("forpython", "python, 7 295 подп.", "низкая — контакты редки"),
    ("job_python", "python, 22 650 подп.", "очень высокая"),
    ("pythonrabota", "python, 12 830 подп.", "очень высокая"),
    ("backend_vacancy", "backend, 1 089 подп.", "очень высокая"),
    ("forgoandrust", "backend, 4 187 подп.", "низкая — контакты редки"),
    ("java_c_net_golang_jobs", "backend, 5 947 подп.", "очень высокая"),
    ("progjob", "backend, 15 393 подп.", "низкая — контакты редки"),
    ("runello_rus_backend", "backend, 13 500 подп.", "низкая — контакты редки"),
    ("devops_jobs_feed", "devops, 21 579 подп.", "очень высокая"),
    ("fordevops", "devops, 4 711 подп.", "низкая — контакты редки"),
    ("sysadmin_jobs", "devops, 4 290 подп.", "очень высокая"),
    ("ai_rabota", "data, 2 848 подп.", "очень высокая"),
    ("analyst_geeklink", "data, 225 подп.", "очень высокая"),
    ("data_engineer_jobs", "data, 3 521 подп.", "очень высокая"),
    ("datajob", "data, 14 551 подп.", "низкая — контакты редки"),
    ("datasciencejobs", "data, 21 741 подп.", "очень высокая"),
    ("de_rabota", "data, 4 810 подп.", "очень высокая"),
    ("foranalysts", "data, 36 087 подп.", "низкая — контакты редки"),
    ("ml_jobs_kz", "data, 11 000 подп.", "низкая — контакты редки"),
    ("rabota_v_ii", "data, 1 654 подп.", "очень высокая"),
    ("Remoteit", "general, 50 180 подп.", "низкая — контакты редки"),
    ("choicy_work", "general, 26 339 подп.", "низкая — контакты редки"),
    ("click_jobs", "general, 9 466 947 подп.", "очень высокая"),
    ("devitjobs", "general, 12 946 подп.", "низкая — контакты редки"),
    ("devkz_jobs", "general, 24 546 подп.", "очень высокая"),
    ("digitaljobkz", "general, 4 494 подп.", "очень высокая"),
    ("geekjobs", "general, 49 720 подп.", "очень высокая"),
    ("getitrussia", "general, 20 782 подп.", "очень высокая"),
    ("it_kz_jobs", "general, 208 подп.", "очень высокая"),
    ("it_remote", "general, 1 172 подп.", "очень высокая"),
    ("it_vakansii_jobs", "general, 93 699 подп.", "очень высокая"),
    ("itjobs_am", "general, 8 подп.", "низкая — контакты редки"),
    ("itjobs_ge", "general, 5 подп.", "низкая — контакты редки"),
    ("jc_it", "general, 15 657 подп.", "низкая — контакты редки"),
    ("jobfortm", "general, 14 784 подп.", "низкая — контакты редки"),
    ("over100", "general, 4 415 подп.", "очень высокая"),
    ("proglib_jobs", "general, 9 528 подп.", "очень высокая"),
    ("refer_me_it", "general, 30 499 подп.", "очень высокая"),
    ("remotejobs", "general, 6 619 подп.", "низкая — контакты редки"),
    ("remotejobsit", "general, 1 612 подп.", "очень высокая"),
    ("zizu_IT_RU", "general, 4 656 подп.", "очень высокая"),
    ("cyithr", "relocation, 19 329 подп.", "очень высокая"),
    ("cyprusvacancy", "relocation, 33 подп.", "очень высокая"),
    ("evacuatejobs", "relocation, 134 554 подп.", "низкая — контакты редки"),
    ("jobsincyprus", "relocation, 4 245 подп.", "очень высокая"),
    ("opento_cyprus", "relocation, 5 980 подп.", "очень высокая"),
    ("opento_relocate", "relocation, 50 880 подп.", "очень высокая"),
    ("rabota_portugal", "relocation, 356 подп.", "очень высокая"),
    ("relocateme", "relocation, 34 082 подп.", "низкая — контакты редки"),
    ("relocats", "relocation, 32 548 подп.", "очень высокая"),
    ("rusukrjobs", "relocation, 25 266 подп.", "низкая — контакты редки"),
    ("theyseeku_it", "relocation, 9 419 подп.", "низкая — контакты редки"),
    ("workitkz", "relocation, 34 745 подп.", "очень высокая"),
    ("worktugal", "relocation, 110 подп.", "низкая — контакты редки"),
    ("young_relocate", "relocation, 50 880 подп.", "очень высокая"),
]

# Хендлы каналов и их кросс-промо семейств. Встречаются почти в каждом посте
# («подпишись на @...»), но контактом работодателя не являются. Частотный
# порог здесь не работает: семейство из 5 каналов даёт по 10-15% каждый и
# ни один не превышает порог.
CROSS_PROMO = [
    "sergekoloskov", "freshproductgo", "productjobgo", "productconsult",
    "productuniversity", "ithumorchannel",
    "forproducts", "fordesigner", "forchiefs", "forallmedia", "forcpp",
    "product_jobs", "it_jobs_remote", "remocate", "vakansii_it", "newhr",
    "geekjobs", "jobprbot", "workurs_bot",
    # Приёмные каналов-агрегаторов: подписывают ЧУЖИЕ вакансии своим хендлом
    # («Публикатор: mikhail» + @devops_jobs). Отклик туда уходит в никуда —
    # это адрес для подачи вакансий, а не рекрутёр.
    "devops_jobs", "de_rabota", "data_engineer_jobs", "datasciencejobs",
    "getitrussia", "workitkz", "jc_it", "devs_it", "myresume_ru",
]

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_MSG_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(?P<body>.*?)</div>',
    re.S)
_ID_RE = re.compile(r'data-post="(?P<post>[^"]+)"')
_TIME_RE = re.compile(r'<time datetime="(?P<dt>[^"]+)"')
# зарплата: 250 000 ₽, $4000, 4 000 — 8 000 $, 250к
_SALARY_RE = re.compile(
    r"(?:от\s*)?\d[\d\s.,]{2,}\s*(?:—|-|–|до)?\s*\d*[\d\s.,]*\s*"
    r"(?:₽|руб|rub|\$|usd|€|eur|тыс|к\b|k\b)", re.I)


def _strip_html(fragment: str) -> str:
    text = _BR_RE.sub("\n", fragment or "")
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# Служебные префиксы постов — в названии вакансии они мусор.
_TITLE_PREFIX = re.compile(
    r"^\s*(?:ваканси[яи]|vacancy|job|позици[яи]|position|ищем|we\s+are\s+looking\s+for)"
    r"\s*[:\-–—]?\s*", re.I)


# Служебные шапки агрегаторов: «Публикатор: Даниэлла», «Company Name: 31C»,
# «Всем привет! 🎰 Наша продуктовая iGaming компания...». Как заголовок
# вакансии это мусор, а он потом уходит рекрутёру в письме:
# «По вакансии "Публикатор: Даниэлла Котовская🖤"» — читается как сломанный бот.
_META_LINE = re.compile(
    r"^\s*(?:публикатор|автор|источник|компания|company\s*name|company|"
    r"локация|location|формат(?:\s+работы)?|график|зарплата|salary|вилка|"
    r"грейд|уровень|занятость|title|position|обсуждение|подробнее|контакт|"
    r"опыт|стек|условия|тип\s+занятости|режим)\s*[:：]", re.I)
_GREETING = re.compile(
    r"^\s*(?:всем\s+)?(?:привет|здравствуйте|добрый\s+день|hi|hello|hey)\b", re.I)


# Ведущие эмодзи и значки: без их срезания «📍 Формат работы: офис» не
# опознаётся как служебная строка — регулярка привязана к началу.
_LEAD_JUNK = re.compile(r"^[\s*#—\-•·\t -㌀\U0001F000-\U0001FAFF]+")


def _first_line(text: str) -> str:
    for raw in (text or "").splitlines():
        s = _LEAD_JUNK.sub("", raw).strip()
        s = _TITLE_PREFIX.sub("", s)
        s = _LEAD_JUNK.sub("", s).strip()
        if len(s) < 6:
            continue
        if _META_LINE.match(s) or _GREETING.match(s):
            continue
        return s[:180]
    return ""


def _guess_tag(text: str) -> str:
    """Грубая категория по содержимому — для скоринга и статистики."""
    t = (text or "").lower()
    pairs = [
        ("Product Manager", r"product\s*manager|продакт|продуктовый менеджер|\bpdm\b"),
        ("Project Manager", r"project\s*manager|проджект|руководитель проект"),
        ("Product Analyst", r"product\s*analyst|продуктовый аналитик"),
        ("DS / ML", r"\bml\b|machine learning|data scien|нейросет|computer vision"),
        ("DevOps", r"devops|sre\b|kubernetes|инфраструктур"),
        ("Python", r"\bpython\b|django|fastapi"),
        ("QA Auto", r"qa\s*auto|автотест|автоматизац.{0,12}тест"),
        ("Backend", r"backend|бэкенд|бекенд"),
        ("Frontend", r"frontend|фронтенд|react|vue\b"),
        ("iOS", r"\bios\b|swift"),
        ("Android", r"android|kotlin"),
        ("System Analyst", r"систем.{0,8}аналитик|system analyst"),
    ]
    for name, pat in pairs:
        if re.search(pat, t):
            return name
    return "IT"


# Пост-резюме СОИСКАТЕЛЯ, а не вакансия. Критично: в таких постах тоже есть
# контакт, и наивный парсер радостно его берёт — после чего система пишет
# другому кандидату, приняв его за рекрутёра. Есть каналы, состоящие из таких
# постов на 100%.
_RESUME_POST = re.compile(
    r"(#резюме|#resume|#cv\b|#ищу|#открыт_к_предложениям|"
    r"ищу\s+(?:работу|проект|команду|вакансию)|в\s+поиске\s+работы|"
    r"рассматриваю\s+предложения|открыт\s+к\s+предложениям|"
    r"open\s+to\s+work|looking\s+for\s+(?:a\s+)?(?:job|work|new\s+role)|"
    r"обо\s+мне[:\s]|мой\s+опыт[:\s]|мои\s+навыки[:\s]|"
    r"мой\s+стек[:\s]|формат\s+работы,\s+который\s+ищу)", re.I)

# Пост-вакансия: работодатель ищет человека.
_VACANCY_MARK = re.compile(
    r"(ваканси|мы\s+ищем|ищем\s+(?:в\s+команду|разработ|специал|человек)|"
    r"требовани|обязанност|что\s+нужно\s+делать|задачи[:\s]|"
    r"условия[:\s]|мы\s+предлагаем|"
    r"hiring|we\s+are\s+looking\s+for|responsibilities|requirements|"
    r"job\s+description|position[:\s])", re.I)

# Короткие объявления тоже бывают настоящими («Вакансия Python developer,
# @hr_company»). Сохраняем строгий признак вакансии и требуем роль, контакт
# или зарплату, чтобы не превратить рекламные заголовки в поток мусора.
_EXPLICIT_VACANCY = re.compile(
    r"(ваканси[яи]?|hiring|job\s+opening|position\s*:|"
    r"we\s+are\s+looking\s+for)", re.I)
_ROLE_OR_CONTACT = re.compile(
    r"(python|backend|frontend|devops|data|аналит|разработ|developer|engineer|"
    r"manager|менеджер|тестиров|qa\b|дизайн|design|@[_a-z][\w]{3,31}\b|"
    r"[\w.+-]+@[\w.-]+\.[a-z]{2,})", re.I)
_MIN_POST_LENGTH = 80


def _is_vacancy(text: str) -> bool:
    """Отсекаем рекламу каналов, дайджесты, болтовню и резюме соискателей."""
    t = (text or "").lower()
    if len(t) < _MIN_POST_LENGTH:
        if not (_EXPLICIT_VACANCY.search(t)
                and (_ROLE_OR_CONTACT.search(t) or _SALARY_RE.search(t))):
            return False
    # резюме соискателя: контакт есть, но писать туда нельзя
    if _RESUME_POST.search(t) and not _VACANCY_MARK.search(t):
        return False
    if re.search(r"(подпис|реклам|розыгрыш|дайджест|подборка каналов|"
                 r"курс|вебинар|марафон|бесплатный интенсив)", t) and \
       not re.search(r"(вакансия|мы ищем|требования|обязанности|hiring|we are looking)", t):
        return False
    return bool(re.search(
        r"(ваканси|мы ищем|ищем|требовани|обязанност|зарплат|"
        r"вилка|hiring|we are looking|position|responsibilities|apply)", t))


# Расширенный список живёт в channels_verified.json рядом с проектом: его
# готовит verify_channels.py, который по каждому кандидату проверяет, что канал
# публичный, постил на этой неделе и в постах есть контакты. Держать полсотни
# юзернеймов в коде смысла нет — каналы умирают и переименовываются, а файл
# перегенерируется одной командой.
def _verified_channels() -> list:
    path = Path(__file__).resolve().parents[2] / "channels_verified.json"
    if not path.is_file():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [r["username"] for r in rows
            if isinstance(r, dict) and r.get("username") and r.get("ok", True)]


def _discovered() -> list:
    """Каналы, принятые автопоиском (jobhunter.ingest.discover --apply)."""
    try:
        from .discover import enabled_channels
        return enabled_channels()
    except Exception:
        return []                       # автопоиск не обязан быть исправен


class TelegramChannelSource:
    name = "tg_channel"

    def __init__(self, channels=None, throttle: float = 2.0):
        base = [c[0] for c in CHANNELS] + _verified_channels() + _discovered()
        # Дедуп без учёта регистра: username в Telegram регистронезависим,
        # и «Remoteit» из одного каталога с «remoteit» из другого — один и
        # тот же канал, который иначе скрейпился бы дважды за прогон.
        self.channels = channels or list(dict.fromkeys(c.lower() for c in base))
        self.throttle = throttle
        self.scan_started_at = utcnow()
        self.scan_stats = {
            channel: {
                "username": channel, "started_at": self.scan_started_at,
                "status": "pending", "error": "", "pages": 0,
                "posts": 0, "vacancies": 0, "contacts": 0,
                "requests": 0,
            }
            for channel in self.channels
        }
        s = get_settings()
        self.http = httpx.Client(
            headers={"User-Agent": s.careered_ua,
                     "Accept": "text/html,application/xhtml+xml"},
            timeout=30.0, trust_env=False, follow_redirects=True)

    def close(self) -> None:
        """Закрыть HTTP-клиент даже при частичном падении прогона."""
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def _fetch(self, channel: str, before: str | None = None) -> str:
        url = "https://t.me/s/%s" % channel
        if before:
            url += "?before=%s" % before
        stat = self.scan_stats[channel]
        stat["requests"] += 1
        last_error = ""
        time.sleep(self.throttle)
        for attempt in range(3):
            try:
                r = self.http.get(url)
                if r.status_code == 200:
                    return r.text
                last_error = "HTTP %d" % r.status_code
            except Exception as exc:
                last_error = type(exc).__name__
            time.sleep(2.0 * (attempt + 1))
        stat["error"] = last_error or "неизвестная ошибка запроса"
        return ""

    def iter_jobs(self, limit: int | None = None, pages_per_channel: int = 3
                  ) -> Iterator[RawJob]:
        produced = 0
        for channel in self.channels:
            stat = self.scan_stats[channel]
            stat["status"] = "running"
            before = None
            for _page in range(pages_per_channel):
                page_html = self._fetch(channel, before)
                if not page_html:
                    break
                posts = list(_MSG_RE.finditer(page_html))
                stat["pages"] += 1
                stat["posts"] += len(posts)
                ids = list(_ID_RE.finditer(page_html))
                times = list(_TIME_RE.finditer(page_html))
                if not posts:
                    break
                for idx, m in enumerate(posts):
                    text = _strip_html(m.group("body"))
                    if not _is_vacancy(text):
                        continue
                    stat["vacancies"] += 1
                    post_id = (ids[idx].group("post") if idx < len(ids)
                               else "%s/%d" % (channel, idx))
                    # Канал и его кросс-промо семейство — не контакт вакансии.
                    # Денилист — не только текущий канал, но и ВСЕ, что мы
                    # читаем. Канал подписывает свои посты собственным
                    # @хендлом («Публикатор: mikhail» + @devops_jobs), и без
                    # этого система принимает канал за рекрутёра и пишет ему
                    # отклик. С расширением списка до 59 каналов ручной
                    # CROSS_PROMO перестал покрывать случай.
                    handle = extract_telegram_handle(
                        text, denylist=[channel] + list(self.channels) + CROSS_PROMO)
                    email = extract_email(text)
                    kind = (ContactKind.USER_HANDLE.value if handle
                            else ContactKind.EMAIL.value if email
                            else ContactKind.UNKNOWN.value)
                    if handle or email:
                        stat["contacts"] += 1
                    sal = _SALARY_RE.search(text)
                    # Дата поста лежит в <time datetime="..."> рядом с телом.
                    # Без неё все посты канала выглядели одинаково свежими, и
                    # отклики уходили в вакансии полугодовой давности —
                    # четыре ответа из семи были «вакансия закрыта».
                    posted = (parse_ts(times[idx].group("dt"))
                              if idx < len(times) else 0)
                    yield RawJob(
                        source="tg:%s" % channel,
                        external_uuid="tg:%s" % post_id,
                        title=_first_line(text),
                        company="",
                        tag=_guess_tag(text),
                        content=text,
                        mode="full",
                        posted_at=posted,
                        salary_raw=sal.group(0).strip() if sal else "",
                        contact_kind=kind,
                        contact_handle=handle,
                        contact_url="https://t.me/%s" % handle if handle else "",
                        contact_email=email,
                        all_links=[{"key": "telegram", "value": "https://t.me/%s" % handle}]
                                  if handle else [],
                        raw={"channel": channel, "post": post_id},
                    )
                    produced += 1
                    if limit and produced >= limit:
                        return
                # пагинация назад: id самого старого поста на странице
                if ids:
                    oldest = ids[0].group("post").split("/")[-1]
                    if before == oldest:
                        break
                    before = oldest
                else:
                    break
            if stat["error"]:
                stat["status"] = "partial" if stat["pages"] else "error"
            elif stat["pages"] and stat["posts"]:
                stat["status"] = "ok"
            else:
                stat["status"] = "empty"


def persist_telegram_scan_stats(scan_stats: dict) -> None:
    """Сохранить результат чтения каналов без содержимого постов."""
    if not scan_stats:
        return
    finished = utcnow()
    with session_scope() as sess:
        for username, data in scan_stats.items():
            row = sess.scalar(select(TelegramChannelStat).where(
                TelegramChannelStat.username == username))
            if not row:
                row = TelegramChannelStat(username=username)
                sess.add(row)
            status = data.get("status", "pending")
            if status == "pending":
                status = "not_run"
            row.last_started_at = data.get("started_at")
            row.last_finished_at = finished
            row.last_status = status
            row.last_error = (data.get("error") or "")[:240]
            row.last_pages = int(data.get("pages", 0) or 0)
            row.last_posts = int(data.get("posts", 0) or 0)
            row.last_vacancies = int(data.get("vacancies", 0) or 0)
            row.last_contacts = int(data.get("contacts", 0) or 0)
            attempted = status not in ("pending", "not_run")
            if attempted:
                row.total_scans = (row.total_scans or 0) + 1
            if status in ("ok", "empty"):
                row.last_success_at = finished
                row.consecutive_failures = 0
            elif attempted:
                row.consecutive_failures = (row.consecutive_failures or 0) + 1


def telegram_scan_summary(scan_stats: dict) -> dict:
    """Сводка прогона для лога и API, без содержимого вакансий."""
    attempted = [data for data in scan_stats.values()
                 if data.get("status") not in ("pending", "not_run")]
    errors = [data for data in attempted
              if data.get("status") in ("error", "partial")]
    return {
        "scan_channels": len(attempted),
        "scan_pages": sum(int(d.get("pages", 0) or 0) for d in attempted),
        "scan_posts": sum(int(d.get("posts", 0) or 0) for d in attempted),
        "scan_vacancies": sum(int(d.get("vacancies", 0) or 0) for d in attempted),
        "scan_contacts": sum(int(d.get("contacts", 0) or 0) for d in attempted),
        "scan_errors": len(errors),
        "scan_error_channels": [d["username"] for d in errors],
    }


def ingest_telegram(pages_per_channel: int | None = None,
                    throttle: float | None = None) -> dict:
    """Один безопасный проход Telegram-ленты с сохранением диагностики."""
    s = get_settings()
    pages = pages_per_channel or max(1, s.telegram_ingest_pages)
    delay = s.telegram_ingest_throttle if throttle is None else throttle
    src = TelegramChannelSource(throttle=max(0.0, delay))
    try:
        from .base import save_jobs
        stats = save_jobs(src.iter_jobs(pages_per_channel=pages), verbose=False)
        stats.update(telegram_scan_summary(src.scan_stats))
        return stats
    finally:
        # Даже ошибка БД после части генератора не должна уничтожать след
        # того, какие каналы уже были прочитаны.
        try:
            persist_telegram_scan_stats(src.scan_stats)
        finally:
            src.close()
