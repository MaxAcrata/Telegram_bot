#!/bin/bash
# Создание GitHub Issues из AUDIT.md
# Запуск: bash create_issues.sh
# Требуется: gh auth login

set -e

REPO="MaxAcrata/Telegramm_bot"

echo "Создание GitHub Issues для $REPO..."
echo ""

# === КРИТИЧЕСКИЕ (уже исправлены в коммите 14158cc, eeabb9a) ===

gh issue create -R "$REPO" -t "C1: Отчёт >4096 символов падает в analysis_filter_handler" \
  -b "**Файл:** \`bot.py:601-609\`
**Проблема:** \`analysis_filter_handler\` отправляет отчёт через \`reply_text\`, а не \`send_long_message\`. При >4096 символов — \`BadRequest\`.
**Статус:** ✅ Исправлено в \`14158cc\`" \
  -l "bug,fix-done"

gh issue create -R "$REPO" -t "C2: split_message ломает HTML-ссылки (потеря href)" \
  -b "**Файл:** \`utils.py:100\`
**Проблема:** При разбивке длинного отчёта \`<a href=\"...\">\" восстанавливается как \`<a>\` без \`href\`.
**Статус:** ✅ Исправлено в \`14158cc\`" \
  -l "bug,fix-done"

gh issue create -R "$REPO" -t "C3: get_photo_counts падает при >999 заявках" \
  -b "**Файл:** \`db.py:301\`
**Проблема:** SQLite лимит 999 placeholders. Молчаливый сбой — все фото = 0.
**Статус:** ✅ Исправлено в \`14158cc\`" \
  -l "bug,fix-done"

gh issue create -R "$REPO" -t "C4: float('5,5') выбрасывает ValueError" \
  -b "**Файл:** \`bot.py:524\`
**Проблема:** \`validate_quantity\ допускает запятую, но \`float()\` не парсит запятую.
**Статус:** ✅ Исправлено в \`14158cc\`" \
  -l "bug,fix-done"

gh issue create -R "$REPO" -t "C5: callback_data превышает 64 байта для кириллицы" \
  -b "**Файл:** \`bot.py:829,1025\`
**Проблема:** Обрезка по символам (50), а не по байтам. Кириллица = 2+ байта/символ.
**Статус:** ✅ Исправлено в \`14158cc\`" \
  -l "bug,fix-done"

# === ВЫСОКИЕ ===

gh issue create -R "$REPO" -t "H1: Два админа могут завершить одну заявку дважды" \
  -b "**Файл:** \`bot.py:644-745\`, \`db.py:239-254\`
**Проблема:** \`complete_request\` не проверяет \`done_date IS NULL\` перед UPDATE. Двойные уведомления.
**Исправление:** Добавить \`WHERE done_date IS NULL\` и проверку \`rowcount\`." \
  -l "bug,high"

gh issue create -R "$REPO" -t "H2: Потеря черновика при ошибке сохранения заявки" \
  -b "**Файл:** \`bot.py:522-559\`
**Проблема:** \`user_data.clear()\` выполняется даже при ошибке. Пользователь не может повторить.
**Статус:** ✅ Исправлено в \`14158cc\` — транзакционное сохранение, черновик сохраняется при ошибке." \
  -l "bug,fix-done"

gh issue create -R "$REPO" -t "H3: Молчаливое подавление Exception в db.py (5 мест)" \
  -b "**Файл:** \`db.py\` — \`get_request_photos\`, \`get_photo_counts\`, \`get_initiator_tg_id\`, \`get_initiator_by_tg_id\`, \`get_setting\`
**Проблема:** \`except Exception: return {}\` скрывает реальные проблемы БД.
**Статус:** ✅ Исправлено в \`14158cc\` — добавлен \`logger.warning\`." \
  -l "bug,fix-done"

gh issue create -R "$REPO" -t "H4: Необработанный ValueError при парсинге дат в core.py" \
  -b "**Файл:** \`core.py:63, 146, 242\`
**Проблема:** \`date.fromisoformat()\` без try/except. Некорректная дата в БД уронит отчёт.
**Исправление:** Обернуть в try/except, при ошибке использовать None." \
  -l "bug,high"

gh issue create -R "$REPO" -t "H5: Привязка инициатора перезаписывается без удаления старой" \
  -b "**Файл:** \`db.py:317-326\`
**Проблема:** Один tg_id может быть привязан к нескольким именам.
**Исправление:** Удалить старую привязку перед INSERT." \
  -l "bug,high"

gh issue create -R "$REPO" -t "H6: Произвольный текст в PHOTO-состоянии молча игнорируется" \
  -b "**Файл:** \`bot.py:1104-1109\`
**Проблема:** Если пользователь отправит не фото и не кнопку — нет ответа.
**Статус:** ✅ Исправлено в \`14158cc\` — добавлен catch-all handler." \
  -l "bug,fix-done"

# === СРЕДНИЕ ===

gh issue create -R "$REPO" -t "M1: Regex 'Настройки' без якорей" \
  -b "**Файл:** \`bot.py:1120\`
**Проблема:** Совпадёт с любым сообщением содержащим 'Настройки'.
**Исправление:** \`filters.Regex('^Настройки\$')\`" \
  -l "tech-debt,medium"

gh issue create -R "$REPO" -t "M2: Несогласованность номеров шагов 5/7 vs 5/6" \
  -b "**Файл:** \`bot.py:317, 365, 385\`
**Проблема:** При автоподстановке инициатора показывается 'Шаг 5/7', при ручном — '5/6'.
**Исправление:** Заменить все /6 на /7." \
  -l "bug,medium"

gh issue create -R "$REPO" -t "M3: Scheduled report без кнопок просмотра фото" \
  -b "**Файл:** \`bot.py:229-240\`
**Проблема:** \`_ids\` получается, но игнорируется. В автоматическом отчёте нет кнопок фото.
**Исправление:** Добавить \`_build_photo_keyboard(_ids)\`." \
  -l "feature,medium"

gh issue create -R "$REPO" -t "M4: Пустая клавиатура если все значения справочника удалены" \
  -b "**Файл:** \`bot.py:281-284\`
**Проблема:** \`get_all_lists()\` возвращает \`{\"unit\": []}\`, fallback не срабатывает.
**Исправление:** Проверять \`if not units\`." \
  -l "bug,medium"

gh issue create -R "$REPO" -t "M5: Двойной query.answer() при удалении" \
  -b "**Файл:** \`bot.py:934, 941\`
**Проблема:** Второй \`query.answer()\` игнорируется, alert не показывается.
**Исправление:** Убрать первый \`query.answer()\`." \
  -l "bug,low"

gh issue create -R "$REPO" -t "M6: Race condition в complete_request" \
  -b "**Файл:** \`db.py:239-254\`
**Проблема:** SELECT и UPDATE не атомарны.
**Исправление:** \`UPDATE ... WHERE done_date IS NULL RETURNING *\`." \
  -l "bug,medium"

gh issue create -R "$REPO" -t "M7: URL regex обрезает скобки в ссылках" \
  -b "**Файл:** \`core.py:12-15\`
**Проблема:** \`wiki/Мат_(prog)\` обрежется на первой \`)\`.
**Исправление:** Обработка сбалансированных скобок." \
  -l "bug,medium"

gh issue create -R "$REPO" -t "M8: Дублирование форматирования в completed-отчёте" \
  -b "**Файл:** \`core.py:139-167\`
**Проблема:** \`_build_completed_report\` дублирует логику вместо \`format_task_html()\`.
**Исправление:** Переиспользовать \`format_task_html()\`." \
  -l "tech-debt,medium"

gh issue create -R "$REPO" -t "M9: int(os.getenv()) без обработки ошибок в config.py" \
  -b "**Файл:** \`config.py:28-30\`
**Проблема:** Мусор в env → ValueError → бот не запустится.
**Исправление:** try/except с fallback." \
  -l "bug,medium"

gh issue create -R "$REPO" -t "M10: split_message режет по символу при отсутствии \\n" \
  -b "**Файл:** \`utils.py:62-64\`
**Проблема:** Разрез по max_length без учёта границ слов/тегов.
**Исправление:** Искать последний пробел." \
  -l "bug,medium"

gh issue create -R "$REPO" -t "M11: Удаление инициатора не чистит initiator_users" \
  -b "**Файл:** \`bot.py:938-941\`, \`db.py:200-207\`
**Проблема:** Удалённый инициатор остаётся привязанным к пользователю.
**Исправление:** Удалять запись из initiator_users." \
  -l "bug,medium"

gh issue create -R "$REPO" -t "M12: Дробные числа без форматирования в отчёте" \
  -b "**Файл:** \`core.py:78\`
**Проблема:** \`2.333333333 кг\` вместо \`2.33 кг\`.
**Исправление:** \`f\"{qty:g} {unit}\` или \`round(qty, 2)\`." \
  -l "tech-debt,low"

# === НИЗКИЕ ===

gh issue create -R "$REPO" -t "L1: PRAGMA WAL на каждое соединение" \
  -b "**Файл:** \`db.py:77\`
**Проблема:** Достаточно установить один раз в \`init_db()\`.
**Исправление:** Вынести PRAGMA в \`init_db()\`." \
  -l "tech-debt,low"

gh issue create -R "$REPO" -t "L2: Новое соединение SQLite на каждую операцию" \
  -b "**Файл:** \`db.py:86\`
**Проблема:** Overhead при частых вызовах.
**Исправление:** threading.local connection pool." \
  -l "tech-debt,low"

gh issue create -R "$REPO" -t "L3: Импорт groupby внутри функции" \
  -b "**Файл:** \`core.py:210\`
**Проблема:** Anti-pattern, затрудняет чтение зависимостей." \
  -l "tech-debt,low"

gh issue create -R "$REPO" -t "L4: Off-by-one в определении просроченности" \
  -b "**Файл:** \`core.py:68\`
**Проблема:** \`> OVERDUE_DAYS\` vs \`>= OVERDUE_DAYS\` — зависит от бизнес-требований." \
  -l "tech-debt,low"

gh issue create -R "$REPO" -t "L5: Дублирование расчёта дней просрочки" \
  -b "**Файл:** \`core.py:226-249\`
**Проблема:** Две секции отчёта используют разную логику расчёта." \
  -l "tech-debt,low"

gh issue create -R "$REPO" -t "L6: Конкурентная запись в report.txt" \
  -b "**Файл:** \`core.py:258-264\`
**Проблема:** Два отчёта могут писать в один файл одновременно.
**Исправление:** Запись во временный файл + os.replace()." \
  -l "tech-debt,low"

gh issue create -R "$REPO" -t "L7: Мёртвый код — дублирующий CommandHandler('start')" \
  -b "**Файл:** \`bot.py:1176\`
**Проблема:** register_conv перехватывает /start раньше, второй handler никогда не вызывается." \
  -l "tech-debt,low"

gh issue create -R "$REPO" -t "L8: Пустой ввод при 'Другое' для объекта/инициатора" \
  -b "**Файл:** \`bot.py:415-424\`
**Проблема:** Пользователь может отправить пустую строку.
**Исправление:** Проверка \`if not text.strip()\`." \
  -l "bug,low"

gh issue create -R "$REPO" -t "L9: MAX_MESSAGE_LENGTH объявлен, но не используется" \
  -b "**Файл:** \`config.py:37\`
**Проблема:** Константа не используется нигде." \
  -l "tech-debt,low"

gh issue create -R "$REPO" -t "L10: Нет проверки /data перед подключением к БД" \
  -b "**Файл:** \`amvera.yaml\`, \`db.py\`
**Проблема:** Если persistent volume не примонтирован — бот упадёт.
**Исправление:** \`os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)\`." \
  -l "bug,low"

echo ""
echo "✅ Все Issues созданы!"