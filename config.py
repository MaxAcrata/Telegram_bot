import os
import sys
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _require_env(name: str) -> str:
    """Возвращает значение обязательной переменной или завершает процесс."""
    value = os.getenv(name)
    if not value:
        print(
            f"❌ Обязательная переменная окружения {name} не задана. "
            f"Добавьте её в .env",
            file=sys.stderr,
        )
        sys.exit(1)
    return value


def _validate_config():
    """Проверяет конфигурацию при запуске: диапазоны, ID, пути."""
    errors = []

    # REPORT_HOUR: 0..23
    if not (0 <= REPORT_HOUR <= 23):
        errors.append(f"REPORT_HOUR={REPORT_HOUR} — допустимый диапазон 0..23")

    # REPORT_MINUTE: 0..59
    if not (0 <= REPORT_MINUTE <= 59):
        errors.append(f"REPORT_MINUTE={REPORT_MINUTE} — допустимый диапазон 0..59")

    # OVERDUE_DAYS: >= 1
    if OVERDUE_DAYS < 1:
        errors.append(f"OVERDUE_DAYS={OVERDUE_DAYS} — должно быть >= 1")

    # DB_PATH: проверяем доступность директории
    db_dir = Path(DB_PATH).parent
    try:
        db_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        errors.append(f"DB_PATH={DB_PATH} — невозможно создать директорию: {e}")

    # ADMIN_IDS: должны быть числами
    if not ADMIN_IDS:
        errors.append("ADMIN_IDS пуст — ни один администратор не назначен")

    # TELEGRAM_CHAT_ID: должно быть числом
    try:
        int(TELEGRAM_CHAT_ID)
    except ValueError:
        errors.append(
            f"TELEGRAM_CHAT_ID={TELEGRAM_CHAT_ID!r} — должно быть целым числом"
        )

    # MAX_PHOTOS_PER_REQUEST: 1..10
    if not (1 <= MAX_PHOTOS_PER_REQUEST <= 10):
        errors.append(
            f"MAX_PHOTOS_PER_REQUEST={MAX_PHOTOS_PER_REQUEST} — допустимый диапазон 1..10"
        )

    # RATE_LIMIT_REQUESTS: >= 1
    if RATE_LIMIT_REQUESTS < 1:
        errors.append(f"RATE_LIMIT_REQUESTS={RATE_LIMIT_REQUESTS} — должно быть >= 1")

    # RATE_LIMIT_WINDOW_SEC: >= 1
    if RATE_LIMIT_WINDOW_SEC < 1:
        errors.append(
            f"RATE_LIMIT_WINDOW_SEC={RATE_LIMIT_WINDOW_SEC} — должно быть >= 1"
        )

    # MAX_FIELD_LENGTH: >= 1
    if MAX_FIELD_LENGTH < 1:
        errors.append(f"MAX_FIELD_LENGTH={MAX_FIELD_LENGTH} — должно быть >= 1")

    # CONVERSATION_TIMEOUT_SEC: >= 30
    if CONVERSATION_TIMEOUT_SEC < 30:
        errors.append(
            f"CONVERSATION_TIMEOUT_SEC={CONVERSATION_TIMEOUT_SEC} — должно быть >= 30"
        )

    # HTTP timeouts: >= 1 секунды
    _http_timeouts = {
        "HTTP_CONNECT_TIMEOUT": HTTP_CONNECT_TIMEOUT,
        "HTTP_READ_TIMEOUT": HTTP_READ_TIMEOUT,
        "HTTP_WRITE_TIMEOUT": HTTP_WRITE_TIMEOUT,
        "HTTP_POOL_TIMEOUT": HTTP_POOL_TIMEOUT,
        "HTTP_GETUPDATES_READ_TIMEOUT": HTTP_GETUPDATES_READ_TIMEOUT,
    }
    for _name, _value in _http_timeouts.items():
        if _value < 1:
            errors.append(f"{_name}={_value} — должно быть >= 1")

    # HTTP_GETUPDATES_READ_TIMEOUT должен превышать long-poll timeout PTB (10 сек)
    if HTTP_GETUPDATES_READ_TIMEOUT <= 10:
        errors.append(
            f"HTTP_GETUPDATES_READ_TIMEOUT={HTTP_GETUPDATES_READ_TIMEOUT} — "
            "должно превышать long-poll timeout PTB (10 сек)"
        )

    # HTTP_API_POOL_SIZE: если задана — >= 32
    if HTTP_API_POOL_SIZE is not None and HTTP_API_POOL_SIZE < 32:
        errors.append(
            f"HTTP_API_POOL_SIZE={HTTP_API_POOL_SIZE} — если задана, то >= 32"
        )

    if errors:
        for e in errors:
            print(f"❌ Ошибка конфигурации: {e}", file=sys.stderr)
        sys.exit(1)


