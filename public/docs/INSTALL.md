# Установка с нуля

Инструкция рассчитана на Windows 10/11 с Docker Desktop; для macOS и Linux
отличия отмечены. В первый раз уйдёт около часа, большая часть — на заполнение
профиля.

**Содержание**

0. [Что понадобится](#0-что-понадобится)
1. [Docker](#1-docker)
2. [Python на компьютере (для разового входа)](#2-python-на-компьютере)
3. [Скачать проект](#3-скачать-проект)
4. [Профиль кандидата](#4-профиль-кандидата)
5. [Резюме](#5-резюме)
6. [Файл настроек .env](#6-файл-настроек-env)
7. [Бот-пульт в Telegram](#7-бот-пульт-в-telegram)
8. [Почта](#8-почта)
9. [Google: Gmail API и календарь](#9-google-gmail-api-и-календарь)
10. [Telegram-аккаунт (по желанию)](#10-telegram-аккаунт-по-желанию)
11. [Нейросети (по желанию)](#11-нейросети-по-желанию)
12. [Первый запуск](#12-первый-запуск)
13. [Проверка, что всё работает](#13-проверка-что-всё-работает)
14. [Автозапуск и резервные копии](#14-автозапуск-и-резервные-копии)
15. [Обновление](#15-обновление)

---

## 0. Что понадобится

| Что | Обязательно | Зачем |
|---|---|---|
| Компьютер, включённый днём | да | расписание работает с 9 до 21; спящий компьютер пропускает шаги (бот догоняет их после пробуждения) |
| Docker Desktop / Docker Engine | да | бот живёт в контейнерах |
| Аккаунт Telegram | да | бот-пульт: уведомления, карточки, кнопки |
| Аккаунт Gmail | да | отправка писем работодателям и чтение ответов |
| Python 3.12 | да, один раз | вход в Google и Telegram открывает браузер или просит код — это делается на компьютере, не в контейнере |
| Проект Google Cloud | желательно | Gmail API (почта не зависит от VPN) и календарь интервью |
| api_id / api_hash Telegram | по желанию | чтение и ответы в личных сообщениях Telegram |
| Ключи бесплатных нейросетей | по желанию | живой язык писем; без них работают шаблоны |

Всё бесплатно. Платных подписок проект не требует.

## 1. Docker

**Windows.** Установите [Docker Desktop](https://www.docker.com/products/docker-desktop/)
(потребуется WSL 2 — установщик предложит сам). После установки:

- Settings → General → **Start Docker Desktop when you sign in** — включить,
  чтобы бот поднимался после перезагрузки;
- Settings → Resources — достаточно 2 ГБ памяти.

**macOS** — Docker Desktop для вашей архитектуры (Apple Silicon / Intel).
**Linux** — Docker Engine и плагин compose по [официальной инструкции](https://docs.docker.com/engine/install/).

Проверка:

```bash
docker --version
docker compose version
```

## 2. Python на компьютере

Нужен только для входа в Google и Telegram (шаги 9 и 10) и для проверки
профиля. Установите [Python 3.12](https://www.python.org/downloads/)
(в установщике Windows отметьте «Add python.exe to PATH»).

В папке проекта (после шага 3):

```bash
python -m venv .venv
```

```bash
.venv\Scripts\activate            # Windows
source .venv/bin/activate         # macOS / Linux
```

```bash
pip install -r requirements.txt
```

## 3. Скачать проект

```bash
git clone <адрес репозитория> jobhunter
cd jobhunter
```

Или скачайте ZIP со страницы репозитория и распакуйте. Путь к папке лучше
без пробелов и кириллицы — например, `C:\Projects\jobhunter`.

## 4. Профиль кандидата

`profile.yaml` — единственный источник фактов о вас. Письма и резюме могут
только выбирать и переформулировать то, что в нём записано.

```bash
cp profile.example.yaml profile.yaml
```

Откройте `profile.yaml` в редакторе (VS Code, Notepad++; кодировка UTF-8) и
перепишите под себя. Пример заполнен за вымышленного кандидата — **замените
всё**: имя, контакты, опыт, навыки, готовые ответы, тексты писем. Подробно
по каждому разделу — [PROFILE.md](PROFILE.md).

Проверка после каждой правки:

```bash
python validate_profile.py
```

Ожидаемый итог — `ОШИБОК НЕТ`.

> `profile.yaml` лежит в `.gitignore` и в репозиторий не попадает. Не
> коммитьте его: там ваш телефон, почта и вся карьера.

## 5. Резюме

Положите своё резюме в PDF в папку `cv_base/`, например `cv_base/resume.pdf`.
Имя файла укажете в `.env` (`BASE_CV_FILE`, шаг 6).

Как бот выбирает резюме:

- **вакансия на русском** — уходит ваш PDF из `cv_base/`, если он есть;
- **вакансия на английском** — бот собирает английское резюме из профиля под
  роль (PDF в `out/cv_out/`);
- файла нет — для всех вакансий собирается резюме из профиля.

## 6. Файл настроек .env

```bash
cp .env.example .env
```

`.env` тоже в `.gitignore`. Каждый параметр прокомментирован прямо в файле;
справочник — [CONFIGURATION.md](CONFIGURATION.md). Для старта достаточно
заполнить то, что описано в шагах 7–9. Остальное можно оставить как есть.

Обязательно проверьте:

```ini
BASE_CV_FILE=resume.pdf          # имя вашего PDF в cv_base/
OWNER_TZ=Europe/Moscow           # ваш часовой пояс
DRY_RUN=false                    # true — ничего не отправлять, только писать в лог
```

Совет для первого запуска: поставьте `DRY_RUN=true`, посмотрите письма в
панели (шаг 13) и только потом переключите на `false`.

## 7. Бот-пульт в Telegram

1. Откройте [@BotFather](https://t.me/BotFather) → `/newbot` → придумайте имя
   и username (должен заканчиваться на `bot`). BotFather пришлёт токен.
2. Узнайте свой числовой id: напишите [@userinfobot](https://t.me/userinfobot).
3. В `.env`:

   ```ini
   TELEGRAM_BOT_TOKEN=123456789:AA...        # токен от BotFather
   BOT_ALLOWED_USER_IDS=123456789            # ваш id; несколько — через запятую
   OWNER_CHANNEL=bot                         # карточки и уведомления — в бота
   ```

4. Откройте своего бота и нажмите **Start**: Telegram не позволяет боту
   писать первым.

Пустой `BOT_ALLOWED_USER_IDS` — бот не выполняет ничего: он управляет
отправкой от вашего имени и открытым быть не может. Сообщения чужих людей он
молча игнорирует.

Аватар для бота есть в `assets/bot_avatar/avatar_640.jpg` — поставить можно
через BotFather (`/setuserpic`).

## 8. Почта

Письма уходят с вашего Gmail. Два способа, бот выбирает сам (`MAIL_TRANSPORT=auto`):

| Способ | Плюсы | Что нужно |
|---|---|---|
| **Gmail API** (рекомендуется) | обычный HTTPS: работает при VPN, который режет почтовые порты; тот же вход даёт календарь | шаг 9 |
| **SMTP / IMAP** | не нужен проект Google Cloud | пароль приложения |

В любом случае укажите адрес:

```ini
SMTP_USER=you@gmail.com
```

**Пароль приложения** (только для SMTP/IMAP): аккаунт Google → Безопасность →
включите двухэтапную аутентификацию → «Пароли приложений» → создайте пароль →
`SMTP_APP_PASSWORD=` в `.env`. Обычный пароль от почты не подойдёт и не нужен.

Лимиты: ящик «прогревается» — 40 писем в первый день, +5 за каждый день без
отбивок, до `EMAIL_DAILY_LIMIT` (80). Не поднимайте резко: новый ящик, который
сразу шлёт сотни писем, Gmail быстро ограничит.

## 9. Google: Gmail API и календарь

Один вход даёт три разрешения: `gmail.send` (отправка), `gmail.readonly`
(чтение ответов; удалить или переместить письмо бот не может) и
`calendar.events` (интервью в календарь).

**9.1. Проект в Google Cloud** — [console.cloud.google.com](https://console.cloud.google.com/):

1. Создайте проект (любое имя, например `jobhunter`).
2. APIs & Services → Library → включите **Gmail API** и **Google Calendar API**.
3. APIs & Services → OAuth consent screen:
   - User type: **External**;
   - заполните название приложения и свою почту;
   - Scopes можно не добавлять — бот запросит их при входе;
   - Test users: добавьте свой адрес Gmail.
4. **Важно: Publishing status → Publish app («В работе»).** Пока проект в
   статусе «Testing», токен живёт 7 дней и потом молча умирает
   (`invalid_grant`). Google предупредит, что приложение не проверено, — для
   личного проекта это нормально, проверка не нужна.
5. APIs & Services → Credentials → Create credentials → **OAuth client ID** →
   Application type: **Desktop app** → Create → **Download JSON**.
6. Сохраните файл в корень проекта как `google_client_secret.json`.

**9.2. Вход** (на компьютере, с активированным `.venv` из шага 2):

```bash
python -m jobhunter.googleauth --login
```

Откроется браузер: выберите свой аккаунт → «Приложение не проверено» →
«Дополнительно» → «Перейти» → отметьте все разрешения. В папке появится
`google_token.json`.

```bash
python -m jobhunter.googleauth --check
```

**9.3. Передать токен контейнерам.** Контейнеры читают его из тома
`jobhunter-data`. Том создаётся при первом запуске (шаг 12) — сначала
выполните шаг 12, затем:

```bash
docker run --rm -v jobhunter-data:/data -v "${PWD}:/src:ro" alpine sh -c "cp /src/google_token.json /data/ && chown 10001:10001 /data/google_token.json"
```

В PowerShell `${PWD}` работает как есть; в cmd.exe замените на полный путь к
папке проекта. `chown` обязателен: бот в контейнере работает не от root и
иначе не сможет обновлять токен.

После копирования удалите `google_token.json` из папки проекта — он даёт
доступ к вашей почте. `google_client_secret.json` в контейнере не нужен.

Проверка из контейнера:

```bash
docker compose exec autopilot python -m jobhunter.googleauth --check
```

## 10. Telegram-аккаунт (по желанию)

Без этого шага бот работает с почтой и Telegram-каналами (их он читает как
обычные веб-страницы). С этим шагом он ещё и читает ответы рекрутёров в личных
сообщениях Telegram и отвечает тем, кто написал первым.

> **Прочтите [SAFETY.md](SAFETY.md) до включения.** Бот действует от имени
> вашего личного аккаунта. Холодные сообщения незнакомцам
> (`TELEGRAM_COLD_ENABLED=true`) — прямой путь к ограничению аккаунта по
> жалобам: Telegram запрещает писать первым тем, кто не в контактах, на
> недели. По умолчанию холодная рассылка выключена.

1. [my.telegram.org](https://my.telegram.org) → войдите по номеру →
   **API development tools** → создайте приложение (название и short name —
   любые). Получите `api_id` и `api_hash`.
2. В `.env`:

   ```ini
   TELEGRAM_API_ID=1234567
   TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
   TELEGRAM_PHONE=+79991234567
   ```

3. Вход — в два шага, чтобы не сжечь код:

   ```bash
   python tg_login.py send
   ```

   Telegram пришлёт код в приложение. Запишите его в файл `tg_code.txt` в
   папке проекта (одна строка) и выполните:

   ```bash
   python tg_login.py finish
   ```

   Если на аккаунте облачный пароль (2FA) — запишите его в `tg_2fa.txt` перед
   `finish`. Оба файла удаляются сразу после чтения.

4. Появится `jobhunter.session`. Передайте его контейнерам (после шага 12):

   ```bash
   docker run --rm -v jobhunter-data:/data -v "${PWD}:/src:ro" alpine sh -c "cp /src/jobhunter.session /data/ && chown 10001:10001 /data/jobhunter.session"
   ```

   И удалите `jobhunter.session` из папки проекта: **этот файл — полный доступ
   к вашему аккаунту Telegram.**

> Никогда не запускайте `python -m jobhunter...` на компьютере, пока работают
> контейнеры: второй клиент с той же сессией разлогинит аккаунт
> (`AuthKeyDuplicatedError`). Ручные команды — только через
> `docker compose exec autopilot ...`.

## 11. Нейросети (по желанию)

Без ключей письма собираются из шаблонов — это нормальный рабочий режим. С
ключами бот пишет живее и умеет готовить черновики ответов на вопросы
рекрутёров. Любой текст нейросети проходит тот же гейт правды.

Бесплатные тарифы, достаточно одного-двух ключей:

| Провайдер | Где взять ключ | Переменная |
|---|---|---|
| Google Gemini | [aistudio.google.com](https://aistudio.google.com/apikey) | `GEMINI_API_KEY` |
| Groq | [console.groq.com](https://console.groq.com/keys) | `GROQ_API_KEY` |
| OpenRouter | [openrouter.ai](https://openrouter.ai/keys) | `OPENROUTER_API_KEY` |
| Cerebras | [cloud.cerebras.ai](https://cloud.cerebras.ai/) | `CEREBRAS_API_KEY` |
| Mistral | [console.mistral.ai](https://console.mistral.ai/) | `MISTRAL_API_KEY` |
| GitHub Models | GitHub → Settings → Developer settings → token | `GITHUB_MODELS_TOKEN` |

Бот перебирает провайдеров по очереди: упёрся в лимит одного — идёт к
следующему. Телефоны и адреса перед отправкой в нейросеть вырезаются
(`LLM_REDACT_PII=true`).

## 12. Первый запуск

```bash
docker compose build migrate
docker compose up -d
```

Образ собирает только сервис `migrate` — остальные используют тот же образ.
`docker compose build autopilot` ничего не пересоберёт.

Четыре сервиса:

| Сервис | Что делает |
|---|---|
| `migrate` | создаёт схему базы и завершается |
| `autopilot` | расписание: сбор, отбор, письма, отправка, переписка |
| `bot` | бот-пульт в Telegram |
| `web` | панель на http://127.0.0.1:8765 (только с этого компьютера) |

```bash
docker compose ps              # через минуту-две все healthy
docker compose logs -f bot     # что делает бот (Ctrl+C — выйти из просмотра)
docker compose logs -f autopilot
```

Где что лежит:

- база, сессия Telegram и токен Google — в томе Docker `jobhunter-data`;
- всё, что нужно открывать глазами (резюме, логи, архив отправленного,
  календарь `.ics`) — в папке `out/` рядом с проектом.

После шагов 9.3 и 10.4 (копирование токенов) перезапустите:

```bash
docker compose restart autopilot bot
```

## 13. Проверка, что всё работает

1. **Бот-пульт.** Напишите своему боту `/start` — придёт главный экран с
   воронкой. Кнопки: статистика, очередь, «нужно сделать».
2. **Панель.** Откройте http://127.0.0.1:8765 — очередь, отправка, чтение ответов.
3. **Сбор без ожидания расписания:**

   ```bash
   docker compose exec autopilot python -m jobhunter.ingest.all_sources
   docker compose exec autopilot python -m jobhunter.pipeline
   ```

   Первая команда соберёт вакансии, вторая подготовит письма и резюме.
4. **Посмотреть письма, не отправляя:**

   ```bash
   docker compose exec autopilot python -m jobhunter.outreach.approve --list
   docker compose exec autopilot python -m jobhunter.outreach.approve --show 12
   ```

   (`12` — номер заявки из списка.) Не нравятся письма — правьте раздел
   `outreach` и опыт в `profile.yaml`, затем
   `docker compose restart autopilot bot` (пересобирать образ не нужно;
   уже подготовленные письма не переписываются).
5. **Почта.** `docker compose exec autopilot python -m jobhunter.googleauth --check`
   — должны быть отмечены gmail.send и gmail.readonly.
6. Всё устраивает — `DRY_RUN=false` в `.env` и:

   ```bash
   docker compose up -d
   ```

   (после правки `.env` нужен именно `up -d`: `restart` не перечитывает
   окружение.)

## 14. Автозапуск и резервные копии

**Автозапуск (Windows).** Контейнеры с `restart: unless-stopped` поднимаются
вместе с Docker Desktop. Страховка на случай, если проект был остановлен
вручную: Win+R → `shell:startup` → ярлык на

```
powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File <папка проекта>\scripts\start.ps1
```

**Сон компьютера.** Электропитание → «Переводить компьютер в спящий режим» →
«Никогда» на время поиска работы (или хотя бы с 9 до 21). Пропущенные шаги
бот догоняет после пробуждения, но письма в итоге уходят позже.

**Резервные копии.** Бот сам делает копию базы каждое утро. Полный архив тома
(база, сессия, токены):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/backup_volume.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify_backup.ps1
```

Архивы складываются в `backup/`. **Не публикуйте их:** внутри сессия
Telegram и токены.

**Внешний сторож (по желанию).** [healthchecks.io](https://healthchecks.io)
бесплатно пришлёт вам уведомление, если бот перестал работать (например,
компьютер выключен): создайте проверку с расписанием `*/5 9-20 * * *` и
вашим часовым поясом, адрес вида `https://hc-ping.com/<uuid>` — в
`HEALTHCHECK_URL`.

## 15. Обновление

```bash
git pull
docker compose build migrate
docker compose up -d
```

База мигрирует сама при старте (`migrate`). Профиль, `.env`, резюме и том с
данными обновление не трогает. Перед крупным обновлением сделайте резервную
копию (шаг 14).

---

Дальше: [ежедневная работа](USAGE.md) · [настройки](CONFIGURATION.md) ·
[если что-то не так](TROUBLESHOOTING.md)
