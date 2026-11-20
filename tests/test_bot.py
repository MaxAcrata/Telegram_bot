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
async def test_cancel_in_create_flow_clears_draft_and_form(mock_clear):
    """Явная отмена внутри создания чистит память формы и conversation_state."""
    update = make_update(user_id=123)
    context = make_context()
    context.user_data.update({"_flow": "create", "_step": NAME, "name": "test"})

    result = await cancel(update, context)

    assert result == ConversationHandler.END
    assert context.user_data == {}
    mock_clear.assert_called_once_with(123)


@pytest.mark.asyncio
@patch("bot.clear_conversation_state")
@patch("bot.ADMIN_IDS", [123])
async def test_cancel_outside_create_keeps_conversation_state(mock_clear):
    """Отмена вне создания не удаляет персистентный черновик."""
    update = make_update(user_id=123)
    context = make_context()
    context.user_data["complete_request"] = {"id": 1}
    context.user_data["name"] = "leftover"

    result = await cancel(update, context)

    assert result == ConversationHandler.END
    assert context.user_data == {}
    # conversation_state не трогаем: черновик другого назначения цел
    mock_clear.assert_not_called()


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
@patch("bot._safe_reply", new_callable=AsyncMock)
@patch("bot.complete_comment_text_handler", new_callable=AsyncMock)
async def test_route_text_dispatches_comment_in_complete_flow(
    mock_handler, mock_reply, mock_admin
):
    """Текст при _flow=complete/COMPLETE_COMMENT уходит в comment handler."""
    from bot import route_text, COMPLETE_COMMENT

    mock_handler.return_value = 51  # COMPLETE_CONFIRM
    update = make_update(text="Готово к отгрузке")
    context = make_context()
    context.user_data["complete_request"] = {"id": 1, "name": "Цемент"}
    context.user_data["_flow"] = "complete"
    context.user_data["_step"] = COMPLETE_COMMENT

    await route_text(update, context)

    mock_handler.assert_called_once()
    # Результат применён: шаг обновлён
    assert context.user_data["_step"] == 51


@pytest.mark.asyncio
@patch("bot.is_admin", return_value=True)
@patch("bot.complete_comment_text_handler", new_callable=AsyncMock)
@patch("bot.start_add_request", new_callable=AsyncMock)
async def test_route_text_menu_not_eaten_as_comment(mock_add, mock_handler, mock_admin):
    """Кнопки меню не съедаются как комментарий: сценарий прерывается."""
    from bot import route_text, COMPLETE_COMMENT

    mock_add.return_value = None
    update = make_update(text="➕ Добавить заявку")
    context = make_context()
    context.user_data["complete_request"] = {"id": 1}
    context.user_data["_flow"] = "complete"
    context.user_data["_step"] = COMPLETE_COMMENT

    result = await route_text(update, context)

    mock_handler.assert_not_called()
    mock_add.assert_called_once()
    assert result is None
    assert "complete_request" not in context.user_data
    assert "_flow" not in context.user_data


@pytest.mark.asyncio
@patch("bot.is_admin", return_value=True)
@patch("bot.complete_comment_text_handler", new_callable=AsyncMock)
@patch("bot._safe_reply", new_callable=AsyncMock)
async def test_route_text_no_flow_generic_hint(mock_reply, mock_handler, mock_admin):
    """Без активного сценария текст не уходит в comment handler."""
    from bot import route_text

    update = make_update(text="Привет")
    context = make_context()

    result = await route_text(update, context)

    mock_handler.assert_not_called()
    assert result is None
    mock_reply.assert_called_once()


# ===== Автоотчёт =====


@pytest.mark.asyncio
@patch("bot.send_long_message", new_callable=AsyncMock)
@patch("bot.build_report")
async def test_scheduled_report_unpacks_four_tuple(mock_build, mock_send):
    """scheduled_report должен принимать 4-кортеж от build_report (regression)."""
    from bot import scheduled_report

    mock_build.return_value = (
        "<b>Отчёт</b>",
        [1, 2],
        [1, 2, 3],
        [{"id": 1, "status": "new", "request_date": "2026-10-01"}],
    )
    context = make_context()
    context.job.chat_id = -100123

    await scheduled_report(context)

    mock_build.assert_called_once()
    mock_send.assert_called_once()
    assert mock_send.call_args[0][1] == -100123
    assert mock_send.call_args[0][2] == "<b>Отчёт</b>"
    keyboard = mock_send.call_args.kwargs["reply_markup"]
    assert keyboard is not None
    btn_texts = [btn.text for row in keyboard.inline_keyboard for btn in row]
    assert any("#1" in t for t in btn_texts)
    assert any("#3" in t for t in btn_texts)
    assert context.bot_data["_report_running_-100123"] is False


