# AGENTS.md

Instructions for AI agents working with this codebase.

## Project Overview

Telegram bot for managing construction material requests. Data lives in a local SQLite database (`bot.db`). Users submit requests through a 6-step conversation flow with optional photo upload via button; admins receive daily HTML reports, can view all requests with filters, change statuses, complete requests, view a dashboard, and manage reference lists.

## Architecture

| File | Responsibility | Depends on |
|---|---|---|
| `bot.py` | Telegram handlers, single text router (`route_text`/`route_non_text`), flow state machine (`_flow`/`_step`), keyboards, scheduled report, admin panel, registration, notifications, photo handling, rate limiting, persistent drafts, my requests, search, pagination, batch complete, requests list with filters, request card, edit/delete/cancel UI, completed-requests section, dashboard | `core`, `db`, `utils`, `config` |
| `config.py` | Loads `.env`, validates config at startup (ranges, paths, IDs), safe parsing of ADMIN_IDS | `dotenv` |
| `core.py` | Report generation: HTML formatting, explicit status labels, overdue analysis, active/completed modes, clickable links, photo count labels, request IDs. Dashboard generation (`build_dashboard()`) | `db`, `config` |
| `db.py` | SQLite database: schema, migrations (status column, UNIQUE telegram_id), CRUD for requests, lists, settings, initiator-user links, photo storage, audit log, backup API, persistent conversation state, delete, search by status, dashboard counts, ownership check | `sqlite3`, `config` |
| `utils.py` | Quantity validation, field truncation, `is_meaningful_text()` (reject empty/emoji-only input), rate limiter with auto-cleanup, HTML-safe message splitting | (standalone) |

Data flow: `bot.py` → `db.py` → SQLite → `core.py` → HTML report / dashboard

## Key Conventions

- **Language**: All user-facing strings are in Russian. Variable names and docstrings in English.
- **Config**: All secrets and environment-dependent values in `.env`. `config.py` defines `_validate_config()` which checks ranges and paths — called explicitly from `bot.py main()` (not at import time, so tests can import without `.env`). `ADMIN_IDS` parsing uses try/except — non-numeric values are skipped with a warning, not a crash.
- **Database**: SQLite via `sqlite3` (stdlib). Schema auto-created by `db.init_db()` with automatic migrations. Tables:
  - `requests` — заявки (with `status`: `new`/`ordered`/`delivered`/`done`/`cancelled`/`archived`)
  - `request_photos` — фото к заявкам (связь 1:N с `requests`)
  - `lists` — справочники (`list_type`: `initiator`, `object`, `unit`)
  - `settings` — настройки (кнопок в строке и т.д.)
  - `initiator_users` — привязка инициаторов к Telegram-ID (UNIQUE на `telegram_id`)
  - `audit_log` — журнал действий (created/completed/cancelled/edited/status_changed/deleted/…)
  - `conversation_state` — сохранённые черновики диалогов (persistent across restarts)
