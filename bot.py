import asyncio
import json
import logging
from datetime import time, datetime, date
from html import escape
from zoneinfo import ZoneInfo
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler,
)
from telegram.error import RetryAfter, TelegramError
from telegram.request import HTTPXRequest

from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    ADMIN_IDS,
    REPORT_HOUR,
    REPORT_MINUTE,
    SOCKS5_PROXY,
    RATE_LIMIT_REQUESTS,
    RATE_LIMIT_WINDOW_SEC,
    MAX_FIELD_LENGTH,
    MAX_PHOTOS_PER_REQUEST,
    REPORT_TIMEZONE,
    OVERDUE_DAYS,
)
from core import build_report, build_dashboard
from db import (
    init_db,
    get_all_lists,
    get_list_items,
    get_list_value_by_id,
    add_request_with_photos,
    rename_list_value,
    delete_list_value,
    add_list_value,
    get_setting,
    set_setting,
    get_active_requests,
    complete_request,
    complete_requests_batch,
    link_initiator,
    get_initiator_tg_id,
    get_initiator_by_tg_id,
    get_request_photos,
    get_photo_counts,
    clear_all_requests,
    backup_db,
    verify_backup,
    save_conversation_state,
    load_conversation_state,
    clear_conversation_state,
    audit_log,
    get_audit_log,
    update_request_status,
    edit_request,
    get_request_by_id,
    get_user_requests,
    search_requests,
    is_request_owner,
    delete_request,
    get_requests_by_status,
    VALID_STATUSES,
    STATUS_LABELS,
    save_user_input,
)
from utils import validate_quantity, split_message, RateLimiter, truncate_field

logger = logging.getLogger(__name__)

# ===== RATE LIMITER =====
rate_limiter = RateLimiter(
    max_requests=RATE_LIMIT_REQUESTS, window_sec=RATE_LIMIT_WINDOW_SEC
)

# ===== СОСТОЯНИЯ CONVERSATION =====
(
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
) = range(10)

# ===== СОСТОЯНИЯ ЗАВЕРШЕНИЯ ЗАЯВКИ =====
COMPLETE_SELECT, COMPLETE_COMMENT, COMPLETE_CONFIRM = range(50, 53)

# ===== СОСТОЯНИЕ РЕГИСТРАЦИИ ИНИЦИАТОРА =====
REG_NAME = 90

# ===== СОСТОЯНИЯ АДМИН-ПАНЕЛИ =====
(
    ADMIN_MENU,
    ADMIN_LIST_VIEW,
    ADMIN_RENAME_OLD,
    ADMIN_RENAME_NEW,
    ADMIN_DELETE,
    ADMIN_ADD_VALUE,
    ADMIN_BUTTONS_EDIT,
    ADMIN_CLEAR_DB_CONFIRM,
) = range(100, 108)

# ===== СОСТОЯНИЯ СМЕНЫ СТАТУСА =====
STATUS_SELECT, STATUS_CONFIRM = range(110, 112)

# ===== СОСТОЯНИЯ ПОИСКА/ФИЛЬТРОВ =====
MY_REQUESTS_SELECT = 120

# ===== СОСТОЯНИЯ РАЗДЕЛА «ЗАЯВКИ» =====
REQUESTS_LIST, REQUESTS_CARD = 130, 131

# ===== СОСТОЯНИЯ РЕДАКТИРОВАНИЯ ЗАЯВКИ =====
EDIT_SELECT_FIELD, EDIT_INPUT_VALUE = 140, 141

# ===== СОСТОЯНИЯ УДАЛЕНИЯ ЗАЯВКИ =====
DELETE_CONFIRM = 145

# ===== КЛАВИАТУРЫ =====
CANCEL_KEYBOARD = ReplyKeyboardMarkup(
    [["❌ Отмена"]], resize_keyboard=True, one_time_keyboard=False
)

BACK_CANCEL_KEYBOARD = ReplyKeyboardMarkup(
    [["⬅️ Назад", "❌ Отмена"]], resize_keyboard=True, one_time_keyboard=False
)


# ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====
def is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором"""
    return user_id in ADMIN_IDS


def _user_name(update: Update) -> str:
    """Возвращает имя пользователя для аудита."""
    u = update.effective_user
    return u.first_name or u.username or str(u.id)


def get_main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    """Возвращает главную клавиатуру в зависимости от прав"""
    if is_admin(user_id):
        # Админское меню с логической группировкой
        keyboard = [
            # Основные действия
            ["➕ Добавить заявку", "📋 Мои заявки"],
            # Управление заявками
            ["📋 Заявки", "✅ Завершить заявку"],
            # Аналитика
            ["📊 Дашборд", "📊 Анализ"],
            # Настройки
            ["⚙️ Настройки"],
            # Выход
            ["⛔ Выход"],
        ]
    else:
        # Пользовательское меню
        keyboard = [
            ["➕ Добавить заявку"],
            ["📋 Мои заявки"],
            ["⛔ Выход"],
        ]

    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


async def _safe_reply(update, text, **kwargs):
    """Отправляет сообщение с обработкой RetryAfter."""
    try:
        return await update.message.reply_text(text, **kwargs)
    except RetryAfter as e:
        logger.warning(f"RetryAfter: ждём {e.retry_after} сек")
        await asyncio.sleep(e.retry_after)
        return await update.message.reply_text(text, **kwargs)
    except TelegramError as e:
        logger.error(f"TelegramError в _safe_reply: {e}")


async def _safe_send(context, chat_id, text, **kwargs):
    """Отправляет сообщение с обработкой RetryAfter."""
    try:
        return await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except RetryAfter as e:
        logger.warning(f"RetryAfter: ждём {e.retry_after} сек")
        await asyncio.sleep(e.retry_after)
        return await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except TelegramError as e:
        logger.error(f"TelegramError в _safe_send: {e}")


async def _safe_edit(query, text, **kwargs):
    """Редактирует сообщение с обработкой RetryAfter."""
    try:
        return await query.edit_message_text(text, **kwargs)
    except RetryAfter as e:
        logger.warning(f"RetryAfter: ждём {e.retry_after} сек")
        await asyncio.sleep(e.retry_after)
        return await query.edit_message_text(text, **kwargs)
    except TelegramError as e:
        logger.error(f"TelegramError в _safe_edit: {e}")


async def _safe_callback_reply(update, text, **kwargs):
    """Отправляет ответ в контексте callback (без update.message)."""
    try:
        chat_id = None
        if (
            hasattr(update, "callback_query")
            and update.callback_query
            and update.callback_query.message
        ):
            chat_id = update.callback_query.message.chat_id
        elif hasattr(update, "effective_chat") and update.effective_chat:
            chat_id = update.effective_chat.id
        if chat_id:
            # Используем bot.send_message напрямую
            await update.callback_query.message.chat.send_message(text, **kwargs)
            return
        logger.error("_safe_callback_reply: не удалось определить chat_id")
    except TelegramError as e:
        logger.error(f"TelegramError в _safe_callback_reply: {e}")


async def send_long_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    parse_mode: str = None,
    disable_web_page_preview: bool = True,
    reply_markup=None,
):
    """Отправляет длинное сообщение по частям"""
    parts = split_message(text)
    for i, part in enumerate(parts):
        markup = reply_markup if i == len(parts) - 1 else None
        await _safe_send(
            context,
            chat_id,
            part,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
            reply_markup=markup,
        )


def _truncate_callback_data(prefix: str, value: str, max_bytes: int = 64) -> str:
    """Обрезает значение так, чтобы prefix + value не превышали max_bytes в UTF-8."""
    result = value
    while len((prefix + result).encode("utf-8")) > max_bytes and result:
        result = result[:-1]
    return result


def _build_photo_keyboard(
    ids_with_photos: list, all_ids: list = None, requests_data: list = None
) -> InlineKeyboardMarkup:
    """Создаёт inline-клавиатуру с кнопками деталей и просмотра фото."""
    buttons = []
    # Кнопки деталей для всех заявок
    if all_ids:
        for rid in all_ids:
            has_photo = "📎" if rid in (ids_with_photos or []) else ""

            # Определяем эмодзи статуса
            status_emoji = "⚪"  # по умолчанию - новая
            if requests_data:
                for req in requests_data:
                    if req.get("id") == rid:
                        status = req.get("status", "new")
                        if status == "done":
                            status_emoji = "⚪"  # выполненная - белый
                        elif status == "ordered":
                            status_emoji = "🟡"  # заказанная - желтый
                        elif status == "delivered":
                            status_emoji = "🔵"  # доставленная - синий
                        else:
                            # Проверяем просрочку
                            req_date_str = req.get("request_date")
                            if req_date_str:
                                try:
                                    req_date = date.fromisoformat(req_date_str)
                                    today = date.today()
                                    if (today - req_date).days > OVERDUE_DAYS:
                                        status_emoji = "🔴"  # просроченная - красный
                                    else:
                                        status_emoji = "🟢"  # новая - зеленый
                                except (ValueError, TypeError):
                                    status_emoji = "🟢"
                            else:
                                status_emoji = "🟢"
                        break

            buttons.append(
                [
                    InlineKeyboardButton(
                        f"{status_emoji} #{rid} {has_photo}".strip(),
                        callback_data=f"detail:{rid}",
                    )
                ]
            )
    elif ids_with_photos:
        # Только фото (обратная совместимость)
        for rid in ids_with_photos:
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"📎 Фото к заявке #{rid}", callback_data=f"photos:{rid}"
                    )
                ]
            )
    return InlineKeyboardMarkup(buttons) if buttons else None


def _build_pagination_keyboard(
    items: list, page: int, page_size: int, callback_prefix: str
) -> tuple:
    """Создаёт клавиатуру с пагинацией. Возвращает (buttons, total_pages)."""
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    end = start + page_size
    page_items = items[start:end]

    buttons = []
    for item in page_items:
        buttons.append(item)

    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                "⬅️ Назад", callback_data=f"{callback_prefix}:{page - 1}"
            )
        )
    if page < total_pages - 1:
        nav.append(
            InlineKeyboardButton(
                "Вперёд ➡️", callback_data=f"{callback_prefix}:{page + 1}"
            )
        )
    if nav:
        buttons.append(nav)

    return buttons, total_pages


async def show_request_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет фото заявки по нажатию inline-кнопки.
    Проверка прав: только админ или инициатор заявки."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    req_id = int(query.data.split(":")[1])

    if not is_admin(user_id) and not is_request_owner(req_id, user_id):
        await _safe_send(
            context, query.message.chat_id, "⛔ У вас нет доступа к этой заявке."
        )
        return

    file_ids = get_request_photos(req_id)

    if not file_ids:
        await _safe_send(context, query.message.chat_id, "📷 Фото не найдены.")
        return

    for file_id in file_ids:
        try:
            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=file_id,
                disable_notification=True,
            )
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await context.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=file_id,
                    disable_notification=True,
                )
            except Exception as e2:
                logger.warning(f"Не удалось отправить фото после retry: {e2}")
        except Exception as e:
            logger.warning(f"Не удалось отправить фото {file_id}: {e}")
            await _safe_send(
                context, query.message.chat_id, "⚠️ Не удалось отправить одно из фото."
            )


async def show_request_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает детали заявки по нажатию inline-кнопки из отчёта.
    Проверка прав: только админ или инициатор заявки."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    req_id = int(query.data.split(":")[1])

    if not is_admin(user_id) and not is_request_owner(req_id, user_id):
        await _safe_send(
            context, query.message.chat_id, "⛔ У вас нет доступа к этой заявке."
        )
        return

    req = get_request_by_id(req_id)

    if not req:
        await _safe_send(context, query.message.chat_id, "⚠️ Заявка не найдена.")
        return

    qty_text = f"{req.get('quantity', '—')} {req.get('unit', '')}".strip()
    status_label = STATUS_LABELS.get(req.get("status", "new"), req.get("status", "—"))
    notes = req.get("notes", "")
    photos = get_request_photos(req_id)
    photo_text = f"\n📎 Фото: {len(photos)} шт." if photos else ""

    text = (
        f"📋 <b>Заявка #{req['id']}</b>\n\n"
        f"📦 Наименование: <b>{escape(req.get('name', '—'))}</b>\n"
        f"🔢 Количество: <code>{escape(qty_text)}</code>\n"
        f"👤 Инициатор: {escape(req.get('initiator', '—'))}\n"
        f"🏗️ Объект: {escape(req.get('object', '—'))}\n"
        f"📊 Статус: {status_label}\n"
        f"📅 Дата: {escape(req.get('request_date', '—'))}"
    )
    if notes and notes != "-":
        text += f"\n📝 Дополнительно: {escape(notes)}"
    if photo_text:
        text += photo_text

    keyboard = None
    if photos:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"📎 Фото ({len(photos)})", callback_data=f"photos:{req_id}"
                    )
                ]
            ]
        )

    await _safe_send(
        context,
        query.message.chat_id,
        text,
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


# ===== КОМАНДЫ =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    user_id = update.effective_user.id
    username = update.effective_user.first_name or "пользователь"

    # Проверяем rate limit
    if not rate_limiter.is_allowed(user_id):
        await _safe_reply(update, "⏳ Слишком много запросов. Подождите немного.")
        return ConversationHandler.END

    if is_admin(user_id):
        markup = get_main_keyboard(user_id)
        text = (
            f"👋 Здравствуйте, <b>{escape(username)}</b>!\n\n"
            f"Вы вошли как <b>администратор</b>.\n"
            f"Автоотчёт приходит каждый день в {REPORT_HOUR:02d}:{REPORT_MINUTE:02d}.\n\n"
            "📋 <b>Возможности администратора:</b>\n"
            "• ➕ Создавать заявки на материалы\n"
            "• 📋 Просматривать и управлять всеми заявками\n"
            "• ✅ Завершать заявки и менять статусы\n"
            "• 📊 Смотреть аналитику и дашборд\n"
            "• ⚙️ Настраивать справочники\n\n"
            "Выберите действие из меню ниже:"
        )
        await _safe_reply(update, text, reply_markup=markup, parse_mode="HTML")
        return ConversationHandler.END
    else:
        existing = get_initiator_by_tg_id(user_id)
        if existing:
            await _safe_reply(
                update,
                f"👋 Здравствуйте, <b>{escape(username)}</b>!\n\n"
                f"Вы привязаны как <b>{escape(existing)}</b>.\n\n"
                "📋 <b>Ваши возможности:</b>\n"
                "• ➕ Создавать заявки на стройматериалы\n"
                "• 📋 Просматривать свои заявки\n"
                "• 🔔 Получать уведомления о статусе\n\n"
                "Выберите действие из меню ниже:",
                reply_markup=get_main_keyboard(user_id),
                parse_mode="HTML",
            )
            return ConversationHandler.END
        else:
            return await register_start(update, context)


async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список инициаторов для привязки"""
    lists = get_all_lists()
    initiators = [i for i in lists.get("initiator", []) if i != "Другое"]

    if not initiators:
        await _safe_reply(
            update,
            "⚠️ Список инициаторов пуст. Обратитесь к администратору.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    cols = int(get_setting("buttons_per_row_initiator", "4"))
    keyboard = [initiators[i : i + cols] for i in range(0, len(initiators), cols)]
    keyboard.append(["Другое"])

    await _safe_reply(
        update,
        "👋 <b>Добро пожаловать в бот «Управление заявками»!</b>\n\n"
        "📦 <b>Что я умею:</b>\n"
        "• Создавать заявки на стройматериалы\n"
        "• Отслеживать статус ваших заявок\n"
        "• Отправлять уведомления о выполнении\n\n"
        "👤 <b>Давайте настроим ваш профиль!</b>\n\n"
        "Выберите своё имя из списка ниже.\n"
        "Это нужно, чтобы:\n"
        "• Автоматически подставлять имя в заявки\n"
        "• Получать уведомления о ваших заявках\n\n"
        "<i>Если вашего имени нет в списке — выберите «Другое»</i>",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode="HTML",
    )
    return REG_NAME


async def register_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет имя инициатора"""
    text = update.message.text.strip()
    user_id = update.effective_user.id

    # Ограничение длины
    text = truncate_field(text, MAX_FIELD_LENGTH)

    if text == "Другое":
        await _safe_reply(
            update,
            "✍️ Введите ваше имя (как в справочнике или новое):",
            reply_markup=ReplyKeyboardRemove(),
        )
        return REG_NAME

    existing_tg = get_initiator_tg_id(text)
    if existing_tg and existing_tg != user_id:
        await _safe_reply(
            update,
            f"⚠️ Имя «{escape(text)}» уже привязано к другому пользователю.\n"
            "Выберите другое имя или обратитесь к администратору.",
            parse_mode="HTML",
        )
        return REG_NAME

    link_initiator(text, user_id)
    audit_log(None, user_id, _user_name(update), "registered", f"Имя: {text}")
    await _safe_reply(
        update,
        f"🎉 <b>Отлично! Вы зарегистрированы как «{escape(text)}»</b>\n\n"
        "📋 <b>Что теперь:</b>\n"
        "• Ваше имя будет автоматически подставляться в заявки\n"
        "• Вы будете получать уведомления о статусе заявок\n\n"
        "🚀 <b>Быстрый старт:</b>\n"
        "1. Нажмите «➕ Добавить заявку»\n"
        "2. Заполните форму (это займёт 1-2 минуты)\n"
        "3. Получите номер заявки и отслеживайте статус\n\n"
        "<i>💡 Совет: Используйте кнопки меню для навигации</i>",
        reply_markup=get_main_keyboard(user_id),
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def run_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запускает анализ вручную"""
    if not is_admin(update.effective_user.id):
        await _safe_reply(update, "⛔ У вас нет прав для этой команды.")
        return

    await _safe_reply(update, "⏳ Запускаю анализ...")

    try:
        result, ids_with_photos, all_ids, requests_data = build_report(mode="active")
        keyboard = _build_photo_keyboard(ids_with_photos, all_ids, requests_data)
        await send_long_message(
            context,
            update.effective_chat.id,
            result,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error(f"Ошибка анализа: {e}", exc_info=True)
        await _safe_reply(
            update, "❌ Ошибка при формировании отчёта. Попробуйте позже."
        )


async def scheduled_report(context: ContextTypes.DEFAULT_TYPE):
    """Автоматический ежедневный отчёт с защитой от зацикливания."""
    chat_id = context.job.chat_id

    # Защита от зацикливания: проверяем флаг в bot_data
    report_flag = f"_report_running_{chat_id}"
    if context.bot_data.get(report_flag):
        logger.warning(f"Отчёт для chat_id={chat_id} уже выполняется — пропускаем")
        return
    context.bot_data[report_flag] = True

    try:
        result, _ids, _all = build_report()
        await send_long_message(context, chat_id, result, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка автоотчёта: {e}", exc_info=True)
        try:
            await _safe_send(context, chat_id, "❌ Ошибка при формировании автоотчёта.")
        except Exception:
            pass
    finally:
        context.bot_data[report_flag] = False


# ===== CONVERSATION HANDLER =====
async def start_add_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало добавления заявки"""
    user_id = update.effective_user.id

    if not rate_limiter.is_allowed(user_id):
        await _safe_reply(update, "⏳ Слишком много запросов. Подождите немного.")
        return ConversationHandler.END

    # Проверяем наличие сохранённого черновика
    saved_state = load_conversation_state(user_id)
    if saved_state:
        try:
            saved_data = json.loads(saved_state)
            if isinstance(saved_data, dict) and saved_data.get("draft_id"):
                keyboard = ReplyKeyboardMarkup(
                    [
                        ["✏️ Продолжить черновик", "🔄 Новая заявка"],
                        ["❌ Отмена"],
                    ],
                    resize_keyboard=True,
                )
                summary_parts = []
                if saved_data.get("name"):
                    summary_parts.append(f"📦 {saved_data['name']}")
                if saved_data.get("quantity"):
                    summary_parts.append(
                        f"🔢 {saved_data['quantity']} {saved_data.get('unit', '')}"
                    )
                summary = (
                    "\n".join(summary_parts) if summary_parts else "(пустой черновик)"
                )
                await _safe_reply(
                    update,
                    f"📋 Найден незавершённый черновик:\n\n{summary}\n\n"
                    "Продолжить заполнение или начать заново?",
                    reply_markup=keyboard,
                    parse_mode="HTML",
                )
                context.user_data["pending_draft"] = saved_data
                return NAME
        except (json.JSONDecodeError, TypeError):
            pass

    context.user_data.clear()
    context.user_data["draft_id"] = f"{user_id}_{datetime.now().timestamp()}"

    await _safe_reply(
        update,
        "📦 <b>Шаг 1/6:</b> Введите наименование материала или товара:",
        reply_markup=CANCEL_KEYBOARD,
        parse_mode="HTML",
    )
    return NAME


async def set_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет название и запрашивает количество"""
    text = update.message.text.strip()

    # Проверка на отмену
    cancel_keywords = ["❌ Отмена", "Отмена", "отмена", "cancel"]
    if text in cancel_keywords or text.lower() in ["отмена", "cancel"]:
        user_id = update.effective_user.id
        context.user_data.clear()
        clear_conversation_state(user_id)
        await _safe_reply(
            update, "❌ Заявка отменена.", reply_markup=get_main_keyboard(user_id)
        )
        return ConversationHandler.END

    # Обработка выбора черновика
    if text == "✏️ Продолжить черновик":
        pending = context.user_data.pop("pending_draft", None)
        if pending:
            context.user_data.update(pending)
            clear_conversation_state(update.effective_user.id)
            # Определяем следующий шаг по заполненности данных
            if not pending.get("name"):
                await _safe_reply(
                    update,
                    "📦 <b>Шаг 1/6:</b> Введите наименование материала или товара:",
                    reply_markup=CANCEL_KEYBOARD,
                    parse_mode="HTML",
                )
                return NAME
            if not pending.get("quantity"):
                await _safe_reply(
                    update,
                    f"📦 Текущее наименование: <code>{escape(pending['name'])}</code>\n\n"
                    "🔢 <b>Шаг 2/6:</b> Введите количество:",
                    reply_markup=CANCEL_KEYBOARD,
                    parse_mode="HTML",
                )
                return QUANTITY
            # Для остальных шагов — показываем сводку и просим продолжить
            summary = f"📦 {escape(pending['name'])} | 🔢 {escape(str(pending.get('quantity', '')))} {escape(pending.get('unit', ''))}"
            await _safe_reply(
                update,
                f"📋 Черновик восстановлен: {summary}\n\n"
                "Продолжайте заполнение с текущего шага. Введите значение для следующего поля "
                "или нажмите ❌ Отмена.",
                reply_markup=CANCEL_KEYBOARD,
                parse_mode="HTML",
            )
            return NAME
        # Черновик пуст — новая заявка
        context.user_data.clear()
        context.user_data["draft_id"] = (
            f"{update.effective_user.id}_{datetime.now().timestamp()}"
        )
        await _safe_reply(
            update,
            "📦 <b>Шаг 1/6:</b> Введите наименование материала или товара:",
            reply_markup=CANCEL_KEYBOARD,
            parse_mode="HTML",
        )
        return NAME

    if text == "🔄 Новая заявка":
        context.user_data.clear()
        clear_conversation_state(update.effective_user.id)
        context.user_data["draft_id"] = (
            f"{update.effective_user.id}_{datetime.now().timestamp()}"
        )
        await _safe_reply(
            update,
            "📦 <b>Шаг 1/6:</b> Введите наименование материала или товара:",
            reply_markup=CANCEL_KEYBOARD,
            parse_mode="HTML",
        )
        return NAME

    name = truncate_field(text, MAX_FIELD_LENGTH)
    context.user_data["name"] = name
    _save_draft(update, context)

    # Сохраняем в историю
    user_id = update.effective_user.id
    save_user_input(user_id, "name", name)

    await _safe_reply(
        update,
        "🔢 <b>Шаг 2/6:</b> Введите количество:",
        reply_markup=BACK_CANCEL_KEYBOARD,
        parse_mode="HTML",
    )
    return QUANTITY


async def set_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет количество и запрашивает единицу измерения"""
    text = update.message.text.strip()

    # Проверка на отмену
    cancel_keywords = ["❌ Отмена", "Отмена", "отмена", "cancel"]
    if text in cancel_keywords or text.lower() in ["отмена", "cancel"]:
        user_id = update.effective_user.id
        context.user_data.clear()
        clear_conversation_state(user_id)
        await _safe_reply(
            update, "❌ Заявка отменена.", reply_markup=get_main_keyboard(user_id)
        )
        return ConversationHandler.END

    # Обработка кнопки "Назад"
    if text == "⬅️ Назад":
        await _safe_reply(
            update,
            "📦 <b>Шаг 1/6:</b> Введите наименование материала или товара:\n\n"
            f"Текущее значение: <code>{escape(context.user_data.get('name', ''))}</code>",
            reply_markup=BACK_CANCEL_KEYBOARD,
            parse_mode="HTML",
        )
        return NAME

    if not validate_quantity(text):
        await _safe_reply(
            update,
            "❌ <b>Неверный формат количества</b>\n\n"
            "Пожалуйста, введите положительное число.\n\n"
            "✅ <b>Правильные примеры:</b>\n"
            "• 10\n"
            "• 5.5\n"
            "• 0.25\n"
            "• 1000\n\n"
            "❌ <b>Неправильные примеры:</b>\n"
            "• -5 (отрицательное)\n"
            "• 0 (ноль)\n"
            "• abc (текст)\n\n"
            "💡 <i>Используйте точку или запятую для дробей</i>\n\n"
            "Или нажмите «⬅️ Назад» для возврата к предыдущему шагу.",
            reply_markup=BACK_CANCEL_KEYBOARD,
            parse_mode="HTML",
        )
        return QUANTITY

    context.user_data["quantity"] = text

    # Сохраняем в историю
    user_id = update.effective_user.id
    save_user_input(user_id, "quantity", text)

    try:
        lists = get_all_lists()
        units = lists.get("unit", ["шт", "кг", "л", "м2", "м3", "лист", "м.п.", "комп"])
    except Exception:
        units = ["шт", "кг", "л", "м2", "м3", "лист", "м.п.", "комп"]

    cols = int(get_setting("buttons_per_row_unit", "3"))
    keyboard = [units[i : i + cols] for i in range(0, len(units), cols)]
    keyboard.append(["⬅️ Назад", "❌ Отмена"])
    _save_draft(update, context)
    await _safe_reply(
        update,
        "📏 <b>Шаг 3/6:</b> Выберите единицу:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode="HTML",
    )
    return UNIT


async def set_unit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет единицу измерения и запрашивает инициатора (или ставит автоматически)"""
    text = update.message.text.strip()

    # Проверка на отмену
    cancel_keywords = ["❌ Отмена", "Отмена", "отмена", "cancel"]
    if text in cancel_keywords or text.lower() in ["отмена", "cancel"]:
        user_id = update.effective_user.id
        context.user_data.clear()
        clear_conversation_state(user_id)
        await _safe_reply(
            update, "❌ Заявка отменена.", reply_markup=get_main_keyboard(user_id)
        )
        return ConversationHandler.END

    # Обработка кнопки "Назад"
    if text == "⬅️ Назад":
        # Возвращаемся к вводу количества
        await _safe_reply(
            update,
            "🔢 <b>Шаг 2/6:</b> Введите количество:\n\n"
            f"Текущее значение: <code>{escape(context.user_data.get('quantity', ''))}</code>",
            reply_markup=BACK_CANCEL_KEYBOARD,
            parse_mode="HTML",
        )
        return QUANTITY

    context.user_data["unit"] = truncate_field(text, MAX_FIELD_LENGTH)

    user_id = update.effective_user.id
    linked_name = get_initiator_by_tg_id(user_id)

    if linked_name:
        context.user_data["initiator"] = linked_name
        try:
            lists = get_all_lists()
            objects = lists.get("object", ["Солнечное", "Привилегия", "Не указан"])
        except Exception:
            objects = ["Солнечное", "Привилегия", "Не указан"]

        cols = int(get_setting("buttons_per_row_object", "2"))
        keyboard = [objects[i : i + cols] for i in range(0, len(objects), cols)]
        keyboard.append(["⬅️ Назад", "❌ Отмена"])
        _save_draft(update, context)
        await _safe_reply(
            update,
            f"👤 Инициатор: <b>{escape(linked_name)}</b> (автоматически)\n\n"
            "🏗️ <b>Шаг 5/6:</b> Выберите объект:",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
            parse_mode="HTML",
        )
        return OBJECT

    try:
        lists = get_all_lists()
        initiators = lists.get(
            "initiator", ["Алексей", "Анатолий", "Михаил", "Игорь", "Другое"]
        )
    except Exception as e:
        logger.error(f"Ошибка загрузки списков: {e}")
        initiators = ["Алексей", "Анатолий", "Михаил", "Игорь", "Другое"]

    cols = int(get_setting("buttons_per_row_initiator", "4"))
    keyboard = [initiators[i : i + cols] for i in range(0, len(initiators), cols)]
    keyboard.append(["⬅️ Назад", "❌ Отмена"])
    _save_draft(update, context)
    await _safe_reply(
        update,
        "👤 <b>Шаг 4/6:</b> Выберите инициатора:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode="HTML",
    )
    return INITIATOR


async def set_initiator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет инициатора и запрашивает объект"""
    text = update.message.text.strip()

    # Проверка на отмену
    cancel_keywords = ["❌ Отмена", "Отмена", "отмена", "cancel"]
    if text in cancel_keywords or text.lower() in ["отмена", "cancel"]:
        user_id = update.effective_user.id
        context.user_data.clear()
        clear_conversation_state(user_id)
        await _safe_reply(
            update, "❌ Заявка отменена.", reply_markup=get_main_keyboard(user_id)
        )
        return ConversationHandler.END

    # Обработка кнопки "Назад"
    if text == "⬅️ Назад":
        # Возвращаемся к выбору единицы измерения
        try:
            lists = get_all_lists()
            units = lists.get(
                "unit", ["шт", "кг", "л", "м2", "м3", "лист", "м.п.", "комп"]
            )
        except Exception:
            units = ["шт", "кг", "л", "м2", "м3", "лист", "м.п.", "комп"]

        cols = int(get_setting("buttons_per_row_unit", "3"))
        keyboard = [units[i : i + cols] for i in range(0, len(units), cols)]
        keyboard.append(["⬅️ Назад", "❌ Отмена"])
        await _safe_reply(
            update,
            "📏 <b>Шаг 3/6:</b> Выберите единицу:\n\n"
            f"Текущее значение: <code>{escape(context.user_data.get('unit', ''))}</code>",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
            parse_mode="HTML",
        )
        return UNIT

    if text == "Другое":
        await _safe_reply(
            update,
            "✍️ Введите имя инициатора:",
            reply_markup=BACK_CANCEL_KEYBOARD,
            parse_mode="HTML",
        )
        return INITIATOR_CUSTOM

    context.user_data["initiator"] = truncate_field(text, MAX_FIELD_LENGTH)

    try:
        lists = get_all_lists()
        objects = lists.get("object", ["Солнечное", "Привилегия", "Не указан"])
    except Exception:
        objects = ["Солнечное", "Привилегия", "Не указан"]

    cols = int(get_setting("buttons_per_row_object", "2"))
    keyboard = [objects[i : i + cols] for i in range(0, len(objects), cols)]
    keyboard.append(["⬅️ Назад", "❌ Отмена"])
    _save_draft(update, context)
    await _safe_reply(
        update,
        "🏗️ <b>Шаг 5/6:</b> Выберите объект:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode="HTML",
    )
    return OBJECT


async def set_initiator_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет кастомное имя инициатора и переходит к объектам"""
    context.user_data["initiator"] = truncate_field(
        update.message.text.strip(), MAX_FIELD_LENGTH
    )

    try:
        lists = get_all_lists()
        objects = lists.get("object", ["Солнечное", "Привилегия", "Не указан"])
    except Exception:
        objects = ["Солнечное", "Привилегия", "Не указан"]

    cols = int(get_setting("buttons_per_row_object", "2"))
    keyboard = [objects[i : i + cols] for i in range(0, len(objects), cols)]
    keyboard.append(["❌ Отмена"])
    _save_draft(update, context)
    await _safe_reply(
        update,
        "🏗️ <b>Шаг 5/6:</b> Выберите объект:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode="HTML",
    )
    return OBJECT


async def set_object(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет объект и запрашивает примечание"""
    text = update.message.text.strip()

    # Проверка на отмену
    cancel_keywords = ["❌ Отмена", "Отмена", "отмена", "cancel"]
    if text in cancel_keywords or text.lower() in ["отмена", "cancel"]:
        user_id = update.effective_user.id
        context.user_data.clear()
        clear_conversation_state(user_id)
        await _safe_reply(
            update, "❌ Заявка отменена.", reply_markup=get_main_keyboard(user_id)
        )
        return ConversationHandler.END

    # Обработка кнопки "Назад"
    if text == "⬅️ Назад":
        # Возвращаемся к выбору инициатора
        try:
            lists = get_all_lists()
            initiators = lists.get(
                "initiator", ["Алексей", "Анатолий", "Михаил", "Игорь", "Другое"]
            )
        except Exception as e:
            logger.error(f"Ошибка загрузки списков: {e}")
            initiators = ["Алексей", "Анатолий", "Михаил", "Игорь", "Другое"]

        cols = int(get_setting("buttons_per_row_initiator", "4"))
        keyboard = [initiators[i : i + cols] for i in range(0, len(initiators), cols)]
        keyboard.append(["⬅️ Назад", "❌ Отмена"])
        await _safe_reply(
            update,
            "👤 <b>Шаг 4/6:</b> Выберите инициатора:\n\n"
            f"Текущее значение: <code>{escape(context.user_data.get('initiator', ''))}</code>",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
            parse_mode="HTML",
        )
        return INITIATOR

    if text == "Другое":
        await _safe_reply(
            update,
            "✍️ Введите название объекта:",
            reply_markup=BACK_CANCEL_KEYBOARD,
            parse_mode="HTML",
        )
        return OBJECT_CUSTOM

    context.user_data["object"] = truncate_field(text, MAX_FIELD_LENGTH)

    notes_keyboard = ReplyKeyboardMarkup(
        [["➡️ Пропустить", "⬅️ Назад", "❌ Отмена"]], resize_keyboard=True
    )
    _save_draft(update, context)
    await _safe_reply(
        update,
        "📝 <b>Шаг 6/6:</b> Дополнительно (ссылки допустимы):",
        reply_markup=notes_keyboard,
        parse_mode="HTML",
    )
    return NOTES


async def set_object_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет кастомное название объекта и переходит к примечанию"""
    context.user_data["object"] = truncate_field(
        update.message.text.strip(), MAX_FIELD_LENGTH
    )

    notes_keyboard = ReplyKeyboardMarkup(
        [["➡️ Пропустить", "❌ Отмена"]], resize_keyboard=True
    )
    _save_draft(update, context)
    await _safe_reply(
        update,
        "📝 <b>Шаг 6/6:</b> Дополнительно (ссылки допустимы):",
        reply_markup=notes_keyboard,
        parse_mode="HTML",
    )
    return NOTES


async def set_notes_and_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет примечание и показывает сводку для подтверждения"""
    text = update.message.text.strip()

    # Проверка на отмену
    cancel_keywords = ["❌ Отмена", "Отмена", "отмена", "cancel"]
    if text in cancel_keywords or text.lower() in ["отмена", "cancel"]:
        user_id = update.effective_user.id
        context.user_data.clear()
        clear_conversation_state(user_id)
        await _safe_reply(
            update, "❌ Заявка отменена.", reply_markup=get_main_keyboard(user_id)
        )
        return ConversationHandler.END

    # Обработка кнопки "Назад"
    if text == "⬅️ Назад":
        # Возвращаемся к выбору объекта
        try:
            lists = get_all_lists()
            objects = lists.get("object", ["Солнечное", "Привилегия", "Не указан"])
        except Exception:
            objects = ["Солнечное", "Привилегия", "Не указан"]

        cols = int(get_setting("buttons_per_row_object", "2"))
        keyboard = [objects[i : i + cols] for i in range(0, len(objects), cols)]
        keyboard.append(["⬅️ Назад", "❌ Отмена"])
        await _safe_reply(
            update,
            "🏗️ <b>Шаг 5/6:</b> Выберите объект:\n\n"
            f"Текущее значение: <code>{escape(context.user_data.get('object', ''))}</code>",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
            parse_mode="HTML",
        )
        return OBJECT

    if text in ("-", "➡️ Пропустить"):
        text = ""

    context.user_data["notes"] = truncate_field(text, MAX_FIELD_LENGTH)
    context.user_data["photos"] = []

    # Показываем сводку для подтверждения
    notes = context.user_data.get("notes", "")
    photos = context.user_data.get("photos", [])
    photo_text = f"📎 Фото: {len(photos)} шт." if photos else "📎 Фото: нет"

    summary = (
        "📋 <b>Проверьте заявку:</b>\n\n"
        f"📦 Наименование: <b>{escape(context.user_data['name'])}</b>\n"
        f"🔢 Количество: <code>{escape(context.user_data['quantity'])} {escape(context.user_data['unit'])}</code>\n"
        f"👤 Инициатор: {escape(context.user_data['initiator'])}\n"
        f"🏗️ Объект: {escape(context.user_data['object'])}\n"
        f"📝 Дополнительно: {escape(notes) if notes else '—'}\n"
        f"{photo_text}\n\n"
        "Подтвердите или отмените:"
    )

    keyboard = ReplyKeyboardMarkup(
        [["✅ Подтвердить", "📎 Добавить фото"], ["✏️ Исправить", "❌ Отмена"]],
        resize_keyboard=True,
    )

    await _safe_reply(update, summary, reply_markup=keyboard, parse_mode="HTML")
    return CONFIRM


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет file_id полученного фото"""
    photos = context.user_data.get("photos", [])
    if len(photos) >= MAX_PHOTOS_PER_REQUEST:
        await _safe_reply(
            update,
            f"⚠️ Достигнут лимит ({MAX_PHOTOS_PER_REQUEST} фото). Нажмите «✅ Готово».",
        )
        return PHOTO

    photo = update.message.photo[-1]
    context.user_data.setdefault("photos", []).append(photo.file_id)
    _save_draft(update, context)

    count = len(context.user_data["photos"])

    keyboard = ReplyKeyboardMarkup(
        [["✅ Готово", "❌ Отмена"]],
        resize_keyboard=True,
    )

    await _safe_reply(
        update,
        f"📷 Фото получено ({count}/{MAX_PHOTOS_PER_REQUEST}).\n"
        "Отправьте ещё или нажмите <b>✅ Готово</b>:",
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    return PHOTO


async def handle_photo_hint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подсказка, если пользователь отправил не фото в PHOTO-состоянии"""
    await _safe_reply(update, "📷 Отправьте фото или нажмите кнопку внизу экрана.")
    return PHOTO


async def handle_photo_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершает шаг фото и показывает сводку"""
    notes = context.user_data.get("notes", "")
    photos = context.user_data.get("photos", [])
    photo_text = f"📎 Фото: {len(photos)} шт." if photos else "📎 Фото: нет"

    summary = (
        "📋 <b>Проверьте заявку:</b>\n\n"
        f"📦 Наименование: <b>{escape(context.user_data['name'])}</b>\n"
        f"🔢 Количество: <code>{escape(context.user_data['quantity'])} {escape(context.user_data['unit'])}</code>\n"
        f"👤 Инициатор: {escape(context.user_data['initiator'])}\n"
        f"🏗️ Объект: {escape(context.user_data['object'])}\n"
        f"📝 Дополнительно: {escape(notes) if notes else '—'}\n"
        f"{photo_text}\n\n"
        "Подтвердите или отмените:"
    )

    keyboard = ReplyKeyboardMarkup(
        [["✅ Подтвердить", "📎 Добавить фото"], ["✏️ Исправить", "❌ Отмена"]],
        resize_keyboard=True,
    )

    await _safe_reply(update, summary, reply_markup=keyboard, parse_mode="HTML")
    return CONFIRM


async def confirm_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждает и сохраняет заявку, либо отменяет"""
    text = update.message.text.strip()
    user_id = update.effective_user.id

    if text == "❌ Отмена":
        context.user_data.clear()
        clear_conversation_state(user_id)
        await _safe_reply(
            update, "❌ Заявка отменена.", reply_markup=get_main_keyboard(user_id)
        )
        return ConversationHandler.END

    if text == "📎 Добавить фото":
        # Переход к добавлению фото
        keyboard = ReplyKeyboardMarkup(
            [["✅ Готово", "❌ Отмена"]],
            resize_keyboard=True,
        )
        await _safe_reply(
            update,
            f"📷 Прикрепите фото (до {MAX_PHOTOS_PER_REQUEST} шт.)\n"
            "или нажмите <b>✅ Готово</b>:",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return PHOTO

    if text == "✏️ Исправить":
        # Возврат к шагу NAME с сохранёнными данными
        await _safe_reply(
            update,
            "✏️ <b>Шаг 1/6:</b> Введите наименование материала или товара "
            f"(текущее: <code>{escape(context.user_data.get('name', ''))}</code>):",
            reply_markup=CANCEL_KEYBOARD,
            parse_mode="HTML",
        )
        return NAME

    if text not in ("✅ Подтвердить", "🔄 Повторить"):
        await _safe_reply(
            update,
            "⚠️ Используйте кнопки:\n"
            "✅ Подтвердить — сохранить заявку\n"
            "📎 Добавить фото — прикрепить фото\n"
            "✏️ Исправить — изменить данные\n"
            "❌ Отмена — отменить",
        )
        return CONFIRM

    # Защита от повторного сохранения
    if context.user_data.get("_saving"):
        await _safe_reply(update, "⏳ Заявка уже сохраняется...")
        return CONFIRM
    context.user_data["_saving"] = True

    today = date.today()

    try:
        photos = context.user_data.get("photos", [])
        request_id = add_request_with_photos(
            name=context.user_data["name"],
            quantity=float(context.user_data["quantity"].replace(",", ".")),
            unit=context.user_data["unit"],
            request_date=today,
            initiator=context.user_data["initiator"],
            object_name=context.user_data["object"],
            notes=context.user_data["notes"],
            photo_file_ids=photos,
            user_id=user_id,
            user_name=_user_name(update),
        )

        photo_text = f"📎 Фото: {len(photos)} шт." if photos else ""
        summary = (
            "✅ <b>Заявка успешно добавлена!</b>\n\n"
            f"🆔 Номер: <b>#{request_id}</b>\n"
            f"📦 Наименование: {escape(context.user_data['name'])}\n"
            f"🔢 Количество: {escape(context.user_data['quantity'])} {escape(context.user_data['unit'])}\n"
            f"👤 Инициатор: {escape(context.user_data['initiator'])}\n"
            f"🏗️ Объект: {escape(context.user_data['object'])}\n"
            f"📝 Дополнительно: {escape(context.user_data['notes']) if context.user_data['notes'] else '—'}"
        )
        if photo_text:
            summary += f"\n{photo_text}"

        await _safe_reply(
            update, summary, reply_markup=get_main_keyboard(user_id), parse_mode="HTML"
        )

        # Уведомление администраторам о новой заявке
        admin_notification = (
            "🆕 <b>Новая заявка</b>\n\n"
            f"🆔 Номер: <b>#{request_id}</b>\n"
            f"📦 {escape(context.user_data['name'])}\n"
            f"🔢 {escape(context.user_data['quantity'])} {escape(context.user_data['unit'])}\n"
            f"👤 {escape(context.user_data['initiator'])}\n"
            f"🏗️ {escape(context.user_data['object'])}\n"
        )
        if context.user_data["notes"]:
            admin_notification += f"📝 {escape(context.user_data['notes'])}\n"
        if photos:
            admin_notification += f"📎 Фото: {len(photos)} шт.\n"
        notify_markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"📋 Открыть заявку #{request_id}",
                        callback_data=f"detail:{request_id}",
                    )
                ]
            ]
        )
        try:
            await _safe_send(
                context,
                int(TELEGRAM_CHAT_ID),
                admin_notification,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=notify_markup,
            )
        except Exception as e:
            logger.warning(f"Не удалось отправить уведомление о новой заявке: {e}")

        context.user_data.clear()
        clear_conversation_state(user_id)

    except Exception as e:
        logger.error(f"Ошибка при сохранении заявки: {e}", exc_info=True)
        context.user_data.pop("_saving", None)
        keyboard = ReplyKeyboardMarkup(
            [["🔄 Повторить", "❌ Отмена"]],
            resize_keyboard=True,
        )
        await _safe_reply(
            update,
            "❌ Ошибка при сохранении заявки. Попробуйте ещё раз или отмените.",
            reply_markup=keyboard,
        )
        return CONFIRM

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отменяет текущую операцию"""
    user_id = update.effective_user.id
    context.user_data.clear()
    clear_conversation_state(user_id)

    await _safe_reply(
        update, "❌ Операция отменена.", reply_markup=get_main_keyboard(user_id)
    )
    return ConversationHandler.END


def _save_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет текущее состояние диалога в БД."""
    user_id = update.effective_user.id
    try:
        state = {k: v for k, v in context.user_data.items() if not k.startswith("_")}
        save_conversation_state(user_id, json.dumps(state, ensure_ascii=False))
    except Exception as e:
        logger.warning(f"Не удалось сохранить черновик: {e}")


# ===== ФИЛЬТР АНАЛИЗА =====
async def analysis_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает выбор фильтра анализа"""
    if not is_admin(update.effective_user.id):
        await _safe_reply(update, "⛔ Нет доступа.")
        return ConversationHandler.END

    keyboard = [
        [
            InlineKeyboardButton("📋 Актуальные", callback_data="report:active"),
            InlineKeyboardButton("✅ Выполненные", callback_data="report:completed"),
        ],
    ]
    await _safe_reply(
        update,
        "📊 Какие заявки показать?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return 0


async def analysis_filter_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Генерирует отчёт по выбранному фильтру"""
    query = update.callback_query
    await query.answer()
    mode = query.data.split(":")[1]

    await _safe_edit(query, "⏳ Формирую отчёт...")

    try:
        result, ids_with_photos, all_ids, requests_data = build_report(mode=mode)
        keyboard = _build_photo_keyboard(ids_with_photos, all_ids, requests_data)
        await send_long_message(
            context,
            query.message.chat_id,
            result,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error(f"Ошибка формирования отчёта: {e}", exc_info=True)
        await _safe_edit(query, "❌ Ошибка при формировании отчёта. Попробуйте позже.")

    return ConversationHandler.END


# ===== ЗАВЕРШЕНИЕ ЗАЯВКИ АДМИНИСТРАТОРОМ =====
async def complete_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список активных заявок для завершения с пагинацией"""
    if not is_admin(update.effective_user.id):
        await _safe_reply(update, "⛔ Нет доступа.")
        return ConversationHandler.END

    requests = get_active_requests()
    if not requests:
        await _safe_reply(update, "✅ Нет активных заявок для завершения.")
        return ConversationHandler.END

    context.user_data["complete_requests"] = requests
    return await _show_complete_page(update, context, 0)


async def _show_complete_page(update_or_query, context, page: int):
    """Показывает страницу завершения заявок с пагинацией."""
    requests = context.user_data.get("complete_requests", [])
    if not requests:
        if hasattr(update_or_query, "message"):
            await _safe_reply(update_or_query, "✅ Нет активных заявок.")
        return ConversationHandler.END

    PAGE_SIZE = 8
    total_pages = max(1, (len(requests) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_requests = requests[start:end]

    keyboard = []
    for r in page_requests:
        name = r["name"][:25]
        obj = r.get("object", "—")[:12]
        label = f"#{r['id']} {name} ({obj})"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"cr:{r['id']}")])

    # Пагинация
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"crp:{page - 1}"))
    nav.append(
        InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="crnoop")
    )
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд ➡️", callback_data=f"crp:{page + 1}"))
    keyboard.append(nav)

    # Кнопка массового завершения
    keyboard.append(
        [
            InlineKeyboardButton(
                "✅ Завершить все на странице", callback_data=f"crb:{page}"
            )
        ]
    )

    text = (
        f"✅ <b>Завершение заявки</b>\n"
        f"Всего активных: {len(requests)}\n\n"
        "Выберите заявку:"
    )

    markup = InlineKeyboardMarkup(keyboard)

    if hasattr(update_or_query, "callback_query") and update_or_query.callback_query:
        await _safe_edit(
            update_or_query.callback_query, text, reply_markup=markup, parse_mode="HTML"
        )
    elif hasattr(update_or_query, "message"):
        await _safe_reply(update_or_query, text, reply_markup=markup, parse_mode="HTML")

    return COMPLETE_SELECT


async def complete_page_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка пагинации в завершении заявок"""
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":")[1])
    return await _show_complete_page(update, context, page)


async def complete_batch_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Массовое завершение заявок на текущей странице"""
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":")[1])

    requests = context.user_data.get("complete_requests", [])
    PAGE_SIZE = 8
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_requests = requests[start:end]

    if not page_requests:
        await _safe_edit(query, "⚠️ Нет заявок для завершения.")
        return ConversationHandler.END

    ids = [r["id"] for r in page_requests]
    today = date.today()
    completed = complete_requests_batch(
        ids,
        today,
        user_id=update.effective_user.id,
        user_name=_user_name(update),
    )

    # Определяем фактически завершённые заявки
    completed_ids = []
    for rid in ids:
        req = get_request_by_id(rid)
        if req and req.get("done_date"):
            completed_ids.append(rid)

    completed_text = ", ".join(f"#{i}" for i in completed_ids) if completed_ids else "—"
    await _safe_edit(
        query,
        f"✅ Завершено заявок: <b>{completed}</b>\n" f"Номера: {completed_text}",
        parse_mode="HTML",
    )

    # Уведомления в общий чат (по фактически завершённым)
    if completed_ids:
        notify_text = (
            f"✅ <b>Массовое завершение: {len(completed_ids)} заявок</b>\n"
            f"📅 {today.strftime('%d.%m.%Y')}\n"
            f"📋 Номера: {completed_text}"
        )
        try:
            await _safe_send(
                context,
                int(TELEGRAM_CHAT_ID),
                notify_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning(
                f"Не удалось отправить уведомление о массовом завершении: {e}"
            )

    context.user_data.pop("complete_requests", None)
    return ConversationHandler.END


async def complete_select_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает подтверждение завершения с возможностью добавить комментарий"""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    req_id = int(query.data.split(":")[1])

    selected = get_request_by_id(req_id)
    if not selected or selected.get("done_date"):
        await _safe_edit(query, "⚠️ Заявка не найдена или уже завершена.")
        return ConversationHandler.END

    context.user_data["complete_request"] = selected
    context.user_data["complete_comment"] = ""
    r = selected
    qty_text = f"{r['quantity']} {r['unit']}".strip() if r.get("quantity") else "—"

    text = (
        f"📋 <b>Заявка #{r['id']}</b>\n\n"
        f"📦 {escape(r['name'])}\n"
        f"🔢 {escape(qty_text)}\n"
        f"👤 {escape(r.get('initiator', '—'))}\n"
        f"🏗️ {escape(r.get('object', '—'))}\n\n"
        "Добавьте комментарий или пропустите:"
    )
    keyboard = [
        [InlineKeyboardButton("➡️ Без комментария", callback_data="cr:nocomment")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cr:cancel")],
    ]
    await _safe_edit(
        query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
    )
    return COMPLETE_COMMENT


async def complete_comment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает комментарий к завершению"""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    query = update.callback_query
    data = query.data

    if data == "cr:cancel":
        await query.answer()
        context.user_data.pop("complete_request", None)
        context.user_data.pop("complete_comment", None)
        await _safe_edit(query, "❌ Отменено.")
        return ConversationHandler.END

    if data == "cr:nocomment":
        await query.answer()
        context.user_data["complete_comment"] = ""
        return await _show_complete_final(query, context)

    return COMPLETE_COMMENT


async def complete_comment_text_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Принимает текст комментария от администратора"""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    comment = update.message.text.strip()
    if comment == "❌ Отмена":
        context.user_data.pop("complete_request", None)
        context.user_data.pop("complete_comment", None)
        user_id = update.effective_user.id
        await _safe_reply(
            update, "❌ Отменено.", reply_markup=get_main_keyboard(user_id)
        )
        return ConversationHandler.END

    context.user_data["complete_comment"] = truncate_field(comment, MAX_FIELD_LENGTH)

    r = context.user_data.get("complete_request", {})
    qty_text = f"{r.get('quantity', '—')} {r.get('unit', '')}".strip()

    text = (
        f"📋 <b>Заявка #{r.get('id', '?')}</b>\n\n"
        f"📦 {escape(r.get('name', '—'))}\n"
        f"🔢 {escape(qty_text)}\n"
        f"👤 {escape(r.get('initiator', '—'))}\n"
        f"🏗️ {escape(r.get('object', '—'))}\n"
        f"💬 Комментарий: {escape(comment)}\n\n"
        "Завершить?"
    )
    keyboard = [
        [
            InlineKeyboardButton("✅ Выполнено", callback_data="cr:confirm"),
            InlineKeyboardButton("❌ Отмена", callback_data="cr:cancel"),
        ],
    ]
    await _safe_reply(
        update, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
    )
    return COMPLETE_CONFIRM


async def _show_complete_final(query, context):
    """Показывает финальное подтверждение завершения (без комментария)"""
    r = context.user_data.get("complete_request", {})
    qty_text = f"{r.get('quantity', '—')} {r.get('unit', '')}".strip()

    text = (
        f"📋 <b>Заявка #{r.get('id', '?')}</b>\n\n"
        f"📦 {escape(r.get('name', '—'))}\n"
        f"🔢 {escape(qty_text)}\n"
        f"👤 {escape(r.get('initiator', '—'))}\n"
        f"🏗️ {escape(r.get('object', '—'))}\n\n"
        "Завершить?"
    )
    keyboard = [
        [
            InlineKeyboardButton("✅ Выполнено", callback_data="cr:confirm"),
            InlineKeyboardButton("❌ Отмена", callback_data="cr:cancel"),
        ],
    ]
    await _safe_edit(
        query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
    )
    return COMPLETE_CONFIRM


async def complete_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершает заявку и отправляет уведомление с комментарием"""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cr:cancel":
        context.user_data.pop("complete_request", None)
        context.user_data.pop("complete_comment", None)
        await _safe_edit(query, "❌ Отменено.")
        return ConversationHandler.END

    r = context.user_data.get("complete_request")
    if not r:
        await _safe_edit(query, "⚠️ Данные не найдены.")
        return ConversationHandler.END

    comment = context.user_data.get("complete_comment", "")

    today = date.today()
    result = complete_request(
        r["id"],
        today,
        user_id=update.effective_user.id,
        user_name=_user_name(update),
    )

    if not result:
        await _safe_edit(query, "⚠️ Заявка не найдена.")
        context.user_data.pop("complete_request", None)
        context.user_data.pop("complete_comment", None)
        return ConversationHandler.END

    # Уведомляем только если заявка была завершена впервые
    # (result содержит done_date из БД, а не из user_data)
    result_name = result.get("name", r.get("name", "—"))
    result_qty = result.get("quantity")
    result_unit = result.get("unit", "")
    qty_text = f"{result_qty} {result_unit}".strip() if result_qty else "—"
    done_str = result.get("done_date", today.isoformat())
    notification = (
        f"✅ <b>Заявка #{r['id']} выполнена</b>\n\n"
        f"📦 {escape(result_name)}\n"
        f"🔢 {escape(qty_text)}\n"
        f"👤 {escape(result.get('initiator', r.get('initiator', '—')))}\n"
        f"🏗️ {escape(result.get('object', r.get('object', '—')))}\n"
        f"📅 Выполнено: {done_str}"
    )
    if comment:
        notification += f"\n💬 Комментарий: {escape(comment)}"

    notify_markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"📋 Открыть заявку #{r['id']}",
                    callback_data=f"detail:{r['id']}",
                )
            ]
        ]
    )

    await _safe_edit(query, notification, parse_mode="HTML", reply_markup=notify_markup)

    # Уведомление в общий чат
    try:
        await _safe_send(
            context,
            int(TELEGRAM_CHAT_ID),
            notification,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=notify_markup,
        )
    except Exception as e:
        logger.warning(f"Не удалось отправить уведомление в чат: {e}")

    # Уведомление инициатору в ЛС
    initiator_name = r.get("initiator", "")
    if initiator_name:
        tg_id = get_initiator_tg_id(initiator_name)
        if tg_id:
            try:
                await _safe_send(
                    context,
                    tg_id,
                    notification,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=notify_markup,
                )
            except Exception as e:
                logger.warning(
                    f"Не удалось отправить ЛС инициатору {initiator_name}: {e}"
                )

    context.user_data.pop("complete_request", None)
    context.user_data.pop("complete_comment", None)
    return ConversationHandler.END


