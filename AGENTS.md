# AGENTS.md

Instructions for AI agents working with this codebase.

## Project Overview

Telegram bot for managing construction material requests. Data lives in a local SQLite database (`bot.db`). Users submit requests through a 7-step conversation flow (including optional photo upload); admins receive daily HTML reports, can view all requests with filters, edit/delete requests, change statuses, complete requests, view a dashboard, and manage reference lists.

## Architecture

| File | Responsibility | Depends on |
|---|---|---|
| `bot.py` | Telegram handlers, ConversationHandlers, keyboards, scheduled report, admin panel, registration, notifications, photo handling, rate limiting, persistent drafts, my requests, search, pagination, batch complete, requests list with filters, request card, edit/delete UI, dashboard | `core`, `db`, `utils`, `config` |
| `config.py` | Loads `.env`, validates config at startup (ranges, paths, IDs), safe parsing of ADMIN_IDS | `dotenv` |
| `core.py` | Report generation: HTML formatting, explicit status labels, overdue analysis, active/completed modes, clickable links, photo count labels, request IDs. Dashboard generation (`build_dashboard()`) | `db`, `config` |
| `db.py` | SQLite database: schema, migrations (status column, UNIQUE telegram_id), CRUD for requests, lists, settings, initiator-user links, photo storage, audit log, backup API, persistent conversation state, delete, search by status, dashboard counts, ownership check | `sqlite3`, `config` |
| `utils.py` | Quantity validation, field truncation, rate limiter with auto-cleanup, HTML-safe message splitting | (standalone) |

Data flow: `bot.py` → `db.py` → SQLite → `core.py` → HTML report / dashboard

## Key Conventions

- **Language**: All user-facing strings are in Russian. Variable names and docstrings in English.
- **Config**: All secrets and environment-dependent values in `.env`. `config.py` defines `_validate_config()` which checks ranges and paths — called explicitly from `bot.py main()` (not at import time, so tests can import without `.env`). `ADMIN_IDS` parsing uses try/except — non-numeric values are skipped with a warning, not a crash.
- **Database**: SQLite via `sqlite3` (stdlib). Schema auto-created by `db.init_db()` with automatic migrations. Tables:
  - `requests` — заявки (with `status`: `new`/`ordered`/`delivered`/`done`/`archived`)
  - `request_photos` — фото к заявкам (связь 1:N с `requests`)
  - `lists` — справочники (`list_type`: `initiator`, `object`, `unit`)
  - `settings` — настройки (кнопок в строке и т.д.)
  - `initiator_users` — привязка инициаторов к Telegram-ID (UNIQUE на `telegram_id`)
  - `audit_log` — журнал действий (created/completed/edited/status_changed/deleted)
  - `conversation_state` — сохранённые черновики диалогов (persistent across restarts)
- **Conversation states (заявки)**: `NAME → QUANTITY → UNIT → INITIATOR (auto-skip if linked) → INITIATOR_CUSTOM → OBJECT → OBJECT_CUSTOM → NOTES → PHOTO → CONFIRM`. PHOTO accepts photos, `✅ Готово`, `➡️ Пропустить`, or `❌ Отмена`. CONFIRM accepts `✅ Подтвердить`, `✏️ Исправить` (returns to NAME with data preserved), `🔄 Повторить`, or `❌ Отмена`.
- **Conversation states (регистрация)**: `REG_NAME` — non-admin users pick their initiator name on first `/start`.
- **Conversation states (завершение заявки)**: `COMPLETE_SELECT → COMPLETE_COMMENT → COMPLETE_CONFIRM` — admin selects active request, optionally adds comment, then confirms.
- **Conversation states (анализ)**: filter choice `active` / `completed` via inline buttons. Report includes inline buttons for viewing attached photos and request details.
- **Conversation states (заявки — список)**: `REQUESTS_LIST → REQUESTS_CARD` — admin views all requests with status filters and pagination. Card shows actions: change status, view photos, edit, delete.
- **Conversation states (редактирование)**: `EDIT_INPUT_VALUE` — admin selects field to edit, then enters new value.
- **Conversation states (админ-панель)**: `ADMIN_MENU → ADMIN_LIST_VIEW → ADMIN_RENAME_NEW / ADMIN_DELETE / ADMIN_ADD_VALUE / ADMIN_BUTTONS_EDIT / ADMIN_CLEAR_DB_CONFIRM` — manage reference lists, backup, audit log, clear DB.
- **HTML in Telegram**: Reports use Telegram HTML parse mode. `split_message()` in `utils.py` splits long messages on `\n` boundaries, closes unclosed HTML tags per chunk, and preserves tag attributes (e.g. `<a href>`).
- **Clickable links**: `make_links_clickable()` in `core.py` converts URLs in notes to `<a href>` tags. `disable_web_page_preview=True` on all report messages.
- **Photos**: Stored as Telegram `file_id` in `request_photos` table. Displayed as `📎 Фото: N шт.` in reports. Viewing via inline buttons — access restricted to admins and request owners (IDOR protection).
- **SOCKS5 proxy**: Optional, configured via `SOCKS5_PROXY` env var.
- **CI/CD**: GitHub Actions runs flake8, black (`==26.5.1`), and pytest on every push/PR to `main`.

