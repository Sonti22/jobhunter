# Профиль кандидата: profile.yaml

Профиль — единственный источник правды о вас. Генератор резюме и писем может
только **выбирать, переставлять и переформулировать** то, что в нём записано.
Добавить факт он не может: гейт правды (`jobhunter/tailor/gate.py`)
отклоняет любой текст, где встретилась технология, число, компания или стаж,
которых в профиле нет.

Отсюда простое правило: **чем подробнее и честнее профиль, тем лучше письма.**
Навык, которого нет в профиле, бот не упомянет даже там, где вакансия прямо
его просит.

Начните с копии примера — он заполнен за вымышленного backend-разработчика и
показывает все разделы в деле:

```bash
cp profile.example.yaml profile.yaml
python validate_profile.py          # после каждой правки
```

После правки профиля на работающем боте: `docker compose restart autopilot bot`.

---

## meta

```yaml
meta:
  version: 1
  source_document: resume.pdf       # из какого резюме собран профиль — для себя
  updated_at: '2026-09-30'
  timeline_start: 2019-01           # с какого месяца считать общий стаж
```

`timeline_start` определяет, сколько лет опыта бот вправе заявить.

## identity — кто вы

```yaml
identity:
  full_name_ru: Смирнов Алексей Викторович
  full_name_en: Alexey Smirnov
  location: Москва, Россия
  relocation: false                 # готовы ли к переезду
  business_trips: true
  work_formats: [remote, hybrid_moscow, office_moscow]
  phone: +7 (900) 000-00-00
  email: alexey.smirnov@example.com
  telegram: https://t.me/example_candidate
  headline_variants:
  - Senior Backend Engineer (Python)
  - Tech Lead / Software Architect
```

- **Контакты** попадают в шапку резюме. Пустое поле просто не выводится.
- **work_formats** — важна часть до подчёркивания: `remote`, `hybrid`,
  `office`. Суффикс (`_moscow`) — для вас, бот его не читает.
- **Как бот решает про офис:**
  - только `remote` в списке → любые офисные вакансии отсеиваются;
  - `relocation: true` → подходит офис в любом городе;
  - иначе → офис подходит, только если в вакансии ваш город (см.
    `outreach.office_city_patterns`) и не требуется переезд.
- **headline_variants** — заголовки резюме. Генератор выбирает **только** из
  этого списка под роль вакансии. Пишите лишь те, что можете защитить.

## languages

```yaml
languages:
- {code: ru, name_ru: Русский, name_en: Russian, level: C2, note: родной}
- {code: en, name_ru: Английский, name_en: English, level: B2}
```

Уровень английского (`code: en`) бот называет в письмах, если вы так
напишете в `outreach` (см. ниже).

## education

```yaml
education:
- id: edu_1
  institution_ru: ...
  institution_en: ...
  degree_ru: Высшее
  field_ru: Прикладная информатика
  year: 2019
```

## skills — навыки

```yaml
skills:
- id: python
  canonical: Python
  aliases: [python3, py]
  level: expert
  years: 7
  evidence_ids: [exp_platform, exp_gamedev]
```

| Поле | Смысл |
|---|---|
| `id` | внутренний ключ, латиницей |
| `canonical` | как навык пишется в резюме |
| `aliases` | как его пишут в вакансиях: `k8s` для Kubernetes, `postgres` для PostgreSQL |
| `level` | `expert` — годы ежедневной работы, защитите на интервью; `working` — уверенно решали реальные задачи; `familiar` — трогали (нельзя в заголовок и рядом со словами «глубокий», «экспертный»); `none` — не использовали: упоминание = отказ гейта |
| `years` | не больше суммарного стажа мест, где навык был (проверяет `validate_profile.py`) |
| `evidence_ids` | `id` мест работы из `experience`, где навык применялся |

**Словарь технологий.** Гейт узнаёт технологии по `lexicon/tech_terms.txt`.
Если нужной технологии там нет, добавьте строку (в нижнем регистре). Иначе
бот не сможет её упомянуть, а в вакансиях не найдёт.

## experience — опыт

```yaml
experience:
- id: exp_platform
  company: Nordlane
  role_ru: Tech Lead / Software Architect
  role_en: Tech Lead / Software Architect
  start: 2025-09
  end: null                         # null — по настоящее время
  bullets:
  - id: b_plt_5
    text_ru: Настраивал observability: requests/sec, error rate, p50/p95/p99...
    text_en: Set up observability: requests/sec, error rate, p50/p95/p99...
    skills: [prometheus, grafana]
    metrics: []
```

- Каждый **bullet** — факт, который бот может процитировать в резюме или
  письме. Пишите конкретно: что сделали, какими средствами, с каким итогом.
- `skills` — какие навыки пункт подтверждает (id из раздела `skills`).
- **`metrics`** — числа, которые разрешено называть. Любое другое число в
  тексте гейт отклонит:

  ```yaml
  metrics:
  - value: '2'
    unit: x
    claim: p95 сократилась почти в два раза
  ```