@pytest.mark.asyncio
@patch("bot.send_long_message", new_callable=AsyncMock)
@patch("bot.build_report", side_effect=ValueError("boom"))
async def test_scheduled_report_error_clears_flag(mock_build, mock_send):
    """Ошибка внутри автоотчёта не должна оставлять флаг зацикливания."""
    from bot import scheduled_report

    context = make_context()
    context.job.chat_id = -100999
    context.bot.send_message = AsyncMock()

    await scheduled_report(context)

    assert context.bot_data["_report_running_-100999"] is False


# ===== B4: отмена заявки из карточки =====


def make_callback_update(data, user_id=123):
    update = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.from_user.id = user_id
    update.callback_query.message.chat_id = 42
    update.effective_user.id = user_id
    update.effective_user.first_name = "Тест"
    return update


def _active_row(req_id=5, status="new", done_date=None):
    return {
        "id": req_id,
        "name": "Цемент",
        "quantity": 10,
        "unit": "меш",
        "initiator": "Иван",
        "object": "Склад",
        "status": status,
        "done_date": done_date,
        "request_date": "2026-01-01",
        "notes": "",
    }


def _markup_datas(send_mock):
    datas = []
    for call in send_mock.call_args_list:
        markup = call.kwargs.get("reply_markup")
        if markup is None:
            continue
        for row in markup.inline_keyboard:
            for btn in row:
                datas.append(btn.callback_data)
    return datas


@pytest.mark.asyncio
@patch("bot.get_request_photos", return_value=[])
@patch("bot.get_request_by_id")
@patch("bot.ADMIN_IDS", [123])
async def test_admin_card_shows_cancel_button_for_active(mock_get, mock_photos):
    """Кнопка «🚫 Отменить» есть у админа для активной заявки."""
    from bot import show_request_details

    mock_get.return_value = _active_row()
    update = make_callback_update("detail:5", user_id=123)
    context = make_context()

    await show_request_details(update, context)

    datas = _markup_datas(context.bot.send_message)
    assert "reqcx:5" in datas
    assert "crdone:5" in datas  # B2: завершение из карточки


@pytest.mark.asyncio
@patch("bot.is_request_owner", return_value=True)
@patch("bot.get_request_photos", return_value=[])
@patch("bot.get_request_by_id")
@patch("bot.ADMIN_IDS", [123])
async def test_user_card_hides_cancel_button(mock_get, mock_photos, mock_owner):
    """Обычный пользователь (владелец) не видит административной кнопки."""
    from bot import show_request_details

    mock_get.return_value = _active_row()
    update = make_callback_update("detail:5", user_id=777)
    context = make_context()

    await show_request_details(update, context)

    datas = _markup_datas(context.bot.send_message)
    assert "reqcx:5" not in datas
    assert "crdone:5" not in datas


@pytest.mark.asyncio
@patch("bot.get_request_photos", return_value=[])
@patch("bot.get_request_by_id")
@patch("bot.ADMIN_IDS", [123])
async def test_done_request_card_hides_cancel_button(mock_get, mock_photos):
    """Для выполненной заявки кнопка отмены не показывается."""
    from bot import show_request_details

    mock_get.return_value = _active_row(status="done", done_date="2026-01-05")
    update = make_callback_update("detail:5", user_id=123)
    context = make_context()

    await show_request_details(update, context)

    datas = _markup_datas(context.bot.send_message)
    assert "reqcx:5" not in datas
    assert "crdone:5" not in datas


