import asyncio
import functools
import json
import logging
from time import monotonic as _monotonic
from typing import Awaitable, Callable, NamedTuple, Optional
from uuid import uuid4

# Маркер сборки — виден в логах Amvera, чтобы понимать, какая версия запущена.
APP_VERSION = "2026-10-22.a4af99e"
from datetime import time, datetime, date
from html import escape
from zoneinfo import ZoneInfo

import httpx
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
from telegram.error import (
    BadRequest,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)
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
    CONVERSATION_TIMEOUT_SEC,
    HTTP_CONNECT_TIMEOUT,
    HTTP_READ_TIMEOUT,
    HTTP_WRITE_TIMEOUT,
    HTTP_POOL_TIMEOUT,
    HTTP_GETUPDATES_READ_TIMEOUT,
    HTTP_API_POOL_SIZE,
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
    cancel_request,
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
from utils import (
    validate_quantity,
    split_message,
    RateLimiter,
    truncate_field,
    is_meaningful_text,
)

logger = logging.getLogger(__name__)

# ===== RATE LIMITER =====
rate_limiter = RateLimiter(
    max_requests=RATE_LIMIT_REQUESTS, window_sec=RATE_LIMIT_WINDOW_SEC
)


async def handle_non_text_hint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отклоняет гифки/стикеры/файлы на текстовых шагах диалога."""
    await _safe_reply(
        update,
        "✏️ Отправьте текстовое сообщение (не файл, гифку или стикер).",
    )
    return None  # не меняем состояние ConversationHandler


def _message_text(update: Update) -> str:
    """Текст сообщения или '' (для гифок/стикеров/файлов)."""
    return (update.message.text or "").strip() if update.message else ""


async def _reject_blank_text(update: Update, prompt: str, state):
    await _safe_reply(update, prompt, reply_markup=CANCEL_KEYBOARD)
    return state


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

# ===== СОСТОЯНИЯ ОТМЕНЫ ЗАЯВКИ (админ, флоу «🚫 Отменить») =====
CANCEL_COMMENT, CANCEL_CONFIRM = 146, 147

# ===== ИНФРАСТРУКТУРА ЕДИНОГО ТЕКСТОВОГО РОУТЕРА =====
# Сценарии хранятся в user_data как _flow/_step; ConversationHandler
# как оркестратор не используется (END остаётся лишь константой-сентинелом).

# Ключи данных формы создания заявки (память сценария).
_CREATE_MEMORY_KEYS = frozenset(
    {
        "draft_id",
        "name",
        "quantity",
        "unit",
        "initiator",
        "object",
        "notes",
        "photos",
        "photo_file_ids",
        "pending_draft",
        "_saving",
    }
)

# Временные ключи всех сценариев (очистка при abort/меню/тайм-ауте).
_FLOW_TEMP_KEYS = frozenset(
    {
        "complete_request",
        "complete_comment",
        "complete_requests",
        "awaiting_search",
        "my_requests",
        "my_page",
        "status_request",
        "status_requests",
        "card_request",
        "edit_request",
        "edit_field",
        "edit_fields_order",
        "edit_current_index",
        "delete_request",
        "admin_list_type",
        "admin_rename_old",
        "admin_delete_value",
        "req_filter",
        "cd_page",
        "cancel_request",
        "cancel_comment",
        "_flow",
        "_step",
        "_flow_token",
        "_timeout_job",
    }
)

# Допустимые шаги каждого сценария (для проверки результатов обработчиков).
_FLOW_STEPS = {
    "create": {
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
    },
    "register": {REG_NAME},
    "complete": {COMPLETE_SELECT, COMPLETE_COMMENT, COMPLETE_CONFIRM},
    "cancel_request": {CANCEL_COMMENT, CANCEL_CONFIRM},
    "my_search": {MY_REQUESTS_SELECT},
    "edit": {EDIT_INPUT_VALUE},
    "admin": {
        ADMIN_MENU,
        ADMIN_LIST_VIEW,
        ADMIN_RENAME_NEW,
        ADMIN_ADD_VALUE,
        ADMIN_DELETE,
        ADMIN_BUTTONS_EDIT,
        ADMIN_CLEAR_DB_CONFIRM,
    },
}

# Шаги, ожидающие ввод текста: для них действует conversation timeout.
_TEXT_FLOW_STEPS = {
    "create": {
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
    },
    "register": {REG_NAME},
    "complete": {COMPLETE_COMMENT},
    "cancel_request": {CANCEL_COMMENT},  # текст комментария отмены
    "my_search": set(),  # поиск включается кнопкой mrsearch — там и ставится таймер
    "edit": {EDIT_INPUT_VALUE},
    "admin": {ADMIN_RENAME_NEW, ADMIN_ADD_VALUE},
}


def _end_flow(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Завершает активный сценарий: таймер, _flow/_step/_flow_token."""
    job = context.user_data.pop("_timeout_job", None)
    if job is not None:
        try:
            job.schedule_removal()
        except Exception:  # pragma: no cover - job уже мог быть удалён
            pass
    context.user_data.pop("_flow", None)
    context.user_data.pop("_step", None)
    context.user_data.pop("_flow_token", None)