# ===== СМЕНА СТАТУСА ЗАЯВКИ =====
async def status_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начинает диалог смены статуса заявки (для админов)"""
    if not is_admin(update.effective_user.id):
        await _safe_reply(update, "⛔ Нет доступа.")
        return ConversationHandler.END

    requests = get_active_requests()
    if not requests:
        await _safe_reply(update, "✅ Нет активных заявок.")
        return ConversationHandler.END

    context.user_data["status_requests"] = requests
    return await _show_status_page(update, context, 0)


async def _show_status_page(update_or_query, context, page: int):
    """Показывает страницу выбора заявки для смены статуса."""
    requests = context.user_data.get("status_requests", [])
    if not requests:
        if hasattr(update_or_query, "message"):
            await _safe_reply(update_or_query, "✅ Нет активных заявок.")
        return ConversationHandler.END

    PAGE_SIZE = 8
    total_pages = max(1, (len(requests) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_requests = requests[start:end]

    keyboard = []
    for r in page_requests:
        status_label = STATUS_LABELS.get(r.get("status", "new"), "?")
        name = r["name"][:20]
        label = f"#{r['id']} {name} [{status_label}]"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"ss:{r['id']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"ssp:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд ➡️", callback_data=f"ssp:{page + 1}"))
    if nav:
        keyboard.append(nav)

    text = "🔄 <b>Смена статуса заявки</b>\n\n" "Выберите заявку:"

    markup = InlineKeyboardMarkup(keyboard)

    if hasattr(update_or_query, "callback_query") and update_or_query.callback_query:
        await _safe_edit(
            update_or_query.callback_query, text, reply_markup=markup, parse_mode="HTML"
        )
    elif hasattr(update_or_query, "message"):
        await _safe_reply(update_or_query, text, reply_markup=markup, parse_mode="HTML")

    return STATUS_SELECT


async def status_page_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пагинация для смены статуса"""
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":")[1])
    return await _show_status_page(update, context, page)


