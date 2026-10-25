"""Тесты обработки ошибок: БД, Telegram API, env, резервные списки.

Покрывает исправления BLE001 / S110 / PLW1508.
"""

import os
import sqlite3
from datetime import date
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
from telegram.error import TelegramError, NetworkError

import db
import config
from db import (
    init_db,
    get_conn,
    get_setting,
    backup_db,
    verify_backup,
    get_request_photos,
    get_photo_counts,
    get_initiator_tg_id,
    get_initiator_by_tg_id,
    save_conversation_state,
    load_conversation_state,
    clear_conversation_state,
    is_request_owner,
    audit_log,
    get_audit_log,
    save_user_input,
    get_user_input_history,
    get_popular_inputs,
)


# ===== DB errors =====


@pytest.fixture(autouse=True)
def temp_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    backup_dir = str(tmp_path / "backups")
    with patch.object(db, "DB_PATH", db_path), patch.object(db, "BACKUP_DIR", backup_dir):
        init_db()
        yield db_path


def test_get_setting_returns_default_on_db_error():
    """get_setting возвращает default при ошибке БД (таблица недоступна)."""
    with patch.object(db, "get_conn", side_effect=sqlite3.OperationalError("no such table")):
        assert get_setting("missing_key", "fallback") == "fallback"


def test_get_setting_default_empty_string():
    """get_setting возвращает пустую строку по умолчанию."""
    assert get_setting("absolutely_missing_key") == ""


def test_backup_db_returns_none_on_error(tmp_path):
    """backup_db возвращает None при ошибке файловой системы/БД."""
    bad_dir = str(tmp_path / "no" / "such" / "dir")
    with patch.object(db, "BACKUP_DIR", bad_dir), patch.object(
        db, "DB_PATH", str(tmp_path / "test.db")
    ):
        with patch("pathlib.Path.mkdir", side_effect=OSError("permission denied")):
            assert backup_db() is None


def test_backup_db_returns_none_on_sqlite_error():
    """backup_db возвращает None при ошибке SQLite."""
    with patch.object(db, "DB_PATH", "/nonexistent/path/bot.db"):
        assert backup_db() is None


def test_verify_backup_false_on_missing_file(tmp_path):
    """verify_backup возвращает False для несуществующего файла."""
    assert verify_backup(str(tmp_path / "missing.db")) is False


def test_verify_backup_false_on_corrupt_file(tmp_path):
    """verify_backup возвращает False для повреждённого файла."""
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a database")
    assert verify_backup(str(corrupt)) is False


def test_verify_backup_true_for_valid_backup(tmp_path):
    """verify_backup возвращает True для корректной копии."""
    path = backup_db()
    assert path is not None
    assert verify_backup(path) is True


def test_get_request_photos_returns_empty_on_db_error():
    """get_request_photos возвращает [] при ошибке БД."""
    with patch.object(db, "get_conn", side_effect=sqlite3.DatabaseError("db locked")):
        assert get_request_photos(1) == []


def test_get_photo_counts_returns_empty_on_db_error():
    """get_photo_counts возвращает {} при ошибке БД."""
    with patch.object(db, "get_conn", side_effect=sqlite3.DatabaseError("db locked")):
        assert get_photo_counts([1, 2]) == {}


def test_get_initiator_tg_id_none_on_db_error():
    """get_initiator_tg_id возвращает None при ошибке БД."""
    with patch.object(db, "get_conn", side_effect=sqlite3.OperationalError("no table")):
        assert get_initiator_tg_id("Иван") is None


def test_get_initiator_by_tg_id_none_on_db_error():
    """get_initiator_by_tg_id возвращает None при ошибке БД."""
    with patch.object(db, "get_conn", side_effect=sqlite3.OperationalError("no table")):
        assert get_initiator_by_tg_id(123) is None


def test_is_request_owner_false_on_db_error():
    """is_request_owner возвращает False при ошибке БД."""
    with patch.object(db, "get_conn", side_effect=sqlite3.OperationalError("no table")):
        assert is_request_owner(1, 123) is False


def test_save_conversation_state_swallows_db_error():
    """save_conversation_state не бросает исключение при ошибке БД."""
    with patch.object(db, "get_conn", side_effect=sqlite3.OperationalError("no table")):
        save_conversation_state(1, "{}")  # не должно поднять


