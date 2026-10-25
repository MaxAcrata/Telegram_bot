import logging
import re
from datetime import datetime, timedelta, date
from html import escape

from config import OVERDUE_DAYS, REPORT_FILE_NAME
from db import (
    get_active_requests,
    get_completed_requests,
    get_photo_counts,
    get_dashboard_counts,
    STATUS_LABELS,
)

logger = logging.getLogger(__name__)

# Регулярное выражение для поиска URLs
# Поддерживает сбалансированные скобки в URL (например Wikipedia: /Material_(steel))
_URL_PATTERN = re.compile(
    r'https?://[^\s<>"]+|'  # http:// или https://
    r'www\.[^\s<>"]+'  # www.
)


def _trim_url_punctuation(url: str) -> str:
    """Убирает конечную пунктуацию, но сохраняет сбалансированные скобки."""
    # Сначала убираем конечные запятые/точки/точки с запятой
    while url and url[-1] in ",.;:!?)":
        # Проверяем, сбалансированы ли скобки после удаления
        stripped = url.rstrip(url[-1])
        if url[-1] == ")" and "(" in stripped:
            # Скобки могут быть частью URL — проверяем баланс
            if stripped.count("(") >= stripped.count(")"):
                break
        url = stripped
    return url

# Эмодзи для статусов
_STATUS_EMOJI = {
    "new": "⚪",
    "ordered": "🟡",
    "delivered": "🔵",
    "done": "🟢",
}


def make_links_clickable(text: str) -> str:
    """Преобразует ссылки в тексте в кликабельные HTML-ссылки для Telegram.

    Остальной текст экранируется для безопасности (защита от XSS).
    """
    if not text:
        return ""

    parts = []
    last_end = 0

    for match in _URL_PATTERN.finditer(text):
        start, end = match.span()
        url = match.group(0)

        # Убираем конечную пунктуацию, сохраняя сбалансированные скобки
        url = _trim_url_punctuation(url)
        end = start + len(url)

        # Экранируем текст до ссылки
        if start > last_end:
            parts.append(escape(text[last_end:start]))

        # Создаём кликабельную ссылку
        href = url
        if url.startswith("www."):
            href = "https://" + url
        parts.append(f'<a href="{escape(href)}">{escape(url)}</a>')

        last_end = end

    # Экранируем оставшийся текст
    if last_end < len(text):
        parts.append(escape(text[last_end:]))

    return "".join(parts)


def format_task_html(task: dict, today: date, photo_count: int = 0) -> str:
    """Форматирует одну задачу в HTML для Telegram"""
    name = escape(task.get("name", "Без названия"))
    qty = task.get("quantity")
    unit = escape(task.get("unit", ""))
    obj = escape(task.get("object", "Не указан"))
    initiator = escape(task.get("initiator", "Не указан"))
    notes_raw = task.get("notes") or ""
    task_status = task.get("status", "new")
    task_id = task.get("id", "?")

    req_date_str = task.get("request_date", "")
    try:
        req_date = date.fromisoformat(req_date_str) if req_date_str else None
    except (ValueError, TypeError):
        req_date = None

    days_elapsed = (today - req_date).days if req_date else 0
    is_overdue = days_elapsed > OVERDUE_DAYS

    # Эмодзи на основе явного статуса
    status_emoji = _STATUS_EMOJI.get(task_status, "⚪")
    status_label = STATUS_LABELS.get(task_status, task_status)

    if is_overdue and task_status not in ("done", "delivered"):
        status_emoji = "🔴"

    qty_text = f"{qty} {unit}".strip() if qty else "—"
    date_text = req_date.strftime("%d.%m.%Y") if req_date else "нет даты"

    lines = [
        f"{status_emoji} <b>#{task_id} {name}</b>",
        f"   └ Статус: {status_label}",
        f"   └ Кол-во: <code>{qty_text}</code>",
        f"   └ Инициатор: {initiator}",
        f"   └ Объект: {obj}",
        f"   └ Заявка: {date_text}",
    ]

    # Добавляем примечание, если оно есть
    if notes_raw and notes_raw != "-":
        notes_html = make_links_clickable(notes_raw)
        lines.append(f"   └ Дополнительно: {notes_html}")

    if photo_count:
        lines.append(f"   └ 📎 Фото: {photo_count} шт.")

    if is_overdue and task_status not in ("done", "delivered"):
        overdue_days = days_elapsed - OVERDUE_DAYS
        lines.append(f"   └ ⚠️ Просрочено на {overdue_days} дн. (возраст: {days_elapsed} дн.)")

    return "\n".join(lines)


def build_report(save_to_file: bool = True, mode: str = "active") -> tuple:
    """Генерирует HTML-отчёт. mode: 'active' или 'completed'.
    Возвращает (text, ids_with_photos, all_request_ids, requests_data)."""
    today = date.today()

    if mode == "completed":
        text, ids, all_ids, requests_data = _build_completed_report(today, save_to_file)
    else:
        text, ids, all_ids, requests_data = _build_active_report(today, save_to_file)

    return text, ids, all_ids, requests_data