def _clear_create_memory(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Удаляет поля формы создания из памяти (черновик БД не трогает)."""
    for key in _CREATE_MEMORY_KEYS:
        context.user_data.pop(key, None)


def _abort_flow(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    preserve_persistent_draft: bool = True,
    user_id: Optional[int] = None,
) -> None:
    """Завершает сценарий и чистит временные данные.

    preserve_persistent_draft=False — явная отмена создания: дополнительно
    удаляются поля формы и запись conversation_state.
    """
    _end_flow(context)
    for key in _FLOW_TEMP_KEYS:
        context.user_data.pop(key, None)
    context.user_data.pop("_saving", None)
    if not preserve_persistent_draft:
        _clear_create_memory(context)
        if user_id is not None:
            clear_conversation_state(user_id)


def _disarm_timeout(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отменяет активный job тайм-аута (если есть)."""
    job = context.user_data.get("_timeout_job")
    if job is not None:
        try:
            job.schedule_removal()
        except Exception:  # pragma: no cover
            pass
        context.user_data.pop("_timeout_job", None)


def _arm_timeout(
    context: ContextTypes.DEFAULT_TYPE,
    update=None,
    *,
    chat_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> None:
    """Планирует тайм-аут текстового шага с уникальным flow_token."""
    if update is not None:
        chat = getattr(update, "effective_chat", None)
        user = getattr(update, "effective_user", None)
        chat_id = getattr(chat, "id", chat_id)
        user_id = getattr(user, "id", user_id)
    if chat_id is None or user_id is None:
        return
    _disarm_timeout(context)
    token = uuid4().hex
    context.user_data["_flow_token"] = token
    job = context.job_queue.run_once(
        on_flow_timeout,
        CONVERSATION_TIMEOUT_SEC,
        chat_id=chat_id,
        user_id=user_id,
        data={
            "user_id": user_id,
            "chat_id": chat_id,
            "flow_token": token,
        },
        name=f"flow_timeout:{chat_id}:{user_id}",
    )
    context.user_data["_timeout_job"] = job


async def on_flow_timeout(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Срабатывает по тайм-ауту: чистит сценарий конкретного пользователя.

    Вызывается JobQueue без Update — идентификаторы берём из job.
    Устаревший job (другой flow_token) ничего не делает.
    """
    job = context.job
    data = job.data or {}
    user_id = data.get("user_id")
    chat_id = data.get("chat_id")
    token = data.get("flow_token")
    if user_id is None or chat_id is None:
        return
    if context.user_data.get("_flow_token") != token:
        # Устаревший таймер: сценарий уже завершён или перезапущен.
        return
    flow_before = context.user_data.get("_flow")
    _abort_flow(context, preserve_persistent_draft=True, user_id=user_id)
    # Поля формы — из памяти; запись conversation_state (черновик) остаётся.
    _clear_create_memory(context)
    _log_op(
        "conversation_timeout",
        user_id=user_id,
        chat_id=chat_id,
        state_before=flow_before,
        state_after=None,
        flow=flow_before,
    )
    await _safe_send(
        context,
        chat_id,
        "⏳ Операция прервана по тайм-ауту бездействия.\n"
        "Начните заново — я вернул вас в главное меню.",
        reply_markup=get_main_keyboard(user_id),
    )


def _set_flow_step(
    context: ContextTypes.DEFAULT_TYPE,
    flow: str,
    step: Optional[int] = None,
    *,
    update=None,
    arm: Optional[bool] = None,
) -> None:
    """Устанавливает активный сценарий и шаг (с тайм-аутом при необходимости).

    При смене сценария чужой активный сценарий безопасно завершается:
    персистентный черновик создания уже сохранён пошагово (_save_draft).
    """
    current = context.user_data.get("_flow")
    if current is not None and current != flow:
        _abort_flow(context, preserve_persistent_draft=True)
        if current == "create":
            # Поля формы — в памяти; запись conversation_state остаётся.
            _clear_create_memory(context)
    context.user_data["_flow"] = flow
    if step is not None:
        context.user_data["_step"] = step
    effective_step = context.user_data.get("_step")
    is_text_step = effective_step in _TEXT_FLOW_STEPS.get(flow, set())
    if arm is None:
        arm = is_text_step
    if arm:
        _arm_timeout(context, update)
    else:
        _disarm_timeout(context)


def _apply_step_result(
    context: ContextTypes.DEFAULT_TYPE,
    result,
    *,
    update=None,
    flow: Optional[str] = None,
) -> None:
    """Переносит результат обработчика в _flow/_step.

    - int >= 0 и допустимый для сценария → новый шаг (+ тайм-аут по правилам);
    - ConversationHandler.END → завершение сценария;
    - None / недопустимый для сценария результат → без изменений.
    """
    if result is None:
        # Обработчик ничего не вернул: состояние сценария не меняем.
        return
    if result == ConversationHandler.END:
        _end_flow(context)
        return
    if not isinstance(result, int) or isinstance(result, bool):
        return
    active_flow = context.user_data.get("_flow") or flow
    if active_flow is None:
        return  # inline-секция без сценария: псевдосостояния игнорируются
    valid_steps = _FLOW_STEPS.get(active_flow, set())
    if result not in valid_steps:
        return  # чужой/устаревший результат — не портим активный сценарий
    context.user_data["_flow"] = active_flow
    context.user_data["_step"] = result
    if result in _TEXT_FLOW_STEPS.get(active_flow, set()):
        _arm_timeout(context, update)
    else:
        _disarm_timeout(context)


def _flow_result(func):
    """Оборачивает callback-обработчик: переносит его результат в _flow/_step."""

    @functools.wraps(func)
    async def wrapper(update, context):
        result = await func(update, context)
        _apply_step_result(context, result, update=update)
        return result

    return wrapper


def _normalize_text(text: str) -> str:
    """Нормализует текст для сравнения (вариант-селектор U+FE0F и т.п.)."""
    return text.replace("\ufe0f", "").strip()


def _menu_label(text: str) -> Optional[str]:
    """Возвращает каноническую метку кнопки главного меню или None."""
    normalized = _normalize_text(text)
    for label in _MENU_BUTTONS:
        if _normalize_text(label) == normalized:
            return label
    return None


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


# ===== СЕТЕВЫЕ ВЫЗОВЫ TELEGRAM =====
# Отдельный небольшой пул соединений для getUpdates (long polling).
_GETUPDATES_POOL_SIZE = 4


class TelegramCallResult(NamedTuple):
    """Явный результат сетевого вызова Telegram API.

    success: True — вызов достоверно успешен.
    delivery_uncertain: True — исход доставки неизвестен (read/write timeout
        и т.п.): Telegram мог принять сообщение, слепой повтор запрещён.
    value: объект Telegram (Message) либо True при успехе, иначе None.
    error: исходное исключение при провале.
    """

    success: bool
    delivery_uncertain: bool
    value: object
    error: Optional[BaseException]


# Лимиты повторов: все конечны (политика A5).
_RETRY_AFTER_REPEATS = {"send": 2, "edit": 2, "answer": 1}
_CONNECT_REPEATS = {"send": 1, "edit": 1, "answer": 1}
_UNCERTAIN_REPEATS = {"edit": 1}  # edit_message_text идемпотентен


def _cause_chain(exc: BaseException):
    """Обходит __cause__/__context__ без зацикливания."""
    seen = set()
    node = exc.__cause__ or exc.__context__
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        yield node
        node = node.__cause__ or node.__context__


def _network_kind(exc: BaseException) -> str:
    """Классифицирует первопричину сетевой ошибки: connect|read|write|pool|unknown."""
    for node in _cause_chain(exc):
        if isinstance(node, httpx.PoolTimeout):
            return "pool"
        if isinstance(node, httpx.ConnectTimeout):
            return "connect"
        if isinstance(node, httpx.ReadTimeout):
            return "read"
        if isinstance(node, httpx.WriteTimeout):
            return "write"
        if isinstance(node, httpx.ConnectError):
            return "connect"
        if isinstance(node, httpx.ReadError):
            return "read"
        if isinstance(node, httpx.WriteError):
            return "write"
    return "unknown"


def _cause_name(exc: BaseException) -> str:
    """Имя класса первопричины для журнала ('-' если отсутствует)."""
    for node in _cause_chain(exc):
        return type(node).__name__
    return "-"


def _log_op(
    op: str,
    *,
    user_id: Optional[int] = None,
    chat_id: Optional[int] = None,
    attempt: Optional[int] = None,
    duration_ms: Optional[int] = None,
    success: Optional[bool] = None,
    error: Optional[BaseException] = None,
    state_before: object = None,
    state_after: object = None,
    flow: Optional[str] = None,
    detail: Optional[str] = None,
) -> None:
    """Структурированная диагностика операции.

    Не содержит токен, пароли прокси, тексты комментариев и полные user_data.
    """
    parts = [f"op={op}"]
    if user_id is not None:
        parts.append(f"user_id={user_id}")
    if chat_id is not None:
        parts.append(f"chat_id={chat_id}")
    if flow:
        parts.append(f"flow={flow}")
    if state_before is not None:
        parts.append(f"state_before={state_before}")
    if state_after is not None:
        parts.append(f"state_after={state_after}")
    if attempt is not None:
        parts.append(f"attempt={attempt}")
    if duration_ms is not None:
        parts.append(f"duration_ms={duration_ms}")
    if success is not None:
        parts.append(f"ok={success}")
    if error is not None:
        parts.append(f"error={type(error).__name__}")
        if isinstance(error, TimedOut):
            parts.append(f"cause={_cause_name(error)} kind={_network_kind(error)}")
    if detail:
        parts.append(f"detail={detail}")
    line = " ".join(parts)
    if success is None:
        logger.info(line)
    elif success:
        logger.debug(line)
    else:
        logger.warning(line)


async def _tg_call(
    op: str,
    factory: Callable[[], Awaitable[object]],
    *,
    kind: str,
    user_id: Optional[int] = None,
    chat_id: Optional[int] = None,
) -> TelegramCallResult:
    """Выполняет сетевой вызов Telegram с конечными повторами.

    kind: 'send' | 'edit' | 'answer' — определяет бюджет повторов.
    Повторы: RetryAfter — ждём retry_after; connect/pool — повтор без риска
    дубля; read/write — для send повтор запрещён (доставка неизвестна),
    для edit допущен один идемпотентный повтор; постоянные ошибки не
    повторяются.
    """
    retry_after_left = _RETRY_AFTER_REPEATS[kind]
    connect_left = _CONNECT_REPEATS[kind]
    uncertain_left = _UNCERTAIN_REPEATS.get(kind, 0)
    attempt = 0
    while True:
        attempt += 1
        started = _monotonic()
        try:
            value = await factory()
            duration_ms = int((_monotonic() - started) * 1000)
            _log_op(
                op,
                user_id=user_id,
                chat_id=chat_id,
                attempt=attempt,
                duration_ms=duration_ms,
                success=True,
            )
            return TelegramCallResult(True, False, value, None)
        except RetryAfter as e:
            delay = float(e.retry_after)
            _log_op(
                op,
                user_id=user_id,
                chat_id=chat_id,
                attempt=attempt,
                success=False,
                error=e,
                detail=f"retry_after={delay}",
            )
            if retry_after_left <= 0:
                return TelegramCallResult(False, False, None, e)
            retry_after_left -= 1
            await asyncio.sleep(delay)
        except TimedOut as e:
            duration_ms = int((_monotonic() - started) * 1000)
            network = _network_kind(e)
            _log_op(
                op,
                user_id=user_id,
                chat_id=chat_id,
                attempt=attempt,
                duration_ms=duration_ms,
                success=False,
                error=e,
            )
            if network in ("connect", "pool") and connect_left > 0:
                connect_left -= 1
                continue
            if network in ("connect", "pool"):
                # Гарантированно не отправлено — безопасно завершить.
                return TelegramCallResult(False, False, None, e)
            if network in ("read", "write"):
                if kind == "edit" and uncertain_left > 0:
                    uncertain_left -= 1
                    continue
                # send/answer: слепой повтор запрещён — исход неизвестен.
                return TelegramCallResult(False, True, None, e)
            return TelegramCallResult(False, True, None, e)
        # BadRequest — наследник NetworkError в PTB 20.7: ветка должна идти
        # РАНЬШЕ NetworkError, иначе она недостижима и ошибки классификации.
        except BadRequest as e:
            duration_ms = int((_monotonic() - started) * 1000)
            if kind == "edit" and "Message is not modified" in str(e):
                _log_op(
                    op,
                    user_id=user_id,
                    chat_id=chat_id,
                    attempt=attempt,
                    success=True,
                    detail="message not modified",
                )
                return TelegramCallResult(True, False, True, e)
            _log_op(
                op,
                user_id=user_id,
                chat_id=chat_id,
                attempt=attempt,
                duration_ms=duration_ms,
                success=False,
                error=e,
                detail="permanent",
            )
            return TelegramCallResult(False, False, None, e)
        except NetworkError as e:
            duration_ms = int((_monotonic() - started) * 1000)
            network = _network_kind(e)
            _log_op(
                op,
                user_id=user_id,
                chat_id=chat_id,
                attempt=attempt,
                duration_ms=duration_ms,
                success=False,
                error=e,
            )
            if network == "connect" and connect_left > 0:
                connect_left -= 1
                continue
            if network == "connect":
                return TelegramCallResult(False, False, None, e)
            if kind == "edit" and network in ("read", "write") and uncertain_left > 0:
                uncertain_left -= 1
                continue
            uncertain = network in ("read", "write", "unknown")
            return TelegramCallResult(False, uncertain, None, e)
        except TelegramError as e:
            duration_ms = int((_monotonic() - started) * 1000)
            _log_op(
                op,
                user_id=user_id,
                chat_id=chat_id,
                attempt=attempt,
                success=False,
                error=e,
                detail="permanent",
            )
            return TelegramCallResult(False, False, None, e)


def _update_ids(update) -> tuple:
    """user_id/chat_id из обновления для журнала (без падений на None)."""
    user_id = getattr(update, "effective_user", None)
    chat_id = getattr(update, "effective_chat", None)
    return (
        getattr(user_id, "id", None),
        getattr(chat_id, "id", None),
    )


async def _safe_reply(update, text, **kwargs) -> TelegramCallResult:
    """Отправляет ответ в чат обновления; явный результат доставки."""
    user_id, chat_id = _update_ids(update)
    return await _tg_call(
        "_safe_reply",
        lambda: update.message.reply_text(text, **kwargs),
        kind="send",
        user_id=user_id,
        chat_id=chat_id,
    )


async def _safe_send(context, chat_id, text, **kwargs) -> TelegramCallResult:
    """Отправляет сообщение в указанный чат; явный результат доставки."""
    return await _tg_call(
        "_safe_send",
        lambda: context.bot.send_message(chat_id=chat_id, text=text, **kwargs),
        kind="send",
        chat_id=chat_id,
    )


async def _safe_edit(query, text, **kwargs) -> TelegramCallResult:
    """Редактирует сообщение; явный результат. Идемпотентно — повторы разрешены."""
    user_id = getattr(getattr(query, "from_user", None), "id", None)
    return await _tg_call(
        "_safe_edit",
        lambda: query.edit_message_text(text, **kwargs),
        kind="edit",
        user_id=user_id,
    )


async def _safe_answer(query, text=None, **kwargs) -> TelegramCallResult:
    """Отвечает на callback-запрос (best-effort, короткий повтор)."""
    user_id = getattr(getattr(query, "from_user", None), "id", None)
    return await _tg_call(
        "_safe_answer",
        lambda: query.answer(text, **kwargs),
        kind="answer",
        user_id=user_id,
    )


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
    await _safe_answer(query)

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
        result = await _tg_call(
            "send_photo",
            lambda: context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=file_id,
                disable_notification=True,
            ),
            kind="send",
            chat_id=query.message.chat_id,
        )
        if not result.success:
            logger.warning(f"Не удалось отправить фото: {result.error}")
            await _safe_send(
                context, query.message.chat_id, "⚠️ Не удалось отправить одно из фото."
            )


async def show_request_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает детали заявки по нажатию inline-кнопки из отчёта.
    Проверка прав: только админ или инициатор заявки."""
    query = update.callback_query
    await _safe_answer(query)

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

    keyboard_rows = []
    if photos:
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    f"📎 Фото ({len(photos)})", callback_data=f"photos:{req_id}"
                )
            ]
        )
    # Административные действия — только для админа и только активной заявки
    if is_admin(user_id) and _is_active_request(req):
        keyboard_rows.append(
            [
                InlineKeyboardButton("✅ Завершить", callback_data=f"crdone:{req_id}"),
                InlineKeyboardButton("🚫 Отменить", callback_data=f"reqcx:{req_id}"),
            ]
        )
    keyboard = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None

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
    """Команда /start.

    Завершает все временные сценарии пользователя, очищает поля формы из
    памяти, но НЕ удаляет сохранённый черновик в conversation_state.
    """
    user_id = update.effective_user.id
    username = update.effective_user.first_name or "пользователь"

    # Останавливает любые активные сценарии (включая чужие по отношению
    # к /start) и чистит временные ключи; черновик БД не затрагивается.
    _abort_flow(context, preserve_persistent_draft=True)
    _clear_create_memory(context)

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
            # Регистрация — тоже сценарий: фиксируем _flow/_step явно,
            # так как CommandHandler не запоминает состояния.
            result = await register_start(update, context)
            if isinstance(result, int) and result >= 0:
                _set_flow_step(context, "register", result, update=update)
            return result


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
    text = _message_text(update)
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

    if not is_meaningful_text(text, min_len=2):
        await _safe_reply(
            update,
            "⚠️ Введите имя (минимум 2 символа, не только эмодзи).",
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
    started = _monotonic()
    _log_op("scheduled_report:started", chat_id=chat_id)

    try:
        # build_report — длительные запросы: не блокируем event loop.
        result, ids_with_photos, all_ids, requests_data = await asyncio.to_thread(
            build_report
        )
        keyboard = _build_photo_keyboard(ids_with_photos, all_ids, requests_data)
        await send_long_message(
            context, chat_id, result, parse_mode="HTML", reply_markup=keyboard
        )
        _log_op(
            "scheduled_report:finished",
            chat_id=chat_id,
            duration_ms=int((_monotonic() - started) * 1000),
            success=True,
        )
    except Exception as e:
        _log_op(
            "scheduled_report:failed",
            chat_id=chat_id,
            duration_ms=int((_monotonic() - started) * 1000),
            success=False,
            error=e,
        )
        logger.error(f"Ошибка автоотчёта: {e}", exc_info=True)
        try:
            await _safe_send(context, chat_id, "❌ Ошибка при формировании автоотчёта.")
        except Exception:
            pass
    finally:
        context.bot_data[report_flag] = False


async def archive_old_requests_job(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневное архивирование старых завершённых заявок (job с журналом)."""
    from db import archive_completed_requests

    started = _monotonic()
    _log_op("archive_old_requests:started")
    try:
        count = await asyncio.to_thread(archive_completed_requests)
    except Exception as e:
        _log_op(
            "archive_old_requests:failed",
            duration_ms=int((_monotonic() - started) * 1000),
            success=False,
            error=e,
        )
        raise
    _log_op(
        "archive_old_requests:finished",
        duration_ms=int((_monotonic() - started) * 1000),
        success=True,
        detail=f"archived={count}",
    )
    if count:
        logger.info(f"Автоматическое архивирование: {count} заявок")


# ===== CONVERSATION HANDLER =====
async def start_add_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало добавления заявки"""
    user_id = update.effective_user.id
    context.user_data.pop("complete_request", None)
    context.user_data.pop("complete_comment", None)

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
    raw = update.message.text
    if raw is None:
        await _safe_reply(
            update,
            "✏️ Отправьте текстовое сообщение (не файл, гифку или стикер).",
        )
        return NAME
    text = raw.strip()

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

    if not is_meaningful_text(text, min_len=2):
        await _safe_reply(
            update,
            "⚠️ Введите осмысленное наименование (минимум 2 символа, не только эмодзи).",
            reply_markup=CANCEL_KEYBOARD,
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
    text = _message_text(update)

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
    text = _message_text(update)

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
    text = _message_text(update)

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
    text = _message_text(update)
    if not is_meaningful_text(text, min_len=2):
        await _reject_blank_text(
            update,
            "⚠️ Введите имя инициатора (минимум 2 символа).",
            INITIATOR_CUSTOM,
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
    text = _message_text(update)

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

    if not is_meaningful_text(text, min_len=2):
        await _reject_blank_text(
            update,
            "⚠️ Выберите объект кнопкой или введите название (минимум 2 символа).",
            OBJECT,
        )
        return OBJECT

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
    text = _message_text(update)
    if not is_meaningful_text(text, min_len=2):
        await _reject_blank_text(
            update,
            "⚠️ Введите название объекта (минимум 2 символа).",
            OBJECT_CUSTOM,
        )
        return OBJECT_CUSTOM
    context.user_data["object"] = truncate_field(text, MAX_FIELD_LENGTH)

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
    text = _message_text(update)

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
    text = _message_text(update)
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
    """Отменяет только текущую операцию и возвращает главное меню.

    Внутри создания — очищаются память формы и conversation_state;
    в остальных сценариях персистентный черновик не затрагивается.
    """
    user_id = update.effective_user.id
    flow = context.user_data.get("_flow")
    if flow == "create":
        _abort_flow(context, preserve_persistent_draft=False, user_id=user_id)
    else:
        _abort_flow(context, preserve_persistent_draft=True)
        # Поля формы убираем и вне создания: чужие ключи сценариев уже
        # очищены, черновик БД (если был) остаётся нетронутым.
        _clear_create_memory(context)

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
    await _safe_answer(query)
    mode = query.data.split(":")[1]

    await _safe_edit(query, "⏳ Формирую отчёт...")

    try:
        # Не блокируем event loop: отчёт — длительные запросы к БД.
        result, ids_with_photos, all_ids, requests_data = await asyncio.to_thread(
            build_report, mode=mode
        )
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

    # Переключение сценария ДО записи ключей списка (abort чужого сценария).
    _set_flow_step(context, "complete", COMPLETE_SELECT, update=update)

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

    # Источник истины входа в сценарий завершения (в т.ч. из callback crnoop).
    _set_flow_step(context, "complete", COMPLETE_SELECT, update=update_or_query)
    return COMPLETE_SELECT


async def complete_page_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка пагинации в завершении заявок"""
    query = update.callback_query
    await _safe_answer(query)
    page = int(query.data.split(":")[1])
    return await _show_complete_page(update, context, page)


async def complete_batch_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Массовое завершение заявок на текущей странице"""
    query = update.callback_query
    await _safe_answer(query)
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
    _end_flow(context)
    return ConversationHandler.END


async def complete_select_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает подтверждение завершения с возможностью добавить комментарий"""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    query = update.callback_query
    await _safe_answer(query)
    # Переключение сценария ДО записи ключей: abort чужого сценария не
    # должен стирать данные завершения. Черновик создания уже в БД.
    req_id = int(query.data.split(":")[1])
    _set_flow_step(context, "complete", COMPLETE_COMMENT, update=update)

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
        await _safe_answer(query)
        context.user_data.pop("complete_request", None)
        context.user_data.pop("complete_comment", None)
        _end_flow(context)
        await _safe_edit(query, "❌ Отменено.")
        return ConversationHandler.END

    if data == "cr:nocomment":
        await _safe_answer(query)
        context.user_data["complete_comment"] = ""
        return await _show_complete_final(query, context)

    return COMPLETE_COMMENT


async def complete_comment_text_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Принимает текст комментария от администратора"""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    comment = _message_text(update)
    if comment == "❌ Отмена":
        context.user_data.pop("complete_request", None)
        context.user_data.pop("complete_comment", None)
        user_id = update.effective_user.id
        await _safe_reply(
            update, "❌ Отменено.", reply_markup=get_main_keyboard(user_id)
        )
        return ConversationHandler.END

    if not comment:
        await _safe_reply(
            update,
            "✏️ Отправьте текст комментария или нажмите «➡️ Без комментария».",
        )
        return COMPLETE_COMMENT

    if not is_meaningful_text(comment, min_len=1):
        await _safe_reply(
            update,
            "⚠️ Комментарий должен содержать хотя бы одну букву или цифру.",
        )
        return COMPLETE_COMMENT

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
    # Шаг подтверждения — inline-кнопки; таймер ожидания текста снимаем.
    _set_flow_step(context, "complete", COMPLETE_CONFIRM)
    return COMPLETE_CONFIRM


async def complete_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершает заявку и отправляет уведомление с комментарием"""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    query = update.callback_query
    await _safe_answer(query)
    data = query.data

    if data == "cr:cancel":
        context.user_data.pop("complete_request", None)
        context.user_data.pop("complete_comment", None)
        _end_flow(context)
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
    _end_flow(context)
    return ConversationHandler.END


async def complete_from_card_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """crdone:{id} — запуск завершения из подробной карточки.

    Переиспользует существующую логику: необязательный комментарий
    (cr:nocomment / текст), подтверждение (cr:confirm), атомарный
    complete_request, уведомления и audit.
    """
    query = update.callback_query
    await _safe_answer(query)
    if not is_admin(update.effective_user.id):
        await _safe_edit(query, "⛔ Нет доступа.")
        return None
    req_id = int(query.data.split(":", 1)[1])
    req = get_request_by_id(req_id)
    if not req:
        await _safe_edit(query, "⚠️ Заявка не найдена.")
        return None
    if req.get("done_date") or req.get("status") in (
        "done",
        "archived",
        "cancelled",
    ):
        # Защита от двойного завершения / завершения недоступных заявок.
        status_label = STATUS_LABELS.get(req.get("status"), req.get("status"))
        await _safe_edit(
            query,
            f"ℹ️ Заявку нельзя завершить — статус: {status_label}.",
        )
        return None
    # Переключение сценария ДО записи ключей (abort чужого сценария).
    _set_flow_step(context, "complete", COMPLETE_COMMENT, update=update)
    context.user_data["complete_request"] = req
    context.user_data["complete_comment"] = ""
    qty_text = f"{req.get('quantity', '—')} {req.get('unit', '')}".strip()
    text = (
        f"📋 <b>Заявка #{req['id']}</b>\n\n"
        f"📦 {escape(req.get('name', '—'))}\n"
        f"🔢 {escape(qty_text)}\n"
        f"👤 {escape(req.get('initiator', '—'))}\n"
        f"🏗️ {escape(req.get('object', '—'))}\n\n"
        "Добавьте комментарий или пропустите:"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➡️ Без комментария", callback_data="cr:nocomment")],
            [InlineKeyboardButton("❌ Отмена", callback_data="cr:cancel")],
        ]
    )
    await _safe_edit(query, text, reply_markup=keyboard, parse_mode="HTML")
    return COMPLETE_COMMENT


# ===== ОТМЕНА ЗАЯВКИ (админ, из карточки) =====
_ACTIVE_STATUSES = ("new", "ordered", "delivered")


def _is_active_request(req) -> bool:
    """Активная заявка: допустимы завершение и отмена."""
    return (
        bool(req) and req.get("status") in _ACTIVE_STATUSES and not req.get("done_date")
    )


async def cancel_flow_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """reqcx:{id} — вход в флоу отмены из административной карточки."""
    query = update.callback_query
    await _safe_answer(query)
    if not is_admin(update.effective_user.id):
        await _safe_edit(query, "⛔ Нет доступа.")
        return None
    req_id = int(query.data.split(":", 1)[1])
    req = get_request_by_id(req_id)
    if not req:
        await _safe_edit(query, "⚠️ Заявка не найдена.")
        return None
    if not _is_active_request(req):
        await _safe_edit(
            query,
            "⛔ Отменить можно только активную заявку "
            f"(сейчас: {STATUS_LABELS.get(req.get('status'), req.get('status'))}).",
        )
        return None
    # Переключение сценария ДО записи ключей (abort чужого сценария).
    _set_flow_step(context, "cancel_request", CANCEL_COMMENT, update=update, arm=True)
    context.user_data["cancel_request"] = req
    keyboard = [
        [
            InlineKeyboardButton(
                "➡️ Без комментария", callback_data=f"reqcxnc:{req_id}"
            ),
            InlineKeyboardButton("❌ Назад", callback_data="reqcxback"),
        ],
    ]
    await _safe_edit(
        query,
        f"🚫 <b>Отменить заявку #{req['id']}?</b>\n\n"
        f"📦 {escape(req.get('name', '—'))}\n"
        f"👤 {escape(req.get('initiator', '—'))}\n"
        f"🏗️ Объект: {escape(req.get('object', '—'))}\n\n"
        "Введите необязательный комментарий сообщением\n"
        "или нажмите «➡️ Без комментария»:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )
    return CANCEL_COMMENT


async def _show_cancel_confirm(update, context, *, via_edit: bool):
    """Финальное подтверждение отмены (CANCEL_CONFIRM — inline-шаг, без таймера)."""
    req = context.user_data.get("cancel_request") or {}
    comment = context.user_data.get("cancel_comment", "")
    text = (
        f"🚫 <b>Отменить заявку #{req.get('id', '?')}?</b>\n\n"
        f"📦 {escape(req.get('name', '—'))}\n"
        f"👤 {escape(req.get('initiator', '—'))}\n"
        f"🏗️ Объект: {escape(req.get('object', '—'))}"
    )
    if comment:
        text += f"\n💬 Комментарий: {escape(comment)}"
    text += "\n\nОтменить заявку?"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Да, отменить", callback_data=f"reqcxyes:{req.get('id', '')}"
                ),
                InlineKeyboardButton("❌ Нет", callback_data="reqcxno"),
            ]
        ]
    )
    _set_flow_step(context, "cancel_request", CANCEL_CONFIRM)
    if via_edit:
        await _safe_edit(
            update.callback_query, text, reply_markup=keyboard, parse_mode="HTML"
        )
    else:
        await _safe_reply(update, text, reply_markup=keyboard, parse_mode="HTML")
    return CANCEL_CONFIRM


async def cancel_comment_text_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Принимает необязательный комментарий отмены (через route_text)."""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    text = _message_text(update)
    if not is_meaningful_text(text, min_len=1):
        await _safe_reply(
            update,
            "⚠️ Комментарий должен содержать хотя бы одну букву или цифру,\n"
            "или нажмите «➡️ Без комментария».",
        )
        return CANCEL_COMMENT
    context.user_data["cancel_comment"] = truncate_field(text, MAX_FIELD_LENGTH)
    return await _show_cancel_confirm(update, context, via_edit=False)


async def cancel_nc_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """reqcxnc:{id} — отмена без комментария."""
    query = update.callback_query
    await _safe_answer(query)
    if not is_admin(update.effective_user.id):
        return None
    if not context.user_data.get("cancel_request"):
        await _safe_edit(query, "⏳ Данные устарели. Откройте карточку заново.")
        _end_flow(context)
        return None
    context.user_data["cancel_comment"] = ""
    return await _show_cancel_confirm(update, context, via_edit=True)


async def cancel_back_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """reqcxback / reqcxno — выход из флоу отмены без действия."""
    query = update.callback_query
    await _safe_answer(query)
    context.user_data.pop("cancel_request", None)
    context.user_data.pop("cancel_comment", None)
    _end_flow(context)
    req = context.user_data.get("card_request")
    if req:
        return await _render_request_card(query, context, req)
    await _safe_edit(query, "↩️ Возврат без отмены.")
    return None


async def cancel_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """reqcxyes:{id} — атомарная отмена + уведомление инициатору."""
    query = update.callback_query
    await _safe_answer(query)
    if not is_admin(update.effective_user.id):
        return None
    req_id = int(query.data.split(":", 1)[1])
    req = context.user_data.get("cancel_request")
    if not req or req.get("id") != req_id:
        await _safe_edit(query, "⏳ Данные устарели. Откройте карточку заново.")
        _end_flow(context)
        return None
    comment = context.user_data.get("cancel_comment", "")

    row, changed = cancel_request(
        req_id,
        user_id=update.effective_user.id,
        user_name=_user_name(update),
        comment=comment,
    )

    context.user_data.pop("cancel_request", None)
    context.user_data.pop("cancel_comment", None)
    _end_flow(context)

    if row is None:
        await _safe_edit(query, "⚠️ Заявка не найдена.")
        return None
    if row.get("status") != "cancelled":
        status_label = STATUS_LABELS.get(row.get("status"), row.get("status"))
        await _safe_edit(query, f"⛔ Нельзя отменить — статус: {status_label}.")
        return None
    if not changed:
        # Идемпотентность: повторная отмена без второго audit и уведомлений.
        await _safe_edit(query, "ℹ️ Заявка уже была отменена.")
        return None

    success_text = f"🚫 Заявка #{req_id} отменена."
    if comment:
        success_text += f"\n💬 Комментарий: {escape(comment)}"
    await _safe_edit(query, success_text, parse_mode="HTML")

    # Уведомление привязанному инициатору (комментарий включается).
    initiator = row.get("initiator") or ""
    if initiator:
        tg_id = get_initiator_tg_id(initiator)
        if tg_id:
            notify = f"🚫 <b>Заявка #{req_id} отменена</b>\n\n"
            notify += f"📦 {escape(row.get('name', '—'))}\n"
            notify += f"🏗️ {escape(row.get('object', '—'))}"
            if comment:
                notify += f"\n💬 Комментарий: {escape(comment)}"
            await _safe_send(
                context,
                tg_id,
                notify,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
    return None


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
    await _safe_answer(query)
    page = int(query.data.split(":")[1])
    return await _show_status_page(update, context, page)


async def status_select_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает варианты нового статуса"""
    query = update.callback_query
    await _safe_answer(query)
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
        if st_key in ("done", "cancelled"):
            continue  # done — через завершение; cancelled — только через cancel_request
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
    await _safe_answer(query)
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
    await _safe_answer(query)
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
    await _safe_answer(query)

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
    await _safe_answer(query)
    # Сначала переключение сценария (abort чужого не сотрёт новые ключи),
    # затем установка флага ожидания текста — включаем conversation timeout.
    _set_flow_step(context, "my_search", MY_REQUESTS_SELECT, update=update, arm=True)
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
    """Обрабатывает текст поиска.

    Кнопки главного меню сюда не попадают: их первым перехватывает
    route_text и переключает раздел.
    """
    text = _message_text(update)

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
    await _safe_answer(query)
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
    # Открываем раздел справочника: сценарий фиксируется до записи ключей.
    _set_flow_step(context, "admin", ADMIN_LIST_VIEW, update=update)
    context.user_data["admin_list_type"] = list_type
    return await _show_list(update, context, list_type)


async def admin_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Создаёт резервную копию БД"""
    query = update.callback_query
    await _safe_answer(query)

    path = await asyncio.to_thread(backup_db)
    if path:
        ok = await asyncio.to_thread(verify_backup, path)
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
    await _safe_answer(query)

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
    await _safe_answer(query)
    data = query.data
    list_type = context.user_data.get("admin_list_type", "object")

    if data == "add":
        # Переключение сценария до записи ключей (abort чужого сценария).
        _set_flow_step(context, "admin", ADMIN_ADD_VALUE, update=update, arm=True)
        context.user_data["admin_list_type"] = list_type
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
        # Переключение сценария до записи ключей (abort чужого сценария).
        _set_flow_step(context, "admin", ADMIN_RENAME_NEW, update=update, arm=True)
        context.user_data["admin_list_type"] = list_type
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
    await _safe_answer(query)
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
            await _safe_answer(query, f"Удалено: {value}", show_alert=True)
        else:
            await _safe_answer(query, "⚠️ Значение не найдено.", show_alert=True)

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
    await _safe_answer(query)
    data = query.data
    list_type = context.user_data.get("admin_list_type", "object")

    if data.startswith("bpr:"):
        n = int(data.split(":")[1])
        if 1 <= n <= 6:
            set_setting(f"buttons_per_row_{list_type}", str(n))
            await _safe_answer(query, f"Установлено: {n} в строке", show_alert=True)
        else:
            await _safe_answer(query, "Значение должно быть от 1 до 6", show_alert=True)

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
    await _safe_answer(query)

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
    await _safe_answer(query)
    data = query.data

    if data == "clear_db:cancel":
        await _safe_edit(query, "❌ Очистка отменена.")
        return await _show_main_settings(query)

    try:
        # Очистка включает backup+verify — выполняется в рабочем потоке целиком.
        count = await asyncio.to_thread(clear_all_requests)
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
    """Выход из админ-панели: закрывает только текущую операцию."""
    context.user_data.pop("admin_list_type", None)
    context.user_data.pop("admin_rename_old", None)
    context.user_data.pop("admin_delete_value", None)
    _end_flow(context)
    user_id = update.effective_user.id
    await _safe_reply(
        update, "❌ Настройки закрыты.", reply_markup=get_main_keyboard(user_id)
    )
    return ConversationHandler.END


# ===== ГЛАВНЫЙ ТЕКСТОВЫЙ РОУТЕР =====
# Диспетчеры шагов создания (определения функций уже выше по модулю).
_CREATE_TEXT_HANDLERS = {
    NAME: set_name,
    QUANTITY: set_quantity,
    UNIT: set_unit,
    INITIATOR: set_initiator,
    INITIATOR_CUSTOM: set_initiator_custom,
    OBJECT: set_object,
    OBJECT_CUSTOM: set_object_custom,
    NOTES: set_notes_and_save,
    CONFIRM: confirm_request,
}


async def _dispatch_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, label):
    """Прерывает активный сценарий и открывает раздел с первого нажатия."""
    flow_before = context.user_data.get("_flow")
    # Персистентный черновик (conversation_state) при переключении сохраняем:
    # удаление возможно только явной отменой внутри создания или по /start.
    _abort_flow(context, preserve_persistent_draft=True)
    user_id, chat_id = _update_ids(update)
    _log_op(
        "menu_dispatch",
        user_id=user_id,
        chat_id=chat_id,
        state_before=flow_before,
        state_after=None,
        flow=flow_before,
        detail=label,
    )

    handler = None
    flow = None
    if label == "➕ Добавить заявку":
        handler, flow = start_add_request, "create"
    elif label == "📋 Мои заявки":
        handler, flow = my_requests_start, "my_search"
    elif label == "📋 Заявки":
        # Кнопка остаётся обрабатываемой для старых Telegram-клавиатур.
        handler = requests_list_start
    elif label == "✅ Завершить заявку":
        handler, flow = complete_start, "complete"
    elif label == "📊 Анализ":
        handler = analysis_start
    elif label == "📊 Дашборд":
        handler = dashboard_start
    elif label == "⚙️ Настройки":
        handler = admin_start
    elif label == "⛔ Выход":
        await _safe_reply(
            update,
            "👋 До встречи! Для возврата введите /start",
            reply_markup=ReplyKeyboardMarkup([["/start"]], resize_keyboard=True),
        )
        return None
    else:
        return None

    result = await handler(update, context)
    _apply_step_result(context, result, update=update, flow=flow)
    return result


async def _dispatch_create_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
):
    """Маршрутизация текста внутри сценария создания заявки."""
    step = context.user_data.get("_step")
    if step == PHOTO:
        if text in ("✅ Готово", "➡️ Пропустить"):
            handler = handle_photo_done
        else:
            handler = handle_photo_hint
    else:
        handler = _CREATE_TEXT_HANDLERS.get(step)
    if handler is None:
        await _safe_reply(
            update,
            "Используйте кнопки меню или /start",
            reply_markup=get_main_keyboard(update.effective_user.id),
        )
        return None
    return await handler(update, context)


async def route_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Единый текстовый маршрутизатор (регистрируется первым в группе).

    Порядок: сначала кнопки главного меню — они завершают несовместимый
    сценарий и открывают раздел с первого нажатия; затем активный сценарий
    ввода текста; иначе — нейтральная подсказка. Сообщение «Выберите заявку
    кнопкой из списка» возможно только вне меню, в состоянии выбора.
    """
    text = _message_text(update)
    user_id = update.effective_user.id

    # 1. Кнопки главного меню — всегда первыми.
    label = _menu_label(text)
    if label is not None:
        return await _dispatch_menu(update, context, label)

    # 2. Отмена текущей операции (не затрагивает чужие данные).
    if _normalize_text(text) == _normalize_text("❌ Отмена"):
        return await cancel(update, context)

    # 3. Активный сценарий ввода.
    flow = context.user_data.get("_flow")
    result = None
    if flow == "create":
        result = await _dispatch_create_text(update, context, text)
    elif flow == "register" and context.user_data.get("_step") == REG_NAME:
        result = await register_name(update, context)
    elif flow == "complete":
        step = context.user_data.get("_step")
        if step == COMPLETE_COMMENT:
            result = await complete_comment_text_handler(update, context)
        elif step == COMPLETE_CONFIRM:
            await _safe_reply(update, "👆 Подтвердите или отмените кнопкой.")
        elif step == COMPLETE_SELECT:
            await _safe_reply(update, "👆 Выберите заявку кнопкой из списка.")
        else:
            flow = None
    elif flow == "cancel_request":
        step = context.user_data.get("_step")
        if step == CANCEL_COMMENT:
            result = await cancel_comment_text_handler(update, context)
        elif step == CANCEL_CONFIRM:
            await _safe_reply(update, "👆 Подтвердите отмену или нажмите «❌ Назад».")
        else:
            flow = None
    elif flow == "my_search":
        result = await my_requests_search_text_handler(update, context)
    elif flow == "edit" and context.user_data.get("_step") == EDIT_INPUT_VALUE:
        result = await edit_input_handler(update, context)
    elif flow == "admin":
        step = context.user_data.get("_step")
        if step == ADMIN_RENAME_NEW:
            result = await admin_rename_new_handler(update, context)
        elif step == ADMIN_ADD_VALUE:
            result = await admin_add_value_handler(update, context)
        else:
            flow = None
    else:
        flow = None

    if flow is None and result is None:
        # Нет активного сценария — нейтральная подсказка, меню не блокируется.
        await _safe_reply(
            update,
            "Используйте кнопки меню или /start",
            reply_markup=get_main_keyboard(user_id),
        )
        return None

    _apply_step_result(context, result, update=update)
    return None


async def route_non_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Маршрутизация не-текстовых сообщений (фото, стикеры, файлы)."""
    flow = context.user_data.get("_flow")
    if flow == "create" and context.user_data.get("_step") == PHOTO:
        if update.message and update.message.photo:
            result = await handle_photo(update, context)
        else:
            result = await handle_photo_hint(update, context)
        _apply_step_result(context, result, update=update)
        return
    if flow is not None:
        # Ошибка формата в активном сценарии — подсказка, состояние не меняем.
        await handle_non_text_hint(update, context)
        return
    # Вне сценариев не-текст игнорируется (как и раньше).


# ===== ДАШБОРД =====
async def dashboard_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает дашборд администратору."""
    if not is_admin(update.effective_user.id):
        await _safe_reply(update, "⛔ У вас нет прав для этой команды.")
        return ConversationHandler.END
    text = await asyncio.to_thread(build_dashboard)
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
    await _safe_answer(query)
    if not is_admin(update.effective_user.id):
        return
    context.user_data["req_filter"] = None
    await _show_requests_list(update, context, 0)


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
        ],
        [
            InlineKeyboardButton(
                "✅ Отменённые" if status_filter == "cancelled" else "🚫 Отменённые",
                callback_data="reqf:cancelled",
            ),
        ],
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
        "cancelled": "🚫",
        "archived": "📦",
    }.get(status, "⚪")


async def requests_filter_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает фильтр по статусу."""
    query = update.callback_query
    await _safe_answer(query)
    filter_val = query.data.split(":")[1]
    context.user_data["req_filter"] = None if filter_val == "none" else filter_val
    return await _show_requests_list(update, context, 0)


async def requests_page_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пагинация списка заявок."""
    query = update.callback_query
    await _safe_answer(query)
    page = int(query.data.split(":")[1])
    return await _show_requests_list(update, context, page)


async def requests_card_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает карточку заявки с действиями."""
    query = update.callback_query
    await _safe_answer(query)
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
    admin_rows = []
    card_user = getattr(getattr(query, "from_user", None), "id", None)
    if is_admin(card_user) and _is_active_request(req):
        admin_rows.append(
            InlineKeyboardButton("✅ Завершить", callback_data=f"crdone:{req_id}")
        )
        admin_rows.append(
            InlineKeyboardButton("🚫 Отменить", callback_data=f"reqcx:{req_id}")
        )
    action_row = admin_rows or []
    action_row.append(
        InlineKeyboardButton("🗑 Удалить", callback_data=f"reqdel:{req_id}")
    )
    buttons.append(action_row)
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
        await _safe_answer(query)
        return await _show_requests_list(update, context, 0)

    if data.startswith("reqst:"):
        # Смена статуса — перенаправляем в status flow
        await _safe_answer(query)
        req_id = int(data.split(":")[1])
        req = get_request_by_id(req_id)
        if not req:
            await _safe_edit(query, "⚠️ Заявка не найдена.")
            return ConversationHandler.END
        if req.get("status") in ("cancelled", "archived"):
            # Переходы из cancelled/archived через generic-UI не предлагаются.
            status_label = STATUS_LABELS.get(req.get("status"), req.get("status"))
            await _safe_edit(query, f"⛔ Смена статуса недоступна: {status_label}.")
            return REQUESTS_CARD
        context.user_data["status_request"] = req
        current = req.get("status", "new")
        current_label = STATUS_LABELS.get(current, current)
        keyboard = []
        for st_key, st_label in STATUS_LABELS.items():
            if st_key in ("done", "archived", "cancelled"):
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
        await _safe_answer(query)
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
        await _safe_answer(query)
        context.user_data.pop("status_request", None)
        req = context.user_data.get("card_request")
        if req:
            return await _render_request_card(query, context, req)
        await _safe_edit(query, "❌ Отменено.")
        return ConversationHandler.END

    if data.startswith("reqe:"):
        # Редактирование — переход к выбору поля
        await _safe_answer(query)
        req_id = int(data.split(":")[1])
        req = get_request_by_id(req_id)
        if not req:
            await _safe_edit(query, "⚠️ Заявка не найдена.")
            return ConversationHandler.END
        # Переключение сценария ДО записи ключей (abort чужого сценария).
        _set_flow_step(context, "edit", EDIT_INPUT_VALUE, update=update, arm=True)
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
        await _safe_answer(query)
        field = data.split(":")[1]
        # Переключение сценария ДО записи ключей (abort чужого сценария).
        _set_flow_step(context, "edit", EDIT_INPUT_VALUE, update=update, arm=True)
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
        await _safe_answer(query)
        return await _finish_editing(update, context, update.effective_user.id)

    if data.startswith("reqdel:"):
        # Удаление — запрос подтверждения
        await _safe_answer(query)
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
        await _safe_answer(query)
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
        await _safe_answer(query)
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
    await _safe_answer(query)
    user_id = update.effective_user.id
    req = context.user_data.get("edit_request")

    if not req:
        await _safe_edit(query, "⚠️ Ошибка: данные не найдены.")
        return ConversationHandler.END

    return await _move_to_next_field(update, context, user_id, req)


async def edit_finish_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки 'Завершить' при редактировании."""
    query = update.callback_query
    await _safe_answer(query)
    user_id = update.effective_user.id

    return await _finish_editing(update, context, user_id)


async def edit_prev_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки 'Назад' при редактировании."""
    query = update.callback_query
    await _safe_answer(query)
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
    await _safe_answer(query)
    return await _show_requests_list(update, context, 0)


async def requests_noop_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Noop для пагинации."""
    query = update.callback_query
    await _safe_answer(query)
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


def register_handlers(app):
    """Регистрация всех обработчиков PTB в одной группе.

    Порядок критичен: сначала команды, затем главный текстовый
    роутер (кнопки меню работают из любого состояния с первого
    нажатия), затем callback-обработчики с якорными паттернами.
    ConversationHandler не используется.
    """
    # ===== Регистрация обработчиков (одна группа) =====
    # Порядок критичен: команды, затем ГЛАВНЫЙ текстовый роутер (кнопки
    # меню работают из любого состояния с первого нажатия), затем
    # не-текстовый роутер и callback-обработчики с якорными паттернами.
    # ConversationHandler как оркестратор не используется — состояния
    # живут в user_data (_flow/_step), предупреждений PTBUserWarning
    # о per_message=False быть не должно.
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("status", status_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_text))
    app.add_handler(MessageHandler(~filters.TEXT & ~filters.COMMAND, route_non_text))

    # --- Завершение заявки ---
    app.add_handler(
        CallbackQueryHandler(_flow_result(complete_page_handler), pattern=r"^crp:\d+$")
    )
    app.add_handler(
        CallbackQueryHandler(_flow_result(complete_batch_handler), pattern=r"^crb:\d+$")
    )
    app.add_handler(
        CallbackQueryHandler(_flow_result(complete_start), pattern=r"^crnoop$")
    )
    app.add_handler(
        CallbackQueryHandler(_flow_result(complete_select_handler), pattern=r"^cr:\d+$")
    )
    app.add_handler(
        CallbackQueryHandler(
            _flow_result(complete_comment_handler),
            pattern=r"^cr:(nocomment|cancel)$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            _flow_result(complete_confirm_handler),
            pattern=r"^cr:(confirm|cancel)$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            _flow_result(complete_from_card_handler), pattern=r"^crdone:\d+$"
        )
    )

    # --- Отмена заявки (админ, из карточки) ---
    app.add_handler(
        CallbackQueryHandler(_flow_result(cancel_flow_start), pattern=r"^reqcx:\d+$")
    )
    app.add_handler(
        CallbackQueryHandler(_flow_result(cancel_nc_handler), pattern=r"^reqcxnc:\d+$")
    )
    app.add_handler(
        CallbackQueryHandler(_flow_result(cancel_back_handler), pattern=r"^reqcxback$")
    )
    app.add_handler(
        CallbackQueryHandler(
            _flow_result(cancel_confirm_handler), pattern=r"^reqcxyes:\d+$"
        )
    )
    app.add_handler(
        CallbackQueryHandler(_flow_result(cancel_back_handler), pattern=r"^reqcxno$")
    )

    # --- Смена статуса (/status и карточки) ---
    app.add_handler(
        CallbackQueryHandler(_flow_result(status_select_handler), pattern=r"^ss:\d+$")
    )
    app.add_handler(
        CallbackQueryHandler(_flow_result(status_page_handler), pattern=r"^ssp:\d+$")
    )
    app.add_handler(
        CallbackQueryHandler(_flow_result(status_confirm_handler), pattern=r"^ssc:.+$")
    )

    # --- Мои заявки ---
    app.add_handler(
        CallbackQueryHandler(
            _flow_result(my_requests_page_handler), pattern=r"^mrp:(prev|next)$"
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            _flow_result(my_requests_refresh_handler), pattern=r"^mrrefresh$"
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            _flow_result(my_requests_search_handler), pattern=r"^mrsearch$"
        )
    )
    app.add_handler(
        CallbackQueryHandler(_flow_result(my_requests_start), pattern=r"^mrnoop$")
    )

    # --- Список заявок и карточка ---
    app.add_handler(
        CallbackQueryHandler(
            _flow_result(requests_filter_handler), pattern=r"^reqf:.+$"
        )
    )
    app.add_handler(
        CallbackQueryHandler(_flow_result(requests_page_handler), pattern=r"^reqp:\d+$")
    )
    app.add_handler(
        CallbackQueryHandler(_flow_result(requests_card_show), pattern=r"^rc:\d+$")
    )
    app.add_handler(
        CallbackQueryHandler(_flow_result(requests_noop_handler), pattern=r"^reqnoop$")
    )
    # Навигация редактирования — отдельными паттернами (раньше входила
    # в составной pattern состояния REQUESTS_CARD).
    app.add_handler(
        CallbackQueryHandler(_flow_result(edit_next_handler), pattern=r"^editnext$")
    )
    app.add_handler(
        CallbackQueryHandler(_flow_result(edit_finish_handler), pattern=r"^editfinish$")
    )
    app.add_handler(
        CallbackQueryHandler(_flow_result(edit_prev_handler), pattern=r"^editprev$")
    )
    app.add_handler(
        CallbackQueryHandler(
            _flow_result(requests_card_handler),
            pattern=(
                r"^(reqst|reqssc|reqsscancel|reqe|editf|editcancel"
                r"|reqdel|reqdelyes|reqdelno|reqback):?.*$"
            ),
        )
    )

    # --- Админ-панель (настройки) ---
    # back_main/list: обслуживает admin_menu_handler (поведение как в
    # старом ADMIN_MENU); кнопки списка — admin_list_view_handler.
    app.add_handler(
        CallbackQueryHandler(
            _flow_result(admin_menu_handler),
            pattern=r"^(buttons|clear_db|backup|audit|back_main|list:.+)$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            _flow_result(admin_list_view_handler),
            pattern=r"^(add|rename:\d+|del:\d+|set_bpr)$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            _flow_result(admin_delete_handler), pattern=r"^confirm_del:\d+$"
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            _flow_result(admin_buttons_edit_handler), pattern=r"^bpr:[1-6]$"
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            _flow_result(admin_clear_db_execute),
            pattern=r"^clear_db:(confirm|cancel)$",
        )
    )

    # --- Анализ и дашборд ---
    app.add_handler(
        CallbackQueryHandler(
            _flow_result(analysis_filter_handler),
            pattern=r"^report:(active|completed)$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(dashboard_reqs_callback, pattern=r"^reqs:all:\d+$")
    )

    # --- Просмотр фото и карточка заявки (stateless, IDOR) ---
    app.add_handler(CallbackQueryHandler(show_request_photos, pattern=r"^photos:\d+$"))
    app.add_handler(CallbackQueryHandler(show_request_details, pattern=r"^detail:\d+$"))


def main():
    """Точка входа"""
    from config import _validate_config

    _validate_config()
    init_db()

    # Подавляем INFO-логи HTTPX/httpcore, чтобы токен не попадал в журналы
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # SOCKS5-прокси применяется и к обычным запросам, и к getUpdates.
    proxy_url = None
    if SOCKS5_PROXY:
        proxy_url = SOCKS5_PROXY
        if not proxy_url.startswith("socks"):
            proxy_url = f"socks5://{proxy_url}"
        masked = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
        logger.info(f"Используется SOCKS5-прокси: {masked}")

    def _make_request(read_timeout: float, pool_size: Optional[int]) -> HTTPXRequest:
        """HTTPXRequest с настроенными тайм-аутами из config (env)."""
        kwargs = {
            "connect_timeout": HTTP_CONNECT_TIMEOUT,
            "read_timeout": read_timeout,
            "write_timeout": HTTP_WRITE_TIMEOUT,
            "pool_timeout": HTTP_POOL_TIMEOUT,
        }
        if proxy_url:
            kwargs["proxy"] = proxy_url
        if pool_size:
            kwargs["connection_pool_size"] = pool_size
        return HTTPXRequest(**kwargs)

    builder = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN)
    # Отдельные HTTP-клиенты: обычный API и long polling (getUpdates).
    builder = builder.request(_make_request(HTTP_READ_TIMEOUT, HTTP_API_POOL_SIZE))
    builder = builder.get_updates_request(
        _make_request(HTTP_GETUPDATES_READ_TIMEOUT, _GETUPDATES_POOL_SIZE)
    )

    async def _on_post_init(_application):
        _log_op("application_start", detail=f"version={APP_VERSION}")

    async def _on_post_shutdown(_application):
        _log_op("application_stop", detail=f"version={APP_VERSION}")

    builder = builder.post_init(_on_post_init).post_shutdown(_on_post_shutdown)
    app = builder.build()

    # Регистрация глобального обработчика ошибок
    app.add_error_handler(error_handler)

    register_handlers(app)

    tz = ZoneInfo(REPORT_TIMEZONE) if REPORT_TIMEZONE else None

    app.job_queue.run_daily(
        scheduled_report,
        time=time(hour=REPORT_HOUR, minute=REPORT_MINUTE, tzinfo=tz),
        chat_id=int(TELEGRAM_CHAT_ID),
        name="daily_report",
    )

    # Архивирование старых завершённых заявок (ежедневно в 03:00)
    app.job_queue.run_daily(
        archive_old_requests_job,
        time=time(hour=3, minute=0, tzinfo=tz),
        name="archive_old_requests",
    )

    logger.info(
        f"Бот запущен (v{APP_VERSION}). Отчёт ежедневно в {REPORT_HOUR:02d}:{REPORT_MINUTE:02d}"
    )
    app.run_polling()


if __name__ == "__main__":
    main()