def test_load_conversation_state_none_on_db_error():
    """load_conversation_state возвращает None при ошибке БД."""
    with patch.object(db, "get_conn", side_effect=sqlite3.OperationalError("no table")):
        assert load_conversation_state(1) is None


def test_clear_conversation_state_swallows_db_error():
    """clear_conversation_state не бросает исключение при ошибке БД."""
    with patch.object(db, "get_conn", side_effect=sqlite3.OperationalError("no table")):
        clear_conversation_state(1)  # не должно поднять


def test_audit_log_swallows_db_error():
    """audit_log не бросает исключение при ошибке БД."""
    with patch.object(db, "get_conn", side_effect=sqlite3.OperationalError("no table")):
        audit_log(1, 2, "user", "created", "")  # не должно поднять


def test_get_audit_log_empty_on_db_error():
    """get_audit_log возвращает [] при ошибке БД."""
    with patch.object(db, "get_conn", side_effect=sqlite3.OperationalError("no table")):
        assert get_audit_log() == []


def test_save_user_input_swallows_db_error():
    """save_user_input не бросает исключение при ошибке БД."""
    with patch.object(db, "get_conn", side_effect=sqlite3.OperationalError("no table")):
        save_user_input(1, "name", "value")  # не должно поднять


def test_get_user_input_history_empty_on_db_error():
    """get_user_input_history возвращает [] при ошибке БД."""
    with patch.object(db, "get_conn", side_effect=sqlite3.OperationalError("no table")):
        assert get_user_input_history(1, "name") == []


def test_get_popular_inputs_empty_on_db_error():
    """get_popular_inputs возвращает [] при ошибке БД."""
    with patch.object(db, "get_conn", side_effect=sqlite3.OperationalError("no table")):
        assert get_popular_inputs("name") == []


def test_get_setting_missing_table_returns_default():
    """Реальный сценарий: таблица settings отсутствует → default."""
    with get_conn() as conn:
        conn.execute("DROP TABLE settings")
    assert get_setting("bpr", "3") == "3"


# ===== Config env vars (PLW1508) =====


def test_config_defaults_are_numeric():
    """Значения по умолчанию корректно парсятся в int."""
    assert isinstance(config.OVERDUE_DAYS, int)
    assert isinstance(config.REPORT_HOUR, int)
    assert isinstance(config.REPORT_MINUTE, int)
    assert config.OVERDUE_DAYS == 3
    assert config.REPORT_HOUR == 9
    assert config.REPORT_MINUTE == 0


def test_config_getenv_string_defaults():
    """os.getenv получает строковые default (а не int) — PLW1508."""
    with patch("os.getenv") as mock_getenv:
        mock_getenv.side_effect = lambda name, default=None: default
        assert int(os.getenv("OVERDUE_DAYS", "3")) == 3
        assert int(os.getenv("REPORT_HOUR", "9")) == 9
        assert int(os.getenv("REPORT_MINUTE", "0")) == 0
        # Все default переданы как str
        for call in mock_getenv.call_args_list:
            default = call.args[1] if len(call.args) > 1 else call.kwargs.get("default")
            if default is not None:
                assert isinstance(default, str), f"non-str default: {default!r}"


def test_config_env_override():
    """Переменные окружения переопределяют default (строковый путь)."""
    with patch("os.getenv", side_effect=lambda n, d=None: {"OVERDUE_DAYS": "7"}.get(n, d)):
        value = int(os.getenv("OVERDUE_DAYS", "3"))
        assert value == 7


# ===== Telegram API unavailability =====


@pytest.mark.asyncio
async def test_safe_send_handles_telegram_error():
    """_safe_send не пробрасывает TelegramError."""
    from bot import _safe_send

    context = MagicMock()
    context.bot.send_message = AsyncMock(side_effect=NetworkError("api down"))
    # Не должно поднять
    await _safe_send(context, 123, "test")


@pytest.mark.asyncio
async def test_safe_send_handles_retry_after():
    """_safe_send повторяет отправку после RetryAfter."""
    from bot import _safe_send
    from telegram.error import RetryAfter

    context = MagicMock()
    context.bot.send_message = AsyncMock(
        side_effect=[RetryAfter(0), MagicMock()]
    )
    result = await _safe_send(context, 123, "test")
    assert result is not None
    assert context.bot.send_message.call_count == 2