# ===== TELEGRAM =====
TELEGRAM_BOT_TOKEN = _require_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _require_env("TELEGRAM_CHAT_ID")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = []
for _x in ADMIN_IDS_RAW.split(","):
    _x = _x.strip()
    if _x:
        try:
            ADMIN_IDS.append(int(_x))
        except ValueError:
            print(
                f"❌ ADMIN_IDS: невозможно преобразовать '{_x}' в число. "
                f"Пропускается.",
                file=sys.stderr,
            )

# ===== DATABASE =====
# Amvera монтирует persistence в /data — храним БД там, чтобы данные
# переживали редеплой. Локально (когда /data нет) — bot.db рядом с bot.py.
# Явный DB_PATH в окружении имеет приоритет.
_LOCAL_DB = str(Path(__file__).resolve().parent / "bot.db")
_CLOUD_DATA = Path("/data")


def _default_db_path() -> str:
    if _CLOUD_DATA.is_dir() and os.access(_CLOUD_DATA, os.W_OK):
        return str(_CLOUD_DATA / "bot.db")
    return _LOCAL_DB


DB_PATH = os.getenv("DB_PATH", _default_db_path())

# ===== REPORTS =====
OVERDUE_DAYS = int(os.getenv("OVERDUE_DAYS", 3))
REPORT_HOUR = int(os.getenv("REPORT_HOUR", 9))
REPORT_MINUTE = int(os.getenv("REPORT_MINUTE", 0))
REPORT_FILE_NAME = "report.txt"

# ===== PROXY =====
SOCKS5_PROXY = os.getenv("SOCKS5_PROXY", "")

# ===== TIMEZONE =====
REPORT_TIMEZONE = os.getenv("REPORT_TIMEZONE", "")

# ===== OTHER =====
MAX_MESSAGE_LENGTH = 4000

# ===== RATE LIMITING =====
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "5"))
RATE_LIMIT_WINDOW_SEC = int(os.getenv("RATE_LIMIT_WINDOW_SEC", "60"))
MAX_FIELD_LENGTH = int(os.getenv("MAX_FIELD_LENGTH", "500"))
MAX_PHOTOS_PER_REQUEST = int(os.getenv("MAX_PHOTOS_PER_REQUEST", "5"))

# ===== CONVERSATION TIMEOUT =====
# Тайм-аут текстовых сценариев (сек). По истечении — очистка состояния,
# уведомление пользователя и возврат в главное меню.
CONVERSATION_TIMEOUT_SEC = int(os.getenv("CONVERSATION_TIMEOUT_SEC", "600"))

# ===== HTTP / NETWORK =====
# Тайм-ауты HTTPXRequest: обычные запросы к Telegram API.
HTTP_CONNECT_TIMEOUT = float(os.getenv("HTTP_CONNECT_TIMEOUT", "10"))
HTTP_READ_TIMEOUT = float(os.getenv("HTTP_READ_TIMEOUT", "30"))
HTTP_WRITE_TIMEOUT = float(os.getenv("HTTP_WRITE_TIMEOUT", "30"))
HTTP_POOL_TIMEOUT = float(os.getenv("HTTP_POOL_TIMEOUT", "5"))
# Отдельный read-timeout для getUpdates (long polling); должен превышать
# long-poll timeout PTB (10 сек по умолчанию).
HTTP_GETUPDATES_READ_TIMEOUT = float(os.getenv("HTTP_GETUPDATES_READ_TIMEOUT", "60"))
# Пул соединений обычного API: не задаём по умолчанию (дефолт PTB),
# только если указана валидная переменная (>= 32).
_http_api_pool_raw = os.getenv("HTTP_API_POOL_SIZE", "").strip()
HTTP_API_POOL_SIZE = int(_http_api_pool_raw) if _http_api_pool_raw else None

# ===== BACKUP =====


def _default_backup_dir() -> str:
    if _CLOUD_DATA.is_dir() and os.access(_CLOUD_DATA, os.W_OK):
        return str(_CLOUD_DATA / "backups")
    return str(Path(__file__).resolve().parent / "backups")


BACKUP_DIR = os.getenv("BACKUP_DIR", _default_backup_dir())

# Валидация вызывается явно из bot.py main() — не при импорте,
# чтобы тесты могли импортировать config без .env.