@pytest.mark.asyncio
@patch("bot.get_initiator_tg_id", return_value=777)
@patch("bot.cancel_request")
@patch("bot.get_request_by_id")
@patch("bot.ADMIN_IDS", [123])
async def test_cancel_flow_with_comment_notifies_initiator(
    mock_get, mock_cancel, mock_tg
):
    """Полный флоу отмены: карточка -> комментарий -> подтверждение."""
    from bot import (
        CANCEL_COMMENT,
        CANCEL_CONFIRM,
        cancel_confirm_handler,
        cancel_flow_start,
        route_text,
    )

    mock_get.return_value = _active_row()
    mock_cancel.return_value = (_active_row(status="cancelled"), True)

    context = make_context()

    # 1) Вход в флоу по кнопке из карточки
    result = await cancel_flow_start(make_callback_update("reqcx:5"), context)
    assert result == CANCEL_COMMENT
    assert context.user_data["_flow"] == "cancel_request"
    assert context.user_data["_step"] == CANCEL_COMMENT
    assert context.user_data["cancel_request"]["id"] == 5

    # 2) Комментарий текстом через единый роутер
    await route_text(make_update(text="Неактуально"), context)
    assert context.user_data["_step"] == CANCEL_CONFIRM
    assert context.user_data["cancel_comment"] == "Неактуально"

    # 3) Подтверждение
    await cancel_confirm_handler(make_callback_update("reqcxyes:5"), context)

    mock_cancel.assert_called_once()
    assert mock_cancel.call_args.kwargs["comment"] == "Неактуально"
    # Состояние завершено
    assert "_flow" not in context.user_data
    assert "cancel_request" not in context.user_data
    # Уведомление инициатору (tg_id=777)
    assert any(
        call.kwargs.get("chat_id") == 777
        for call in context.bot.send_message.call_args_list
    )


@pytest.mark.asyncio
@patch("bot.get_initiator_tg_id", return_value=777)
@patch("bot.cancel_request")
@patch("bot.get_request_by_id")
@patch("bot.ADMIN_IDS", [123])
async def test_cancel_repeat_is_idempotent_in_ui(mock_get, mock_cancel, mock_tg):
    """Повторное подтверждение: «уже отменена», без второго уведомления."""
    from bot import (
        CANCEL_COMMENT,
        CANCEL_CONFIRM,
        cancel_confirm_handler,
        cancel_flow_start,
        route_text,
    )

    mock_get.return_value = _active_row()
    # Второй вызов: строка уже cancelled, changed=False
    mock_cancel.return_value = (_active_row(status="cancelled"), False)

    context = make_context()
    await cancel_flow_start(make_callback_update("reqcx:5"), context)
    await route_text(make_update(text="Комментарий"), context)
    confirm = make_callback_update("reqcxyes:5")
    await cancel_confirm_handler(confirm, context)

    # Идемпотентно: повторно НЕ уведомляем инициатора
    assert not any(
        call.kwargs.get("chat_id") == 777
        for call in context.bot.send_message.call_args_list
    )
    edits = [
        str(call.args[0])
        for call in confirm.callback_query.edit_message_text.call_args_list
    ]
    assert any("уже была отменена" in e for e in edits)


# ===== B2: завершение заявки из подробной карточки =====


@pytest.mark.asyncio
@patch("bot.get_initiator_tg_id", return_value=555)
@patch("bot.complete_request")
@patch("bot.get_request_by_id")
@patch("bot.ADMIN_IDS", [123])
async def test_complete_from_card_flow(mock_get, mock_complete, mock_tg):
    """15/18: карточка -> crdone -> без комментария -> подтверждение.

    Сохраняются уведомления (общий чат + инициатор) и вызов
    атомарного complete_request.
    """
    from bot import (
        COMPLETE_COMMENT,
        COMPLETE_CONFIRM,
        complete_comment_handler,
        complete_confirm_handler,
        complete_from_card_handler,
    )
    from config import TELEGRAM_CHAT_ID

    mock_get.return_value = _active_row()
    mock_complete.return_value = _active_row(status="done", done_date="2026-01-10")

    context = make_context()

    # 1) Кнопка «✅ Завершить» в карточке
    result = await complete_from_card_handler(make_callback_update("crdone:5"), context)
    assert result == COMPLETE_COMMENT
    assert context.user_data["_flow"] == "complete"
    assert context.user_data["_step"] == COMPLETE_COMMENT
    assert context.user_data["complete_request"]["id"] == 5

    # 2) «Без комментария»
    update_nc = make_callback_update("cr:nocomment")
    result = await complete_comment_handler(update_nc, context)
    assert result == COMPLETE_CONFIRM
    assert context.user_data["_step"] == COMPLETE_CONFIRM

    # 3) Подтверждение
    update_confirm = make_callback_update("cr:confirm")
    await complete_confirm_handler(update_confirm, context)

    # Атомарное завершение вызвано с нужным ID
    assert mock_complete.call_args.args[0] == 5
    # Уведомление в общий чат
    assert any(
        call.kwargs.get("chat_id") == int(TELEGRAM_CHAT_ID)
        for call in context.bot.send_message.call_args_list
    )
    # Уведомление инициатору (tg_id=555)
    assert any(
        call.kwargs.get("chat_id") == 555
        for call in context.bot.send_message.call_args_list
    )
    # Состояние завершено
    assert "_flow" not in context.user_data
    assert "complete_request" not in context.user_data


