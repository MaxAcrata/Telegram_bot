"""Интеграционные тесты диалогов.

Симулируют полные сценарии: регистрация, создание заявки,
завершение, смена статуса, отмена на разных шагах.
"""

import pytest
from datetime import date
from unittest.mock import patch, MagicMock, AsyncMock

from telegram.ext import ConversationHandler

from bot import (
    start,
    register_start,
    register_name,
    start_add_request,
    set_name,
    set_quantity,
    set_unit,
    set_initiator,
    set_initiator_custom,
    set_object,
    set_object_custom,
    set_notes_and_save,
    handle_photo,
    handle_photo_done,
    confirm_request,
    cancel,
    complete_start,
    complete_select_handler,
    complete_comment_handler,
    complete_confirm_handler,
    my_requests_start,
    NAME,
    QUANTITY,
    UNIT,
    INITIATOR,
    INITIATOR_CUSTOM,
    OBJECT,
    OBJECT_CUSTOM,
    NOTES,
    PHOTO,
    CONFIRM,
    REG_NAME,
    COMPLETE_SELECT,
    COMPLETE_COMMENT,
    COMPLETE_CONFIRM,
    MY_REQUESTS_SELECT,
)

# ===== Helpers =====


def make_update(text=None, user_id=123, first_name="Тест", chat_id=999):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.first_name = first_name
    update.effective_user.username = "testuser"
    update.effective_chat.id = chat_id
    if text is not None:
        update.message.text = text
    update.message.reply_text = AsyncMock()
    update.message.reply_photo = AsyncMock()
    return update


def make_callback_update(data, user_id=123, chat_id=999):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.first_name = "Тест"
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message.chat_id = chat_id
    return update


def make_context(user_data=None):
    context = MagicMock()
    context.user_data = user_data or {}
    context.bot.send_message = AsyncMock()
    context.bot.send_photo = AsyncMock()
    context.bot_data = {}
    return context


# ===== Тест: полный диалог создания заявки (admin, linked) =====


@pytest.mark.asyncio
@patch("bot._save_draft")
@patch("bot.add_request_with_photos", return_value=42)
@patch("bot.get_initiator_by_tg_id", return_value="Алексей")
@patch("bot.get_setting", return_value="2")
@patch(
    "bot.get_all_lists",
    return_value={
        "initiator": ["Алексей", "Игорь"],
        "object": ["Солнечное", "Привилегия"],
        "unit": ["шт", "кг"],
    },
)
@patch("bot.clear_conversation_state")
@patch("bot.ADMIN_IDS", [123])
async def test_full_create_request_flow(
    mock_clear, mock_lists, mock_setting, mock_linked, mock_add, mock_draft
):
    """Полный сценарий: шаг 1-7 → подтверждение → сохранение."""
    ctx = make_context()

    # Шаг 1: Имя
    u = make_update(text="Кирпич облицовочный")
    result = await set_name(u, ctx)
    assert result == QUANTITY
    assert ctx.user_data["name"] == "Кирпич облицовочный"

    # Шаг 2: Количество
    u = make_update(text="500")
    result = await set_quantity(u, ctx)
    assert result == UNIT

    # Шаг 3: Единица → автопереход к объекту (инициатор привязан)
    u = make_update(text="шт")
    result = await set_unit(u, ctx)
    assert result == OBJECT
    assert ctx.user_data["initiator"] == "Алексей"

    # Шаг 5: Объект
    u = make_update(text="Солнечное")
    result = await set_object(u, ctx)
    assert result == NOTES

    # Шаг 6: Дополнительно
    u = make_update(text="Для фасада")
    result = await set_notes_and_save(u, ctx)
    assert result == CONFIRM

    # Подтверждение
    u = make_update(text="✅ Подтвердить", user_id=123)
    result = await confirm_request(u, ctx)
    assert result == ConversationHandler.END
    mock_add.assert_called_once()
    call_kwargs = mock_add.call_args
    assert call_kwargs.kwargs["name"] == "Кирпич облицовочный"
    assert call_kwargs.kwargs["quantity"] == 500.0
    assert call_kwargs.kwargs["unit"] == "шт"
    assert call_kwargs.kwargs["initiator"] == "Алексей"
    assert call_kwargs.kwargs["object_name"] == "Солнечное"
    assert call_kwargs.kwargs["notes"] == "Для фасада"


