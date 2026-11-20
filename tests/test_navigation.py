"""Навигация: главное меню из любого состояния, /start, тайм-ауты, инварианты."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
)

from bot import (
    COMPLETE_COMMENT,
    COMPLETE_CONFIRM,
    COMPLETE_SELECT,
    CONVERSATION_TIMEOUT_SEC,
    NAME,
    _apply_step_result,
    _arm_timeout,
    cancel,
    on_flow_timeout,
    register_handlers,
    route_text,
    start,
)
from telegram.ext import ConversationHandler as CH

# ===== Вспомогательные функции =====

MENU_BUTTONS = [
    "➕ Добавить заявку",
    "📋 Мои заявки",
    "📋 Заявки",
    "✅ Завершить заявку",
    "✅ Выполненные заявки",
    "📊 Анализ",
    "📊 Дашборд",
    "⚙️ Настройки",
    "⛔ Выход",
]

_MENU_TARGETS = {
    "➕ Добавить заявку": "start_add_request",
    "📋 Мои заявки": "my_requests_start",
    "📋 Заявки": "requests_list_start",
    "✅ Завершить заявку": "complete_start",
    "✅ Выполненные заявки": "completed_start",
    "📊 Анализ": "analysis_start",
    "📊 Дашборд": "dashboard_start",
    "⚙️ Настройки": "admin_start",
}


def make_update(text=None, user_id=123, first_name="Тест"):
    update = type("U", (), {})()
    update.effective_user = type("U2", (), {})()
    update.effective_user.id = user_id
    update.effective_user.first_name = first_name
    update.effective_chat = type("C", (), {})()
    update.effective_chat.id = 42
    update.message = type("M", (), {})()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def make_context(user_data=None):
    context = type("Ctx", (), {})()
    context.user_data = dict(user_data or {})
    context.bot = type("B", (), {})()
    context.bot.send_message = AsyncMock()
    context.bot_data = {}
    context.job_queue = type("JQ", (), {})()
    context.job_queue.run_once = MagicMock()
    context.job_queue.run_daily = MagicMock()
    return context


def complete_select_state():
    """Состояние «Завершить заявку» без выбора (COMPLETE_SELECT)."""
    return {
        "_flow": "complete",
        "_step": COMPLETE_SELECT,
        "complete_requests": [{"id": 1, "name": "Цемент", "object": "Склад"}],
        # временные ключи других сценариев для проверки очистки
        "awaiting_search": True,
        "my_requests": [{"id": 9}],
        "card_request": {"id": 2},
        "edit_request": {"id": 3},
        "status_request": {"id": 4},
    }


def _replies(update):
    return [
        str(call.args[0]) if call.args else ""
        for call in update.message.reply_text.call_args_list
    ]


# ===== 1-5: главное меню работает из COMPLETE_SELECT с первого нажатия =====


@pytest.mark.parametrize("button", MENU_BUTTONS)
@pytest.mark.asyncio
@patch("bot.ADMIN_IDS", [123])
async def test_menu_from_complete_select_opens_section_first_press(button):
    """Кнопка меню из состояния выбора открывает раздел с первого нажатия,
    не отвечает «Выберите заявку кнопкой из списка» и чистит временные ключи."""
    context = make_context(complete_select_state())
    update = make_update(text=button, user_id=123)

    if button == "⛔ Выход":
        await route_text(update, context)
        assert any("До встречи" in r for r in _replies(update))
    else:
        target = _MENU_TARGETS[button]
        with patch(f"bot.{target}", new_callable=AsyncMock) as entry:
            await route_text(update, context)
            entry.assert_called_once()

    # Подсказка из COMPLETE_SELECT не должна появляться для кнопок меню
    assert not any("Выберите заявку кнопкой" in r for r in _replies(update))
    # Временные ключи сценариев очищены, сценарий прерван
    for key in (
        "_flow",
        "_step",
        "complete_requests",
        "awaiting_search",
        "my_requests",
        "card_request",
        "edit_request",
        "status_request",
    ):
        assert key not in context.user_data, key


@pytest.mark.asyncio
@patch("bot.ADMIN_IDS", [123])
async def test_menu_button_with_variation_selector_matches():
    """Кнопка без U+FE0F (Telegram может опустить селектор) тоже распознаётся."""
    context = make_context(complete_select_state())
    update = make_update(text="⚙ Настройки", user_id=123)

    with patch("bot.admin_start", new_callable=AsyncMock) as entry:
        await route_text(update, context)

    entry.assert_called_once()


@pytest.mark.asyncio
@patch("bot.ADMIN_IDS", [123])
async def test_complete_select_text_still_gets_hint():
    """Обычный текст вне меню в состоянии выбора получает контекстную подсказку."""
    context = make_context(complete_select_state())
    update = make_update(text="просто текст", user_id=123)

    await route_text(update, context)

    assert any("Выберите заявку кнопкой" in r for r in _replies(update))
    # Состояние не разрушено подсказкой
    assert context.user_data["_flow"] == "complete"


# ===== 6: /start восстанавливает управление, черновик сохраняется =====


@pytest.mark.asyncio
@patch("bot.clear_conversation_state")
@patch("bot.rate_limiter")
@patch("bot.ADMIN_IDS", [123])
async def test_start_clears_scenarios_but_keeps_draft(mock_limiter, mock_clear):
    mock_limiter.is_allowed.return_value = True
    context = make_context(
        {
            "_flow": "complete",
            "_step": COMPLETE_COMMENT,
            "complete_request": {"id": 1},
            "awaiting_search": True,
            "name": "Кирпич",  # поле формы
            "_saving": True,
            "draft_id": "d1",
        }
    )
    update = make_update(text="/start", user_id=123)

    result = await start(update, context)

    assert result == CH.END
    # Временные ключи и поля формы очищены
    for key in (
        "_flow",
        "_step",
        "complete_request",
        "awaiting_search",
        "name",
        "_saving",
        "draft_id",
    ):
        assert key not in context.user_data, key
    # Персистентный черновик в БД не тронут
    mock_clear.assert_not_called()

    # После /start первая же кнопка меню работает
    update2 = make_update(text="📊 Анализ", user_id=123)
    with patch("bot.analysis_start", new_callable=AsyncMock) as entry:
        await route_text(update2, context)
        entry.assert_called_once()


# ===== 7: тайм-аут очищает состояние, уведомляет и возвращает меню =====


@pytest.mark.asyncio
@patch("bot.clear_conversation_state")
@patch("bot._safe_send", new_callable=AsyncMock)
async def test_timeout_notifies_and_keeps_draft(mock_send, mock_clear):
    context = make_context(
        {
            "_flow": "create",
            "_step": NAME,
            "name": "Кирпич",
            "_saving": True,
            "_flow_token": "tok-1",
        }
    )
    context.job = type(
        "J",
        (),
        {
            "data": {
                "user_id": 123,
                "chat_id": 42,
                "flow_token": "tok-1",
            }
        },
    )()

    await on_flow_timeout(context)

    mock_send.assert_called_once()
    send_args = mock_send.call_args
    assert send_args.args[1] == 42  # chat_id
    assert "тайм-аут" in str(send_args.args[2])
    # Состояние очищено, поля формы убраны, черновик БД цел
    for key in ("_flow", "_step", "name", "_saving", "_flow_token"):
        assert key not in context.user_data, key
    mock_clear.assert_not_called()

    # Меню после тайм-аута работает
    update = make_update(text="📋 Заявки", user_id=123)
    with patch("bot.requests_list_start", new_callable=AsyncMock) as entry:
        await route_text(update, context)
        entry.assert_called_once()


@pytest.mark.asyncio
@patch("bot._safe_send", new_callable=AsyncMock)
async def test_stale_timeout_job_is_noop(mock_send):
    """Устаревший job с другим flow_token ничего не делает."""
    context = make_context({"_flow": "create", "_step": NAME, "_flow_token": "new"})
    context.job = type(
        "J",
        (),
        {"data": {"user_id": 123, "chat_id": 42, "flow_token": "old"}},
    )()

    await on_flow_timeout(context)

    mock_send.assert_not_called()
    assert context.user_data["_flow"] == "create"
    assert context.user_data["_flow_token"] == "new"


def test_arm_timeout_passes_ids_and_token():
    """_arm_timeout планирует job с chat_id/user_id и уникальным токеном."""
    context = make_context()

    _arm_timeout(context, chat_id=42, user_id=123)

    context.job_queue.run_once.assert_called_once()
    kwargs = context.job_queue.run_once.call_args.kwargs
    assert kwargs["chat_id"] == 42
    assert kwargs["user_id"] == 123
    assert kwargs["data"]["user_id"] == 123
    assert kwargs["data"]["chat_id"] == 42
    assert kwargs["data"]["flow_token"] == context.user_data["_flow_token"]
    # Тайм-аут берётся из конфигурации
    assert context.job_queue.run_once.call_args.args[1] == CONVERSATION_TIMEOUT_SEC


def test_transition_to_inline_step_disarms_timer():
    """Переход из текстового шага в inline-шаг снимает таймер."""
    context = make_context()
    context.user_data["_flow"] = "complete"
    context.user_data["_step"] = COMPLETE_COMMENT
    _arm_timeout(context, chat_id=42, user_id=123)
    assert "_timeout_job" in context.user_data

    # COMPLETE_CONFIRM — inline-шаг: таймер снимается
    _apply_step_result(context, COMPLETE_CONFIRM)

    assert context.user_data["_step"] == COMPLETE_CONFIRM
    assert "_timeout_job" not in context.user_data


# ===== 8: старые inline-кнопки не повреждают новый сценарий =====


@pytest.mark.asyncio
@patch("bot.get_request_photos", return_value=[])
@patch("bot.get_request_by_id")
@patch("bot.ADMIN_IDS", [123])
async def test_stale_detail_callback_keeps_active_flow(mock_get, mock_photos):
    """Stateless-кнопка detail: не трогает активный сценарий и черновик."""
    from bot import show_request_details

    mock_get.return_value = {
        "id": 5,
        "name": "Цемент",
        "quantity": 10,
        "unit": "меш",
        "initiator": "Иван",
        "object": "Склад",
        "status": "new",
        "request_date": "2026-01-01",
        "notes": "",
    }
    context = make_context(
        {"_flow": "create", "_step": NAME, "name": "Кирпич", "draft_id": "d1"}
    )
    update = type("U", (), {})()
    update.callback_query = type("Q", (), {})()
    update.callback_query.data = "detail:5"
    update.callback_query.answer = AsyncMock()
    update.callback_query.message = type("Msg", (), {})()
    update.callback_query.message.chat_id = 42
    update.effective_user = type("Us", (), {})()
    update.effective_user.id = 123

    await show_request_details(update, context)

    assert context.user_data["_flow"] == "create"
    assert context.user_data["name"] == "Кирпич"
    assert context.user_data["draft_id"] == "d1"


@pytest.mark.asyncio
@patch("bot.get_request_by_id")
@patch("bot.ADMIN_IDS", [123])
async def test_stale_complete_callback_starts_scenario_safely(mock_get):
    """Устаревшая кнопка cr:{id} начинает сценарий, безопасно прерывая чужой."""
    from bot import complete_select_handler

    mock_get.return_value = {
        "id": 7,
        "name": "Цемент",
        "quantity": 5,
        "unit": "меш",
        "initiator": "Иван",
        "object": "Склад",
        "request_date": "2026-01-01",
        "done_date": None,
        "notes": "",
    }
    # Активен посторонний сценарий редактирования
    context = make_context({"_flow": "edit", "_step": 141, "edit_field": "name"})
    update = type("U", (), {})()
    update.callback_query = type("Q", (), {})()
    update.callback_query.data = "cr:7"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.effective_user = type("Us", (), {})()
    update.effective_user.id = 123
    update.effective_chat = type("C", (), {})()
    update.effective_chat.id = 42

    result = await complete_select_handler(update, context)

    assert result == COMPLETE_COMMENT
    assert context.user_data["_flow"] == "complete"
    assert context.user_data["_step"] == COMPLETE_COMMENT
    assert context.user_data["complete_request"]["id"] == 7
    # Чужой сценарий прерван
    assert "edit_field" not in context.user_data
    # Включён тайм-аут текстового шага
    assert context.job_queue.run_once.called


# ===== _saving: очистка при ошибке сохранения =====


@pytest.mark.asyncio
@patch("bot.add_request_with_photos", side_effect=RuntimeError("db locked"))
@patch("bot._safe_reply", new_callable=AsyncMock)
@patch("bot.ADMIN_IDS", [123])
async def test_saving_flag_cleared_after_save_error(mock_reply, mock_add):
    from bot import confirm_request

    context = make_context(
        {
            "_flow": "create",
            "_step": 9,  # CONFIRM
            "name": "Кирпич",
            "quantity": "10",
            "unit": "шт",
            "initiator": "Иван",
            "object": "Склад",
            "notes": "",
            "photos": [],
        }
    )
    update = make_update(text="✅ Подтвердить", user_id=123)

    result = await confirm_request(update, context)

    assert result == 9  # CONFIRM — можно повторить
    assert "_saving" not in context.user_data


# ===== Инварианты регистрации (критерий полного отказа от CH) =====


def _build_app():
    return ApplicationBuilder().token("123456:TEST-TOKEN").build()


def test_register_handlers_has_no_conversation_handler():
    """ConversationHandler не используется как оркестратор."""
    app = _build_app()
    register_handlers(app)
    group = app.handlers[0]
    convs = [h for h in group if isinstance(h, ConversationHandler)]
    assert convs == []


def test_route_text_registered_before_other_text_handlers():
    """Главный текстовый роутер идёт раньше прочих текстовых хендлеров."""
    app = _build_app()
    register_handlers(app)
    group = app.handlers[0]
    text_callbacks = [
        h.callback.__name__
        for h in group
        if isinstance(h, MessageHandler)
        and getattr(h, "filters", None) is not None
        and h.callback.__name__ in ("route_text", "route_non_text")
    ]
    assert text_callbacks[0] == "route_text"
    assert "route_non_text" in text_callbacks
    # Команды /start и /cancel зарегистрированы
    commands = [
        h for h in group if isinstance(h, CommandHandler) and "start" in h.commands
    ]
    assert commands


def test_callback_patterns_are_unique():
    """Все callback-паттерны в группе уникальны (нет коллизий)."""
    app = _build_app()
    register_handlers(app)
    group = app.handlers[0]
    patterns = [
        h.pattern for h in group if isinstance(h, CallbackQueryHandler) and h.pattern
    ]
    assert len(patterns) == len(set(patterns))


# ===== Отмена из текста вне создания не дёргает черновик =====


@pytest.mark.asyncio
@patch("bot.clear_conversation_state")
@patch("bot.ADMIN_IDS", [123])
async def test_cancel_button_in_inline_screen_keeps_draft(mock_clear):
    context = make_context(complete_select_state())
    update = make_update(text="❌ Отмена", user_id=123)

    result = await cancel(update, context)

    assert result == CH.END
    mock_clear.assert_not_called()
    assert context.user_data == {}


# ===== Роутер dispatch для редактирования, настроек и поиска =====


@pytest.mark.asyncio
@patch("bot.edit_input_handler", new_callable=AsyncMock)
async def test_route_text_dispatches_edit_input(mock_edit):
    """_flow=edit/EDIT_INPUT_VALUE принимает текст."""
    from bot import EDIT_INPUT_VALUE, route_text

    mock_edit.return_value = EDIT_INPUT_VALUE
    context = make_context({"_flow": "edit", "_step": EDIT_INPUT_VALUE})
    update = make_update(text="Новое значение", user_id=123)

    await route_text(update, context)

    mock_edit.assert_called_once()
    assert context.user_data["_step"] == EDIT_INPUT_VALUE


@pytest.mark.asyncio
@patch("bot.admin_rename_new_handler", new_callable=AsyncMock)
async def test_route_text_dispatches_admin_rename(mock_admin_text):
    """_flow=admin/ADMIN_RENAME_NEW принимает текст."""
    from bot import ADMIN_LIST_VIEW, ADMIN_RENAME_NEW, route_text

    mock_admin_text.return_value = ADMIN_LIST_VIEW
    context = make_context({"_flow": "admin", "_step": ADMIN_RENAME_NEW})
    update = make_update(text="Новое имя объекта", user_id=123)

    await route_text(update, context)

    mock_admin_text.assert_called_once()
    # Успешный возврат к списку — inline-шаг, таймер снят
    assert context.user_data["_step"] == ADMIN_LIST_VIEW
    assert "_timeout_job" not in context.user_data


@pytest.mark.asyncio
@patch("bot.my_requests_search_text_handler", new_callable=AsyncMock)
async def test_route_text_dispatches_search(mock_search):
    """_flow=my_search принимает поисковый запрос."""
    from bot import MY_REQUESTS_SELECT, route_text

    mock_search.return_value = MY_REQUESTS_SELECT
    context = make_context(
        {"_flow": "my_search", "_step": MY_REQUESTS_SELECT, "awaiting_search": True}
    )
    update = make_update(text="кирпич", user_id=123)

    await route_text(update, context)

    mock_search.assert_called_once()


# ===== Регистрация не генерирует PTBUserWarning =====


def test_register_handlers_no_ptb_warnings():
    """После рефакторинга не должно оставаться предупреждений PTB."""
    import warnings

    from telegram.warnings import PTBUserWarning

    app = _build_app()
    with warnings.catch_warnings():
        warnings.simplefilter("error", PTBUserWarning)
        register_handlers(app)
