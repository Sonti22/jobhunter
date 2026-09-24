"""Генератор холодного сопроводительного письма в Telegram (§3d, §5 плана).

Правила:
  - первая строка называет роль и источник («По вакансии X (careered.io)»)
  - ≥1 фраза цитирует конкретное требование ЭТОЙ вакансии
  - 4-6 разных скелетов, выбор детерминированный по uuid (стабильно при ретрае)
  - НИКАКИХ ссылок в первом сообщении (GitHub/LinkedIn — во втором, после ответа)
  - 400-600 символов, без эмодзи-вёрстки, не форвард
  - похожесть к последним отправленным < 0.75, body_hash уникален
  - наследует анти-фабрикация гейт (kind=message)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from ..profile import Profile, get_profile
from ..textutil import max_similarity, norm, norm_hash
from .gate import DocModel, GateResult, _find_terms, check

SIMILARITY_MAX = 0.75
# Потолок 400 знаков — не вкусовщина. LinkedIn на выборке в десятки миллионов
# сообщений (май 2021 — апр 2022): короче 400 знаков → +22% ответов,
# длиннее 1200 → −11%. Самый надёжный количественный сигнал во всём аутриче.
# linkedin.com/business/talent/blog/talent-strategy/these-inmails-get-best-response-rates
LEN_MIN, LEN_MAX = 200, 400

# Скелеты различаются структурой, не синонимами приветствия.
SKELETONS = [
    {   # S1 — лид с ролью
        "id": "s1_role_first",
        "tpl": ("По вакансии «{role}» ({source}). {intro} "
                "Зацепило требование: «{req}» — {bridge} {ask}"),
    },
    {   # S2 — лид с релевантным опытом
        "id": "s2_relevance_first",
        "tpl": ("{intro} Пишу по вакансии «{role}» ({source}). "
                "У вас в требованиях «{req}» — {bridge} {ask}"),
    },
    {   # S3 — лид с конкретикой стека
        "id": "s3_stack_first",
        "tpl": ("«{role}» ({source}) — по адресу. {stack_line} "
                "Особенно по части «{req}»: {bridge} {ask}"),
    },
    {   # S4 — короткий деловой
        "id": "s4_concise",
        "tpl": ("Здравствуйте! По вакансии «{role}» ({source}). {intro} "
                "«{req}» — {bridge} {ask}"),
    },
    {   # S5 — два предложения о совпадении
        "id": "s5_fit",
        "tpl": ("Пишу по «{role}» ({source}). {intro} {stack_line} "
                "Про «{req}» — {bridge} {ask}"),
    },
    {   # S6 — вопрос-первым
        "id": "s6_question_first",
        "tpl": ("Вакансия «{role}» ({source}) ещё актуальна? {intro} "
                "В требованиях увидел «{req}» — {bridge} {ask}"),
    },
    {   # S7 — от требования к опыту
        "id": "s7_req_first",
        "tpl": ("У вас в «{role}» ({source}) есть пункт «{req}» — {bridge} {intro} {ask}"),
    },
    {   # S8 — сухой деловой, стек в конце
        "id": "s8_dry",
        "tpl": ("Отклик на «{role}» ({source}). {intro} По пункту «{req}» — {bridge} "
                "{stack_line} {ask}"),
    },
    {   # S9 — короткий, без стека
        "id": "s9_minimal",
        "tpl": ("«{role}» ({source}). {intro} Совпадает с «{req}»: {bridge} {ask}"),
    },
]

BRIDGES = [
    "делал это на реальных проектах, могу показать как.",
    "есть прямой рабочий опыт, покажу на примерах.",
    "закрывал такие задачи в продакшене.",
    "это ровно то, чем занимался последние годы.",
    "с этим работал вплотную, расскажу детали.",
]

REQ_MAX = 70          # цитата требования: длиннее — не влезаем в 400 знаков

# Ограничение владельца, которое должно стоять в КАЖДОМ письме: рассматривается
# только удалённый формат, страна роли не играет. Ставится первой же строкой
# после сути, а не в конце: если формат не совпал, рекрутёр должен понять это
# раньше, чем потратит время на резюме.
#
# Английский C2 — из profile.yaml (languages.en.level = C2), поэтому
# анти-фабрикация гейт пропускает: заявляем ровно то, что подтверждено
# профилем (C2 — решение владельца 24.09, как и в резюме).
FORMAT_LINE = "Формат — только удалённый (страна не важна), английский C2."
# То же условие для англоязычных вакансий. Не перевод-калька, а живая
# формулировка; проверка _states_remote в llm_writer слово «remote» понимает.
FORMAT_LINE_EN = "Remote-only, please — any country works for me. English: C2."


# ── английские пулы ────────────────────────────────────────────────────
# Зеркала четырёх самых удачных русских скелетов, а не все девять: EN-корпус
# начинается с нуля, и четырёх структур хватает, чтобы похожесть писем
# держалась ниже порога. Факты те же: 7 лет, backend → tech lead, стек из
# профиля. Ничего, что не прошло бы анти-фабрикация гейт.
SKELETONS_EN = [
    {
        "id": "e1_role_first",
        "tpl": ("Regarding the “{role}” role ({source}). {intro} "
                "One requirement caught my eye: “{req}” — {bridge} {ask}"),
    },
    {
        "id": "e4_concise",
        "tpl": ("Hi! I'm writing about the “{role}” position ({source}). "
                "{intro} “{req}” — {bridge} {ask}"),
    },
    {
        "id": "e6_question_first",
        "tpl": ("Is the “{role}” position ({source}) still open? {intro} "
                "I noticed “{req}” in the requirements — {bridge} {ask}"),
    },
    {
        "id": "e9_minimal",
        "tpl": ("“{role}” ({source}). {intro} "
                "Matches “{req}”: {bridge} {ask}"),
    },
]

BRIDGES_EN = [
    "I've done exactly this in production and can walk you through it.",
    "that's hands-on experience for me, happy to share details.",
    "this is what I've been doing for the past few years.",
    "I've shipped this in real projects.",
]

INTROS_EN = [
    "7+ years in development, grew from backend engineer to tech lead / architect.",
    "Backend engineer with 7+ years, the last few as tech lead owning architecture.",
    "7 years in backend and product services, from code to owning requirements.",
    "Engineer with 7 years of experience; currently responsible for architecture.",
]
INTROS_MIDDLE_EN = [
    "Backend developer: Python, FastAPI, PostgreSQL, integrations.",
    "I write Python and work with REST APIs, data schemas and integrations.",
    "Backend engineer, working stack Python / PostgreSQL / Docker.",
]
INTROS_SHORT_EN = [
    "7 years in development, currently tech lead / architect.",
    "7 years in backend, lately tech lead and architecture.",
    "Engineer, 7 years; now owning architecture and requirements.",
]
ASKS_EN = [
    "Happy to send my CV and discuss details — when works for you?",
    "If this looks relevant, I'll send my CV and we can hop on a call.",
    "Is the position still open? I can send my CV right away.",
    "I'd love to hear more about the role — when would be a good time to talk?",
]
ASKS_SHORT_EN = [
    "Still open? I'll send my CV.",
    "Happy to discuss — when works for you?",
    "If relevant, let's set up a quick call.",
]


# Шаблонные концовки обещают «пришлю резюме», а по почте резюме уже во
# вложении: письмо с PDF и фразой «I can send my CV right away» читается как
# рассылка, собранная не глядя. Для Telegram обещание честное — файл уходит
# следующим сообщением, поэтому правка только для писем с вложением.
_CV_ATTACHED = {
    "Happy to send my CV and discuss details — when works for you?":
        "My CV is attached — happy to discuss details. When works for you?",
    "If this looks relevant, I'll send my CV and we can hop on a call.":
        "My CV is attached; if this looks relevant, we can hop on a call.",
    "Is the position still open? I can send my CV right away.":
        "Is the position still open? My CV is attached.",
    "Still open? I'll send my CV.": "Still open? My CV is attached.",
    "Актуально? Пришлю резюме.": "Актуально? Резюме во вложении.",
    "Вакансия открыта? Готов прислать резюме.": "Вакансия открыта? Резюме во вложении.",
    "Если интересно — пришлю резюме и созвонимся.": "Резюме во вложении — если интересно, созвонимся.",
    "Готов прислать резюме и обсудить детали — когда удобно?":
        "Резюме во вложении, готов обсудить детали — когда удобно?",
    "Если интересно — пришлю резюме и созвонимся, подскажите удобное время.":
        "Резюме во вложении. Если интересно — созвонимся, подскажите удобное время.",
    "Могу прислать резюме и ответить на вопросы. Актуальна ли вакансия?":
        "Резюме во вложении, отвечу на вопросы. Актуальна ли вакансия?",
    "Подскажите, вакансия ещё открыта? Готов прислать резюме.":
        "Подскажите, вакансия ещё открыта? Резюме во вложении.",
}
_CV_PROMISE = re.compile(
    r"\bI(?:'ll| will| can)\s+send\s+(?:you\s+)?my\s+(?:CV|resume)(?:\s+right\s+away)?|"
    r"(?:готов\s+)?пришл(?:ю|ю\s+вам)\s+(?:своё\s+)?резюме|готов\s+прислать\s+резюме", re.I)


def with_cv_attached(text: str, lang: str = "ru") -> str:
    """Текст письма, к которому резюме приложено: без обещания прислать его."""
    out = text or ""
    for promise, attached in _CV_ATTACHED.items():
        out = out.replace(promise, attached)
    if _CV_PROMISE.search(out):
        out = _CV_PROMISE.sub("my CV is attached" if lang == "en" else "резюме во вложении", out)
    return out


def _clip(text: str, limit: int) -> str:
    """Обрезает по границе слова — «such as Docker &a» выглядит небрежно."""
    t = (text or "").strip()
    if len(t) <= limit:
        return t.rstrip(" ,;:—-")
    cut = t[:limit]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    # Обрезка посреди скобки оставляет её незакрытой: «Знание Linux (у нас используется…».
    # Проверка читаемости бракует такое письмо целиком — так потеряны 22 отклика из 89
    # отказов гейта (проверка 19.09). Незакрытый хвост отбрасываем вместе со скобкой.
    for opener, closer in (("(", ")"), ("«", "»"), ("“", "”"), ("[", "]")):
        if cut.count(opener) > cut.count(closer):
            head = cut[:cut.rfind(opener)].rstrip(" ,;:—-")
            if len(head) >= 12:
                cut = head
    return cut.rstrip(" ,;:—-") + "…"

# Для Middle-вакансий: без счётчика лет и без «Tech Lead» — тот же принцип,
# что и в укороченном резюме. Ничего ложного, просто не выпячиваем то, что
# читается как переквалификация.
INTROS_MIDDLE = [
    "Backend-разработчик: Python, FastAPI, PostgreSQL, интеграции.",
    "Пишу на Python, работаю с REST API, схемами данных и интеграциями.",
    "Backend-инженер, рабочий стек Python / PostgreSQL / Docker.",
    "Разрабатываю бэкенд-сервисы: API, база, очереди, тесты.",
]

INTROS_SHORT = [
    "7 лет в разработке, сейчас Tech Lead / архитектор.",
    "7 лет в бэкенде, последние — Tech Lead и архитектура.",
    "Инженер, 7 лет; сейчас отвечаю за архитектуру и требования.",
    "7 лет в разработке: от кода до владения требованиями.",
    "Backend-инженер, 7 лет, ныне технический лидер команды.",
]
ASKS_SHORT = [
    "Актуально? Пришлю резюме.",
    "Вакансия открыта? Готов прислать резюме.",
    "Если интересно — пришлю резюме и созвонимся.",
    "Расскажу подробнее на созвоне. Когда удобно?",
    "Готов обсудить. Когда вам удобно?",
]

INTROS = [
    "7+ лет в разработке, вырос из backend-инженера в Tech Lead / архитектора.",
    "Backend-инженер с 7+ годами опыта, последние годы — Tech Lead и архитектура.",
    "7 лет в бэкенде и продуктовых сервисах, от кода до владения требованиями.",
    "Семь лет в разработке: путь от инженера до технического лидера в небольшой команде.",
    "Инженер с 7-летним стажем; сейчас отвечаю за архитектуру и требования к продукту.",
    "За 7 лет прошёл путь от backend-разработки до владения техническими решениями продукта.",
    "Работаю в разработке 7 лет, последние два года — единственный архитектор в команде.",
]
ASKS = [
    "Готов прислать резюме и обсудить детали — когда удобно?",
    "Если интересно — пришлю резюме и созвонимся, подскажите удобное время.",
    "Могу прислать резюме и ответить на вопросы. Актуальна ли вакансия?",
    "Подскажите, вакансия ещё открыта? Готов прислать резюме.",
    "Расскажу подробнее на созвоне, если релевантно. Когда вам удобно?",
    "Открыт к разговору — скажите, когда удобно созвониться.",
    "Если профиль подходит — с радостью обсужу задачи подробнее.",
]


@dataclass
class MessageResult:
    text: str
    skeleton_id: str
    body_hash: str
    similarity_max: float
    gate: GateResult
    quoted_requirement: str = ""

    @property
    def ok(self) -> bool:
        return (self.gate.passed and self.similarity_max < SIMILARITY_MAX
                and LEN_MIN <= len(self.text) <= LEN_MAX)


# Ключевые требования вакансии — берём сильные строки из JD.
_REQ_LINE = re.compile(r"[•\-\*]?\s*(.{20,120})")


def _extract_requirement(jd_text: str, matched_terms: list, profile: Profile,
                         title: str = "", lang: str = "ru") -> str:
    """Безопасная цитата требования вакансии.

    Возвращает строку, которую можно честно сопроводить «делал это»:
      - без запрещённых терминов (C#/.NET/...) — иначе это ложное заявление
      - без незнакомых кандидату технологий (unknown)
      - желательно с термином, который кандидат реально знает (matched)
    Если такой строки нет — пустая строка (генератор уйдёт без цитаты).
    """
    matched = {t for t, _, _ in matched_terms}
    candidates = []
    lines = (jd_text or "").splitlines()
    # Строка годится в цитату, только если это действительно требование:
    # есть маркер навыка/обязанности. Без этого в письмо попадает «My name is
    # Marius and I am the CTO», а следом «этим я и занимался» — читается как
    # сломанный бот и хуже, чем отсутствие письма.
    req_marker = re.compile(
        r"(опыт|знани|умени|навык|владени|уверенн|понимани|"
        r"разраб|проектир|架|设|支持|支援|"
        r"experience|knowledge|proficien|familiar|expertise|ability to|"
        r"skills?\b|strong\b|hands-on|background in|"
        r"you (?:will|'ll|have|know)|we (?:need|expect|require)|"
        r"required|requirements?\b|must have|nice to have|"
        r"работа(?:ть|л) с|разбира|отвеча(?:ть|ете)|"
        r"\bpython\b|\bapi\b|\bsql\b|postgres|docker|kubernetes|"
        r"микросервис|архитектур|интеграц|метрик|аналитик)", re.I)
    # Явный мусор: представления, локация, самореклама компании, оргвопросы.
    junk = re.compile(
        r"(my name is|меня зовут|i am the|i'm the|мы\s+—|мы\s+-\s|"
        r"we(?:'re| are) (?:growing|hiring|looking to build|a )|"
        r"open roles?:|our team is|about (?:us|the company)|о компании|"
        r"формат работы|график работы|тип занятости|локац|офис|remote\s*\(|"
        r"зарплат|вилка|оклад|salary|compensation|benefits|"
        r"условия|мы предлагаем|we offer|что мы предлагаем|"
        r"откликнуться|apply|контакт|резюме|cv\b|телеграм|"
        r"^\W*\d+\s*$|подпи[сш]|канал|"
        # Куски сериализованных структур из фидов: в письмо уходило
        # «В требованиях увидел "{'education': 'Высшее образование…"».
        # Ловим по признакам JSON и Python-словаря.
        r"\{\s*'|\{\s*\"|'\s*:\s*'|\"\s*:\s*\"|^\s*[\[\]{}]|"
        r"\bnull\b|\bnone\b|\btrue\b|\bfalse\b|"
        # Требование про ТИП КОМПАНИИ или ДОМЕН, а не про навык. Реальный
        # случай: «опыт работы в продуктовой компании, чей основной продукт —
        # скрапер, парсер или агрегатор данных» → письмо отвечало «работал
        # с этим в прошлых проектах». Технологий из never_claim в строке нет,
        # знакомый термин («данных») есть — гейт и проверка на подтверждение
        # пропускают, а заявление ложное: он в такой компании не работал.
        r"опыт\s+работы\s+в\s+\w*\s*компани|в\s+продуктов\w+\s+компани|"
        r"чей\s+основной\s+продукт|опыт\s+в\s+(?:домене|сфере|отрасли|индустрии)|"
        r"experience\s+(?:working\s+)?(?:at|in)\s+(?:a\s+)?(?:product|startup|"
        r"fintech|gaming|igaming)\s+compan|"
        # Требования к стажу не цитируем: ответ «закрывал такие задачи»
        # к ним не подходит и читается как заявление о нужном стаже.
        r"\d+\s*\+?\s*(?:года?|лет|years?)\s+(?:опыта|стажа|of|in|experience)|"
        r"опыт[^.!?]{0,40}?от\s*\d+|from\s+\d+\+?\s*years?|"
        # «Опыт в Product Management от 4–5 лет», «Опыт Python от 3 лет»
        r"опыт[^.!?]{0,45}?\d+\s*[-–—]?\s*\d*\s*(?:года?|лет|years?)|"
        r"(?:experience|background)[^.!?]{0,35}?\d+\+?\s*years?)", re.I)
    for idx, raw in enumerate(lines):
        s = raw.strip().strip("*").strip("•").strip("-").strip()
        s = re.sub(r"\s+", " ", s)
        # страховка: любая разметка в цитате = сломанный бот в глазах получателя
        if re.search(r"<[a-z/!][^>]*>|&[a-z]{2,6};|&#\d+;", s, re.I):
            continue
        if not (20 <= len(s) <= 130):
            continue
        if s.lower().startswith(("job", "apply", "please", "posting", "о компании",
                                 "about", "вакансия", "http")):
            continue
        if "*****" in s:                          # затёртый careered контент
            continue
        # первая строка HN — «Company | Role | Location | REMOTE» — это шапка,
        # цитировать её как «требование» бессмысленно
        if idx == 0 and s.count("|") >= 2:
            continue
        if s.count("|") >= 3:
            continue
        if junk.search(s) or not req_marker.search(s):
            continue                              # не требование — не цитируем
        # В английское письмо кириллическую строку не цитируем: смешанные
        # телеграм-посты дают EN-тело с русскими вкраплениями, и «Matches
        # "…обязательно"» посреди английского текста выдаёт автомат.
        # Без цитаты у генератора есть безопасная ветка.
        if lang == "en" and re.search(r"[а-яёА-ЯЁ]", s):
            continue
        # Цитата не должна повторять заголовок: «У вас в "X" есть пункт "X"»
        # читается как сломанный бот.
        if title and SequenceMatcher(None, norm(title), norm(s)).ratio() > 0.55:
            continue
        terms = _find_terms(s)
        if terms & profile.forbidden_terms:       # содержит C#/.NET/... — нельзя
            continue
        unknown = terms - profile.allowed_terms - profile.forbidden_terms
        if unknown:                               # содержит незнакомую технологию
            continue
        # Цитируем ТОЛЬКО требование, в котором есть термин, реально знакомый
        # кандидату. Иначе скелет письма подтверждает произвольный пункт:
        # «Зацепило требование: "Опыт вайбкодинга, подтверждённый портфолио" —
        # это ровно то, чем занимался последние годы». Запрещённых технологий
        # там нет, гейт молчит, а заявление ложное. Нет подтверждаемой строки —
        # письмо уходит без цитаты, это нормально: скелеты без цитаты есть.
        if not (terms & matched):
            continue
        candidates.append(_clip(s, REQ_MAX))
    return candidates[0] if candidates else ""


# «Стек» — это технологии. Приоритизация и работа со стейкхолдерами —
# компетенции; в строке «Мой стек: Приоритизация» они выглядят абсурдно.
NON_STACK = {"prioritization", "planning", "stakeholder_mgmt", "risk_mgmt",
             "team_leadership", "requirements", "agile", "code_review",
             "mentoring", "tech_docs", "tdd", "algorithms"}


def _stack_line(profile: Profile, matched_terms: list, lang: str = "ru") -> str:
    names, seen = [], set()
    for t, _lvl, _ in sorted(matched_terms, key=lambda x: -x[2]):
        sk = next((s for s in profile.skills if t in s.terms), None)
        if not sk or sk.id in NON_STACK:
            continue
        if sk.level in ("expert", "working") and sk.canonical.lower() not in seen:
            names.append(sk.canonical)
            seen.add(sk.canonical.lower())
        if len(names) >= 4:
            break
    if len(names) < 2:                     # нечего показать — не выдумываем
        return ""
    if lang == "en":
        # Навыки с русским canonical переводятся тем же словарём, что и в
        # резюме, — иначе в английское письмо утечёт кириллица.
        from .select import EN_SKILL
        names = [EN_SKILL.get(n, n) for n in names]
        return "My stack for your requirements: " + ", ".join(names) + "."
    return "Мой стек по вашим требованиям: " + ", ".join(names) + "."


def _pick(seq, seed):
    return seq[seed % len(seq)]


def source_label(source: str, lang: str = "ru") -> str:
    """Человекочитаемое имя источника для первой строки письма.

    Получатель должен за секунду понять, откуда пришёл отклик — это главное,
    что отличает адресное письмо от спама.
    """
    s = (source or "").lower()
    if s.startswith("tg:"):
        handle = source.split(":", 1)[1]
        return ("your post in @" + handle) if lang == "en"             else ("ваш пост в @" + handle)
    if s.startswith("ats:"):
        return "your careers site" if lang == "en" else "ваш сайт вакансий"
    if s == "careered":
        return "careered.io"
    # Международные борды: адресность («нашёл вас на X») поднимает ответы, а
    # до этой таблицы все они давали пустую строку.
    boards = {"remoteok": "remoteok.com", "wwr": "We Work Remotely",
              "himalayas": "himalayas.app", "jobicy": "jobicy.com",
              "remotive": "remotive.com", "workingnomads": "Working Nomads",
              "arbeitnow": "arbeitnow.com", "euremote": "EU Remote Jobs",
              "cryptojobs": "cryptocurrencyjobs.co", "habr": "Хабр Карьера",
              "fourdayweek": "4dayweek.io", "ergodotisi": "ergodotisi.com",
              "workable": "jobs.workable.com", "muse": "The Muse",
              "trudvsem": "Работа России"}
    if s in boards:
        return boards[s]
    if s == "hn":
        return "HN Who is hiring"
    # Пусто, а не слово «вакансия»: подстановка давала в письме
    # «Вакансия "Архитектор ПО" (вакансия) ещё актуальна?» — читается как
    # сломанный шаблон, а это первая строка, которую видит рекрутёр.
    return ""


def _trim_words(text: str, limit: int) -> str:
    """Обрезка по границе слова: «...в приорите» в письме выглядит как опечатка."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    cut = cut.rstrip(" ,;:-–—(")
    # висящий предлог/союз в конце обрезки читается как обрыв фразы
    tail_junk = {"в", "на", "и", "с", "по", "для", "из", "к", "о", "от",
                 "до", "за", "у", "не", "or", "and", "in", "at", "for"}
    words = cut.split(" ")
    while len(words) > 1 and words[-1].lower().strip(",;:") in tail_junk:
        words.pop()
    return " ".join(words).rstrip(" ,;:-–—(")


def _drop_empty_parens(text: str) -> str:
    """Схлопывает пробелы и убирает «()» от неизвестного источника."""
    t = re.sub(r"\s*\(\s*\)", "", text or "")
    return re.sub(r"\s+", " ", t).strip()


def generate(role: str, jd_text: str, score, profile: Profile | None = None,
             seed_str: str = "", recent_corpus=None,
             source: str = "careered.io", lang: str = "ru",
             template_preferences: dict | None = None) -> MessageResult:
    p = profile or get_profile()
    recent_corpus = recent_corpus or []
    seed = abs(hash(seed_str)) if seed_str else 0
    en = lang == "en"

    req = _extract_requirement(jd_text, score.matched_skills, p, role, lang=lang)
    stack = _stack_line(p, score.matched_skills, lang=lang)
    role_short = _trim_words(role or ("developer" if en else "разработчик"), 70)
    # Пустой источник — не повод писать слово «вакансия» в скобках: получалось
    # «Вакансия "Архитектор ПО" (вакансия) ещё актуальна?». Пустые скобки
    # вычищаются из готового текста ниже.
    source_name = source or ""

    # Языковые пулы выбираются один раз: смешение русской интро с английским
    # аском — верный способ выглядеть автоматом.
    fmt_line = FORMAT_LINE_EN if en else FORMAT_LINE
    pool_intros = INTROS_EN if en else INTROS
    pool_intros_mid = INTROS_MIDDLE_EN if en else INTROS_MIDDLE
    pool_intros_short = INTROS_SHORT_EN if en else INTROS_SHORT
    pool_asks = ASKS_EN if en else ASKS
    pool_asks_short = ASKS_SHORT_EN if en else ASKS_SHORT
    pool_bridges = BRIDGES_EN if en else BRIDGES

    def _build(sk, intro, ask, bridge):
        ask = fmt_line + " " + ask
        if req:
            t = sk["tpl"].format(role=role_short, source=source_name, intro=intro,
                                 req=req, bridge=bridge, ask=ask, stack_line=stack)
        elif en:
            t = "Regarding the “%s” role (%s). %s %s%s" % (
                role_short, source_name, intro, (stack + " ") if stack else "", ask)
        else:
            # нет безопасной цитаты — только про свой опыт, без заявлений на чужой стек
            t = "По вакансии «%s» (%s). %s %s%s" % (
                role_short, source_name, intro, (stack + " ") if stack else "", ask)
        t = _drop_empty_parens(t)
        if len(t) > LEN_MAX:
            # ужимаем до 400: короткие интро/аск, стек без преамбулы
            short_stack = (stack.replace("My stack for your requirements: ", "Stack: ")
                           if en else
                           stack.replace("Мой стек по вашим требованиям: ", "Стек: "))
            t = sk["tpl"].format(role=role_short, source=source_name,
                                 intro=_pick(pool_intros_mid if getattr(score, "is_middle", False)
                                             else pool_intros_short, seed),
                                 req=req or ("your stack" if en else "ваш стек"),
                                 bridge=("hands-on experience." if en
                                         else "есть прямой опыт."),
                                 ask=fmt_line + " " + _pick(pool_asks_short, seed // 3),
                                 stack_line=short_stack)
            t = _drop_empty_parens(t)
        return t

    # Перебираем комбинации детерминированно (стабильно при ретрае отправки),
    # берём первую, чья похожесть на недавние письма ниже порога.
    base_skeletons = SKELETONS_EN if en else SKELETONS
    skeletons = base_skeletons if req else [{"id": "s0_stack_only"}]
    if not stack:      # без стека — только скелеты, где он не упоминается
        skeletons = [s for s in skeletons if "{stack_line}" not in s.get("tpl", "")] \
                    or [{"id": "s0_stack_only"}]
    if template_preferences and len(skeletons) > 1:
        # Статистика влияет только на порядок равноправных скелетов. Минимум
        # выборки уже обеспечен report.template_preferences(), а стабильная
        # сортировка сохраняет разнообразие за счёт seed и не превращает
        # кампанию в один и тот же текст.
        skeletons = sorted(
            skeletons,
            key=lambda sk: -float(template_preferences.get(sk["id"], 0.0)))
    best = None
    for i in range(len(skeletons) * 3):
        sk = skeletons[(seed // 7 + i) % len(skeletons)]
        pool = pool_intros_mid if getattr(score, "is_middle", False) else pool_intros
        intro = _pick(pool, seed + i)
        ask = _pick(pool_asks, seed // 3 + i)
        bridge = _pick(pool_bridges, seed // 11 + i)
        text = _build(sk, intro, ask, bridge)
        sim = max_similarity(text, recent_corpus)
        if best is None or sim < best[1]:
            best = (text, sim, sk["id"])
        if sim < SIMILARITY_MAX and LEN_MIN <= len(text) <= LEN_MAX:
            break

    text, sim, skeleton_id = best
    doc = DocModel(lang=lang, kind="message", free_text=text, rendered_bullets=[])
    gate = check(doc, jd_text=jd_text, profile=p)

    return MessageResult(
        text=text, skeleton_id=skeleton_id, body_hash=norm_hash(text),
        similarity_max=sim, gate=gate, quoted_requirement=req)