@pytest.mark.asyncio
@patch("bot.get_request_by_id")
@patch("bot.ADMIN_IDS", [123])
async def test_complete_from_card_rejects_done_request(mock_get):
    """17/15: повторное завершение из устаревшей кнопки не выполняется."""
    from bot import complete_from_card_handler

    mock_get.return_value = _active_row(status="done", done_date="2026-01-10")
    context = make_context()
    update = make_callback_update("crdone:5")

    result = await complete_from_card_handler(update, context)

    assert result is None
    assert "_flow" not in context.user_data
    assert "complete_request" not in context.user_data
    edits = [
        str(call.args[0])
        for call in update.callback_query.edit_message_text.call_args_list
    ]
    assert any("нельзя завершить" in e or "Выполнена" in e for e in edits)


# ===== B3: раздел «✅ Выполненные заявки» =====


@pytest.mark.asyncio
@patch("bot.get_completed_requests_paginated")
@patch("bot.ADMIN_IDS", [123])
async def test_completed_list_pagination(mock_paginated):
    """Список выполненных: полная пагинация со счётчиком, без скрытых лимитов."""
    from bot import _COMPLETED_PAGE_SIZE, completed_start

    rows = [
        {
            "id": i,
            "name": f"Заявка {i}",
            "quantity": 1,
            "unit": "шт",
            "initiator": "Иван",
            "object": "Склад",
            "status": "done",
            "done_date": "2026-01-10",
            "request_date": "2026-01-01",
            "notes": "",
        }
        for i in range(1, _COMPLETED_PAGE_SIZE + 1)
    ]
    mock_paginated.return_value = (rows, 12)

    update = make_update(text="✅ Выполненные заявки", user_id=123)
    context = make_context()
    await completed_start(update, context)

    # OFFSET учитывает страницу
    assert mock_paginated.call_args.kwargs["offset"] == 0
    assert mock_paginated.call_args.kwargs["limit"] == _COMPLETED_PAGE_SIZE

    reply_kwargs = update.message.reply_text.call_args.kwargs
    text = update.message.reply_text.call_args.args[0]
    assert "Всего: 12" in text
    assert "Страница 1/2" in text
    datas = [
        btn.callback_data
        for row in reply_kwargs["reply_markup"].inline_keyboard
        for btn in row
    ]
    # Строки карточек с номером страницы + переход на 2-ю страницу
    assert "cd:0:1" in datas
    assert "cd:0:8" in datas
    assert "cdp:1" in datas


@pytest.mark.asyncio
@patch("bot.get_request_photos", return_value=["file1"])
@patch("bot.get_request_by_id")
@patch("bot.ADMIN_IDS", [123])
async def test_completed_detail_back_keeps_page(mock_get, mock_photos):
    """Кнопка «К списку» возвращает на страницу из callback_data."""
    from bot import completed_detail_handler

    mock_get.return_value = _active_row(status="done", done_date="2026-01-10")
    update = make_callback_update("cd:1:5", user_id=123)
    context = make_context()

    await completed_detail_handler(update, context)

    markup = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
    datas = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "cdb:1" in datas  # прежняя страница, не 0
    assert "photos:5" in datas


@pytest.mark.asyncio
@patch("bot.get_completed_requests_paginated", return_value=([], 0))
@patch("bot.ADMIN_IDS", [123])
async def test_completed_list_empty(mock_paginated):
    """Пустой раздел показывает понятное сообщение."""
    from bot import completed_start

    update = make_update(text="✅ Выполненные заявки", user_id=123)
    context = make_context()
    await completed_start(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "Нет выполненных" in text


@pytest.mark.asyncio
@patch("bot.ADMIN_IDS", [123])
async def test_completed_start_denied_for_user():
    """Обычный пользователь не открывает раздел."""
    from bot import completed_start

    update = make_update(text="✅ Выполненные заявки", user_id=777)
    context = make_context()
    await completed_start(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "Нет доступа" in text
    context.bot.send_message.assert_not_called()