- **State model (вместо ConversationHandler)**: текстовые сценарии живут в `context.user_data` как `_flow` (`create`/`register`/`complete`/`cancel_request`/`my_search`/`edit`/`admin`) + `_step`. Допустимые шаги — `_FLOW_STEPS`, шаги, ожидающие текста (с ними работает тайм-аут) — `_TEXT_FLOW_STEPS`. Установка через `_set_flow_step()` (чужой сценарий безопасно прерывается; персистентный черновик сохраняется), результаты обработчиков переносятся через `_apply_step_result()`/`_flow_result` (эндпоинты `ConversationHandler.END` = конец сценария, int = новый шаг, None = без изменений).
- **Routing**: `route_text` регистрируется ПЕРВЫМ среди текстовых хендлеров: сначала кнопки главного меню (`_menu_label()` нормализует U+FE0F) → `_abort_flow()` + открытие раздела с первого нажатия; затем активный `_flow`; иначе — нейтральная подсказка. `route_non_text` обрабатывает фото/файлы (шаг PHOTO в `create`). ConversationHandler не инстанцируется; `ConversationHandler.END` используется только как константа-сентинел `-1`.
- **Timeouts**: `_arm_timeout()` планирует `job_queue.run_once(on_flow_timeout, CONVERSATION_TIMEOUT_SEC, …)` с уникальным `_flow_token`; устаревший job (токен не совпал) — no-op; тайм-аут чистит форму из памяти, сохраняет `conversation_state` и уведомляет через `_safe_send`.
- **Drafts (двухуровневые)**: персистентный черновик — таблица `conversation_state`; текущая форма — `user_data` (`_CREATE_MEMORY_KEYS`). `/start` и тайм-аут удаляют форму из памяти, НЕ трогая `conversation_state`; явная «❌ Отмена» внутри создания чистит оба уровня; `_saving` удаляется всегда (finally/abort/start/timeout).
- **HTML in Telegram**: Reports use Telegram HTML parse mode. `split_message()` in `utils.py` splits long messages on `\n` boundaries, closes unclosed HTML tags per chunk, and preserves tag attributes (e.g. `<a href>`).
- **Clickable links**: `make_links_clickable()` in `core.py` converts URLs in notes to `<a href>` tags. `disable_web_page_preview=True` on all report messages.
- **Report contract**: `build_report(save_to_file=True, mode="active"|"completed")` returns a **4-tuple** `(text, ids_with_photos, all_ids, requests_data)`. All callers must unpack 4 values (including `scheduled_report`).
- **Photos**: Stored as Telegram `file_id` in `request_photos` table. Displayed as `📎 Фото: N шт.` in reports. Viewing via inline buttons — access restricted to admins and request owners (IDOR protection).
- **SOCKS5 proxy**: Optional, configured via `SOCKS5_PROXY` env var.
- **CI/CD**: GitHub Actions runs flake8, black (`==26.5.1`), and pytest on every push/PR to `main`.

## Handler registration (`register_handlers(app)` in `bot.py`)

All handlers are registered by `register_handlers(app)` (called from `main()`) — the function is extracted specifically so tests can build an Application offline and assert registry invariants. Order within the single group (critical):

1. `CommandHandler("start", start)` / `CommandHandler("cancel", cancel)` / `CommandHandler("status", status_start)`
2. `MessageHandler(TEXT & ~COMMAND, route_text)` — **главный роутер** (меню → сценарий → подсказка)
3. `MessageHandler(~TEXT & ~COMMAND, route_non_text)` — фото/файлы
4. Anchored `CallbackQueryHandler`s (each wrapped in `_flow_result` to sync return values into `_flow/_step`):

| Prefix | Purpose |
|---|---|
| `cr:`/`crp:`/`crb:`/`crnoop`/`crdone:` | завершение (список/комментарий/подтверждение/батч/из карточки) |
| `reqcx*` | отмена заявки (ввод комментария/подтверждение) |
| `cdp:`/`cd:{page}:{id}`/`cdb:`/`cdnoop` | раздел «Выполненные заявки» |
| `ss:`/`ssp:`/`ssc:` | смена статуса (/status) |
| `mrp:`/`mrnoop`/`mrsearch`/`mrrefresh` | мои заявки |
| `reqf:`/`reqp:`/`reqnoop`/`rc:` | список заявок + фильтры |
| `reqst|reqssc|reqsscancel|reqe|editf|editcancel|reqdel|reqdelyes|reqdelno|reqback` | карточка заявки (составной паттерн) |
| `editnext|editfinish|editprev` | навигация редактирования (отдельно от составного) |
| `list:`/`add`/`rename:`/`del:`/`set_bpr`/`bpr:`/`confirm_del:`/`clear_db`/`backup`/`audit`/`buttons`/`back_main` | админ-панель (back_main/list: — в `admin_menu_handler`) |
| `report:(active\|completed\|cancelled)` | анализ (cancelled → список-секция с фильтром) |
| `reqs:all:{page}` | дашборд → список заявок |
| `photos:`/`detail:` | stateless-просмотр (IDOR), не трогает `_flow` |

