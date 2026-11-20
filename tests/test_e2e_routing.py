"""E2E-маршрутизация через Application.process_update.

Проверяет реальную регистрацию обработчиков (regex-фильтры главного меню,
команды, callback-паттерны), а не прямые вызовы функций.
"""

from unittest.mock import AsyncMock, patch

import pytest
from telegram import Update, User
from telegram.ext import ExtBot

import bot as bot_module
from bot import (
    CONFIRM,
    INITIATOR,
    NAME,
    NOTES,
    OBJECT,
    QUANTITY,
    UNIT,
)

TOKEN = "123456:TEST-TOKEN"


def make_app():
    app = bot_module.ApplicationBuilder().token(TOKEN).build()
    bot_module.register_handlers(app)
    return app


def text_update(app, text, uid=1, user_id=123, chat_id=42):
    message = {
        "message_id": uid,
        "date": 1767225600,
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": user_id, "is_bot": False, "first_name": "Тест"},
        "text": text,
    }
    # У реальных команд есть bot_command-entity (без неё CommandHandler
    # и filters.COMMAND не распознают сообщение).
    if text.startswith("/"):
        command = text.split()[0]
        message["entities"] = [
            {"type": "bot_command", "offset": 0, "length": len(command)}
        ]
    data = {"update_id": uid, "message": message}
    return Update.de_json(data, app.bot)


def _sent_texts(send_mock):
    return [str(call.kwargs.get("text", "")) for call in send_mock.call_args_list]


async def _initialize(app, me):
    """Инициализация без сети: get_me замокан, кэш бота — вручную.

    CommandHandler.check_update обращается к bot.username, которому нужен
    _bot_user (обычно заполняется реальным get_me).
    """
    await app.initialize()
    app.bot._bot_user = me


@pytest.mark.asyncio
async def test_menu_works_from_complete_select_via_process_update():
    """Главное меню работает с первого нажатия из состояния выбора заявки."""
    app = make_app()
    me = User(id=1, is_bot=True, first_name="TestBot", username="testbot")
    active = [
        {
            "id": 1,
            "name": "Цемент",
            "object": "Склад",
            "status": "new",
            "done_date": None,
            "quantity": 1,
            "unit": "меш",
            "initiator": "Иван",
            "request_date": "2026-01-01",
            "notes": "",
        }
    ]
    with patch.object(ExtBot, "get_me", new=AsyncMock(return_value=me)), patch.object(
        ExtBot, "send_message", new_callable=AsyncMock
    ) as send, patch("bot.get_active_requests", return_value=active), patch(
        "bot.get_requests_by_status", return_value=[]
    ), patch(
        "bot.rate_limiter"
    ) as limiter:
        limiter.is_allowed.return_value = True
        await _initialize(app, me)
        try:
            # Вход в завершение через regex кнопки меню (реальный фильтр)
            await app.process_update(text_update(app, "✅ Завершить заявку"))
            assert app.user_data[123]["_flow"] == "complete"
            assert app.user_data[123]["_step"] == 50  # COMPLETE_SELECT

            # Первое нажатие другой кнопки меню открывает раздел
            await app.process_update(text_update(app, "📋 Заявки", uid=2))
            assert "complete_requests" not in app.user_data[123]
            assert app.user_data[123].get("_flow") != "complete"

            texts = _sent_texts(send)
            assert not any("Выберите заявку кнопкой" in t for t in texts)
            assert any("Заявки" in t for t in texts)
        finally:
            await app.shutdown()


@pytest.mark.asyncio
async def test_start_resets_and_menu_opens_section_via_process_update():
    """После /start первая же кнопка меню работает, черновик в БД цел."""
    app = make_app()
    me = User(id=1, is_bot=True, first_name="TestBot", username="testbot")
    # Засеиваем «зависший» сценарий и поле формы
    app.user_data[123].update(
        {
            "_flow": "complete",
            "_step": 51,
            "complete_request": {"id": 5},
            "name": "Кирпич",
            "_saving": True,
        }
    )
    with patch.object(ExtBot, "get_me", new=AsyncMock(return_value=me)), patch.object(
        ExtBot, "send_message", new_callable=AsyncMock
    ) as send, patch("bot.rate_limiter") as limiter, patch(
        "bot.clear_conversation_state"
    ) as clear_state, patch(
        "bot.load_conversation_state", return_value=None
    ):
        limiter.is_allowed.return_value = True
        await _initialize(app, me)
        try:
            await app.process_update(text_update(app, "/start"))

            data = app.user_data[123]
            for key in ("_flow", "_step", "complete_request", "name", "_saving"):
                assert key not in data, key
            # Персистентный черновик не удалялся
            clear_state.assert_not_called()

            # Первая кнопка меню после /start открывает раздел
            await app.process_update(text_update(app, "➕ Добавить заявку", uid=2))
            assert app.user_data[123]["_flow"] == "create"
            assert app.user_data[123]["_step"] == NAME
            texts = _sent_texts(send)
            assert any("Шаг 1/6" in t for t in texts)
        finally:
            await app.shutdown()


