"""Тесты для bot.py"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from bot import (
    is_admin,
    get_main_keyboard,
    cancel,
    start_add_request,
    set_name,
    set_quantity,
    set_unit,
    set_notes_and_save,
    confirm_request,
    NAME,
    QUANTITY,
    UNIT,
    INITIATOR,
    PHOTO,
    CONFIRM,
    CANCEL_KEYBOARD,
)
from telegram.ext import ConversationHandler

# ===== Вспомогательные функции =====


def make_update(text: str = None, user_id: int = 123, first_name: str = "Тест"):
    """Создаёт мок Update"""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.first_name = first_name
    if text is not None:
        update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def make_context():
    """Создаёт мок Context"""
    context = MagicMock()
    context.user_data = {}
    context.bot.send_message = AsyncMock()
    context.bot_data = {}
    return context


# ===== Тесты вспомогательных функций =====


@patch("bot.ADMIN_IDS", [111, 222])
def test_is_admin_true():
    assert is_admin(111) is True


@patch("bot.ADMIN_IDS", [111, 222])
def test_is_admin_false():
    assert is_admin(999) is False


@patch("bot.ADMIN_IDS", [111])
def test_get_main_keyboard_admin():
    kb = get_main_keyboard(111)
    text = str(kb)
    assert "📊 Анализ" in text
    assert "➕ Добавить заявку" in text
    assert "📋 Мои заявки" in text


@patch("bot.ADMIN_IDS", [111])
def test_get_main_keyboard_user():
    kb = get_main_keyboard(999)
    text = str(kb)
    assert "➕ Добавить заявку" in text
    assert "📊 Анализ" not in text
    assert "📋 Мои заявки" in text


# ===== Тесты отмены =====


@pytest.mark.asyncio
@patch("bot.clear_conversation_state")
@patch("bot.ADMIN_IDS", [123])
async def test_cancel_clears_user_data(mock_clear):
    update = make_update(user_id=123)
    context = make_context()
    context.user_data["name"] = "test"

    result = await cancel(update, context)

    assert result == ConversationHandler.END
    assert context.user_data == {}
    mock_clear.assert_called_once_with(123)


# ===== Тесты начала диалога =====


@pytest.mark.asyncio
@patch("bot.rate_limiter")
@patch("bot.ADMIN_IDS", [123])
async def test_start_add_request(mock_limiter):
    mock_limiter.is_allowed.return_value = True
    update = make_update(user_id=123)
    context = make_context()

    result = await start_add_request(update, context)

    assert result == NAME
    assert "draft_id" in context.user_data


@pytest.mark.asyncio
@patch("bot.rate_limiter")
@patch("bot.ADMIN_IDS", [123])
async def test_start_add_request_rate_limited(mock_limiter):
    mock_limiter.is_allowed.return_value = False
    update = make_update(user_id=123)
    context = make_context()

    result = await start_add_request(update, context)

    assert result == ConversationHandler.END


# ===== Тесты шагов диалога =====


@pytest.mark.asyncio
async def test_set_name():
    update = make_update(text="Кирпич")
    context = make_context()

    with patch("bot._save_draft"):
        result = await set_name(update, context)

    assert result == QUANTITY
    assert context.user_data["name"] == "Кирпич"


@pytest.mark.asyncio
async def test_set_quantity_valid():
    update = make_update(text="10")
    context = make_context()

    with patch("bot._save_draft"):
        result = await set_quantity(update, context)

    assert result == UNIT
    assert context.user_data["quantity"] == "10"


@pytest.mark.asyncio
async def test_set_quantity_invalid():
    update = make_update(text="abc")
    context = make_context()

    result = await set_quantity(update, context)

    assert result == QUANTITY
    assert "quantity" not in context.user_data


@pytest.mark.asyncio
@patch(
    "bot.get_all_lists",
    return_value={
        "initiator": ["Иван", "Петр", "Другое"],
        "unit": ["шт", "кг"],
        "object": ["Объект1"],
    },
)
@patch("bot.get_setting", return_value="3")
async def test_set_unit(mock_setting, mock_lists):
    update = make_update(text="шт")
    update.effective_user.id = 123
    context = make_context()

    with patch("bot.get_initiator_by_tg_id", return_value=None), patch(
        "bot._save_draft"
    ):
        result = await set_unit(update, context)

    assert result == INITIATOR
    assert context.user_data["unit"] == "шт"


# ===== Тесты подтверждения =====


@pytest.mark.asyncio
async def test_set_notes_shows_confirmation():
    update = make_update(text="Срочно")
    context = make_context()
    context.user_data = {
        "name": "Кирпич",
        "quantity": "100",
        "unit": "шт",
        "initiator": "Иван",
        "object": "Стройка",
    }

    with patch("bot._save_draft"):
        result = await set_notes_and_save(update, context)

    assert result == CONFIRM
    assert context.user_data["notes"] == "Срочно"
    assert context.user_data["photos"] == []
    reply_text = update.message.reply_text.call_args[0][0]
    assert "Проверьте заявку" in reply_text
    assert "Подтвердите или отмените" in reply_text


@pytest.mark.asyncio
@patch("bot.clear_conversation_state")
@patch("bot.add_request_with_photos", return_value=1)
@patch("bot.ADMIN_IDS", [123])
async def test_confirm_request_saves(mock_add, mock_clear):
    update = make_update(text="✅ Подтвердить", user_id=123)
    context = make_context()
    context.user_data = {
        "name": "Кирпич",
        "quantity": "100",
        "unit": "шт",
        "initiator": "Иван",
        "object": "Стройка",
        "notes": "Срочно",
        "photos": [],
    }

    result = await confirm_request(update, context)

    assert result == ConversationHandler.END
    mock_add.assert_called_once()
    assert context.user_data == {}


@pytest.mark.asyncio
@patch("bot.clear_conversation_state")
@patch("bot.ADMIN_IDS", [123])
async def test_confirm_request_cancel(mock_clear):
    update = make_update(text="❌ Отмена", user_id=123)
    context = make_context()
    context.user_data = {"name": "Кирпич"}

    result = await confirm_request(update, context)

    assert result == ConversationHandler.END
    assert context.user_data == {}


# ===== Баг: гифки/эмодзи в текстовых полях =====


@pytest.mark.asyncio
@patch("bot.clear_conversation_state")
async def test_set_name_rejects_empty_text(mock_clear):
    """text=None (гифка/стикер) не должна сохраняться как имя."""
    update = make_update(text=None)
    update.message.text = None
    context = make_context()

    result = await set_name(update, context)

    assert result == NAME
    assert "name" not in context.user_data


@pytest.mark.asyncio
@patch("bot.clear_conversation_state")
async def test_set_name_rejects_emoji_only(mock_clear):
    """Эмодзи-«стикер» не должен проходить как наименование."""
    update = make_update(text="🎮")
    context = make_context()

    result = await set_name(update, context)

    assert result == NAME
    assert "name" not in context.user_data


@pytest.mark.asyncio
@patch("bot.clear_conversation_state")
@patch("bot.save_user_input")
@patch("bot._save_draft")
async def test_set_name_accepts_real_text(mock_draft, mock_save, mock_clear):
    update = make_update(text="Кирпич облицовочный")
    context = make_context()

    result = await set_name(update, context)

    assert result == QUANTITY
    assert context.user_data["name"] == "Кирпич облицовочный"


# ===== Баг: комментарий при завершении через роутер =====


@pytest.mark.asyncio
@patch("bot.is_admin", return_value=True)
@patch("bot._safe_edit", new_callable=AsyncMock)
@patch("bot._safe_reply", new_callable=AsyncMock)
@patch("bot.complete_comment_text_handler", new_callable=AsyncMock)
async def test_complete_text_router_accepts_comment(
    mock_handler, mock_reply, mock_edit, mock_admin
):
    """Текст при complete_request в user_data уходит в comment handler."""
    from bot import complete_text_router

    mock_handler.return_value = 51  # COMPLETE_CONFIRM-like
    update = make_update(text="Готово к отгрузке")
    context = make_context()
    context.user_data["complete_request"] = {"id": 1, "name": "Цемент"}

    result = await complete_text_router(update, context)

    mock_handler.assert_called_once()
    assert result == 51


@pytest.mark.asyncio
@patch("bot.is_admin", return_value=True)
@patch("bot.complete_comment_text_handler", new_callable=AsyncMock)
async def test_complete_text_router_ignores_menu(mock_handler, mock_admin):
    """Кнопки меню не должны съедаться как комментарий."""
    from bot import complete_text_router

    update = make_update(text="➕ Добавить заявку")
    context = make_context()
    context.user_data["complete_request"] = {"id": 1}

    result = await complete_text_router(update, context)

    mock_handler.assert_not_called()
    assert result is None
    assert "complete_request" not in context.user_data


@pytest.mark.asyncio
@patch("bot.is_admin", return_value=True)
@patch("bot.complete_comment_text_handler", new_callable=AsyncMock)
async def test_complete_text_router_no_request(mock_handler, mock_admin):
    """Без complete_request роутер не трогает текст (пусть handle_menu)."""
    from bot import complete_text_router

    update = make_update(text="Привет")
    context = make_context()

    result = await complete_text_router(update, context)

    mock_handler.assert_not_called()
    assert result is None