def _build_completed_report(today: date, save_to_file: bool) -> tuple:
    """Отчёт по выполненным заявкам. Возвращает (text, ids_with_photos, all_ids, requests_data)."""
    tasks = get_completed_requests()

    report = [
        "<b>✅ ВЫПОЛНЕННЫЕ ЗАЯВКИ</b>",
        f"📅 Дата: {today.strftime('%d.%m.%Y')}",
        f"📊 Всего: <b>{len(tasks)}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    if not tasks:
        report.append("Нет выполненных заявок.")
        result = "\n".join(report)
        if save_to_file:
            _save_report(result)
        return result, [], [], []

    # Получаем количество фото для всех заявок
    task_ids = [t["id"] for t in tasks]
    photo_counts = get_photo_counts(task_ids)
    ids_with_photos = [rid for rid, cnt in photo_counts.items() if cnt > 0]
    all_ids = [t["id"] for t in tasks]

    for task in tasks:
        name = escape(task.get("name", "—"))
        qty = task.get("quantity")
        unit = escape(task.get("unit", ""))
        obj = escape(task.get("object", "—"))
        initiator = escape(task.get("initiator", "—"))
        done_str = task.get("done_date", "")
        try:
            done_date = date.fromisoformat(done_str) if done_str else None
        except (ValueError, TypeError):
            done_date = None
        done_text = done_date.strftime("%d.%m.%Y") if done_date else "—"
        qty_text = f"{qty} {unit}".strip() if qty else "—"
        notes_raw = task.get("notes") or ""
        task_id = task.get("id", "?")

        report.append(f"✅ <b>#{task_id} {name}</b>")
        report.append(f"   └ Кол-во: <code>{qty_text}</code>")
        report.append(f"   └ Инициатор: {initiator}")
        report.append(f"   └ Объект: {obj}")
        report.append(f"   └ Выполнено: {done_text}")

        # Добавляем примечание, если оно есть
        if notes_raw and notes_raw != "-":
            notes_html = make_links_clickable(notes_raw)
            report.append(f"   └ Дополнительно: {notes_html}")

        # Добавляем количество фото
        pc = photo_counts.get(task["id"], 0)
        if pc:
            report.append(f"   └ 📎 Фото: {pc} шт.")

        report.append("")

    result = "\n".join(report)
    if save_to_file:
        _save_report(result)
    return result, ids_with_photos, all_ids, tasks


def _build_active_report(today: date, save_to_file: bool) -> tuple:
    """Отчёт по активным заявкам. Возвращает (text, ids_with_photos, all_ids, requests_data)."""
    active_tasks = get_active_requests()

    report = [
        "<b>📋 СВОДКА ПО АКТИВНЫМ ЗАЯВКАМ</b>",
        f"📅 Дата: {today.strftime('%d.%m.%Y')}",
        f"📊 Всего активных: <b>{len(active_tasks)}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    if not active_tasks:
        report.append("✅ На данный момент активных заявок нет.")
        result = "\n".join(report)
        if save_to_file:
            _save_report(result)
        return result, [], [], []

    # Статистика по явным статусам
    by_status = {}
    for t in active_tasks:
        st = t.get("status", "new")
        by_status[st] = by_status.get(st, 0) + 1

    overdue = []
    for t in active_tasks:
        req_date_str = t.get("request_date")
        if not req_date_str:
            continue
        try:
            req_date = date.fromisoformat(req_date_str)
        except (ValueError, TypeError):
            continue
        if req_date < today - timedelta(days=OVERDUE_DAYS) and t.get("status") not in (
            "done",
            "delivered",
        ):
            overdue.append(t)

    for st_key, st_label in STATUS_LABELS.items():
        if st_key in by_status and st_key != "done":
            report.append(f"  {st_label}: <b>{by_status[st_key]}</b>")

    report.append(f"  Просрочено (&gt;{OVERDUE_DAYS} дн.): <b>{len(overdue)}</b>")
    report.extend(["━━━━━━━━━━━━━━━━━━━━", ""])

    # Получаем количество фото для всех заявок
    task_ids = [t["id"] for t in active_tasks]
    photo_counts = get_photo_counts(task_ids)
    ids_with_photos = [rid for rid, cnt in photo_counts.items() if cnt > 0]
    all_ids = [t["id"] for t in active_tasks]

    result = "\n".join(report)
    if save_to_file:
        _save_report(result)
    return result, ids_with_photos, all_ids, active_tasks


def build_dashboard() -> str:
    """Генерирует HTML-дашборд с ключевыми метриками."""
    today = date.today()
    counts = get_dashboard_counts()

    lines = [
        f"📊 <b>Сводка на {today.strftime('%d.%m.%Y')}</b>",
        "",
        f"⚪ Новых: <b>{counts.get('new', 0)}</b>",
        f"🟡 Заказанных: <b>{counts.get('ordered', 0)}</b>",
        f"🔵 Доставленных: <b>{counts.get('delivered', 0)}</b>",
        f"🔴 Просроченных: <b>{counts.get('overdue', 0)}</b>",
        f"✅ Выполнено сегодня: <b>{counts.get('done_today', 0)}</b>",
        f"📦 Всего активных: <b>{counts.get('total_active', 0)}</b>",
    ]
    return "\n".join(lines)


def _save_report(text: str):
    """Сохраняет отчёт в файл."""
    try:
        with open(REPORT_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        logger.warning(f"Не удалось сохранить отчёт: {e}")
