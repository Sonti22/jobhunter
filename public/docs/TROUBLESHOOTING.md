# Если что-то не так

Сначала — три команды:

```bash
docker compose ps                         # все ли сервисы healthy
docker compose logs --since 1h autopilot  # что делал автопилот
docker compose logs --since 1h bot
```

И экран `/sending` в боте: он говорит, почему заявка ждёт.

---

### Бот-пульт молчит

- Нажали ли вы **Start** в своём боте? Telegram не даёт боту писать первым.
- `BOT_ALLOWED_USER_IDS` — ваш **числовой** id (от @userinfobot), а не ник.
  Пусто — бот не отвечает никому.
- В логах `getMe: сеть недоступна` — у Docker пропал DNS или интернет. Бот сам
  повторяет попытки и поднимется, когда сеть вернётся. На Windows резолвер
  Docker Desktop иногда отваливается — поэтому в `docker-compose.yml` прописаны
  публичные DNS.
- После правки `.env` нужен `docker compose up -d`, а не `restart`.

### Письма не уходят

- `/sending` в боте покажет причину: стоп-кран, `DRY_RUN=true`, лимит дня,
  прогрев, нет одобренных заявок с email.
- «Нет одобренных заявок с email-контактом» — в очереди только вакансии с
  Telegram-контактом. Это нормально; их можно отправить руками через `/outreach`.
- `googleauth --check` не показывает `gmail.send` — повторите вход (шаг 9
  установки) и скопируйте токен в том заново.
- `invalid_grant` у Google — токен умер. Почти всегда причина — проект Google
  Cloud в статусе **Testing** (токен живёт 7 дней). Переведите в **In
  production**, войдите заново, скопируйте токен в том.
- `gaierror`, «Temporary failure in name resolution», таймауты SMTP — нет
  сети или VPN режет почтовые порты 465/587/993. Решение — Gmail API
  (`MAIL_TRANSPORT=auto` и вход в Google): он ходит по HTTPS.
- После копирования токена в том бот не может его обновить — забыли
  `chown 10001:10001`. Команда в шаге 9.3 установки.

### Telegram

- `AuthKeyDuplicatedError`, сессия разлогинилась — запускали `python -m
  jobhunter...` на компьютере одновременно с контейнерами. Войдите заново
  (`tg_login.py`), скопируйте сессию в том, и дальше — только
  `docker compose exec`.
- «Sorry, too many tries» на my.telegram.org или при входе — слишком много
  запросов кода. Подождите сутки. `tg_login.py` специально разделён на
  `send` и `finish`, чтобы не запрашивать код повторно.
- `PeerFlood`, аккаунт ограничен — см. [SAFETY.md](SAFETY.md). Бот сам
  останавливает холодную отправку, каждое утро опрашивает @SpamBot и сам
  возобновит, когда ограничение снимут. Ответы тем, кто написал первым,
  продолжают работать.

### Вакансий мало или нет

- `/reading` в боте — какие источники живы, где ошибки.
- `docker compose exec autopilot python -m jobhunter.ingest.boards --check`
  — какие борды отвечают сейчас.
- Все вакансии отсеиваются — посмотрите причины: панель → заявка →
  «почему». Частые: чужой стек (нет навыков в профиле), junior, офис не в
  вашем городе, `never_claim` в требованиях. Проверьте `skills` и `aliases`
  в профиле.
- Много `GATE_FAILED` — гейт находит в текстах то, чего нет в профиле. Панель
  → заявка покажет правило и слово. Обычно не хватает технологии в
  `lexicon/tech_terms.txt` или числа в `metrics`.

### Контейнер перезапускается по кругу

```bash
docker compose logs --tail 50 autopilot
```

- `SyntaxError`, `ImportError` после правки кода — исправьте и пересоберите:
  `docker compose build migrate && docker compose up -d`.
- `yaml` — ошибка в `profile.yaml`: `python validate_profile.py`.
- `unhealthy` без ошибок — автопилот завис (например, сеть оборвалась посреди
  запроса). Сторож перезапускает процесс сам в течение часа; быстрее —
  `docker compose restart autopilot`.

### profile.yaml стал папкой

Вы запустили `docker compose up` до того, как создали `profile.yaml`, и
Docker создал на его месте пустую папку. Остановите контейнеры, удалите папку
`profile.yaml`, выполните `cp profile.example.yaml profile.yaml`, заполните и
запустите снова.

### Изменения в коде не применились

Образ собирает **только** сервис `migrate`:

```bash
docker compose build migrate
docker compose up -d
```

`docker compose build autopilot` молча ничего не делает. Правка
`profile.yaml` пересборки не требует — достаточно
`docker compose restart autopilot bot`.

### «database is locked»

Два процесса пишут в одну базу: обычно ручной запуск на компьютере
параллельно контейнерам. Все ручные команды — через `docker compose exec`.

### Бот пропускает утро

Компьютер спал. Пропущенные шаги бот выполняет после пробуждения, но письма
уходят позже. Отключите сон на день (шаг 14 установки) и подключите внешнего
сторожа healthchecks.io — он напишет вам, если бот не работает.

### Проверить базу и восстановить из копии

```bash
docker compose exec autopilot python -m jobhunter.ops check
```

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify_backup.ps1
```

Восстановление (`python -m jobhunter.ops restore --source ... --target ...`)
пишет в **отдельный** файл после проверки целостности. Рабочую базу оно не
перезаписывает: замену делаете вы, остановив контейнеры.