Invariant tests (`tests/test_navigation.py`): no `ConversationHandler` instances registered, `route_text` is the first text handler, callback patterns are unique, `PTBUserWarning` is never raised at registration.

**Important**: сценарий, начинающийся из callback, обязан сначала вызвать `_set_flow_step()` (переключение/abort), и только потом писать ключи в `user_data` — иначе abort чужого сценария сотрёт свежие данные.

## Notifications

When a request is **created**:
1. User confirmation with request details
2. Shared chat (`TELEGRAM_CHAT_ID`) with inline button `📋 Открыть заявку #N`

When a request is **completed** (single):
1. Admin inline message with inline button `📋 Открыть заявку #N`
2. Shared chat (`TELEGRAM_CHAT_ID`) with inline button
3. Initiator personal message (if linked via `initiator_users` table) with inline button

When requests are **batch-completed** (all on page):
1. Admin inline message with count and actual IDs
2. Shared chat (`TELEGRAM_CHAT_ID`) with summary notification

When a request is **cancelled** (admin, from the card):
1. `cancel_request()` writes audit action `cancelled` (with optional comment)
2. Linked initiator (via `initiator_users`) receives a personal message including the comment

Photos are NOT included in notifications. Daily `scheduled_report` and manual analysis attach `_build_photo_keyboard()` (detail buttons `detail:<id>` with status emoji).

## Running

```bash
# Activate venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # Linux/Mac

# Run bot (creates bot.db automatically)
python bot.py

# Run tests
python -m pytest tests/ -v

# Lint and format
flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
black --check --diff .
```

## Testing

- **Framework**: pytest + pytest-asyncio
- **Test runner**: `python -m pytest tests/ -v` (must use venv Python; на Windows со сломанным junction в `%TEMP%\pytest-of-*` добавляйте `--basetemp=<свежий путь>`)
- **DB tests**: `tests/test_db.py` uses a `tmp_path` fixture with `patch.object(db, "DB_PATH", ...)` — includes cancel semantics, old-schema migration, backup-guard, WAL and concurrency tests.
- **Core tests**: `tests/test_core.py` mocks `db.get_active_requests` and `db.get_photo_counts`.
- **Bot tests**: `tests/test_bot.py` mocks DB functions at the `bot.` namespace; covers card buttons, complete-from-card, cancel flow, completed section, menu keyboard.
- **Routing/navigation**: `tests/test_navigation.py` — menu from COMPLETE_SELECT, `/start`, timeouts, stale callbacks, registry invariants (no ConversationHandler, unique patterns, no PTBUserWarning).
- **Network**: `tests/test_network.py` — TelegramCallResult retry policy, TimedOut classification, RetryAfter, BadRequest, logging of cause.
- **E2E**: `tests/test_e2e_routing.py` drives `Application.process_update` with real handler registration (synthetic commands include `bot_command` entities; `get_me` is mocked and `bot._bot_user` set manually).
- **Integration**: `tests/test_integration.py` — full conversation flows (create, complete, register, cancel, rate limit, limits).

When adding new tests:
- Async bot handlers: use `@pytest.mark.asyncio`, mock `update.message.reply_text` with `AsyncMock()`, mock `context.user_data` as a plain dict; for `job_queue.run_once` a `MagicMock` suffices.
- DB functions: use the `tmp_path` fixture pattern from `test_db.py`.
- Text/validation helpers: see `tests/test_utils.py`.

## Common Pitfalls