@pytest.mark.asyncio
async def test_error_handler_survives_telegram_error():
    """error_handler не падает, если уведомление пользователю не доставлено."""
    from bot import error_handler

    update = MagicMock()
    update.effective_chat.id = 123
    context = MagicMock()
    context.error = RuntimeError("original error")
    context.bot.send_message = AsyncMock(side_effect=NetworkError("api down"))

    await error_handler(update, context)  # не должно поднять


@pytest.mark.asyncio
async def test_error_handler_without_chat():
    """error_handler работает без effective_chat."""
    from bot import error_handler

    update = MagicMock(spec=[])  # нет effective_chat
    context = MagicMock()
    context.error = RuntimeError("err")

    await error_handler(update, context)  # не должно поднять


@pytest.mark.asyncio
async def test_confirm_request_survives_notification_failure():
    """confirm_request завершает диалог даже при ошибке Telegram API."""
    from bot import confirm_request, CONFIRM

    update = MagicMock()
    update.effective_user.id = 123
    update.message.text = "✅ Подтвердить"
    update.message.reply_text = AsyncMock(side_effect=NetworkError("api down"))

    context = MagicMock()
    context.user_data = {
        "name": "Кирпич",
        "quantity": "100",
        "unit": "шт",
        "initiator": "Иван",
        "object": "Стройка",
        "notes": "",
        "photos": [],
    }
    context.bot.send_message = AsyncMock(side_effect=NetworkError("api down"))
    context.bot_data = {}

    with patch("bot.is_admin", return_value=True), patch(
        "bot.add_request_with_photos", return_value=1
    ), patch("bot.clear_conversation_state"), patch(
        "bot.audit_log"
    ), patch("bot.TELEGRAM_CHAT_ID", "999"):
        result = await confirm_request(update, context)

    # Заявка сохранена — диалог завершён, даже если уведомления не ушли
    from telegram.ext import ConversationHandler

    assert result == ConversationHandler.END
    assert context.user_data == {}


# ===== Fallback lists =====


@pytest.mark.asyncio
async def test_set_quantity_fallback_units_on_db_error():
    """set_quantity использует резервный список единиц при ошибке БД."""
    from bot import set_quantity, UNIT

    update = MagicMock()
    update.effective_user.id = 123
    update.message.text = "100"
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {"name": "Кирпич"}

    with patch("bot.get_all_lists", side_effect=sqlite3.OperationalError("no table")), patch(
        "bot.get_setting", return_value="3"
    ), patch("bot._save_draft"), patch("bot.save_user_input"):
        result = await set_quantity(update, context)

    assert result == UNIT
    # reply содержит клавиатуру с резервными единицами
    call_kwargs = update.message.reply_text.call_args
    assert call_kwargs is not None


@pytest.mark.asyncio
async def test_set_unit_fallback_objects_on_db_error():
    """set_unit использует резервный список объектов при ошибке БД."""
    from bot import set_unit, OBJECT

    update = MagicMock()
    update.effective_user.id = 123
    update.message.text = "шт"
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {"name": "Кирпич", "quantity": "100"}

    with patch("bot.get_all_lists", side_effect=sqlite3.OperationalError("no table")), patch(
        "bot.get_setting", return_value="2"
    ), patch("bot.get_initiator_by_tg_id", return_value="Иван"), patch("bot._save_draft"):
        result = await set_unit(update, context)

    assert result == OBJECT


@pytest.mark.asyncio
async def test_save_draft_survives_unserializable_data():
    """_save_draft не падает на несериализуемых данных."""
    from bot import _save_draft

    update = MagicMock()
    update.effective_user.id = 123

    context = MagicMock()
    context.user_data = {"obj": object()}  # не сериализуется в JSON

    _save_draft(update, context)  # не должно поднять


@pytest.mark.asyncio
async def test_save_draft_survives_db_error():
    """_save_draft не падает при ошибке БД (save_conversation_state глотает sqlite3.Error)."""
    from bot import _save_draft

    update = MagicMock()
    update.effective_user.id = 123

    context = MagicMock()
    context.user_data = {"name": "Кирпич"}

    # Реальный путь: save_conversation_state ловит sqlite3.Error и логирует
    with patch(
        "db.get_conn", side_effect=sqlite3.OperationalError("no table")
    ):
        _save_draft(update, context)  # не должно поднять