## ConversationHandlers registration order (in `main()`)

1. `register_conv` — `/start` for unregistered non-admin users (registration)
2. `CommandHandler("start", start)` — `/start` for admins and registered users
3. `requests_handler` — `📋 Заявки` button (admin: all requests with filters, card, edit, delete)
4. `analysis_handler` — `📊 Анализ` button
5. `complete_handler` — `✅ Завершить заявку` button (with comment, pagination and batch complete)
6. `status_handler` — `/status` command (admin: change request status)
7. `my_requests_handler` — `📋 Мои заявки` button (with pagination and search)
8. `admin_handler` — `⚙️ Настройки` button (backup, audit log, clear DB)
9. `conv_handler` — `➕ Добавить заявку` button
10. `CallbackQueryHandler(show_request_photos)` — photo viewing (with IDOR check)
11. `CallbackQueryHandler(show_request_details)` — request details (with IDOR check)
12. `MessageHandler(📊 Дашборд)` — dashboard for admins
13. `handle_menu` — catch-all for remaining text

**Important**: `start()` must return `ConversationHandler.END` for admins and registered users to avoid blocking subsequent handlers.

## Notifications

When a request is **created**:
1. User confirmation with request details
2. Shared chat (`TELEGRAM_CHAT_ID`) with inline button `📋 Открыть заявку #N`

When a request is **completed**:
1. Admin inline message with inline button `📋 Открыть заявку #N`
2. Shared chat (`TELEGRAM_CHAT_ID`) with inline button
3. Initiator personal message (if linked via `initiator_users` table) with inline button

Photos are NOT included in notifications.

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
- **Test runner**: `python -m pytest tests/ -v` (must use venv Python)
- **DB tests**: `test_db.py` uses a `tmp_path` fixture with `patch.object(db, "DB_PATH", ...)` to isolate each test to a temporary database.
- **Core tests**: `test_core.py` mocks `db.get_active_requests` and `db.get_photo_counts` to return fixture data.
- **Bot tests**: `test_bot.py` mocks `db.get_all_lists`, `db.add_request_with_photos`, and `db.add_request_photo` to avoid real DB calls.
- **Integration tests**: `test_integration.py` tests full conversation flows (create request, complete, register, cancel, rate limit, field limits).
- **All 77 tests pass**.

When adding new tests:
- Async bot handlers: use `@pytest.mark.asyncio`, mock `update.message.reply_text` with `AsyncMock()`, mock `context.user_data` as a plain dict.
- DB functions: use the `tmp_path` fixture pattern from `test_db.py`.

## Common Pitfalls

- **Import-time side effects**: `config.py` calls `_require_env()` at import time for mandatory vars. `_validate_config()` is called explicitly from `bot.py main()`. Tests set env vars in `conftest.py` before any imports.
- **SQLite file locking**: Only one process should write to `bot.db` at a time. The `get_conn()` context manager uses WAL journal mode for better concurrency.
- **SQLite placeholder limit**: `SQLITE_MAX_VARIABLE_NUMBER = 999`. Use batch queries (see `get_photo_counts()` for example).
- **ConversationHandler fallbacks**: The `❌ Отмена` button is handled via `MessageHandler(filters.Regex('^❌ Отмена$'), cancel)` in fallbacks, not per-state. This means it works in all states including CONFIRM.
- **Emoji in regex filters**: `⚙️` (U+2699 + U+FE0F variation selector) may arrive without the selector in Telegram messages. Use `filters.Regex('Настройки')` (match on text, not emoji) instead of `filters.Regex('^⚙️ Настройки$')`.
- **Admin panel "Настройки" restart**: The `admin_handler` must include `MessageHandler(filters.Regex('Настройки'), admin_start)` in **fallbacks** (not just entry_points) so the button works when pressed while already inside the settings conversation.
- **Admin panel "Назад" button**: `back_main` callback appears in both `ADMIN_MENU` and `ADMIN_LIST_VIEW` states. Both `admin_menu_handler` and `admin_list_view_handler` must handle it (use shared `_show_main_settings()` helper).
- **PowerShell syntax**: On Windows, use `;` not `&&` to chain shell commands.
- **New tables need fallback**: Functions accessing `settings` and `initiator_users` tables must use try/except to handle cases where the table doesn't exist yet (e.g., in tests with temporary DBs). See `get_setting()` and `get_initiator_by_tg_id()` for examples.
- **callback_data byte limit**: Telegram limits `callback_data` to 64 bytes (not characters). Use `_truncate_callback_data()` helper for Cyrillic text.
- **Black formatting**: All Python files must pass `black --check`. Pin version in CI (`black==26.5.1`).