# ===== Тест: диалог с фото =====


@pytest.mark.asyncio
@patch("bot._save_draft")
@patch("bot.add_request_with_photos", return_value=43)
@patch("bot.get_initiator_by_tg_id", return_value=None)
@patch("bot.get_setting", return_value="2")
@patch(
    "bot.get_all_lists",
    return_value={
        "initiator": ["Алексей", "Другое"],
        "object": ["Объект1"],
        "unit": ["шт"],
    },
)
@patch("bot.clear_conversation_state")
async def test_create_request_with_photos(
    mock_clear, mock_lists, mock_setting, mock_linked, mock_add, mock_draft
):
    """Заявка с двумя фото."""
    ctx = make_context({"name": "Тест", "quantity": "5", "unit": "шт"})

    # Шаг 4: Инициатор (не привязан)
    u = make_update(text="Алексей")
    result = await set_initiator(u, ctx)
    assert result == OBJECT

    # Шаг 5: Объект
    u = make_update(text="Объект1")
    result = await set_object(u, ctx)
    assert result == NOTES

    # Шаг 6: Пропустить примечание
    u = make_update(text="➡️ Пропустить")
    result = await set_notes_and_save(u, ctx)
    assert result == CONFIRM

    # Шаг 7a: Первое фото
    u = make_update()
    u.message.photo = [MagicMock(file_id="file_abc")]
    result = await handle_photo(u, ctx)
    assert result == PHOTO
    assert len(ctx.user_data["photos"]) == 1

    # Шаг 7b: Второе фото
    u = make_update()
    u.message.photo = [MagicMock(file_id="file_def")]
    result = await handle_photo(u, ctx)
    assert result == PHOTO
    assert len(ctx.user_data["photos"]) == 2

    # Шаг 7c: Готово
    u = make_update(text="✅ Готово")
    result = await handle_photo_done(u, ctx)
    assert result == CONFIRM

    # Подтверждение
    u = make_update(text="✅ Подтвердить", user_id=456)
    result = await confirm_request(u, ctx)
    assert result == ConversationHandler.END
    call_kwargs = mock_add.call_args.kwargs
    assert call_kwargs["photo_file_ids"] == ["file_abc", "file_def"]


# ===== Тест: отмена на шаге имени =====


@pytest.mark.asyncio
@patch("bot.clear_conversation_state")
async def test_cancel_at_name_step(mock_clear):
    """Отмена на шаге ввода имени."""
    ctx = make_context({"name": "Незавершённый"})
    u = make_update(text="❌ Отмена", user_id=777)

    result = await cancel(u, ctx)

    assert result == ConversationHandler.END
    assert ctx.user_data == {}
    mock_clear.assert_called_once_with(777)


# ===== Тест: отмена на шаге фото =====


@pytest.mark.asyncio
@patch("bot.clear_conversation_state")
async def test_cancel_at_photo_step(mock_clear):
    """Отмена на шаге фото."""
    ctx = make_context({"name": "Тест", "photos": ["file1"]})
    u = make_update(text="❌ Отмена", user_id=888)

    result = await cancel(u, ctx)

    assert result == ConversationHandler.END
    assert ctx.user_data == {}


# ===== Тест: отмена подтверждения =====


@pytest.mark.asyncio
@patch("bot.clear_conversation_state")
@patch("bot.ADMIN_IDS", [123])
async def test_cancel_at_confirm(mock_clear):
    """Отмена на шаге подтверждения."""
    ctx = make_context({"name": "Тест", "quantity": "1"})
    u = make_update(text="❌ Отмена", user_id=123)

    result = await confirm_request(u, ctx)

    assert result == ConversationHandler.END
    assert ctx.user_data == {}


# ===== Тест: некорректное количество =====