@pytest.mark.asyncio
async def test_full_create_flow_via_process_update():
    """Полный сценарий создания заявки через реальную маршрутизацию."""
    app = make_app()
    me = User(id=1, is_bot=True, first_name="TestBot", username="testbot")
    lists = {
        "initiator": ["Иван"],
        "object": ["Склад"],
        "unit": ["шт"],
    }
    uid = 1

    def send(text):
        nonlocal uid
        uid += 1
        return text_update(app, text, uid=uid)

    with patch.object(ExtBot, "get_me", new=AsyncMock(return_value=me)), patch.object(
        ExtBot, "send_message", new_callable=AsyncMock
    ), patch("bot.rate_limiter") as limiter, patch(
        "bot.load_conversation_state", return_value=None
    ), patch(
        "bot.save_conversation_state"
    ), patch(
        "bot.clear_conversation_state"
    ) as clear_state, patch(
        "bot.get_initiator_by_tg_id", return_value=None
    ), patch(
        "bot.get_initiator_tg_id", return_value=None
    ), patch(
        "bot.get_all_lists", return_value=lists
    ), patch(
        "bot.get_setting", return_value="4"
    ), patch(
        "bot.add_request_with_photos", return_value=77
    ) as add_mock:
        limiter.is_allowed.return_value = True
        await _initialize(app, me)
        try:
            steps = [
                ("➕ Добавить заявку", NAME),
                ("Кирпич", QUANTITY),
                ("10", UNIT),
                ("шт", INITIATOR),
                ("Иван", OBJECT),
                ("Склад", NOTES),
                ("без замечаний", CONFIRM),
            ]
            for text, expected_step in steps:
                await app.process_update(send(text))
                assert app.user_data[123]["_step"] == expected_step, text

            await app.process_update(send("✅ Подтвердить"))

            # Заявка сохранена ровно один раз, состояние завершено
            add_mock.assert_called_once()
            assert "_flow" not in app.user_data[123]
            assert "_saving" not in app.user_data[123]
            clear_state.assert_called_once()
        finally:
            await app.shutdown()


@pytest.mark.asyncio
async def test_menu_button_during_create_keeps_persistent_draft():
    """Нажатие меню посреди создания прерывает сценарий, черновик БД цел."""
    app = make_app()
    me = User(id=1, is_bot=True, first_name="TestBot", username="testbot")
    uid = 1

    def send(text):
        nonlocal uid
        uid += 1
        return text_update(app, text, uid=uid)

    with patch.object(ExtBot, "get_me", new=AsyncMock(return_value=me)), patch.object(
        ExtBot, "send_message", new_callable=AsyncMock
    ), patch("bot.rate_limiter") as limiter, patch(
        "bot.load_conversation_state", return_value=None
    ), patch(
        "bot.save_conversation_state"
    ), patch(
        "bot.clear_conversation_state"
    ) as clear_state, patch(
        "bot.get_initiator_by_tg_id", return_value=None
    ), patch(
        "bot.analysis_start", new_callable=AsyncMock
    ) as analysis:
        limiter.is_allowed.return_value = True
        await _initialize(app, me)
        try:
            await app.process_update(send("➕ Добавить заявку"))
            await app.process_update(send("Кирпич"))
            assert app.user_data[123]["_step"] == QUANTITY

            # Кнопка меню посреди сценария
            await app.process_update(send("📊 Анализ"))

            analysis.assert_called_once()
            assert "_flow" not in app.user_data[123]
            # conversation_state (персистентный черновик) не удалялся
            clear_state.assert_not_called()
        finally:
            await app.shutdown()
