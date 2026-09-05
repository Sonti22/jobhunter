# -*- coding: utf-8 -*-
"""Вакансия или резюме соискателя — на живых текстах из базы.

Инцидент 05.09: система написала четырём соискателям (@devops_jobs и
похожие каналы мешают вакансии с резюме, у обоих есть контакт). Здесь —
ровно те тексты, на которых старый фильтр промахнулся, плюс настоящие
вакансии с «обманными» тегами, которые терять нельзя (#9290, #4428 —
их я сам сначала отозвал по тегу, и это было ошибкой).
"""
import pytest

from jobhunter.ingest.postkind import OTHER, SEEKER, VACANCY, classify_post

# (текст из БД, ожидание)
LIVE = [
    ("резюме #devops #linux #docker #automation #remote\nПубликатор: Вайз\n"
     "Обсуждение: @devops_jobs\n#резюме #devops\nПозиция: DevOps Engineer\n"
     "Обо мне:\nDevOps / Infrastructure Engineer с сильным Python-бэкграундом.",
     SEEKER),
    ("резюме #cv #devops #senior #sre #platform #удаленка #fulltime\n"
     "Публикатор: Deployer Professional", SEEKER),
    ("резюме #ищу #devops #sre #iac #observability #cicd #remote\n"
     "Публикатор: Альберт", SEEKER),
    ("Меня зовут Иван. Ищу позицию Project Coordinator / Junior Project "
     "Manager. Всем привет.", SEEKER),
    ("ИщуРаботу #CV #AccountManagement #SalesOperations #Tech #Budva "
     "#ИщуРаботу", SEEKER),
    # тег соискателя + только секции («требования») без глагола найма —
    # так пишут и соискатели про свои требования к работе: не пишем
    ("#резюме #python\nТребования к работе: удалёнка, от 200к\nОпыт: 5 лет",
     SEEKER),
    # тег #резюме навешен на ВАКАНСИЮ (живой #9290): глагол найма решает
    ("Senior DevOps-инженер (полная или частичная занятость) #резюме #devops\n"
     "Публикатор: Анна\nПроект: https://camble.tv/\nИщем DevOps-инженера с "
     "сильным практическим опытом работы с высоконагруженной инфраструктурой.",
     VACANCY),
    # хештег-каша с #ищу_работу поверх вакансии (живой #4428)
    ("frontend #lead #solidity #web3 #crypto #dex #defi #ищу_работу #cv "
     "#Вакансия #NetworkEngineer #DevOps\nТребуется Infrastructure Engineer | "
     "AdRise\nМеждународная продуктовая команда запускает новый сервис.",
     VACANCY),
    ("вакансия #job #работа #hiring #ищу #devops #blockchain #ai #Kubernetes\n"
     "Публикатор: Tatiana\nТребования: Kubernetes, GPU", VACANCY),
    ("vacancy #NetworkEngineer #infrastructure #fulltime #relocation "
     "#Amsterdam #hiring #opentowork\nWe are looking for a network engineer",
     VACANCY),
    ("AI Automation Specialist / AI Engineer в команду 🚀 #vacancy #ai\n"
     "Ищем AI Automation Specialist в команду. Обязанности: …", VACANCY),
    ("nodejs #backend #senior #typescript #fulltime #remote #удаленка #вакансия\n"
     "Требуется senior backend", VACANCY),
    ("Python-разработчик, удалённо\nМы ищем разработчика в команду.\n"
     "Требования:\n— Python 3.10+\nМы предлагаем: удалёнку", VACANCY),
    ("Присылайте резюме на hr@example.com — ждём ваши резюме до пятницы",
     VACANCY),
    # советы карьеристам / реклама канала — ни то ни другое
    ("1. «Специалист» не объясняет, на какую должность претендует кандидат. "
     "ОТВЕТ: пишите роль явно.", OTHER),
]


@pytest.mark.parametrize("text,want", LIVE, ids=[t[0][:26] for t in LIVE])
def test_live_posts(text, want):
    got = classify_post(text)
    assert got.kind == want, (got.kind, got.seeker, got.vacancy, got.reasons)


def test_real_conflict_goes_to_seeker():
    """Фразы соискателя против глагола найма при равных весах — не пишем."""
    got = classify_post("ищу работу python-разработчиком; "
                        "также требуется: удалёнка")
    assert got.kind == SEEKER, (got.seeker, got.vacancy, got.reasons)


def test_eligibility_blocks_seeker_posts_everywhere():
    """Единая точка подготовки и обоих отправщиков отсекает пост соискателя."""
    from types import SimpleNamespace

    from jobhunter.outreach.eligibility import vacancy_problem

    job = SimpleNamespace(is_closed=False, source="tg:devops_jobs",
                          title="DevOps Engineer",
                          description_raw="резюме #devops\nПубликатор: Абдулла\n"
                                          "Обо мне: 5 лет в инфраструктуре",
                          posted_at=0)
    p = vacancy_problem(job)
    assert not p.allowed and p.code == "candidate", p


def test_eligibility_keeps_mistagged_vacancy():
    """Ошибочный #резюме на настоящем найме не должен терять вакансию."""
    from types import SimpleNamespace

    from jobhunter.outreach.eligibility import vacancy_problem

    job = SimpleNamespace(is_closed=False, source="tg:devops_jobs",
                          title="Senior DevOps-инженер #резюме #devops",
                          description_raw="Публикатор: Анна\nИщем DevOps-инженера "
                                          "с опытом высоконагруженной инфраструктуры."
                                          "\nТребования: Kubernetes\n@hr_company",
                          posted_at=0)
    p = vacancy_problem(job)
    assert p.allowed, p
