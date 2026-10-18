# Деплой на Amvera Cloud

Приложение: **mundo / telega**  
Панель: https://cloud.amvera.ru/projects/applications/msk1/~/mundo/telega

## Как это работает

| Параметр | Значение |
|----------|----------|
| Источник кода | GitHub `MaxAcrata/Telegram_bot` |
| Ветка | `main` |
| Конфиг | `amvera.yaml` |
| Python | 3.10, pip |
| Точка входа | `bot.py` |
| Persistent volume | `/data` |

При старте контейнера Amvera:

1. Устанавливает зависимости из `requirements.txt`
2. Запускает `python bot.py`
3. Монтирует `/data` — туда пишутся `bot.db` и `backups/`

## Переменные окружения (панель Amvera)

Задаются в разделе «Переменные окружения» приложения (не в `.env` в git).

| Переменная | Обязательна | Пример | Назначение |
|------------|-------------|--------|------------|
| `TELEGRAM_BOT_TOKEN` | ✅ | `1234567890:AA...` | Токен от @BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | `-1001234567890` | Чат для отчётов и уведомлений |
| `ADMIN_IDS` | ✅ | `123456789,987654321` | Telegram ID админов через запятую |
| `DB_PATH` | ❌ | `/data/bot.db` | По умолчанию определяется автоматически |
| `BACKUP_DIR` | ❌ | `/data/backups` | По умолчанию `/data/backups` на Amvera |
| `OVERDUE_DAYS` | ❌ | `3` | Дней до просрочки |
| `REPORT_HOUR` | ❌ | `9` | Час ежедневного отчёта |
| `REPORT_MINUTE` | ❌ | `0` | Минута отчёта |
| `REPORT_TIMEZONE` | ❌ | `Europe/Moscow` | IANA-таймзона |
| `RATE_LIMIT_REQUESTS` | ❌ | `5` | Лимит запросов |
| `RATE_LIMIT_WINDOW_SEC` | ❌ | `60` | Окно rate limit (сек) |
| `MAX_PHOTOS_PER_REQUEST` | ❌ | `5` | Максимум фото в заявке |
| `SOCKS5_PROXY` | ❌ | — | Прокси (обычно не нужен на Amvera) |

> Код сам определяет Amvera: если смонтирован `/data`, БД и бэкапы идут туда.
> Задавать `DB_PATH` вручную не обязательно.

## Деплой / редеплой

### Через GitHub (основной путь)

1. Запушить изменения в `main` (или `ver2` — ветки синхронизированы)
2. В панели Amvera: приложение **telega** → **Deploy** / **Пересобрать**
3. Если включён авто-deploy при push — достаточно коммита в `main`

### После деплоя проверить в логах

```
INFO БД инициализирована: /data/bot.db
INFO Бот запущен...
INFO Задание ежедневного отчёта зарегистрировано
```

### Проверить в Telegram

1. Открыть бота → `/start`
2. `➕ Добавить заявку` — создать тестовую заявку
3. `📊 Анализ` / `📊 Дашборд` (админ)
4. Убедиться, что после редеплоя данные на месте (БД в `/data`)

## Локальный запуск (контраст с облаком)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
# создать .env по .env.example
python bot.py
```

Локально БД — `./bot.db`, на Amvera — `/data/bot.db`.

## Troubleshooting

| Симптом | Причина | Решение |
|---------|---------|---------|
| Контейнер падает сразу | Нет `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Задать переменные в панели Amvera |
| «ADMIN_IDS пуст» | Не задан `ADMIN_IDS` | Добавить хотя бы один ID |
| Бот работает, но данные пропали | БД не в `/data` | Обновить код (автоопределение `/data`) и задеплоить |
| Не приходят отчёты | `REPORT_HOUR` / таймзона | Проверить `REPORT_TIMEZONE=Europe/Moscow` |
| Ошибка ZoneInfo | Нет tzdata | В `requirements.txt` есть `tzdata` |

## Ссылки

- Репозиторий: https://github.com/MaxAcrata/Telegram_bot
- Анализ проекта: [ANALYSIS.md](ANALYSIS.md)