async def status_select_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает варианты нового статуса"""
    query = update.callback_query
    await query.answer()
    req_id = int(query.data.split(":")[1])

    req = get_request_by_id(req_id)
    if not req:
        await _safe_edit(query, "⚠️ Заявка не найдена.")
        return ConversationHandler.END

    context.user_data["status_request"] = req
    current = req.get("status", "new")
    current_label = STATUS_LABELS.get(current, current)

    keyboard = []
    for st_key, st_label in STATUS_LABELS.items():
        if st_key == "done":
            continue  # done handled via complete flow
        if st_key == current:
            continue
        keyboard.append([InlineKeyboardButton(st_label, callback_data=f"ssc:{st_key}")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="ssc:cancel")])

    await _safe_edit(
        query,
        f"🔄 <b>Заявка #{req['id']}</b>\n"
        f"📦 {escape(req['name'])}\n"
        f"Текущий статус: {current_label}\n\n"
        "Выберите новый статус:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )
    return STATUS_CONFIRM


async def status_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Применяет новый статус"""
    query = update.callback_query
    await query.answer()
    new_status = query.data.split(":")[1]

    if new_status == "cancel":
        context.user_data.pop("status_request", None)
        await _safe_edit(query, "❌ Отменено.")
        return ConversationHandler.END

    req = context.user_data.get("status_request")
    if not req:
        await _safe_edit(query, "⚠️ Данные не найдены.")
        return ConversationHandler.END

    success = update_request_status(
        req["id"],
        new_status,
        user_id=update.effective_user.id,
        user_name=_user_name(update),
    )

    if success:
        new_label = STATUS_LABELS.get(new_status, new_status)
        await _safe_edit(
            query,
            f"✅ Заявка #{req['id']} → <b>{new_label}</b>",
            parse_mode="HTML",
        )
    else:
        await _safe_edit(query, "❌ Не удалось изменить статус.")

    context.user_data.pop("status_request", None)
    return ConversationHandler.END