- **Import-time side effects**: `config.py` calls `_require_env()` at import time for mandatory vars. `_validate_config()` is called explicitly from `bot.py main()`. Tests set env vars in `conftest.py` before any imports.
- **SQLite file locking**: Only one process should write to `bot.db` at a time. `get_conn()` sets `foreign_keys=ON` and `busy_timeout=5000` per connection; `journal_mode=WAL` is set **once in `init_db()`** (not per read).
- **SQLite placeholder limit**: `SQLITE_MAX_VARIABLE_NUMBER = 999`. Use batch queries (see `get_photo_counts()` for example).
- **Cancel semantics**: «❌ Отмена»/`/cancel` обрабатываются централизованно в `route_text`/`cancel`. Внутри создания (`_flow == "create"`) очищаются форма И `conversation_state`; вне создания — только временные ключи, `conversation_state` не трогается. Не добавляйте `user_data.clear()`.
- **Menu labels**: кнопки меню сравниваются через `_menu_label()`/`_normalize_text()` (срезает U+FE0F) — Telegram может доставить `⚙️` без variation selector. Новый пункт меню добавляется ТРИжды: клавиатура `get_main_keyboard`, множество `_MENU_BUTTONS`, диспетчер `_dispatch_menu`.
- **Callback entry ordering**: сценарий, начинающийся из callback (`cr:`, `reqcx`, `reqcxnc`, `reqe`, `editf`, `mrsearch`, админ `add`/`rename:`), сначала вызывает `_set_flow_step()` (переключение abort-ит чужой сценарий), потом пишет ключи — иначе abort сотрёт свежие данные.
- **except-порядок сетевых ошибок**: `RetryAfter → TimedOut → BadRequest → NetworkError → TelegramError` — в PTB 20.7 `BadRequest` и `TimedOut` являются наследниками `NetworkError`, и `NetworkError`-ветка первой сделала бы ветку `BadRequest` недостижимой. `_safe_*` возвращают `TelegramCallResult(success, delivery_uncertain, value, error)`; для send read/write-timeout повтор запрещён (риск дублей).
- **e2e-тесты с командами**: `CommandHandler`/`filters.COMMAND` требуют entity `bot_command` — синтетическим `/start` добавляйте entities (см. `tests/test_e2e_routing.py`), иначе команда уходит в `route_text`.
- **PowerShell syntax**: On Windows, use `;` not `&&` to chain shell commands. Правки файлов через `Add-Content` ломают кириллицу (кодировка по умолчанию) — используйте UTF-8.
- **New tables need fallback**: Functions accessing `settings` and `initiator_users` tables must use try/except to handle cases where the table doesn't exist yet (e.g., in tests with temporary DBs). See `get_setting()` and `get_initiator_by_tg_id()` for examples.
- **callback_data uses numeric IDs**: List items in admin panel use numeric database IDs (`rename:42`, `del:42`, `confirm_del:42`) instead of truncated text values. This guarantees Telegram's 64-byte limit is never exceeded. Do NOT revert to text-based callbacks.
- **Black formatting**: All Python files must pass `black --check`. Pin version in CI (`black==26.5.1`).

## Files to Touch Together

- Changing flow/navigation → update `bot.py` (`_FLOW_STEPS`/`_TEXT_FLOW_STEPS`/`_dispatch_menu`/`register_handlers`), `tests/test_bot.py`, `tests/test_navigation.py`, `tests/test_e2e_routing.py`
- Changing report format → update `core.py` and `tests/test_core.py`
- Changing DB schema → update `db.py` (schema + CRUD + migrations) and `tests/test_db.py`
- Adding a new env var → add to `config.py` (incl. `_validate_config`), update `.env.example`, update README.md
- Changing default names or lists → update `_DEFAULT_LISTS` in `db.py` and fallbacks in `bot.py`
- Changing CI workflow → update `.github/workflows/ci.yml`
- Adding a new menu button → `get_main_keyboard` + `_MENU_BUTTONS` + `_dispatch_menu` + navigation tests
- Adding a new callback prefix → anchored pattern in `register_handlers` + uniqueness test in `tests/test_navigation.py`

## Security Notes

