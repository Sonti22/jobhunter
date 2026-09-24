"""Оценка вакансий: не инженерные профессии и роли вне профиля.

24.09, после того как московский офис перестал резаться: «роль профильная» получали
продюсер видео, HR-партнёр, бьюти-дистрибутор, «Программист 1С», «Инженер ПЛИС».
Слова «разработчик», «инженер», «продукт» находились в тексте почти любой вакансии.
Заголовки ниже — настоящие, из базы.
"""
import pytest

from jobhunter.match.scorer import score_job

JD = ("Ищем в команду. Python, FastAPI, PostgreSQL, Docker, Kubernetes, Redis. "
      "Работа в продуктовой команде разработчиков. Офис в Москве.")


@pytest.mark.parametrize("title", [
    "Москва #BeautyIndustry #FMCG #ДистрибуцияКосметики",
    "Программист 1С ERP",
    "Бренд одежды ZNWR ищет ПРОДЮСЕРА короткого видео",
    "БИЗНЕС ПАРТНЕР ПО ПЕРСОНАЛУ (МАЛЫЙ И МИКРО-БИЗНЕС)",
    "Инженер-разработчик ПЛИС (RTL/FPGA)",
])
def test_non_engineering_or_out_of_profile_roles_are_not_recommended(title):
    s = score_job(title, "", JD)
    assert s.misfit_role and not s.recommend, s.reason


@pytest.mark.parametrize("title", [
    "Python-разработчик HR-платформы",           # владелец сам делал HR-платформу
    "Senior Engineering Manager — Platform",
    "Senior Fullstack Developer (Python)",
    "Kubernetes Administrator / Private Cloud Engineer",
    "Backend-разработчик (Python) в продуктовую команду",
    # Роли из профиля, которые раньше проходили только за счёт голого «продукт»
    # в тексте; сравнение на живых вакансиях 24.09 показало, что их терять нельзя.
    "СТО в новый продукт с «0» (удаленно)",
    "Менеджер продукта (CIS) в Lamoda",
    "CPO / Product Director в Big Tech компанию",
    "Project Manager в AI-стартап",
    "Middle+/Senior- BA analyst.",
    "Quality Assurance Engineer | SaaS | Remote",
])
def test_engineering_roles_keep_passing(title):
    s = score_job(title, "", JD)
    assert not s.misfit_role and s.recommend, s.reason


# Текст без инженерных слов: роль видна только по заголовку.
PLAIN_JD = "Удалённая работа, гибкий график, дружная команда."


@pytest.mark.parametrize("title", [
    "Technical Project Manager / Clay",
    "Project Manager - Software Implementation (m/f/x)",
    "Project Manager в AI-стартап",
    "Ведущий системный аналитик",
    "Middle+/Senior- BA analyst.",
    "Quality Assurance Engineer | SaaS | Remote",
    "СТО в новый продукт с «0» (удаленно)",
])
def test_profile_roles_recognised_by_title(title):
    assert score_job(title, "", PLAIN_JD).fit_role


def test_role_in_first_line_of_channel_post_counts_as_title():
    # #18864: парсер записал в заголовок «москва», должность — первой строкой.
    jd = "#москва  Chief Product Officer   Обязанности Продуктовая стратегия. " + PLAIN_JD
    assert score_job("москва", "", jd).fit_role


@pytest.mark.parametrize("title", [
    # Первая версия (проектные роли и аналитики по всему тексту) на живых данных
    # 24.09 пустила 51 вакансию, среди них вот эти.
    "PROJECT MANAGER В КРЕАТИВНОЕ АГЕНТСТВО THE CLIENTS, Удаленно",
    "Marketing Project Manager в PRHub.ae",
    "Бизнес-ассистент / Project Manager",
    "МЕНЕДЖЕР ПРОЕКТОВ ПО СТРАТЕГИЧЕСКОМУ ПЛАНИРОВАНИЮ ЧИСЛЕННОСТИ",
    "Автоматизатор / Специалист по автоматизации",
    "Lead People Partner",
    "Технический писатель",
])
def test_non_it_project_and_analyst_roles_are_not_profile_roles(title):
    jd = PLAIN_JD + " Менеджер проектов, руководитель проектов, бизнес-аналитик."
    assert not score_job(title, "", jd).fit_role