## Files to Touch Together

- Changing conversation flow → update `bot.py` (handlers + ConversationHandler states), `tests/test_bot.py`, and `tests/test_integration.py`
- Changing report format → update `core.py` and `tests/test_core.py`
- Changing DB schema → update `db.py` (schema + CRUD + migrations) and `tests/test_db.py`
- Adding a new env var → add to `config.py`, update `.env.example` if present, update README.md
- Changing default names or lists → update `_DEFAULT_LISTS` in `db.py` and fallbacks in `bot.py`
- Changing CI workflow → update `.github/workflows/ci.yml`
- Adding new ConversationHandler → register in `main()` in correct order (before catch-all `handle_menu`)

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
- Registration checks `get_initiator_tg_id()` before linking — prevents hijacking an already-linked name.
- **IDOR protection**: `show_request_photos()` and `show_request_details()` verify `is_admin() or is_request_owner()` before returning data.
- **Ownership check**: `is_request_owner(request_id, telegram_id)` in `db.py` verifies the user is the linked initiator of the request.

### Race conditions
- `update_request_status()` uses `cursor.rowcount` check after UPDATE to detect lost updates.
- `complete_requests_batch()` uses a single `UPDATE ... WHERE id IN (...) AND done_date IS NULL` — atomic across all IDs.

### HTML injection prevention
- **All user-supplied fields** (`name`, `unit`, `initiator`, `object`, `notes`, `username`, DB list values) must be escaped with `html.escape()` before embedding in Telegram HTML messages (`parse_mode='HTML'`).
- This applies in both `bot.py` (user messages) and `core.py` (reports).
- Telegram's HTML parser supports `<a href>` — unescaped user input can inject phishing links.

### Input validation
- `validate_quantity()` rejects negative, zero, `inf`, and `NaN` values (`math.isfinite` check). Commas are normalized to dots before saving.
- `bpr` setting is range-checked (1–6).
- `callback_data` values are truncated to fit Telegram's 64-byte limit using `_truncate_callback_data()`.
- `date.fromisoformat()` calls in `core.py` are wrapped in try/except to handle corrupt dates without crashing reports.

### Error handling
- Exception messages are never shown to users — only generic messages. Full details go to `logger.error()` or `logger.warning()`.
- Proxy URL credentials are masked before logging.
- `context.user_data.clear()` is NOT called on save errors — user can retry with `🔄 Повторить`.

### Rate limiting
- `RateLimiter` in `utils.py` implements per-user sliding window (default: 5 requests / 60 sec).
- Auto-cleanup of stale user entries every 100 calls to prevent memory leaks.
- Configurable via `RATE_LIMIT_REQUESTS` and `RATE_LIMIT_WINDOW_SEC` env vars.
- Applied to `/start` and `➕ Добавить заявку` entry points.
- `context.user_data["_saving"]` flag prevents double-submit on confirm.

### Backup and audit
- `backup_db()` creates a copy via SQLite backup API; `verify_backup()` checks integrity.
- `clear_all_requests()` auto-backs up before wipe; aborts if backup verification fails.
- `audit_log` table records all significant actions with user_id, action type, and details (created, completed, edited, status_changed, deleted, batch_completed, archived, db_cleared, list_added, list_deleted, list_renamed).
- Admin panel includes `💾 Резервная копия` and `📋 Журнал действий` sections.

### Persistent state
- `conversation_state` table stores `user_data` as JSON at each conversation step.
- `_save_draft()` called after each step; `clear_conversation_state()` on cancel/complete.
- Drafts survive bot restarts.

### DB migrations
- `init_db()` checks for missing columns/tables before applying changes.
- Status column migration: `ALTER TABLE requests ADD COLUMN status TEXT DEFAULT 'new'` (only if column doesn't exist).
- Unique index on `telegram_id`: `CREATE UNIQUE INDEX IF NOT EXISTS idx_initiator_tg_id ON initiator_users(telegram_id)`.
- `archive_completed_requests()` uses `date('now', '-' || N || ' days')` for correct date arithmetic.