### Secrets and configuration
- `.env` is in `.gitignore` — never commit it.
- `bot.db` is in `.gitignore` — it contains operational data, not code.
- Telegram bot token gives full control over the bot. If exposed, revoke via @BotFather immediately.
- `ADMIN_IDS` controls who can trigger analysis and manage settings. Non-numeric values are safely skipped at parse time. Validate before adding new admin-only features.
- `initiator_users` table stores Telegram user IDs — treat as personal data.

### Authorization
- Every admin/complete handler re-checks `is_admin()` as defense-in-depth (not just the entry point).
- `is_admin()` uses `update.effective_user.id` from Telegram server — cannot be spoofed via `callback_data`.
- Registration checks `get_initiator_tg_id()` before linking — prevents hijacking an already-linked name. `link_initiator()` does NOT overwrite existing bindings (INSERT only, no UPSERT).
- **IDOR protection**: `show_request_photos()` and `show_request_details()` verify `is_admin() or is_request_owner()` before returning data.
- **Ownership check**: `is_request_owner(request_id, telegram_id)` in `db.py` verifies the user is the linked initiator of the request.

### Race conditions
- `update_request_status()` syncs `done_date` with status transitions: sets `today` when status changes to `done`, clears `done_date` when reopening from `done` to `new`/`ordered`/`delivered`.
- `complete_request()` uses conditional `UPDATE ... WHERE id=? AND done_date IS NULL AND status NOT IN ('cancelled','archived')` with `rowcount` check to prevent double-completion by concurrent writers.
- `complete_requests_batch()` uses a single `UPDATE ... WHERE id IN (...) AND done_date IS NULL AND status NOT IN ('cancelled','archived')` — atomic across all IDs.
- `cancel_request()` is the ONLY way to set `status='cancelled'`: conditional `UPDATE ... WHERE done_date IS NULL AND status IN ('new','ordered','delivered')`; on `rowcount==0` the row is re-read (already-cancelled → idempotent without a second audit; done/archived → rejected). `update_request_status()` refuses transitions to/from `cancelled`.

### HTML injection prevention
- **All user-supplied fields** (`name`, `unit`, `initiator`, `object`, `notes`, `username`, DB list values) must be escaped with `html.escape()` before embedding in Telegram HTML messages (`parse_mode='HTML'`).
- This applies in both `bot.py` (user messages) and `core.py` (reports).
- Telegram's HTML parser supports `<a href>` — unescaped user input can inject phishing links.

### Input validation
- `validate_quantity()` rejects negative, zero, `inf`, and `NaN` values (`math.isfinite` check). Commas are normalized to dots before saving.
- `is_meaningful_text()` rejects empty/whitespace/single-char/emoji-only names. Non-text (GIF, sticker, document, voice) is rejected in `route_non_text`/`handle_non_text_hint` on each dialog step — do not rely on `filters.TEXT` alone for user feedback.
- `bpr` setting is range-checked (1–6).
- `callback_data` values use numeric list IDs (not truncated text) to fit Telegram's 64-byte limit.
- `date.fromisoformat()` calls in `core.py` are wrapped in try/except to handle corrupt dates without crashing reports.
- `_validate_config()` checks: `RATE_LIMIT_REQUESTS >= 1`, `RATE_LIMIT_WINDOW_SEC >= 1`, `MAX_FIELD_LENGTH >= 1`, `MAX_PHOTOS_PER_REQUEST` 1–10, `REPORT_HOUR` 0–23, `REPORT_MINUTE` 0–59, `OVERDUE_DAYS >= 1`, `CONVERSATION_TIMEOUT_SEC >= 30`, `HTTP_CONNECT/READ/WRITE/POOL_TIMEOUT >= 1`, `HTTP_GETUPDATES_READ_TIMEOUT > 10` (long-poll), `HTTP_API_POOL_SIZE >= 32` (если задана).

