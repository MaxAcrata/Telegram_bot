import os
import tempfile
import pytest

# Устанавливаем обязательные переменные окружения ДО импорта config
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:fake-token-for-testing")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123456")
os.environ.setdefault("ADMIN_IDS", "123")
# Тесты без явного patch DB_PATH не должны трогать ./bot.db в проекте:
# направляем БД и бэкапы во временную директорию (CI-переопределение
# DB_PATH уважается thanks to setdefault).
_DB_TMP = tempfile.mkdtemp(prefix="tgbot_tests_")
os.environ.setdefault("DB_PATH", os.path.join(_DB_TMP, "bot.db"))
os.environ.setdefault("BACKUP_DIR", os.path.join(_DB_TMP, "backups"))

pytest_plugins = ["pytest_asyncio"]