# ===== МОИ ЗАЯВКИ =====
async def my_requests_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает заявки текущего пользователя"""
    user_id = update.effective_user.id
    linked_name = get_initiator_by_tg_id(user_id)

    if not linked_name:
        if is_admin(user_id):
            # Админ видит все заявки
            requests = get_active_requests()[:20]
        else:
            await _safe_reply(
                update, "⚠️ Вы не привязаны к инициатору. Используйте /start."
            )
            return ConversationHandler.END
    else:
        requests = get_user_requests(linked_name, status_filter="active")

    if not requests:
        await _safe_reply(update, "📋 У вас нет активных заявок.")
        return ConversationHandler.END

    PAGE_SIZE = 10
    context.user_data["my_requests"] = requests
    context.user_data["my_page"] = 0

    return await _show_my_requests_page(update, context)


async def _show_my_requests_page(update_or_query, context):
    """Показывает страницу «Мои заявки»."""
    requests = context.user_data.get("my_requests", [])
    page = context.user_data.get("my_page", 0)
    PAGE_SIZE = 10
    total_pages = max(1, (len(requests) + PAGE_SIZE - 1) // PAGE_SIZE)
    clamped_page = max(0, min(page, total_pages - 1))
    # Сохраняем нормализованную страницу обратно
    if clamped_page != page:
        context.user_data["my_page"] = clamped_page
    start = clamped_page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_requests = requests[start:end]

    lines = [f"📋 <b>Мои заявки</b> (стр. {page + 1}/{total_pages})\n"]
    for r in page_requests:
        status_emoji = "⚪"
        if r.get("done_date"):
            status_emoji = "✅"
        elif r.get("status") == "ordered":
            status_emoji = "🟡"
        elif r.get("status") == "delivered":
            status_emoji = "🔵"

        name = escape(r["name"][:40])
        qty = r.get("quantity", "—")
        unit = escape(r.get("unit", ""))
        lines.append(f"{status_emoji} <b>#{r['id']}</b> {name} — {qty} {unit}")

    text = "\n".join(lines)

    keyboard = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data="mrp:prev"))
    nav.append(
        InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="mrnoop")
    )
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data="mrp:next"))
    keyboard.append(nav)
    keyboard.append(
        [
            InlineKeyboardButton("🔍 Поиск", callback_data="mrsearch"),
            InlineKeyboardButton("🔄 Обновить", callback_data="mrrefresh"),
        ]
    )

    markup = InlineKeyboardMarkup(keyboard)

    if hasattr(update_or_query, "callback_query") and update_or_query.callback_query:
        await _safe_edit(
            update_or_query.callback_query, text, reply_markup=markup, parse_mode="HTML"
        )
    elif hasattr(update_or_query, "message"):
        await _safe_reply(update_or_query, text, reply_markup=markup, parse_mode="HTML")
    return MY_REQUESTS_SELECT


async def my_requests_page_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пагинация «Моих заявок»"""
    query = update.callback_query
    await query.answer()
    direction = query.data.split(":")[1]
    page = context.user_data.get("my_page", 0)
    if direction == "next":
        context.user_data["my_page"] = page + 1
    elif direction == "prev":
        context.user_data["my_page"] = max(0, page - 1)
    return await _show_my_requests_page(update, context)


async def my_requests_refresh_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Обновляет «Мои заявки»"""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    linked_name = get_initiator_by_tg_id(user_id)
    if linked_name:
        requests = get_user_requests(linked_name, status_filter="active")
    elif is_admin(user_id):
        requests = get_active_requests()[:20]
    else:
        requests = []

    context.user_data["my_requests"] = requests
    context.user_data["my_page"] = 0
    return await _show_my_requests_page(update, context)


async def my_requests_search_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Запрашивает поисковый запрос"""
    query = update.callback_query
    await query.answer()
    await _safe_edit(query, "🔍 Введите поисковый запрос (наименование или фрагмент):")
    context.user_data["awaiting_search"] = True
    return MY_REQUESTS_SELECT


_MENU_BUTTONS = {
    "➕ Добавить заявку",
    "📋 Мои заявки",
    "📋 Заявки",
    "📊 Анализ",
    "📊 Дашборд",
    "✅ Завершить заявку",
    "⚙️ Настройки",
    "⛔ Выход",
}


