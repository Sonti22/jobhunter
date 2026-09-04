"""Пресеты резюме по семействам ролей.

Одно семейство — один пресет: заголовок, какие навыки поднять при ранжировании
буллетов, в каком порядке показать секцию «Навыки», из какого шаблона собрать
саммари. Раньше это лежало разбросанно по select.py (константы PM_SKILLS,
DEVOPS_SKILLS и три ветки внутри _summary), и добавить новое семейство означало
дописать ещё одну ветку в каждую функцию.

Что пресет НЕ делает: не добавляет фактов. Все id навыков обязаны существовать
в profile.yaml, все заголовки — в identity.headline_variants, тексты саммари
собираются только из терминов, которые кандидат реально знает. Проверяет это
не автор пресета, а гейт (gate.check) на выходе — как и для любого другого
сгенерированного текста.

Шаблон саммари — запасной вариант. Основной путь: LLM пишет живой текст из тех
же фактов (llm_writer.write_summary), гейт его проверяет, не прошло — берётся
summary_ru/summary_en отсюда.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Ядро, которое имеет смысл в любой инженерной роли. Идёт хвостом после
# профильных навыков семейства.
CORE_SKILLS = ["python", "postgresql", "rest_api", "fastapi", "docker",
               "microservices", "gitlab_ci", "agile"]

MANAGEMENT_SKILLS = ["requirements", "prioritization", "planning",
                     "stakeholder_mgmt", "team_leadership", "risk_mgmt",
                     "agile", "tech_docs", "mentoring"]


@dataclass(frozen=True)
class RolePreset:
    key: str                                  # совпадает с семейством из role.py
    cv_slug: str                              # попадает в имя PDF
    headline: str                             # обязан быть в headline_variants
    summary_ru: str                           # %d — общий стаж
    summary_en: str
    boost_skills: list = field(default_factory=list)   # поднять буллеты с ними
    skill_order: list = field(default_factory=list)    # порядок секции «Навыки»
    max_bullets: int = 9

    def skills(self) -> list:
        """Полный порядок навыков: профильные, потом ядро."""
        out = list(self.skill_order)
        out += [s for s in CORE_SKILLS if s not in out]
        return out


PRESETS: dict[str, RolePreset] = {

    "backend": RolePreset(
        key="backend", cv_slug="Backend",
        headline="Senior Backend Engineer / Tech Lead",
        boost_skills=["python", "fastapi", "django", "postgresql", "rest_api",
                      "asyncio", "sqlalchemy", "docker", "microservices"],
        skill_order=["python", "fastapi", "django", "asyncio", "postgresql",
                     "sqlalchemy", "alembic", "rest_api", "openapi", "redis",
                     "rabbitmq", "kafka", "docker", "microservices", "pytest",
                     "gitlab_ci"],
        summary_ru=("%d+ лет в разработке ПО, вырос из backend-разработчика в "
                    "Tech Lead / Software Architect. Проектирую API-контракты, "
                    "схемы данных, модель прав, коды ошибок и сценарии отказа — "
                    "и довожу до релиза. Рабочий стек: Python, FastAPI, Django, "
                    "PostgreSQL, Docker, микросервисы."),
        summary_en=("%d+ years in software engineering, grown from backend "
                    "developer into Tech Lead / Software Architect. I design API "
                    "contracts, data models, the permission model, error codes and "
                    "failure paths, and ship them. Daily stack: Python, FastAPI, "
                    "Django, PostgreSQL, Docker, microservices."),
    ),

    "devops": RolePreset(
        key="devops", cv_slug="DevOps",
        headline="DevOps / Platform Engineer",
        boost_skills=["docker", "kubernetes", "gitlab_ci", "jenkins",
                      "azure_devops", "linux", "prometheus", "grafana",
                      "kibana", "bash", "profiling"],
        skill_order=["docker", "kubernetes", "gitlab_ci", "jenkins",
                     "azure_devops", "linux", "bash", "prometheus", "grafana",
                     "kibana", "profiling", "postgresql", "python",
                     "microservices"],
        summary_ru=("%d+ лет в разработке с сильной эксплуатационной частью: "
                    "Docker и Kubernetes через Helm, пайплайны GitLab CI "
                    "(линтеры → тесты → сборка → стейдж → прод), Jenkins и "
                    "Azure DevOps, работа в Linux. Настраивал алерты "
                    "Prometheus/Grafana и SLA по задержкам, дежурил по инцидентам, "
                    "разбирал их в Kibana, внедрил feature flags для безопасного "
                    "отката релизов."),
        summary_en=("%d+ years in software engineering with a strong operations "
                    "side: Docker and Kubernetes/Helm, GitLab CI pipelines "
                    "(linters → tests → build → staging → production), Jenkins and "
                    "Azure DevOps, Linux operations. Set up Prometheus/Grafana "
                    "alerting and latency SLAs, was on call, investigated incidents "
                    "in Kibana and introduced feature flags for safe rollbacks."),
    ),

    "data_engineer": RolePreset(
        key="data_engineer", cv_slug="DataEngineer",
        headline="Data Engineer (Python / SQL)",
        boost_skills=["sql", "postgresql", "sqlalchemy", "alembic", "kafka",
                      "rabbitmq", "python", "profiling", "mongodb", "redis"],
        skill_order=["python", "sql", "postgresql", "sqlalchemy", "alembic",
                     "kafka", "rabbitmq", "redis", "mongodb", "profiling",
                     "docker", "gitlab_ci", "linux", "microservices"],
        summary_ru=("%d+ лет в разработке ПО; данные — постоянная часть работы. "
                    "Сложный SQL и PostgreSQL в проде: схемы, миграции через "
                    "Alembic, индексы и профилирование запросов. Потоковая часть — "
                    "Kafka и RabbitMQ, обвязка на Python и SQLAlchemy, всё "
                    "упаковано в Docker и катится через GitLab CI."),
        summary_en=("%d+ years in software engineering, with data work throughout. "
                    "Advanced SQL and production PostgreSQL: schemas, Alembic "
                    "migrations, indexing and query profiling. Streaming side — "
                    "Kafka and RabbitMQ, Python and SQLAlchemy around them, "
                    "packaged in Docker and shipped through GitLab CI."),
    ),

    "ml": RolePreset(
        key="ml", cv_slug="MLIntegration",
        headline="Backend Engineer / ML Systems Integrator",
        boost_skills=["opencv", "yolov8", "video_pipeline", "llm_stack",
                      "speech", "python", "profiling", "fastapi", "docker"],
        skill_order=["python", "opencv", "yolov8", "video_pipeline", "llm_stack",
                     "speech", "profiling", "fastapi", "asyncio", "postgresql",
                     "docker", "kubernetes", "gitlab_ci"],
        # Ровно то, что есть: интеграция и вывод в продакшн. Не research,
        # не обучение моделей. CUDA у кандидата familiar — в саммари её нет,
        # и гейт бы её не пропустил.
        summary_ru=("%d+ лет в разработке ПО. Параллельно с backend выводил в "
                    "продакшн ML/CV-системы: детекция OpenCV и YOLOv8, "
                    "видео-пайплайны RTSP, LLM-пайплайны (vLLM/OpenAI, Whisper, "
                    "ElevenLabs) с бюджетом задержки p50/p95 и профилированием. "
                    "Интеграция и вывод в продакшн, не research."),
        summary_en=("%d+ years in software engineering. Alongside backend I shipped "
                    "production ML/CV systems: OpenCV and YOLOv8 detection, RTSP "
                    "video pipelines, and LLM pipelines (vLLM/OpenAI, Whisper, "
                    "ElevenLabs) with p50/p95 latency budgets and profiling. "
                    "Integration and productionisation, not research."),
    ),

    "product": RolePreset(
        key="product", cv_slug="ProductManager",
        headline="Technical Product Manager",
        boost_skills=MANAGEMENT_SKILLS + ["rest_api", "openapi"],
        skill_order=MANAGEMENT_SKILLS,
        max_bullets=12,
        summary_ru=("%d+ лет в разработке ПО, вырос из backend-разработчика в "
                    "Tech Lead / Software Architect. С 2022 отвечаю за технические "
                    "требования и поведение продукта: проектирую API-контракты, "
                    "модель прав, коды ошибок и сценарии отказа, довожу до релиза "
                    "и отвечаю за метрики. Приоритизация, планирование и работа "
                    "со стейкхолдерами — ежедневная часть роли."),
        summary_en=("%d+ years in software engineering, grown from backend "
                    "developer into Tech Lead / Software Architect. Since 2022 I own "
                    "technical requirements and product behaviour: I design API "
                    "contracts, the permission model, error codes and failure paths, "
                    "ship them and own the metrics. Prioritisation, planning and "
                    "stakeholder work are part of the daily job."),
    ),

    "project": RolePreset(
        key="project", cv_slug="ProjectManager",
        headline="Project Manager (Tech)",
        boost_skills=MANAGEMENT_SKILLS,
        skill_order=MANAGEMENT_SKILLS,
        max_bullets=12,
        summary_ru=("%d+ лет в разработке ПО, последние годы — Tech Lead: "
                    "планирование, оценка и приоритизация задач, управление "
                    "техдолгом и рисками, координация команды и коммуникация с "
                    "техническими и нетехническими заказчиками. Работаю по Agile, "
                    "веду техническую документацию, менторю разработчиков."),
        summary_en=("%d+ years in software engineering, recent years as Tech Lead: "
                    "planning, estimation and prioritisation, technical debt and "
                    "risk management, team coordination and communication with both "
                    "technical and non-technical stakeholders. Agile process, "
                    "technical documentation, mentoring."),
    ),

    "analyst": RolePreset(
        key="analyst", cv_slug="SystemsAnalyst",
        headline="Systems Analyst / Requirements Engineer",
        boost_skills=["requirements", "tech_docs", "openapi", "rest_api",
                      "stakeholder_mgmt", "sql", "postgresql", "ddd"],
        skill_order=["requirements", "tech_docs", "openapi", "rest_api", "sql",
                     "postgresql", "stakeholder_mgmt", "prioritization",
                     "ddd", "microservices", "agile", "python"],
        max_bullets=12,
        summary_ru=("%d+ лет в разработке ПО; с 2022 отвечаю за технические "
                    "требования и поведение продукта. Пишу и согласую требования, "
                    "проектирую API-контракты и описываю их в OpenAPI, разбираю "
                    "интеграции и сценарии отказа, веду техническую документацию. "
                    "Знаю систему с обеих сторон — и как автор требований, и как "
                    "тот, кто их потом реализует."),
        summary_en=("%d+ years in software engineering; since 2022 I own technical "
                    "requirements and product behaviour. I write and negotiate "
                    "requirements, design API contracts and document them in "
                    "OpenAPI, work through integrations and failure paths, and "
                    "maintain technical documentation — from both sides: the one "
                    "who writes the spec and the one who implements it."),
    ),

    "qa": RolePreset(
        key="qa", cv_slug="QAAutomation",
        headline="QA Automation Engineer (Python)",
        boost_skills=["pytest", "tdd", "python", "code_review", "rest_api",
                      "openapi", "docker", "gitlab_ci", "profiling"],
        skill_order=["python", "pytest", "tdd", "code_review", "rest_api",
                     "openapi", "sql", "postgresql", "docker", "gitlab_ci",
                     "linux", "profiling"],
        summary_ru=("%d+ лет в разработке ПО. Тесты — часть моей работы, а не "
                    "отдельная профессия: pytest, TDD, тесты на API-контракты и "
                    "сценарии отказа, прогон в GitLab CI до стейджа. Ревью кода и "
                    "профилирование — оттуда же. Пишу автотесты на Python и знаю "
                    "систему изнутри, потому что сам её проектировал."),
        summary_en=("%d+ years in software engineering. Testing is part of my job "
                    "rather than a separate profession: pytest, TDD, tests against "
                    "API contracts and failure paths, running in GitLab CI before "
                    "staging. Code review and profiling come from the same place. "
                    "I write Python automation and know the system from the inside "
                    "because I designed it."),
    ),

    "architect": RolePreset(
        key="architect", cv_slug="TechLead",
        headline="Tech Lead / Software Architect",
        boost_skills=["microservices", "ddd", "cqrs", "event_sourcing", "saga",
                      "rest_api", "openapi", "team_leadership", "mentoring",
                      "code_review", "tech_docs"],
        skill_order=["microservices", "ddd", "cqrs", "event_sourcing", "saga",
                     "rest_api", "openapi", "postgresql", "kafka", "rabbitmq",
                     "kubernetes", "team_leadership", "mentoring", "code_review",
                     "tech_docs"],
        max_bullets=12,
        summary_ru=("%d+ лет в разработке ПО, вырос из backend-разработчика в "
                    "Tech Lead / Software Architect. Проектирую микросервисные "
                    "системы: границы сервисов по DDD, CQRS и event sourcing, "
                    "распределённые транзакции через saga, контракты в OpenAPI. "
                    "Веду команду: ревью, менторинг, техдолг, техническая "
                    "документация."),
        summary_en=("%d+ years in software engineering, grown from backend "
                    "developer into Tech Lead / Software Architect. I design "
                    "microservice systems: service boundaries via DDD, CQRS and "
                    "event sourcing, distributed transactions with the saga "
                    "pattern, contracts in OpenAPI. I lead the team: reviews, "
                    "mentoring, technical debt, documentation."),
    ),
}


def get(family: str) -> RolePreset | None:
    return PRESETS.get(family)


def fallback() -> RolePreset:
    """Когда роль не распознана, но отклик всё же собирается вручную."""
    return PRESETS["backend"]
