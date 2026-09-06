EXTRA={}
def S(code,title,url,kind='docs',language='EN',focus='',access='Открытый материал; вычисления и внешние сервисы могут оплачиваться отдельно.',verification='Открыта первичная страница 06.09.2026. Видео, если присутствует, не просмотрено целиком.'):
    EXTRA[code]=dict(title=title,url=url,kind=kind,language=language,what_to_watch=focus,access=access,verification=verification)
S('X01','Django: Writing your first app','https://docs.djangoproject.com/en/stable/intro/tutorial01/',focus='Проект, приложение, views, URL, модели и миграции.')
S('X02','Flask: Tutorial','https://flask.palletsprojects.com/en/stable/tutorial/',focus='Application factory, request, templates, данные и тестирование.')
S('X03','CS50x: Flask','https://cs50.harvard.edu/x/weeks/9/','video',focus='Лекция Harvard о Flask, HTTP, формах и сессиях.',access='Бесплатная лекция с плеером, кодом и транскриптом.')
S('X04','Milvus: Quickstart','https://milvus.io/docs/quickstart.md',focus='Коллекция, схема, вставка, поиск и фильтры.')
S('X05','Ollama: Quickstart','https://docs.ollama.com/quickstart',focus='Локальный запуск и API; проверять выбранную модель и оборудование.')
S('X06','Ollama: Structured Outputs','https://docs.ollama.com/capabilities/structured-outputs',focus='Схема ответа и поддержка structured output.')
S('X08','SQLAlchemy: Unified Tutorial','https://docs.sqlalchemy.org/en/20/tutorial/',focus='Core/ORM, соединения, запросы, отношения и транзакции.')
S('X09','Alembic: Tutorial','https://alembic.sqlalchemy.org/en/latest/tutorial.html',focus='Миграции схемы, окружение, revision, upgrade и downgrade.')
S('X10','Langfuse: Replace Vibes With Evals','https://langfuse.com/guides/videos/replace-vibes-with-evals','video',focus='Trace, error analysis, dataset, scoring и сравнение вариантов.',access='Открытая страница с видеоплеером; сервис и API имеют отдельные условия.')
S('X11','Langfuse: Evaluation of LLM Applications','https://langfuse.com/docs/evaluation/overview',focus='Code/human/LLM evaluation, datasets и experiments.')
S('X12','Ollama: Streaming','https://docs.ollama.com/api/streaming',focus='Формат потокового ответа и обработка частей результата.')
S('X13','Ollama: Tool calling','https://docs.ollama.com/capabilities/tool-calling',focus='Описание инструментов, вызов и передача фактического результата.')
S('X14','MDN: Overview of HTTP','https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Overview',focus='Клиент, сервер, запросы, ответы, соединения и состояние.')
S('X15','Pro Git: русское издание','https://git-scm.com/book/ru/v2',language='RU',focus='Коммиты, ветки, merge, история и восстановление.')
S('X16','CS50W: Django','https://cs50.harvard.edu/web/weeks/3/','video',focus='HTTP, Django, routes, templates, forms и sessions.',access='Бесплатная лекция Harvard с плеером, исходниками и транскриптом.')
S('X17','3Blue1Brown: Transformers, the tech behind LLMs','https://www.3blue1brown.com/lessons/gpt/','video',focus='Токены, векторы, attention/MLP и предсказание следующего токена.',access='Открытая авторская видеолекция и иллюстрированный конспект, 2024; принципы сохраняют учебную ценность.')
S('X18','3Blue1Brown: Attention in transformers','https://www.3blue1brown.com/lessons/attention/','video',focus='Attention, контекст и матрицы Q/K/V.',access='Открытая авторская видеолекция и конспект, 2024.')
S('X19','Milvus: MCP + Milvus','https://milvus.io/docs/milvus_and_mcp.md','video_course',focus='Встроенное видео о подключении MCP, коллекциях и поиске; базовый Quickstart изучается отдельно.')
S('X20','n8n: Learning paths','https://github.com/n8n-io/n8n-docs/blob/main/docs/get-started/learning-paths.md',focus='Официальный маршрут обучения; практические API и переход к Academy.',verification='Проверена официальная индексированная страница 06.09.2026; старые video-courses URL меняются.')
S('X21','n8n Academy: Essentials, Integrations, In Practice','https://learn.n8n.io/','course',focus='QS101, N8N101-103: workflows, API, AI и проверки.',access='Бесплатные интерактивные курсы; нужна регистрация. Не обозначаются как полностью видеокурсы.')
S('X22','LangChain: Overview','https://docs.langchain.com/oss/python/langchain/overview',focus='Модели, инструменты и архитектура прикладного агента.')
S('X23','ФНС: приказ ЕД-7-14/559@, структура ИНН и КПП','https://storage.consultant.ru/site20/202510/09/fns_091025_559.pdf',language='RU',focus='Приложение: структура ИНН и КПП; у КПП нет контрольного разряда.',access='PDF первичного приказа ФНС на сайте КонсультантПлюс; бесплатное чтение.',verification='Открыта 8-страничная PDF-копия приказа; структура проверена при разборе исходного плана 05.09.2026.')
S('X24','ФНС: Налог на добавленную стоимость','https://www.nalog.gov.ru/rn77/taxation/taxes/nds/',language='RU',focus='База и расчёт НДС; актуальные ставки и особенности сверять на дату операции.')
S('X25','PyTorch: Learn the Basics','https://docs.pytorch.org/tutorials/beginner/basics/intro.html',focus='Tensors, DataLoader, модель, autograd, optimization и сохранение.')
S('X27','Python: Coroutines and tasks','https://docs.python.org/3/library/asyncio-task.html',focus='Task, ожидание, тайм-аут, отмена и управление группой задач.')
S('X28','PostgreSQL: Using EXPLAIN','https://www.postgresql.org/docs/current/using-explain.html',focus='План исполнения, оценки, фактические затраты и индексы.')
S('X29','PostgreSQL: Backup and Restore','https://www.postgresql.org/docs/current/backup.html',focus='SQL dump, файловая копия и непрерывное архивирование; выбрать подходящий способ.')
S('X30','OWASP GenAI: Prompt Injection','https://genai.owasp.org/llmrisk/llm01-prompt-injection/',focus='Прямые и косвенные инструкции из недоверенного содержимого; границы контролей.')
S('X31','Architectural Decision Records','https://adr.github.io/',focus='Структура ADR, решения и альтернативы.')
S('X32','LangChain: RAG From Scratch','https://github.com/langchain-ai/rag-from-scratch','video_course',focus='Серия видео и ноутбуков: indexing, retrieval, generation, query transforms и routing.',access='Открытый официальный репозиторий с видеоссылками; старые API сверять с текущими docs.')
S('X33','Артём Шумейко: курс SQLAlchemy','https://www.youtube.com/playlist?list=PLeLN0qH0-mCXARD_K-USF2wHctxzEVp40','video_course','RU',focus='Подключение, модели, запросы, связи и миграции; сначала понимать обычный SQL.',access='Открытый авторский плейлист; старые импорты сверять с текущей документацией.',verification='06.09.2026: точный URL плейлиста и репозиторий подтверждены в официальном канале автора t.me/s/artemshumeiko?before=30; YouTube отдаёт ограниченное представление страницы.')
S('X34','Артём Шумейко: курс FastAPI','https://rutube.ru/plst/717901/','video_course','RU',focus='REST, Pydantic, Depends, БД, авторизация, файлы, архитектура и Docker.',access='Открытый авторский плейлист на RUTUBE; версии библиотек сверять с документацией.',verification='06.09.2026: подтверждены автор, название и список 12 видео в индексированной странице RUTUBE. Полное воспроизведение не проверялось.')
S('X35','NeuralNine: Professional Task Queues in Python','https://www.youtube.com/watch?v=0gtdUkEzzn4','video','EN',focus='Celery, RabbitMQ, Redis: worker, отправка заданий и результаты.',access='Открытый авторский разбор, 2025. Гарантии доставки и настройки брать из текущих Celery/RabbitMQ docs.',verification='06.09.2026: открыт точный YouTube URL с соответствующим названием; содержимое страницы ограничено. Материал также указан в исходном плане.')
S('X36','Артём Шумейко: Python - конкурентность','https://rutube.ru/video/f944bdec38c1a7b279b419d070c93e4e/','video','RU',focus='Конкурентность, async и работа с I/O; повторить ограниченные параллельные запросы.',access='Открытая авторская запись от 11.06.2026.',verification='06.09.2026: открыта страница конкретного видео через канал автора; название и публикация подтверждены.')
S('X37','Артём Шумейко: вопросы по HTTP с разбором','https://rutube.ru/video/9b02bbef791d8821e271478c72c4c7cb/','video','RU',focus='HTTP-вопросы для интервью; сопоставить с собственными запросами и MDN.',access='Открытая авторская запись от 20.07.2026.',verification='06.09.2026: открыта страница конкретного видео через канал автора; название и публикация подтверждены.')
S('X38','МойСклад: вебинар по API для разработчиков (архив)','https://www.youtube.com/watch?v=k2o-IFe0L9s','video','RU',focus='Общая модель JSON API и webhooks. Запись 14.03.2017 про API 1.1; текущие методы и адреса брать только из docs JSON API 1.2.',access='Открытая архивная запись; смотреть для устройства интеграций, не копировать старый API.',verification='06.09.2026: ссылка и содержание записи подтверждены публикацией официального канала МоегоСклада: telegram.me/s/moysklad?before=48. Полное воспроизведение не проверялось.')

# Дополнительные материалы прямо привязаны к модулю; никаких поисковых URL.
EXTRA_BY_MODULE={
 'Б1':['X27'],'Б2':['X14'],'Б3':['X09','X28'],'Б4':['B08'],
 '01':['X31'],'02':['X09'],'03':['B23'],'04':['A03'],'05':['R15','R17'],
 '06':['X18','A04'],'07':['X06','X13'],'08':['R01','R04','R05','R06'],
 '09':['X32'],'10':['A13'],'11':['X11'],'12':['X23','X24'],
 '15':['R03','R11'],'16':['X21','R19'],'17':['R07','R08'],'18':['R10'],
 '19':['X30','R18'],'20':['B17'],'21':['X29'],'22':['X31'],
 'Д1':['X01','X02'],'Д2':['X22'],'Д3':['X04'],'Д4':['A27'],
 'Д5':['A29'],'Д6':['X05','A21'],'Д7':['X25'],'Д8':['A24']}
