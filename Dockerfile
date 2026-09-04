# jobhunter — образ на все три процесса (автопилот, бот, дашборд).
#
# Один образ, разные команды: код общий, зависимости общие, а разделение по
# ролям задаётся в docker-compose.yml. Собирать три почти одинаковых образа
# ради разных ENTRYPOINT смысла нет.
#
# Python 3.12, хотя на машине владельца стоит 3.10: google-api-core перестанет
# поддерживать 3.10 с 4 октября 2026 и уже предупреждает об этом на каждом
# импорте. Код версионно-нейтрален (`from __future__ import annotations`
# везде), весь набор тестов проходит на обеих версиях — проверено.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow

# /data — именованный том: база SQLite и сессия Telethon.
# /out  — смонтированная папка проекта: резюме, логи, календарь.
# chown до монтирования обязателен: Docker копирует владельца каталога из
# образа при первом заполнении пустого тома. Без этого том создастся root'ом,
# и процесс под непривилегированным пользователем не сможет в него писать.
# Шрифты с кириллицей нужны reportlab. Без них генерация резюме падает, а
# вместе с ней весь конвейер подготовки откликов — и падает тихо: заявка
# просто не доходит до очереди, а причина видна лишь в трассировке.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core fonts-dejavu-extra \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data /out /app \
    && chown -R app:app /data /out /app

WORKDIR /app

# Зависимости отдельным слоем: правка кода не пересобирает pip install.
COPY --chown=app:app requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Код и данные, нужные для работы. .env сюда НЕ попадает (см. .dockerignore):
# секреты приходят переменными окружения в момент запуска.
COPY --chown=app:app jobhunter/ ./jobhunter/
COPY --chown=app:app profile.yaml ./
COPY --chown=app:app lexicon/ ./lexicon/
COPY --chown=app:app tests/ ./tests/
# Реестр проверенных Telegram-каналов нужен автопилоту внутри образа.
# Раньше файл оставался только на хосте, и контейнер молча видел 0 записей.
COPY --chown=app:app channels_verified.json ./

# Работаем без root: в контейнере живёт активная сессия Telegram, и
# компрометация процесса не должна давать в нём привилегий.
USER app

# Команду задаёт docker-compose.yml для каждого сервиса своя.
CMD ["python", "-m", "jobhunter.autopilot"]
