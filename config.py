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
        errors.append(f"RATE_LIMIT_WINDOW_SEC={RATE_LIMIT_WINDOW_SEC} — должно быть >= 1")

    # MAX_FIELD_LENGTH: >= 1
    if MAX_FIELD_LENGTH < 1:
        errors.append(f"MAX_FIELD_LENGTH={MAX_FIELD_LENGTH} — должно быть >= 1")

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
# По умолчанию bot.db рядом с bot.py (для локального запуска).
# Облачный деплой переопределяет через DB_PATH=/data/bot.db в .env.
_DEFAULT_DB = str(Path(__file__).resolve().parent / "bot.db")
DB_PATH = os.getenv("DB_PATH", _DEFAULT_DB)

# ===== REPORTS =====
OVERDUE_DAYS = int(os.getenv("OVERDUE_DAYS", "3"))
REPORT_HOUR = int(os.getenv("REPORT_HOUR", "9"))
REPORT_MINUTE = int(os.getenv("REPORT_MINUTE", "0"))
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

# ===== BACKUP =====
BACKUP_DIR = os.getenv("BACKUP_DIR", str(Path(__file__).resolve().parent / "backups"))

# Валидация вызывается явно из bot.py main() — не при импорте,
# чтобы тесты могли импортировать config без .env.