async def my_requests_search_text_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Обрабатывает текст поиска"""
    text = update.message.text.strip()

    # Если пользователь нажал кнопку меню — выходим из диалога
    if text in _MENU_BUTTONS:
        # Сначала выходим из диалога
        context.user_data.pop("my_requests", None)
        context.user_data.pop("my_page", None)
        context.user_data.pop("awaiting_search", None)

        # Затем обрабатываем кнопку меню
        if text == "📊 Анализ":
            return await analysis_start(update, context)
        elif text == "📊 Дашборд":
            return await dashboard_start(update, context)
        else:
            await _safe_reply(
                update,
                "↩️ Вышли из «Моих заявок». Нажмите кнопку ещё раз.",
                reply_markup=get_main_keyboard(update.effective_user.id),
            )
            return ConversationHandler.END

    if not context.user_data.get("awaiting_search"):
        await _safe_reply(
            update,
            "📋 Нажмите 🔍 Поиск для ввода запроса, или выберите действие из меню.",
        )
        return ConversationHandler.END

    context.user_data["awaiting_search"] = False
    query_text = text

    user_id = update.effective_user.id
    linked_name = get_initiator_by_tg_id(user_id)
    initiator_filter = linked_name if linked_name and not is_admin(user_id) else None

    results = search_requests(query_text, initiator_filter=initiator_filter, limit=20)

    if not results:
        await _safe_reply(
            update, f"🔍 По запросу «{escape(query_text)}» ничего не найдено."
        )
        return ConversationHandler.END

    context.user_data["my_requests"] = results
    context.user_data["my_page"] = 0
    return await _show_my_requests_page(update, context)


# ===== АДМИН-ПАНЕЛЬ: УПРАВЛЕНИЕ СПРАВОЧНИКАМИ =====
LIST_TYPE_LABELS = {
    "object": "🏗️ Объекты",
    "initiator": "👤 Исполнители",
    "unit": "📏 Единицы измерения",
}


async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вход в админ-панель настроек"""
    if not is_admin(update.effective_user.id):
        await _safe_reply(update, "⛔ Нет доступа.")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("🏗️ Объекты", callback_data="list:object")],
        [InlineKeyboardButton("👤 Исполнители", callback_data="list:initiator")],
        [InlineKeyboardButton("📏 Единицы измерения", callback_data="list:unit")],
        [InlineKeyboardButton("🔢 Кнопок в строке", callback_data="buttons")],
        [InlineKeyboardButton("💾 Резервная копия", callback_data="backup")],
        [InlineKeyboardButton("📋 Журнал действий", callback_data="audit")],
        [InlineKeyboardButton("🗑 Очистить БД", callback_data="clear_db")],
    ]
    await _safe_reply(
        update,
        "⚙️ <b>Настройки</b>\n\nВыберите раздел:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )
    return ADMIN_MENU


async def _show_main_settings(query):
    """Показывает главное меню настроек (редактирует сообщение)"""
    keyboard = [
        [InlineKeyboardButton("🏗️ Объекты", callback_data="list:object")],
        [InlineKeyboardButton("👤 Исполнители", callback_data="list:initiator")],
        [InlineKeyboardButton("📏 Единицы измерения", callback_data="list:unit")],
        [InlineKeyboardButton("🔢 Кнопок в строке", callback_data="buttons")],
        [InlineKeyboardButton("💾 Резервная копия", callback_data="backup")],
        [InlineKeyboardButton("📋 Журнал действий", callback_data="audit")],
        [InlineKeyboardButton("🗑 Очистить БД", callback_data="clear_db")],
    ]
    await _safe_edit(
        query,
        "⚙️ <b>Настройки</b>\n\nВыберите раздел:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )
    return ADMIN_MENU


async def admin_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора в главном меню настроек"""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "buttons":
        return await admin_buttons_show(update, context)

    if data == "clear_db":
        return await admin_clear_db_confirm(update, context)

    if data == "backup":
        return await admin_backup(update, context)

    if data == "audit":
        return await admin_show_audit(update, context)

    if data == "back_main":
        return await _show_main_settings(query)

    list_type = data.split(":")[1]
    context.user_data["admin_list_type"] = list_type
    return await _show_list(update, context, list_type)


async def admin_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Создаёт резервную копию БД"""
    query = update.callback_query
    await query.answer()

    path = backup_db()
    if path:
        ok = verify_backup(path)
        status = "✅ Проверена" if ok else "⚠️ Проверка не пройдена"
        await _safe_edit(
            query,
            f"💾 <b>Резервная копия создана</b>\n\n"
            f"📁 {escape(path)}\n"
            f"Статус: {status}",
            parse_mode="HTML",
        )
    else:
        await _safe_edit(query, "❌ Не удалось создать резервную копию.")

    return ADMIN_MENU


async def admin_show_audit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает последние записи журнала аудита"""
    query = update.callback_query
    await query.answer()

    entries = get_audit_log(limit=15)
    if not entries:
        await _safe_edit(query, "📋 Журнал пуст.")
        return ADMIN_MENU

    lines = ["<b>📋 Последние действия:</b>\n"]
    for e in entries:
        ts = e.get("timestamp", "")[:16]
        action = escape(e.get("action", ""))
        user = escape(e.get("user_name", "?"))
        details = escape(e.get("details", "")[:50])
        req = f"#{e['request_id']}" if e.get("request_id") else ""
        lines.append(f"• {ts} | {user} | {action} {req}")
        if details:
            lines.append(f"  {details}")

    text = "\n".join(lines)
    keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="back_main")]]

    await _safe_edit(
        query,
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )
    return ADMIN_MENU


async def _show_list(
    update: Update, context: ContextTypes.DEFAULT_TYPE, list_type: str
):
    """Показывает значения справочника с inline-кнопками"""
    query = update.callback_query
    items = get_list_items(list_type)
    label = LIST_TYPE_LABELS.get(list_type, list_type)

    text = f"<b>{label}</b>\n\n"
    if items:
        text += "\n".join(f"• {escape(item['value'])}" for item in items)
    else:
        text += "(пусто)"

    keyboard = []
    for item in items:
        if item["value"] == "Другое":
            continue
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"✏️ {escape(item['value'])}", callback_data=f"rename:{item['id']}"
                ),
                InlineKeyboardButton(f"🗑", callback_data=f"del:{item['id']}"),
            ]
        )
    keyboard.append([InlineKeyboardButton("➕ Добавить", callback_data="add")])

    bpr_key = f"buttons_per_row_{list_type}"
    cols = int(get_setting(bpr_key, "2"))
    keyboard.append(
        [InlineKeyboardButton(f"🔢 В строке: {cols}", callback_data="set_bpr")]
    )
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_main")])

    await _safe_edit(
        query,
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )
    return ADMIN_LIST_VIEW


async def admin_list_view_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка действий внутри просмотра справочника"""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    data = query.data
    list_type = context.user_data.get("admin_list_type", "object")

    if data == "add":
        await _safe_edit(
            query,
            f"✍️ Введите новое значение для <b>{LIST_TYPE_LABELS.get(list_type, list_type)}</b>:",
            parse_mode="HTML",
        )
        return ADMIN_ADD_VALUE

    if data.startswith("rename:"):
        item_id = int(data.split(":", 1)[1])
        item = get_list_value_by_id(item_id)
        if not item:
            await _safe_edit(query, "⚠️ Значение не найдено.")
            return ADMIN_LIST_VIEW
        old_value = item["value"]
        context.user_data["admin_rename_old"] = old_value
        await _safe_edit(
            query,
            f"✏️ Текущее значение: <b>{escape(old_value)}</b>\n\nВведите новое название:",
            parse_mode="HTML",
        )
        return ADMIN_RENAME_NEW

    if data.startswith("del:"):
        item_id = int(data.split(":", 1)[1])
        item = get_list_value_by_id(item_id)
        if not item:
            await _safe_edit(query, "⚠️ Значение не найдено.")
            return ADMIN_LIST_VIEW
        value = item["value"]
        context.user_data["admin_delete_value"] = value
        keyboard = [
            [
                InlineKeyboardButton("✅ Да", callback_data=f"confirm_del:{item_id}"),
                InlineKeyboardButton("❌ Нет", callback_data="back_list"),
            ]
        ]
        await _safe_edit(
            query,
            f"🗑 Удалить <b>{escape(value)}</b>?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )
        return ADMIN_DELETE

    if data == "set_bpr":
        bpr_key = f"buttons_per_row_{list_type}"
        current = int(get_setting(bpr_key, "2"))
        keyboard = []
        for n in range(1, 7):
            marker = " ✅" if n == current else ""
            keyboard.append(
                InlineKeyboardButton(f"{n}{marker}", callback_data=f"bpr:{n}")
            )
        await _safe_edit(
            query,
            "🔢 Сколько кнопок в строке?",
            reply_markup=InlineKeyboardMarkup([keyboard]),
        )
        return ADMIN_BUTTONS_EDIT

    if data == "back_main":
        return await _show_main_settings(query)

    return ADMIN_LIST_VIEW


async def admin_rename_new_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет новое значение после переименования"""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    new_value = update.message.text.strip()
    old_value = context.user_data.get("admin_rename_old", "")
    list_type = context.user_data.get("admin_list_type", "object")

    # Проверка на отмену
    cancel_keywords = ["❌ Отмена", "Отмена", "отмена", "cancel"]
    if new_value in cancel_keywords or new_value.lower() in ["отмена", "cancel"]:
        await _safe_reply(
            update,
            "❌ Переименование отменено.",
            reply_markup=get_main_keyboard(update.effective_user.id),
        )
        return await _show_list_msg(update, context, list_type)

    new_value = truncate_field(new_value, MAX_FIELD_LENGTH)

    if not new_value:
        await _safe_reply(
            update, "❌ Значение не может быть пустым. Попробуйте ещё раз:"
        )
        return ADMIN_RENAME_NEW

    success = rename_list_value(list_type, old_value, new_value)
    if success:
        audit_log(
            None,
            update.effective_user.id,
            _user_name(update),
            "list_renamed",
            f"{list_type}: {old_value} → {new_value}",
        )
        await _safe_reply(
            update, f"✅ Переименовано: {escape(old_value)} → {escape(new_value)}"
        )
    else:
        await _safe_reply(update, "❌ Не удалось переименовать (возможно, дубликат).")

    return await _show_list_msg(update, context, list_type)


async def admin_delete_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение удаления"""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    data = query.data
    list_type = context.user_data.get("admin_list_type", "object")

    if data.startswith("confirm_del:"):
        item_id = int(data.split(":", 1)[1])
        item = get_list_value_by_id(item_id)
        if item:
            value = item["value"]
            delete_list_value(list_type, value)
            audit_log(
                None,
                update.effective_user.id,
                _user_name(update),
                "list_deleted",
                f"{list_type}: {value}",
            )
            await query.answer(f"Удалено: {value}", show_alert=True)
        else:
            await query.answer("⚠️ Значение не найдено.", show_alert=True)

    return await _show_list(update, context, list_type)


async def admin_add_value_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавляет новое значение в справочник"""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    new_value = update.message.text.strip()
    list_type = context.user_data.get("admin_list_type", "object")

    # Проверка на отмену
    cancel_keywords = ["❌ Отмена", "Отмена", "отмена", "cancel"]
    if new_value in cancel_keywords or new_value.lower() in ["отмена", "cancel"]:
        await _safe_reply(
            update,
            "❌ Добавление отменено.",
            reply_markup=get_main_keyboard(update.effective_user.id),
        )
        return await _show_list_msg(update, context, list_type)

    new_value = truncate_field(new_value, MAX_FIELD_LENGTH)

    if not new_value:
        await _safe_reply(
            update, "❌ Значение не может быть пустым. Попробуйте ещё раз:"
        )
        return ADMIN_ADD_VALUE

    success = add_list_value(list_type, new_value)
    if success:
        audit_log(
            None,
            update.effective_user.id,
            _user_name(update),
            "list_added",
            f"{list_type}: {new_value}",
        )
        await _safe_reply(update, f"✅ Добавлено: {escape(new_value)}")
    else:
        await _safe_reply(update, f"⚠️ Значение «{escape(new_value)}» уже существует.")

    return await _show_list_msg(update, context, list_type)


async def admin_buttons_edit_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Устанавливает количество кнопок в строке"""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    data = query.data
    list_type = context.user_data.get("admin_list_type", "object")

    if data.startswith("bpr:"):
        n = int(data.split(":")[1])
        if 1 <= n <= 6:
            set_setting(f"buttons_per_row_{list_type}", str(n))
            await query.answer(f"Установлено: {n} в строке", show_alert=True)
        else:
            await query.answer("Значение должно быть от 1 до 6", show_alert=True)

    return await _show_list(update, context, list_type)


async def admin_buttons_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает общую настройку кнопок"""
    query = update.callback_query

    text = "🔢 <b>Количество кнопок в строке:</b>\n\n"
    for lt, label in LIST_TYPE_LABELS.items():
        cols = int(get_setting(f"buttons_per_row_{lt}", "2"))
        text += f"  {label}: <b>{cols}</b>\n"

    keyboard = [
        [InlineKeyboardButton("🏗️ Объекты", callback_data="list:object")],
        [InlineKeyboardButton("👤 Исполнители", callback_data="list:initiator")],
        [InlineKeyboardButton("📏 Единицы", callback_data="list:unit")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_main")],
    ]
    await _safe_edit(
        query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
    )
    return ADMIN_MENU


async def _show_list_msg(
    update: Update, context: ContextTypes.DEFAULT_TYPE, list_type: str
):
    """Показывает справочник через обычное сообщение (не callback)"""
    items = get_list_items(list_type)
    label = LIST_TYPE_LABELS.get(list_type, list_type)

    text = f"<b>{label}</b>\n\n"
    if items:
        text += "\n".join(f"• {escape(item['value'])}" for item in items)
    else:
        text += "(пусто)"

    keyboard = []
    for item in items:
        if item["value"] == "Другое":
            continue
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"✏️ {escape(item['value'])}", callback_data=f"rename:{item['id']}"
                ),
                InlineKeyboardButton(f"🗑", callback_data=f"del:{item['id']}"),
            ]
        )
    keyboard.append([InlineKeyboardButton("➕ Добавить", callback_data="add")])

    bpr_key = f"buttons_per_row_{list_type}"
    cols = int(get_setting(bpr_key, "2"))
    keyboard.append(
        [InlineKeyboardButton(f"🔢 В строке: {cols}", callback_data="set_bpr")]
    )
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_main")])

    await _safe_reply(
        update,
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )
    return ADMIN_LIST_VIEW


async def admin_clear_db_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает подтверждение очистки БД"""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [
            InlineKeyboardButton("✅ Да, очистить", callback_data="clear_db:confirm"),
            InlineKeyboardButton("❌ Отмена", callback_data="clear_db:cancel"),
        ],
    ]
    await _safe_edit(
        query,
        "⚠️ <b>Очистка базы данных</b>\n\n"
        "Перед очисткой автоматически создаётся резервная копия.\n\n"
        "Будут удалены <b>все заявки</b> и прикреплённые фото.\n"
        "Справочники (инициаторы, объекты, единицы) сохранятся.\n\n"
        "Продолжить?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )
    return ADMIN_CLEAR_DB_CONFIRM


async def admin_clear_db_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выполняет или отменяет очистку БД"""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "clear_db:cancel":
        await _safe_edit(query, "❌ Очистка отменена.")
        return await _show_main_settings(query)

    try:
        count = clear_all_requests()
        await _safe_edit(
            query,
            f"✅ База данных очищена.\nУдалено заявок: <b>{count}</b>\n"
            "💾 Резервная копия создана автоматически.",
            parse_mode="HTML",
        )
    except RuntimeError as e:
        await _safe_edit(
            query,
            f"❌ Очистка отменена: {escape(str(e))}",
            parse_mode="HTML",
        )
        return ADMIN_MENU

    return ConversationHandler.END


async def admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выход из админ-панели"""
    context.user_data.clear()
    user_id = update.effective_user.id
    await _safe_reply(
        update, "❌ Настройки закрыты.", reply_markup=get_main_keyboard(user_id)
    )
    return ConversationHandler.END


# ===== ОБРАБОТКА ГЛАВНОГО МЕНЮ =====
async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает кнопки главного меню"""
    text = update.message.text.strip()
    user_id = update.effective_user.id

    if text == "⛔ Выход":
        await _safe_reply(
            update,
            "👋 До встречи! Для возврата введите /start",
            reply_markup=ReplyKeyboardMarkup([["/start"]], resize_keyboard=True),
        )
    elif text == "📊 Анализ":
        # Перенаправляем на analysis_start
        return await analysis_start(update, context)
    elif text == "📊 Дашборд":
        # Перенаправляем на dashboard_start
        return await dashboard_start(update, context)
    else:
        await _safe_reply(
            update,
            "Используйте кнопки меню или /start",
            reply_markup=get_main_keyboard(user_id),
        )


# ===== ДАШБОРД =====
async def dashboard_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает дашборд администратору."""
    if not is_admin(update.effective_user.id):
        await _safe_reply(update, "⛔ У вас нет прав для этой команды.")
        return ConversationHandler.END
    text = build_dashboard()
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📋 Все заявки", callback_data="reqs:all:0"),
                InlineKeyboardButton("📊 Отчёт", callback_data="report:active"),
            ]
        ]
    )
    await _safe_reply(update, text, parse_mode="HTML", reply_markup=keyboard)
    return ConversationHandler.END


async def dashboard_reqs_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки «Все заявки» на дашборде — открывает список заявок."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    context.user_data["req_filter"] = None
    await _show_requests_list(update, context, 0)