@pytest.mark.asyncio
async def test_invalid_quantity_retries():
    """Некорректное количество → повтор запроса."""
    ctx = make_context()

    # Буквы
    u = make_update(text="abc")
    result = await set_quantity(u, ctx)
    assert result == QUANTITY
    assert "quantity" not in ctx.user_data

    # Отрицательное
    u = make_update(text="-5")
    result = await set_quantity(u, ctx)
    assert result == QUANTITY

    # Ноль
    u = make_update(text="0")
    result = await set_quantity(u, ctx)
    assert result == QUANTITY

    # Корректное — проходит
    u = make_update(text="10,5")
    result = await set_quantity(u, ctx)
    assert result == UNIT
    assert ctx.user_data["quantity"] == "10,5"


# ===== Тест: «Другое» для инициатора =====


@pytest.mark.asyncio
@patch("bot._save_draft")
@patch("bot.get_setting", return_value="2")
@patch(
    "bot.get_all_lists",
    return_value={"initiator": ["Алексей", "Другое"], "object": ["О"], "unit": ["шт"]},
)
async def test_custom_initiator(mock_lists, mock_setting, mock_draft):
    """Выбор «Другое» → ввод кастомного имени."""
    ctx = make_context()

    u = make_update(text="Другое")
    result = await set_initiator(u, ctx)
    assert result == INITIATOR_CUSTOM

    u = make_update(text="Василий")
    result = await set_initiator_custom(u, ctx)
    assert result == OBJECT
    assert ctx.user_data["initiator"] == "Василий"


# ===== Тест: «Другое» для объекта =====


@pytest.mark.asyncio
@patch("bot._save_draft")
async def test_custom_object(mock_draft):
    """Выбор «Другое» → ввод кастомного объекта."""
    ctx = make_context()

    u = make_update(text="Другое")
    result = await set_object(u, ctx)
    assert result == OBJECT_CUSTOM

    u = make_update(text="Новый объект")
    result = await set_object_custom(u, ctx)
    assert result == NOTES
    assert ctx.user_data["object"] == "Новый объект"


# ===== Тест: полный flow завершения заявки =====


@pytest.mark.asyncio
@patch("bot.get_request_by_id")
@patch("bot.get_active_requests")
@patch("bot._safe_reply", new_callable=AsyncMock)
@patch("bot._safe_edit", new_callable=AsyncMock)
@patch("bot._safe_send", new_callable=AsyncMock)
@patch("bot.ADMIN_IDS", [123])
async def test_complete_request_flow(
    mock_send, mock_edit, mock_reply, mock_active, mock_get_req
):
    """Полный сценарий завершения заявки."""
    req = {
        "id": 5,
        "name": "Цемент",
        "quantity": 100.0,
        "unit": "кг",
        "initiator": "Иван",
        "object": "Стройка",
    }
    mock_active.return_value = [req]
    mock_get_req.return_value = req

    # Шаг 1: Показать список
    u = make_update(user_id=123)
    ctx = make_context()
    result = await complete_start(u, ctx)
    assert result == COMPLETE_SELECT

    # Шаг 2: Выбрать заявку → переход к комментарию
    u = make_callback_update("cr:5", user_id=123)
    ctx = make_context()
    result = await complete_select_handler(u, ctx)
    assert result == COMPLETE_COMMENT
    assert ctx.user_data["complete_request"] == req

    # Шаг 3: Без комментария → переход к подтверждению
    u = make_callback_update("cr:nocomment", user_id=123)
    with patch("bot._safe_edit", new_callable=AsyncMock):
        result = await complete_comment_handler(u, ctx)
    assert result == COMPLETE_CONFIRM

    # Шаг 4: Подтвердить
    with patch(
        "bot.complete_request",
        return_value={**req, "done_date": "2026-10-05", "status": "done"},
    ) as mock_complete, patch("bot.get_initiator_tg_id", return_value=999):
        u = make_callback_update("cr:confirm", user_id=123)
        result = await complete_confirm_handler(u, ctx)
        assert result == ConversationHandler.END
        mock_complete.assert_called_once_with(
            5, date.today(), user_id=123, user_name="Тест"
        )


# ===== Тест: отмена завершения =====


@pytest.mark.asyncio
@patch("bot.ADMIN_IDS", [123])
async def test_complete_cancel():
    """Отмена завершения заявки."""
    u = make_callback_update("cr:cancel", user_id=123)
    ctx = make_context({"complete_request": {"id": 1}})

    with patch("bot._safe_edit", new_callable=AsyncMock):
        result = await complete_confirm_handler(u, ctx)

    assert result == ConversationHandler.END
    assert "complete_request" not in ctx.user_data