### Error handling
- Exception messages are never shown to users — only generic messages. Full details go to `logger.error()` or `logger.warning()`.
- Proxy URL credentials are masked before logging. HTTPX/HTTPcore loggers are set to WARNING level to prevent token leakage in INFO logs.
- `_safe_reply/_safe_send/_safe_edit/_safe_answer` возвращают `TelegramCallResult(success, delivery_uncertain, value, error)`; обработчики продвигают `_step` только при успешной доставке вопроса, а после SQL-коммита сетевая ошибка уведомления НЕ повторяет SQL и НЕ откатывает данные (идемпотентность `confirm_request`/`complete_request`/`cancel_request`).
- `context.user_data.clear()` is NOT called on save errors — user can retry with `🔄 Повторить`.
- Логи операций (`_log_op`) содержат op/user_id/chat_id/attempt/duration/state_before/state_after и класс первопричины `TimedOut` (cause), НЕ содержат токен, пароль прокси и тексты комментариев.

### Rate limiting
- `RateLimiter` in `utils.py` implements per-user sliding window (default: 5 requests / 60 sec).
- Auto-cleanup of stale user entries every 100 calls to prevent memory leaks.
- Configurable via `RATE_LIMIT_REQUESTS` and `RATE_LIMIT_WINDOW_SEC` env vars.
- Applied to `/start` and `➕ Добавить заявку` entry points.
- `context.user_data["_saving"]` flag prevents double-submit on confirm.

### Backup and audit
- `backup_db()` creates a copy via SQLite backup API with UUID-enriched filenames to prevent collisions. Connections are closed in `finally` blocks.
- `verify_backup()` opens the file in `mode=ro` (read-only), checks `PRAGMA integrity_check` AND verifies that key application tables (`requests`, `lists`, `request_photos`) exist. Rejects empty/corrupt databases.
- `clear_all_requests()` **aborts if `backup_db()` returns `None`** (backup creation failed). Only proceeds after successful backup + verification.
- `audit_log` table records all significant actions with user_id, action type, and details (created, completed, cancelled, edited, status_changed, deleted, batch_completed, archived, db_cleared, registered, list_added, list_deleted, list_renamed).
- Admin panel includes `💾 Резервная копия` and `📋 Журнал действий` sections.

### Persistent state
- `conversation_state` table stores `user_data` as JSON at each create-flow step (`_save_draft()`; underscore-prefixed keys — `_flow`, `_step`, `_flow_token`, `_timeout_job`, `_saving` — are never serialized).
- `/start` and conversation timeout remove the in-memory form (`_CREATE_MEMORY_KEYS` + `_FLOW_TEMP_KEYS`) but keep `conversation_state` (draft resumable).
- Explicit `❌ Отмена` inside creation clears both levels; cancel/abort of any OTHER scenario never touches `conversation_state`.
- After a successful save `clear_conversation_state()` runs; `_saving` is cleared in `finally`, on `/start`, abort and timeout.

### DB migrations
- `init_db()` order: diagnostic check `_needs_status_migration()` → (existing DB needing migration) `backup_db()` + `verify_backup()` — **stops with RuntimeError if backup fails** → open connection → `PRAGMA journal_mode=WAL` → `_ensure_status_column()` **before** `executescript(_SCHEMA)` (indexes reference `status`) → schema/seed (idempotent; repeated `init_db()` is safe). Fresh DBs skip the backup.
- Status column migration: `ALTER TABLE requests ADD COLUMN status TEXT DEFAULT 'new'` (only if column doesn't exist). `status` is plain TEXT without CHECK — new values like `cancelled` need no schema change.
- Unique index on `telegram_id`: `CREATE UNIQUE INDEX IF NOT EXISTS idx_initiator_tg_id ON initiator_users(telegram_id)`.
- `archive_completed_requests()` uses `date('now', '-' || N || ' days')` for correct date arithmetic. Scheduled as a daily job at 03:00 (`archive_old_requests_job`, лог start/finish, вызов через `asyncio.to_thread`).