async def dashboard_report_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки «Отчёт» на дашборде — генерирует отчёт."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    mode = query.data.split(":")[1]
    await _safe_edit(query, "⏳ Формирую отчёт...")
    try:
        result, ids_with_photos, all_ids, requests_data = build_report(mode=mode)
        keyboard = _build_photo_keyboard(ids_with_photos, all_ids, requests_data)
        await send_long_message(
            context,
            query.message.chat_id,
            result,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error(f"Ошибка формирования отчёта из дашборда: {e}", exc_info=True)
        await _safe_edit(query, "❌ Ошибка при формировании отчёта.")


# ===== РАЗДЕЛ «ЗАЯВКИ» =====
async def requests_list_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список заявок с фильтрами."""
    if not is_admin(update.effective_user.id):
        await _safe_reply(update, "⛔ Нет доступа.")
        return ConversationHandler.END
    context.user_data["req_filter"] = None
    return await _show_requests_list(update, context, 0)


async def _show_requests_list(update_or_query, context, page: int):
    """Показывает страницу списка заявок с фильтрацией по статусу."""
    status_filter = context.user_data.get("req_filter")
    requests = get_requests_by_status(status=status_filter, limit=200)

    PAGE_SIZE = 8
    total_pages = max(1, (len(requests) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_requests = requests[start:end]

    # Кнопки фильтров
    filter_buttons = [
        [
            InlineKeyboardButton(
                "✅ Все" if not status_filter else "Все",
                callback_data="reqf:none",
            ),
            InlineKeyboardButton(
                "⚪ Новые" if status_filter == "new" else "⚪",
                callback_data="reqf:new",
            ),
            InlineKeyboardButton(
                "🟡 Заказ" if status_filter == "ordered" else "🟡",
                callback_data="reqf:ordered",
            ),
            InlineKeyboardButton(
                "🔵 Достав" if status_filter == "delivered" else "🔵",
                callback_data="reqf:delivered",
            ),
            InlineKeyboardButton(
                "🟢 Сделано" if status_filter == "done" else "🟢",
                callback_data="reqf:done",
            ),
        ]
    ]

    # Список заявок
    keyboard = []
    for r in page_requests:
        status_emoji = _status_emoji(r.get("status", "new"))
        name = r["name"][:20]
        obj = r.get("object", "—")[:10]
        label = f"{status_emoji} #{r['id']} {name} ({obj})"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"rc:{r['id']}")])

    # Пагинация
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"reqp:{page - 1}"))
    nav.append(
        InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="reqnoop")
    )
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд ➡️", callback_data=f"reqp:{page + 1}"))
    if nav:
        keyboard.append(nav)

    all_buttons = filter_buttons + keyboard
    markup = InlineKeyboardMarkup(all_buttons) if all_buttons else None

    status_text = (
        f" ({STATUS_LABELS.get(status_filter, 'Все')})" if status_filter else ""
    )
    text = (
        f"📋 <b>Заявки{status_text}</b>\n"
        f"Всего: {len(requests)}\n\n"
        "Выберите заявку:"
    )

    if hasattr(update_or_query, "callback_query") and update_or_query.callback_query:
        await _safe_edit(
            update_or_query.callback_query,
            text,
            reply_markup=markup,
            parse_mode="HTML",
        )
    elif hasattr(update_or_query, "message"):
        await _safe_reply(update_or_query, text, reply_markup=markup, parse_mode="HTML")
    return REQUESTS_LIST


def _status_emoji(status: str) -> str:
    """Возвращает эмодзи для статуса."""
    return {
        "new": "⚪",
        "ordered": "🟡",
        "delivered": "🔵",
        "done": "🟢",
        "archived": "📦",
    }.get(status, "⚪")


async def requests_filter_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает фильтр по статусу."""
    query = update.callback_query
    await query.answer()
    filter_val = query.data.split(":")[1]
    context.user_data["req_filter"] = None if filter_val == "none" else filter_val
    return await _show_requests_list(update, context, 0)


async def requests_page_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пагинация списка заявок."""
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":")[1])
    return await _show_requests_list(update, context, page)


async def requests_card_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает карточку заявки с действиями."""
    query = update.callback_query
    await query.answer()
    req_id = int(query.data.split(":")[1])

    req = get_request_by_id(req_id)
    if not req:
        await _safe_edit(query, "⚠️ Заявка не найдена.")
        return REQUESTS_LIST

    context.user_data["card_request"] = req
    return await _render_request_card(query, context, req)


async def _render_request_card(query, context, req: dict):
    """Рендерит карточку заявки с inline-кнопками."""
    req_id = req["id"]
    qty_text = f"{req.get('quantity', '—')} {req.get('unit', '')}".strip()
    status_label = STATUS_LABELS.get(req.get("status", "new"), req.get("status", "—"))
    status_emoji = _status_emoji(req.get("status", "new"))
    notes = req.get("notes", "")
    photos = get_request_photos(req_id)
    photo_text = f"\n📎 Фото: {len(photos)} шт." if photos else ""

    text = (
        f"📋 <b>Заявка #{req_id}</b>\n\n"
        f"📦 Наименование: <b>{escape(req.get('name', '—'))}</b>\n"
        f"🔢 Количество: <code>{escape(qty_text)}</code>\n"
        f"👤 Инициатор: {escape(req.get('initiator', '—'))}\n"
        f"🏗️ Объект: {escape(req.get('object', '—'))}\n"
        f"📊 Статус: {status_emoji} {status_label}\n"
        f"📅 Дата: {escape(req.get('request_date', '—'))}"
    )
    if req.get("done_date"):
        text += f"\n📅 Выполнено: {escape(req['done_date'])}"
    if notes and notes != "-":
        text += f"\n📝 Дополнительно: {escape(notes)}"
    if photo_text:
        text += photo_text

    buttons = []
    # Кнопки действий
    buttons.append(
        [
            InlineKeyboardButton("🔄 Статус", callback_data=f"reqst:{req_id}"),
            InlineKeyboardButton(
                f"📷 Фото ({len(photos)})" if photos else "📷 Фото",
                callback_data=f"photos:{req_id}",
            ),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton("🗑 Удалить", callback_data=f"reqdel:{req_id}"),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton("⬅️ Назад к списку", callback_data="reqback"),
        ]
    )

    await _safe_edit(
        query, text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML"
    )
    return REQUESTS_CARD


async def requests_card_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает действия в карточке заявки."""
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id

    if data == "reqback":
        await query.answer()
        return await _show_requests_list(update, context, 0)

    if data.startswith("reqst:"):
        # Смена статуса — перенаправляем в status flow
        await query.answer()
        req_id = int(data.split(":")[1])
        req = get_request_by_id(req_id)
        if not req:
            await _safe_edit(query, "⚠️ Заявка не найдена.")
            return ConversationHandler.END
        context.user_data["status_request"] = req
        current = req.get("status", "new")
        current_label = STATUS_LABELS.get(current, current)
        keyboard = []
        for st_key, st_label in STATUS_LABELS.items():
            if st_key == "done" or st_key == "archived":
                continue
            if st_key == current:
                continue
            keyboard.append(
                [InlineKeyboardButton(st_label, callback_data=f"reqssc:{st_key}")]
            )
        keyboard.append(
            [InlineKeyboardButton("❌ Отмена", callback_data="reqsscancel")]
        )
        await _safe_edit(
            query,
            f"🔄 <b>Заявка #{req_id}</b>\n"
            f"📦 {escape(req['name'])}\n"
            f"Текущий статус: {current_label}\n\n"
            "Выберите новый статус:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )
        return REQUESTS_CARD

    if data.startswith("reqssc:"):
        # Подтверждение смены статуса
        await query.answer()
        new_status = data.split(":")[1]
        req = context.user_data.get("status_request")
        if not req:
            await _safe_edit(query, "⚠️ Данные не найдены.")
            return ConversationHandler.END
        success = update_request_status(
            req["id"],
            new_status,
            user_id=user_id,
            user_name=_user_name(update),
        )
        if success:
            new_label = STATUS_LABELS.get(new_status, new_status)
            await _safe_edit(
                query,
                f"✅ Заявка #{req['id']} → <b>{new_label}</b>",
                parse_mode="HTML",
            )
        else:
            await _safe_edit(query, "❌ Не удалось изменить статус.")
        context.user_data.pop("status_request", None)
        return ConversationHandler.END

    if data == "reqsscancel":
        await query.answer()
        context.user_data.pop("status_request", None)
        req = context.user_data.get("card_request")
        if req:
            return await _render_request_card(query, context, req)
        await _safe_edit(query, "❌ Отменено.")
        return ConversationHandler.END

    if data.startswith("reqe:"):
        # Редактирование — переход к выбору поля
        await query.answer()
        req_id = int(data.split(":")[1])
        req = get_request_by_id(req_id)
        if not req:
            await _safe_edit(query, "⚠️ Заявка не найдена.")
            return ConversationHandler.END
        context.user_data["edit_request"] = req
        context.user_data["edit_fields_order"] = [
            "name",
            "quantity",
            "unit",
            "object",
            "notes",
        ]
        context.user_data["edit_current_index"] = 0

        # Показываем первое поле для редактирования
        field = "name"
        field_names = {
            "name": "наименование",
            "quantity": "количество",
            "unit": "единица измерения",
            "object": "объект",
            "notes": "примечание",
        }
        field_name = field_names.get(field, field)
        current_value = req.get(field, "")

        # В первом пункте только кнопка "Вперёд"
        keyboard = [
            [InlineKeyboardButton("➡️ Вперёд", callback_data="editnext")],
        ]

        await _safe_edit(
            query,
            f"✏️ <b>Редактирование заявки #{req_id}</b>\n\n"
            f"📦 Наименование: <code>{escape(str(current_value))}</code>\n\n"
            f"Введите новое значение для <b>{field_name}</b>:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

        # Показываем клавиатуру для ввода (только "Вперёд")
        input_keyboard = ReplyKeyboardMarkup(
            [["➡️ Вперёд"]], resize_keyboard=True, one_time_keyboard=True
        )
        await _safe_send(
            context,
            query.message.chat_id,
            "Или используйте кнопку ниже:",
            reply_markup=input_keyboard,
        )

        context.user_data["edit_field"] = field
        return EDIT_INPUT_VALUE

    if data.startswith("editf:"):
        # Выбор поля → запрос нового значения
        await query.answer()
        field = data.split(":")[1]
        context.user_data["edit_field"] = field
        field_names = {
            "name": "наименование",
            "quantity": "количество",
            "unit": "единица измерения",
            "object": "объект",
            "notes": "примечание",
        }
        field_name = field_names.get(field, field)

        # Получаем текущее значение поля для подсказки
        req = context.user_data.get("edit_request", {})
        current_value = req.get(field, "")

        # Обновляем порядок полей и текущий индекс
        fields_order = ["name", "quantity", "unit", "object", "notes"]
        context.user_data["edit_fields_order"] = fields_order
        context.user_data["edit_current_index"] = (
            fields_order.index(field) if field in fields_order else 0
        )

        keyboard = [
            [
                InlineKeyboardButton("➡️ Вперёд", callback_data="editnext"),
                InlineKeyboardButton("✅ Завершить", callback_data="editfinish"),
            ],
        ]

        await _safe_edit(
            query,
            f"✏️ Введите новое значение для <b>{field_name}</b>:\n\n"
            f"Текущее значение: <code>{escape(str(current_value))}</code>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

        # Показываем клавиатуру для ввода
        input_keyboard = ReplyKeyboardMarkup(
            [["➡️ Вперёд", "✅ Завершить"]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await _safe_send(
            context,
            query.message.chat_id,
            "Или используйте кнопки ниже:",
            reply_markup=input_keyboard,
        )

        return EDIT_INPUT_VALUE

    if data == "editcancel":
        # Завершаем редактирование (для обратной совместимости)
        await query.answer()
        return await _finish_editing(update, context, update.effective_user.id)

    if data.startswith("reqdel:"):
        # Удаление — запрос подтверждения
        await query.answer()
        req_id = int(data.split(":")[1])
        req = get_request_by_id(req_id)
        if not req:
            await _safe_edit(query, "⚠️ Заявка не найдена.")
            return ConversationHandler.END
        context.user_data["delete_request"] = req
        keyboard = [
            [
                InlineKeyboardButton(
                    "✅ Да, удалить", callback_data=f"reqdelyes:{req_id}"
                ),
                InlineKeyboardButton("❌ Нет", callback_data="reqdelno"),
            ]
        ]
        await _safe_edit(
            query,
            f"🗑 <b>Удалить заявку #{req_id}?</b>\n\n"
            f"📦 {escape(req.get('name', '—'))}\n"
            f"👤 {escape(req.get('initiator', '—'))}\n\n"
            "Это действие нельзя отменить.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )
        return REQUESTS_CARD

    if data.startswith("reqdelyes:"):
        # Подтверждение удаления
        await query.answer()
        req_id = int(data.split(":")[1])
        success = delete_request(
            req_id,
            user_id=user_id,
            user_name=_user_name(update),
        )
        if success:
            await _safe_edit(query, f"✅ Заявка #{req_id} удалена.", parse_mode="HTML")
        else:
            await _safe_edit(query, "❌ Не удалось удалить заявку.")
        context.user_data.pop("delete_request", None)
        context.user_data.pop("card_request", None)
        return ConversationHandler.END

    if data == "reqdelno":
        await query.answer()
        context.user_data.pop("delete_request", None)
        req = context.user_data.get("card_request")
        if req:
            return await _render_request_card(query, context, req)
        await _safe_edit(query, "❌ Удаление отменено.")
        return ConversationHandler.END

    return REQUESTS_CARD


async def edit_next_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки 'Вперёд' при редактировании."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    req = context.user_data.get("edit_request")

    if not req:
        await _safe_edit(query, "⚠️ Ошибка: данные не найдены.")
        return ConversationHandler.END

    return await _move_to_next_field(update, context, user_id, req)


async def edit_finish_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки 'Завершить' при редактировании."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    return await _finish_editing(update, context, user_id)


async def edit_prev_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки 'Назад' при редактировании."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    req = context.user_data.get("edit_request")

    if not req:
        await _safe_edit(query, "⚠️ Ошибка: данные не найдены.")
        return ConversationHandler.END

    return await _move_to_prev_field(update, context, user_id, req)


async def edit_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принимает новое значение поля при редактировании."""
    user_id = update.effective_user.id
    new_value = update.message.text.strip()
    req = context.user_data.get("edit_request")
    field = context.user_data.get("edit_field")

    if not req or not field:
        await _safe_reply(update, "⚠️ Ошибка: данные не найдены.")
        return ConversationHandler.END

    # Проверка на завершение редактирования
    if new_value in ["✅ Завершить", "Завершить", "завершить", "Готово", "готово"]:
        return await _finish_editing(update, context, user_id)

    # Проверка на переход к следующему полю без изменений
    if new_value in ["➡️ Вперёд", "Вперёд", "вперёд", "Далее", "далее"]:
        return await _move_to_next_field(update, context, user_id, req)

    # Проверка на переход к предыдущему полю
    if new_value in ["⬅️ Назад", "Назад", "назад"]:
        return await _move_to_prev_field(update, context, user_id, req)

    new_value = truncate_field(new_value, MAX_FIELD_LENGTH)

    # Валидация количества
    if field == "quantity":
        if not validate_quantity(new_value):
            await _safe_reply(
                update,
                "❌ <b>Неверный формат количества</b>\n\n"
                "Пожалуйста, введите положительное число.\n\n"
                "✅ <b>Правильные примеры:</b>\n"
                "• 10\n• 5.5\n• 0.25\n\n"
                "Или нажмите «➡️ Вперёд» для перехода к следующему полю.",
                parse_mode="HTML",
            )
            return EDIT_INPUT_VALUE
        new_value = float(new_value.replace(",", "."))

    kwargs = {f"{'object_name' if field == 'object' else field}": new_value}
    success = edit_request(
        req["id"],
        user_id=user_id,
        user_name=_user_name(update),
        **kwargs,
    )

    if success:
        field_names = {
            "name": "Наименование",
            "quantity": "Количество",
            "unit": "Единица",
            "object": "Объект",
            "notes": "Дополнительно",
        }
        await _safe_reply(
            update,
            f"✅ {field_names.get(field, field)} обновлено: <b>{escape(str(new_value))}</b>",
            parse_mode="HTML",
        )

        # Обновляем данные заявки в контексте
        updated_req = get_request_by_id(req["id"])
        if updated_req:
            context.user_data["edit_request"] = updated_req

        # Переходим к следующему полю
        return await _move_to_next_field(update, context, user_id, updated_req or req)
    else:
        await _safe_reply(
            update, "❌ Не удалось обновить.", reply_markup=get_main_keyboard(user_id)
        )
        context.user_data.pop("edit_request", None)
        context.user_data.pop("edit_field", None)
        return ConversationHandler.END


async def _move_to_next_field(update, context, user_id, req):
    """Переходит к следующему полю для редактирования."""
    fields_order = context.user_data.get(
        "edit_fields_order", ["name", "quantity", "unit", "object", "notes"]
    )
    current_index = context.user_data.get("edit_current_index", 0)

    # Переходим к следующему полю
    next_index = current_index + 1

    if next_index >= len(fields_order):
        # Все поля отредактированы
        return await _finish_editing(update, context, user_id)

    # Обновляем индекс
    context.user_data["edit_current_index"] = next_index
    next_field = fields_order[next_index]
    context.user_data["edit_field"] = next_field

    field_names = {
        "name": "наименование",
        "quantity": "количество",
        "unit": "единица измерения",
        "object": "объект",
        "notes": "примечание",
    }
    field_name = field_names.get(next_field, next_field)
    current_value = req.get(next_field, "")

    # Если это последний пункт, показываем "Завершить" вместо "Вперёд"
    is_last_field = next_index == len(fields_order) - 1

    if is_last_field:
        keyboard = [
            [
                InlineKeyboardButton("⬅️ Назад", callback_data="editprev"),
                InlineKeyboardButton("✅ Завершить", callback_data="editfinish"),
            ],
        ]
        input_keyboard = ReplyKeyboardMarkup(
            [["⬅️ Назад", "✅ Завершить"]], resize_keyboard=True, one_time_keyboard=True
        )
    else:
        keyboard = [
            [
                InlineKeyboardButton("⬅️ Назад", callback_data="editprev"),
                InlineKeyboardButton("➡️ Вперёд", callback_data="editnext"),
            ],
        ]
        input_keyboard = ReplyKeyboardMarkup(
            [["⬅️ Назад", "➡️ Вперёд"]], resize_keyboard=True, one_time_keyboard=True
        )

    await _safe_reply(
        update,
        f"✏️ <b>Редактирование заявки #{req['id']}</b>\n\n"
        f"Текущее значение: <code>{escape(str(current_value))}</code>\n\n"
        f"Введите новое значение для <b>{field_name}</b>:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )

    # Показываем клавиатуру для ввода
    await _safe_reply(
        update,
        "Или используйте кнопки ниже:",
        reply_markup=input_keyboard,
    )

    return EDIT_INPUT_VALUE


async def _move_to_prev_field(update, context, user_id, req):
    """Переходит к предыдущему полю для редактирования."""
    fields_order = context.user_data.get(
        "edit_fields_order", ["name", "quantity", "unit", "object", "notes"]
    )
    current_index = context.user_data.get("edit_current_index", 0)

    # Переходим к предыдущему полю
    prev_index = max(0, current_index - 1)

    # Обновляем индекс
    context.user_data["edit_current_index"] = prev_index
    prev_field = fields_order[prev_index]
    context.user_data["edit_field"] = prev_field

    field_names = {
        "name": "наименование",
        "quantity": "количество",
        "unit": "единица измерения",
        "object": "объект",
        "notes": "примечание",
    }
    field_name = field_names.get(prev_field, prev_field)
    current_value = req.get(prev_field, "")

    # Если это первый пункт, показываем только "Вперёд"
    if prev_index == 0:
        keyboard = [
            [InlineKeyboardButton("➡️ Вперёд", callback_data="editnext")],
        ]
        input_keyboard = ReplyKeyboardMarkup(
            [["➡️ Вперёд"]], resize_keyboard=True, one_time_keyboard=True
        )
    else:
        keyboard = [
            [
                InlineKeyboardButton("⬅️ Назад", callback_data="editprev"),
                InlineKeyboardButton("➡️ Вперёд", callback_data="editnext"),
            ],
        ]
        input_keyboard = ReplyKeyboardMarkup(
            [["⬅️ Назад", "➡️ Вперёд"]], resize_keyboard=True, one_time_keyboard=True
        )

    await _safe_reply(
        update,
        f"✏️ <b>Редактирование заявки #{req['id']}</b>\n\n"
        f"Текущее значение: <code>{escape(str(current_value))}</code>\n\n"
        f"Введите новое значение для <b>{field_name}</b>:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )

    # Показываем клавиатуру для ввода
    await _safe_reply(
        update,
        "Или используйте кнопки ниже:",
        reply_markup=input_keyboard,
    )

    return EDIT_INPUT_VALUE


async def _finish_editing(update, context, user_id):
    """Завершает редактирование заявки."""
    req = context.user_data.get("edit_request")
    context.user_data.pop("edit_request", None)
    context.user_data.pop("edit_field", None)
    context.user_data.pop("edit_fields_order", None)
    context.user_data.pop("edit_current_index", None)

    if req:
        await _safe_reply(
            update,
            f"✅ <b>Редактирование заявки #{req['id']} завершено</b>\n\n"
            f"📦 {escape(req.get('name', '—'))}\n"
            f"🔢 {escape(str(req.get('quantity', '—')))} {escape(req.get('unit', ''))}\n"
            f"🏗️ {escape(req.get('object', '—'))}\n"
            f"📝 {escape(req.get('notes', '—'))}",
            reply_markup=get_main_keyboard(user_id),
            parse_mode="HTML",
        )
    else:
        await _safe_reply(
            update,
            "✅ Редактирование завершено.",
            reply_markup=get_main_keyboard(user_id),
        )

    return ConversationHandler.END


async def requests_back_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат к списку заявок из карточки."""
    query = update.callback_query
    await query.answer()
    return await _show_requests_list(update, context, 0)


async def requests_noop_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Noop для пагинации."""
    query = update.callback_query
    await query.answer()
    return REQUESTS_LIST


# ===== ОБЩИЙ ОБРАБОТЧИК ОШИБОК =====
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Глобальный обработчик ошибок."""
    logger.error("Исключение при обработке обновления:", exc_info=context.error)

    if isinstance(update, Update) and update.effective_chat:
        try:
            await _safe_send(
                context,
                update.effective_chat.id,
                "⚠️ Произошла ошибка. Попробуйте ещё раз или обратитесь к администратору.",
            )
        except Exception:
            pass


# ===== ГЛАВНАЯ ФУНКЦИЯ =====
def main():
    """Точка входа"""
    from config import _validate_config

    _validate_config()
    init_db()

    # Подавляем INFO-логи HTTPX/httpcore, чтобы токен не попадал в журналы
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    builder = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN)
    if SOCKS5_PROXY:
        proxy_url = SOCKS5_PROXY
        if not proxy_url.startswith("socks"):
            proxy_url = f"socks5://{proxy_url}"
        http_request = HTTPXRequest(proxy_url=proxy_url)
        builder = builder.request(http_request)
        masked = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
        logger.info(f"Используется SOCKS5-прокси: {masked}")
    app = builder.build()

    # Регистрация глобального обработчика ошибок
    app.add_error_handler(error_handler)

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^➕ Добавить заявку$"), start_add_request)
        ],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_name)],
            QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_quantity)],
            UNIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_unit)],
            INITIATOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_initiator)],
            INITIATOR_CUSTOM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_initiator_custom)
            ],
            OBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_object)],
            OBJECT_CUSTOM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_object_custom)
            ],
            NOTES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_notes_and_save)
            ],
            PHOTO: [
                MessageHandler(filters.PHOTO, handle_photo),
                MessageHandler(filters.Regex("^✅ Готово$"), handle_photo_done),
                MessageHandler(filters.Regex("^➡️ Пропустить$"), handle_photo_done),
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
                MessageHandler(filters.ALL & ~filters.COMMAND, handle_photo_hint),
            ],
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_request)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
        ],
    )

    admin_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("Настройки"), admin_start)],
        states={
            ADMIN_MENU: [CallbackQueryHandler(admin_menu_handler)],
            ADMIN_LIST_VIEW: [CallbackQueryHandler(admin_list_view_handler)],
            ADMIN_RENAME_NEW: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, admin_rename_new_handler
                )
            ],
            ADMIN_DELETE: [CallbackQueryHandler(admin_delete_handler)],
            ADMIN_ADD_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_value_handler)
            ],
            ADMIN_BUTTONS_EDIT: [CallbackQueryHandler(admin_buttons_edit_handler)],
            ADMIN_CLEAR_DB_CONFIRM: [
                CallbackQueryHandler(
                    admin_clear_db_execute, pattern=r"^clear_db:(confirm|cancel)$"
                )
            ],
        },
        fallbacks=[
            CommandHandler("cancel", admin_cancel),
            MessageHandler(filters.Regex("^❌ Отмена$"), admin_cancel),
            MessageHandler(filters.Regex("Настройки"), admin_start),
        ],
    )

    analysis_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📊 Анализ$"), analysis_start)],
        states={
            0: [
                CallbackQueryHandler(
                    analysis_filter_handler, pattern=r"^report:(active|completed)$"
                )
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
        ],
    )

    complete_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^✅ Завершить заявку$"), complete_start)
        ],
        states={
            COMPLETE_SELECT: [
                CallbackQueryHandler(complete_select_handler, pattern=r"^cr:\d+$"),
                CallbackQueryHandler(complete_page_handler, pattern=r"^crp:\d+$"),
                CallbackQueryHandler(complete_batch_handler, pattern=r"^crb:\d+$"),
                CallbackQueryHandler(complete_start, pattern=r"^crnoop$"),
            ],
            COMPLETE_COMMENT: [
                CallbackQueryHandler(
                    complete_comment_handler, pattern=r"^cr:(nocomment|cancel)$"
                ),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, complete_comment_text_handler
                ),
            ],
            COMPLETE_CONFIRM: [
                CallbackQueryHandler(
                    complete_confirm_handler, pattern=r"^cr:(confirm|cancel)$"
                )
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
        ],
    )

    status_handler = ConversationHandler(
        entry_points=[
            CommandHandler("status", status_start),
        ],
        states={
            STATUS_SELECT: [
                CallbackQueryHandler(status_select_handler, pattern=r"^ss:\d+$"),
                CallbackQueryHandler(status_page_handler, pattern=r"^ssp:\d+$"),
            ],
            STATUS_CONFIRM: [
                CallbackQueryHandler(status_confirm_handler, pattern=r"^ssc:.+$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
        ],
    )

    my_requests_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^📋 Мои заявки$"), my_requests_start)
        ],
        states={
            MY_REQUESTS_SELECT: [
                CallbackQueryHandler(
                    my_requests_page_handler, pattern=r"^mrp:(prev|next)$"
                ),
                CallbackQueryHandler(
                    my_requests_refresh_handler, pattern=r"^mrrefresh$"
                ),
                CallbackQueryHandler(my_requests_search_handler, pattern=r"^mrsearch$"),
                CallbackQueryHandler(my_requests_start, pattern=r"^mrnoop$"),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, my_requests_search_text_handler
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
        ],
    )

    requests_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^📋 Заявки$"), requests_list_start)
        ],
        states={
            REQUESTS_LIST: [
                CallbackQueryHandler(requests_filter_handler, pattern=r"^reqf:.+$"),
                CallbackQueryHandler(requests_page_handler, pattern=r"^reqp:\d+$"),
                CallbackQueryHandler(requests_card_show, pattern=r"^rc:\d+$"),
                CallbackQueryHandler(requests_noop_handler, pattern=r"^reqnoop$"),
            ],
            REQUESTS_CARD: [
                CallbackQueryHandler(
                    requests_card_handler,
                    pattern=r"^(reqst|reqssc|reqsscancel|reqe|editf|editcancel|editnext|editfinish|reqdel|reqdelyes|reqdelno|reqback):?.*$",
                ),
            ],
            EDIT_INPUT_VALUE: [
                CallbackQueryHandler(
                    edit_next_handler,
                    pattern=r"^editnext$",
                ),
                CallbackQueryHandler(
                    edit_finish_handler,
                    pattern=r"^editfinish$",
                ),
                CallbackQueryHandler(
                    edit_prev_handler,
                    pattern=r"^editprev$",
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_input_handler),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
        ],
    )

    register_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            REG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
        ],
    )

    app.add_handler(register_conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(requests_handler)
    app.add_handler(analysis_handler)
    app.add_handler(complete_handler)
    app.add_handler(status_handler)
    app.add_handler(my_requests_handler)
    app.add_handler(admin_handler)
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(show_request_photos, pattern=r"^photos:\d+$"))
    app.add_handler(CallbackQueryHandler(show_request_details, pattern=r"^detail:\d+$"))
    app.add_handler(
        CallbackQueryHandler(dashboard_reqs_callback, pattern=r"^reqs:all:\d+$")
    )
    app.add_handler(
        CallbackQueryHandler(
            dashboard_report_callback, pattern=r"^report:(active|completed)$"
        )
    )
    app.add_handler(MessageHandler(filters.Regex("^📊 Дашборд$"), dashboard_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))

    tz = ZoneInfo(REPORT_TIMEZONE) if REPORT_TIMEZONE else None

    app.job_queue.run_daily(
        scheduled_report,
        time=time(hour=REPORT_HOUR, minute=REPORT_MINUTE, tzinfo=tz),
        chat_id=int(TELEGRAM_CHAT_ID),
        name="daily_report",
    )

    # Архивирование старых завершённых заявок (ежедневно в 03:00)
    async def _archive_job(context):
        from db import archive_completed_requests

        count = archive_completed_requests()
        if count:
            logger.info(f"Автоматическое архивирование: {count} заявок")

    app.job_queue.run_daily(
        _archive_job,
        time=time(hour=3, minute=0, tzinfo=tz),
        name="archive_old_requests",
    )

    logger.info(
        f" Бот запущен. Отчёт ежедневно в {REPORT_HOUR:02d}:{REPORT_MINUTE:02d}"
    )
    app.run_polling()


if __name__ == "__main__":
    main()