# ===== Тест: регистрация нового пользователя =====


@pytest.mark.asyncio
@patch("bot.get_initiator_tg_id", return_value=None)
@patch("bot.link_initiator")
@patch("bot.audit_log")
async def test_registration_flow(mock_audit, mock_link, mock_tg_id):
    """Полный сценарий регистрации."""
    # Показать список инициаторов
    with patch(
        "bot.get_all_lists",
        return_value={"initiator": ["Алексей", "Игорь", "Другое"]},
    ), patch("bot.get_setting", return_value="4"):
        u = make_update(user_id=555)
        ctx = make_context()
        result = await register_start(u, ctx)
        assert result == REG_NAME

    # Выбрать имя
    u = make_update(text="Алексей", user_id=555)
    ctx = make_context()
    result = await register_name(u, ctx)
    assert result == ConversationHandler.END
    mock_link.assert_called_once_with("Алексей", 555)
    mock_audit.assert_called_once()


# ===== Тест: регистрация — имя уже занято =====


@pytest.mark.asyncio
@patch("bot.get_initiator_tg_id", return_value=999)
async def test_registration_name_taken(mock_tg_id):
    """Попытка привязать занятое имя."""
    u = make_update(text="Алексей", user_id=555)
    ctx = make_context()
    result = await register_name(u, ctx)
    assert result == REG_NAME  # Остаёмся на том же шаге


# ===== Тест: проверка rate limiter =====


@pytest.mark.asyncio
@patch("bot.rate_limiter")
@patch("bot.ADMIN_IDS", [123])
async def test_rate_limit_on_start(mock_limiter):
    """Rate limit блокирует /start."""
    mock_limiter.is_allowed.return_value = False
    u = make_update(user_id=123)
    ctx = make_context()

    result = await start(u, ctx)
    assert result == ConversationHandler.END


# ===== Тест: лимит фото =====


@pytest.mark.asyncio
@patch("bot.MAX_PHOTOS_PER_REQUEST", 2)
@patch("bot._save_draft")
async def test_photo_limit(mock_draft):
    """Достижение лимита фото."""
    ctx = make_context({"photos": ["f1", "f2"]})

    u = make_update()
    u.message.photo = [MagicMock(file_id="f3")]
    result = await handle_photo(u, ctx)
    assert result == PHOTO
    assert len(ctx.user_data["photos"]) == 2  # Не добавилось


# ===== Тест: truncation длинных полей =====


@pytest.mark.asyncio
@patch("bot._save_draft")
@patch("bot.MAX_FIELD_LENGTH", 20)
async def test_field_truncation(mock_draft):
    """Длинное поле обрезается."""
    ctx = make_context()

    u = make_update(text="А" * 100)
    result = await set_name(u, ctx)
    assert result == QUANTITY
    assert len(ctx.user_data["name"]) == 20


# ===== Тест: мой запрос rate limit =====


@pytest.mark.asyncio
@patch("bot.get_initiator_by_tg_id", return_value="Алексей")
@patch("bot.get_user_requests", return_value=[])
async def test_my_requests_empty(mock_requests, mock_linked):
    """Пустой список «Мои заявки»."""
    u = make_update(user_id=123)
    ctx = make_context()

    result = await my_requests_start(u, ctx)
    # Пустой список → ConversationHandler.END (без перехода)
    # или показ сообщения «нет заявок»
    mock_requests.assert_called_once_with("Алексей", status_filter="active")


# ===== Тест: confirm_request — дубликат блокируется =====


@pytest.mark.asyncio
@patch("bot.ADMIN_IDS", [123])
async def test_duplicate_save_blocked():
    """Повторное нажатие «Подтвердить» блокируется."""
    ctx = make_context(
        {
            "name": "Тест",
            "quantity": "1",
            "unit": "шт",
            "initiator": "И",
            "object": "О",
            "notes": "",
            "photos": [],
            "_saving": True,
        }
    )
    u = make_update(text="✅ Подтвердить", user_id=123)

    result = await confirm_request(u, ctx)
    assert result == CONFIRM  # Остаёмся на подтверждении
