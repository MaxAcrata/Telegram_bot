import os
import pytest

# Устанавливаем обязательные переменные окружения ДО импорта config
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:fake-token-for-testing")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123456")
os.environ.setdefault("ADMIN_IDS", "123")

pytest_plugins = ["pytest_asyncio"]
