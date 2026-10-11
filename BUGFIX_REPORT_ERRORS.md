# Исправление ошибок в отчете

## Дата: 2026-10-11

## Ошибки

###1. NameError: name 'OVERDUE_DAYS' is not defined
**Место:** `_build_photo_keyboard` в bot.py, строка296

**Причина:** Переменная `OVERDUE_DAYS` не была импортирована из config.py

**Решение:** Добавлен импорт `OVERDUE_DAYS` в bot.py:
```python
from config import (
    ...
    OVERDUE_DAYS,
)
```

###2. AttributeError: 'NoneType' object has no attribute 'reply_text'
**Место:** `analysis_filter_handler` в bot.py, строка1450

**Причина:** В обработчике ошибок использовался `_safe_reply` вместо `_safe_edit` для callback query

**Решение:** Заменен `_safe_reply` на `_safe_edit`:
```python
except Exception as e:
    logger.error(f"Ошибка формирования отчёта: {e}", exc_info=True)
    await _safe_edit(
        query, "❌ Ошибка при формировании отчёта. Попробуйте позже."
    )
```

---

## Результаты тестирования

**Всего тестов:** 77  
**Пройдено:** 77  
**Провалено:** 0

```
============================= 77 passed in 1.13s ==============================
```

---

## Запуск бота

```bash
cd C:\Users\Admin\Desktop\bot\Telegramm_bot
C:\Users\Admin\AppData\Local\Programs\Python\Python312\python.exe bot.py
```

---

## Как проверить

1. Запустите бота
2. Нажмите "📊 Анализ"
3. Выберите "Актуальные" или "Выполненные"
4. **Ожидаемый результат:** Отчет формируется без ошибок

---

## Контакт
Для вопросов обращайтесь к команде разработки.