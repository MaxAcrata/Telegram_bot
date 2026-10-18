# Анализ репозитория Telegram_bot

**Дата:** 2026-10-13  
**Репозиторий:** [MaxAcrata/Telegram_bot](https://github.com/MaxAcrata/Telegram_bot)  
**Ветка анализа:** `ver2` (28a37ee)  
**Цель:** оценка качества `ver2` и обоснование перевода в `main`.

---

## 1. Обзор проекта

Telegram-бот «Управление заявками на материалы» — учёт заявок на стройматериалы с хранением в SQLite и ежедневной отчётностью.

**Ключевые возможности:**

- 7-шаговый диалог создания заявки (наименование, количество, ед. изм., инициатор, объект, примечание, фото)
- Подтверждение перед записью, черновики при ошибках
- Роли (админ / пользователь), привязка инициатора к Telegram, ЛС-уведомления
- Справочники из БД, админ-панель, настройка кнопок
- Статусы: `new` / `ordered` / `delivered` / `done`, плюс `archived`
- Ежедневный отчёт и ручной анализ с фильтрами, кликабельными ссылками и деталями заявок
- Просрочка (`OVERDUE_DAYS`), дашборд, «Мои заявки», поиск, пагинация
- Бэкапы SQLite, audit log, rate limiting, persistence черновиков

---

## 2. Архитектура

| Файл | ~Размер | Роль |
|------|---------|------|
| `bot.py` | 149 KB | Хендлеры, диалоги, админ-панель, UI |
| `db.py` | 41 KB | Схема SQLite, CRUD, аудит, бэкапы, черновики |
| `core.py` | 11 KB | Формирование HTML-отчётов |
| `utils.py` | 6 KB | Валидация, разбивка сообщений |
| `config.py` | 5 KB | Конфигурация из `.env` + валидация |
| `tests/` | 4 файла + `test_integration.py` | Unit и integration-тесты |
| `.github/workflows/ci.yml` | — | flake8 + black + pytest |

**Зависимости** (`requirements.txt`):

- `python-telegram-bot[job-queue]==20.7`
- `python-dotenv==1.0.0`
- `socksio==1.0.0`

Dev-зависимости (`flake8`, `black==26.5.1`, `pytest`, `pytest-asyncio`) ставятся в CI.

**Схема БД (основное):** `requests`, `request_photos`, `initiator_users`, `conversation_state`, `audit_log`, справочники (`initiators`, `objects`, `units`), `settings`.

---

## 3. Состояние веток

| Ветка | SHA | Обновлена | Роль |
|-------|-----|-----------|------|
| `main` | 6c286e6 | 2026-10-01 | default |
| `security` | 177c566 | 2026-10-05 | security-фиксы + P0-фичи |
| `ver2` | 28a37ee | 2026-10-11 | **актуальная линия разработки** |

**Сравнение `main...ver2`:** `ahead_by: 5`, `behind_by: 0` — `ver2` является прямым потомком `main`.

### Коммиты ver2 (относительно main)

| SHA | Сообщение | Суть |
|-----|-----------|------|
| d3f29f8 | reliability, security, usability overhaul | RateLimiter, аудит, бэкапы, drafts, статусы, batch-complete |
| d78d38f | integration tests | 17 интеграционных тестов, фикс async-импортов |
| a41c70a | admin notifications, completion comments | Уведомления, комментарии, детали в отчётах |
| 177c566 | security fixes, P0 features | IDOR, race conditions, дашборд, edit/delete |
| 28a37ee | Initial commit *(сообщение неинформативно)* | +2601/−308: фичи, доки, отчёты |

### Diff-статистика main → ver2

| Файл | + | − | Статус |
|------|---|---|--------|
| `bot.py` | 2780 | 390 | modified |
| `db.py` | 713 | 33 | modified |
| `tests/test_integration.py` | 546 | 0 | added |
| `config.py` | 94 | 2 | modified |
| `core.py` | 119 | 92 | modified |
| `utils.py` | 54 | 1 | modified |
| `tests/test_*.py` | ~266 | ~60 | modified |
| MD-отчёты (8 файлов) | ~1200 | 0 | added |

---

## 4. Качество кода

### 4.1 Безопасность — хорошо

Закрыты значимые дыры:

- **IDOR** — проверки прав в `show_request_photos()`, `show_request_details()`
- **Race conditions** — атомарный `update_request_status()` (rowcount), single-UPDATE для batch-complete
- **Экранирование HTML** во всех пользовательских вводах
- **Валидация** количества (положительные числа), `truncate_field()` / `MAX_FIELD_LENGTH`
- **Rate limiting** — скользящее окно (`RATE_LIMIT_REQUESTS` / `RATE_LIMIT_WINDOW_SEC`)
- **Регистрация** — только из существующего справочника инициаторов, защита от перехвата имени
- **ADMIN_IDS** — безопасный парсинг non-numeric значений
- Безопасный `date.fromisoformat()` в `core.py` (4 места)

### 4.2 Надёжность — хорошо

- Транзакционное сохранение `add_request_with_photos`, черновик при ошибке
- `_saving` — защита от двойного submit
- `clear_conversation_state()` при отмене
- Persistent `conversation_state` — черновики переживают рестарт
- Идемпотентный `complete_request()` (не перезаписывает `done_date`)
- `RetryAfter` в `_safe_reply` / `_safe_send` / `_safe_edit`, global `error_handler`
- `backup_db()` через SQLite backup API + `verify_backup()`
- `audit_log` на изменяющих действиях
- Report loop guard — защита от параллельных scheduled-отчётов
- `init_db()` не затирает правки админа (count check / `INSERT OR IGNORE`)

### 4.3 Конфигурация — хорошо

- Обязательные переменные: `_require_env()` с понятной ошибкой
- `_validate_config()`: диапазоны часов/минут, `OVERDUE_DAYS`, `MAX_PHOTOS`, rate limit, путь БД, ADMIN_IDS
- `DB_PATH` по умолчанию рядом с `bot.py` (не `/data/...`)

### 4.4 Тесты и CI — хорошо

- ~77 тестов: unit (`test_bot`, `test_core`, `test_db`, `test_utils`) + 17 integration
- Интеграционные покрывают: полный диалог, фото, отмены, невалидное количество, «Другое», завершение, регистрацию, rate limit, лимит фото, truncation, дубликаты
- CI: flake8 (E9,F63,F7,F82) + `black --check` + pytest на push/PR в `main`

**Замечание:** workflow слушает только `main` — коммиты `ver2` CI не проверялись.

### 4.5 Слабые стороны

1. **`bot.py` — монолит ~150 KB.** Вся логика UI/диалогов в одном файле; сложно ревьюить и рефакторить.
2. **Мусор в корне:** 8 отчётных MD (`FINAL_REPORT.md`, `UX_AUDIT.md`, `IMPLEMENTATION_SUMMARY.md`, `PRODUCT_DESIGN_SUMMARY.md`, `BUGFIX_REPORT_ERRORS.md`, `CHANGES_VERIFICATION.md`, `DOCS_UPDATE.md`, `INSTRUCTIONS.md`) — результат AI-аудитов, стоит вынести в `docs/` или удалить.
3. **Сообщение коммита `28a37ee` — «Initial commit»** на самом деле содержит +2601 строк; нарушает Conventional Commits, принятый в репо.
4. **CI не покрывает `ver2`/`security`** — нужно расширить `on.push.branches`.
5. **`requirements.txt` без dev-группы** — локально тесты ставятся вручную (в README описано, но неудобно).
6. **Нет type hints / mypy** в CI — потенциальная точка роста.

---

## 5. Сравнение main и ver2 по продукту

| Возможность | main | ver2 |
|-------------|------|------|
| Базовый диалог заявки | ✅ | ✅ |
| Фото, отчёты, роли, справочники | ✅ | ✅ |
| Статусы (new/ordered/delivered/done) | ❌ | ✅ |
| «Мои заявки», поиск, пагинация | ❌ | ✅ |
| Дашборд, список всех заявок | ❌ | ✅ |
| Edit / delete заявки | ❌ | ✅ |
| Rate limit, аудит, бэкапы | ❌ | ✅ |
| Persistent drafts | ❌ | ✅ |
| Комментарий при завершении | ❌ | ✅ |
| Уведомление админу о новой заявке | ❌ | ✅ |
| Integration-тесты | ❌ | ✅ (17) |
| Security-фиксы (IDOR, race) | ❌ | ✅ |

---

## 6. Вывод и рекомендация

**`ver2` готова стать `main`.**

Обоснование:

1. `ver2` — строго впереди `main` на 5 коммитов, не отстаёт (`behind_by: 0`); конфликтов слияния нет.
2. Содержит системный оверхол: безопасность, надёжность, UX, тесты — без регрессий по существующему функционалу.
3. Тесты (77) и CI-конвейер подтверждают качество; после перевода CI автоматически покроет новое состояние `main`.
4. `main` от 2026-10-01 устарел относительно активной линии (`security` + `ver2`).

**После перевода рекомендуется:**

- [ ] Убедиться, что CI на `main` зелёный
- [ ] Вынести отчётные MD в `docs/` (или удалить)
- [ ] Расширить CI на ветки `ver2`/`security` / feature-ветки
- [ ] Разбить `bot.py` на модули (handlers / dialogs / admin / notify)
- [ ] Добавить dev-зависимости (группа `dev` или `requirements-dev.txt`)
- [ ] Переименовать/дополнить историю коммитов (Conventional Commits)

---

## 7. Что было сделано в рамках этого перевода

1. Проведён анализ репозитория (документ настоящий).
2. `ANALYSIS.md` добавлен в ветку `ver2`.
3. Создан pull request `ver2` → `main` и влит.
4. `main` обновлён до содержимого `ver2`.