- `is_own_project: true` — свой проект, а не работа по найму; бот честно
  так его и называет.

## never_claim — чего у вас нет

```yaml
never_claim:
- id: csharp
  canonical: C#
  aliases: [c sharp, csharp]
```

Технологии, которые часто просят, но вы с ними не работали. Упоминание в любом
тексте — жёсткий отказ гейта, даже если вакансия требует. Побеждает `skills`
при конфликте.

## claims — сводные цифры

```yaml
claims:
  total_years_software: 7
  years_hands_on_engineer: 3.5
```

`total_years_software` — общий стаж. Эти числа гейт разрешает называть.

## faq — ваши готовые ответы

```yaml
faq:
- id: about_me
  answer_ru: Backend-разработчик на Python, больше шести лет...
  answer_en: Python backend engineer with more than six years...
```

На типовые вопросы рекрутёров бот отвечает **дословно** этими текстами, без
нейросети. Известные темы:

| id | На какой вопрос |
|---|---|
| `about_me` | «Расскажите о себе» |
| `last_project` | «Расскажите о последнем проекте» |
| `location_and_format` | «Где вы находитесь, какой формат подходит?» |
| `start_date` | «Когда можете выйти?» |
| `salary` | «Какие ожидания по зарплате?» |
| `payments_experience` | «Есть опыт с платежами?» (пример узкой темы) |

Вопрос, которого нет в `faq`, бот не угадывает — присылает вам карточку.
`answer_en` можно не заполнять: тогда уйдёт русский вариант.

## salary_expectation

```yaml
salary_expectation: ""
```

Пусто — бот никогда не называет цифр и спрашивает бюджет работодателя.

## outreach — что бот пишет о вас от первого лица

Все фразы о вас, которые уходят работодателям, живут здесь, а не в коде.

| Ключ | Что это | Пример |
|---|---|---|
| `subject_direct_ru/_en` | тема письма человеку; `{about}` = «роль в компании» | `{about} — Алексей Смирнов (backend / tech lead, 7+ лет)` |
| `subject_apply_ru/_en` | тема отклика; `{role}` = роль | `Отклик: {role} — Смирнов Алексей (7+ лет, backend/tech lead)` |
| `intros_ru/_en` | вступления для Senior-вакансий (бот чередует) | `7+ лет в разработке, вырос из backend-инженера в Tech Lead.` |
| `intros_short_ru/_en` | короткие — когда письмо не влезает в 400 знаков | `7 лет в разработке, сейчас Tech Lead.` |
| `intros_middle_ru/_en` | для Middle-вакансий: без стажа и «Tech Lead», чтобы не выглядеть переквалифицированным | `Backend-разработчик: Python, FastAPI, PostgreSQL.` |
| `interest_ru/_en` | письмо компании без открытой роли; `{company}` | `Мне интересна бэкенд-разработка в {company}...` |
| `format_line_ru/_en` | строка о формате работы в **каждом** письме | `Формат — удалённо, гибрид или офис в Москве; английский C2.` |
| `letter_tail_ru/_en` | последняя строка прямого письма | `Резюме во вложении. Работаю удалённо...` |
| `format_rule_llm_ru/_en` | формат своими словами — инструкция для нейросети | `удалённо, гибрид или офис в Москве (к переезду не готов)` |
| `format_replies_ru/_en` | ответ на «где вы / готовы ли к офису?» | `Живу в Москве: подходит удалённый формат...` |
| `office_city_patterns` | где подходит офис (регулярные выражения, без учёта регистра) | `['москв\w*', '\bmoscow\b']` |
| `never_say` | слова, которых не должно быть в автоответах | `{pattern: 'финтех', why: '...'}` |
| `cv_file_prefix` | начало имени файла резюме | `Smirnov` → `Smirnov_Python_ab12.pdf` |
| `city_ru`, `location_ru/_en` | город и строка локации для резюме | `Москва`, `Москва, Россия` |

Советы:

- Держите вступления короткими: всё письмо — 200–400 знаков.
- Несколько вариантов в каждом наборе обязательны: одинаковые письма разным
  рекрутёрам выглядят как рассылка, и бот следит, чтобы похожесть писем была
  ниже порога.
- Числа во вступлениях («7+ лет») должны следовать из `claims` и опыта —
  иначе гейт отклонит письмо. `tests/test_profile_real.py` проверяет это на
  вашем профиле: `python -m pytest tests/test_profile_real.py -q`.
- Нет раздела `outreach` — бот соберёт нейтральные фразы из `identity` и
  `claims` («7+ лет в разработке.»). Работать будет, но скучно.

## Проверка

```bash
python validate_profile.py
python -m pytest tests/test_profile_real.py -q
```

Первая команда ищет битые ссылки, завышенные годы и метрики без описания,
вторая — прогоняет ваши тексты из `outreach` через гейт правды.
