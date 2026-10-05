"""Тесты для core.py"""

import pytest
from datetime import date
from unittest.mock import patch

from core import format_task_html, build_report, make_links_clickable


def test_make_links_clickable_http():
    """Тест обработки HTTP ссылок"""
    text = "Смотрите тут: https://example.com/page?id=123"
    result = make_links_clickable(text)

    assert '<a href="https://example.com/page?id=123">' in result
    assert "Смотрите тут:" in result


def test_make_links_clickable_www():
    """Тест обработки www ссылок"""
    text = "Сайт: www.example.com"
    result = make_links_clickable(text)

    assert '<a href="https://www.example.com">' in result
    assert "Сайт:" in result


def test_make_links_clickable_multiple():
    """Тест обработки нескольких ссылок"""
    text = "Документы: https://docs.google.com и https://yandex.ru"
    result = make_links_clickable(text)

    assert result.count("<a href") == 2


def test_make_links_clickable_no_links():
    """Тест без ссылок"""
    text = "Просто текст без ссылок"
    result = make_links_clickable(text)

    assert result == "Просто текст без ссылок"
    assert "<a href" not in result


def test_make_links_clickable_empty():
    """Тест с пустым текстом"""
    result = make_links_clickable("")
    assert result == ""


def test_format_task_html():
    """Тест форматирования задачи в HTML"""
    task = {
        "id": 1,
        "name": "Кирпич",
        "quantity": 1000.0,
        "unit": "шт",
        "request_date": "2023-01-01",
        "object": "Стройка А",
        "initiator": "Иван",
        "notes": "срочно",
        "status": "new",
    }

    today = date(2023, 1, 5)
    result = format_task_html(task, today)

    assert "#1" in result
    assert "<b>#1 Кирпич</b>" in result
    assert "1000.0 шт" in result
    assert "Стройка А" in result
    assert "Иван" in result
    assert "01.01.2023" in result
    assert "Примечание:" in result
    assert "срочно" in result
    assert "Новая" in result


def test_format_task_html_overdue():
    """Просроченная задача показывает предупреждение"""
    task = {
        "id": 1,
        "name": "Тест",
        "quantity": 1.0,
        "unit": "шт",
        "request_date": "2023-01-01",
        "object": "О",
        "initiator": "И",
        "notes": "",
        "status": "new",
    }

    today = date(2023, 1, 10)
    result = format_task_html(task, today)

    assert "Просрочено" in result
    assert "🔴" in result


def test_format_task_html_ordered():
    """Заказанная задача показывает жёлтый индикатор"""
    task = {
        "id": 1,
        "name": "Тест",
        "quantity": 1.0,
        "unit": "шт",
        "request_date": "2023-01-01",
        "object": "О",
        "initiator": "И",
        "notes": "",
        "status": "ordered",
    }

    today = date(2023, 1, 2)
    result = format_task_html(task, today)

    assert "🟡" in result
    assert "Заказана" in result


def test_format_task_html_delivered():
    """Доставленная задача показывает синий индикатор"""
    task = {
        "id": 1,
        "name": "Тест",
        "quantity": 1.0,
        "unit": "шт",
        "request_date": "2023-01-01",
        "object": "О",
        "initiator": "И",
        "notes": "",
        "status": "delivered",
    }

    today = date(2023, 1, 2)
    result = format_task_html(task, today)

    assert "🔵" in result
    assert "Доставлена" in result


def test_format_task_html_with_link():
    """Тест отображения ссылки в примечании"""
    task = {
        "id": 1,
        "name": "Материал",
        "quantity": 10.0,
        "unit": "шт",
        "request_date": "2023-01-01",
        "object": "Объект",
        "initiator": "Иван",
        "notes": "Ссылка: https://example.com/catalog",
        "status": "new",
    }

    today = date(2023, 1, 5)
    result = format_task_html(task, today)

    assert "Примечание:" in result
    assert '<a href="https://example.com/catalog">' in result


def test_format_task_html_notes_dash():
    """Тест что прочерк не отображается как примечание"""
    task = {
        "id": 1,
        "name": "Материал",
        "quantity": 10.0,
        "unit": "шт",
        "request_date": "2023-01-01",
        "object": "Объект",
        "initiator": "Иван",
        "notes": "-",
        "status": "new",
    }

    today = date(2023, 1, 5)
    result = format_task_html(task, today)

    assert "Примечание:" not in result


def test_format_task_html_with_photo_count():
    """Тест отображения количества фото"""
    task = {
        "id": 1,
        "name": "Материал",
        "quantity": 10.0,
        "unit": "шт",
        "request_date": "2023-01-01",
        "object": "Объект",
        "initiator": "Иван",
        "notes": "",
        "status": "new",
    }

    today = date(2023, 1, 5)
    result = format_task_html(task, today, photo_count=3)

    assert "📎 Фото: 3 шт." in result


def test_format_task_html_no_photo_count():
    """Тест что при photo_count=0 метка не показывается"""
    task = {
        "id": 1,
        "name": "Материал",
        "quantity": 10.0,
        "unit": "шт",
        "request_date": "2023-01-01",
        "object": "Объект",
        "initiator": "Иван",
        "notes": "",
        "status": "new",
    }

    today = date(2023, 1, 5)
    result = format_task_html(task, today, photo_count=0)

    assert "📎 Фото" not in result


@patch("core.get_photo_counts")
@patch("core.get_active_requests")
def test_build_report_empty(mock_get, mock_photos):
    """Пустой отчёт"""
    mock_get.return_value = []
    mock_photos.return_value = {}

    report, ids, all_ids = build_report(save_to_file=False)

    assert "активных заявок нет" in report
    assert "Всего активных: <b>0</b>" in report
    assert ids == []
    assert all_ids == []


@patch("core.get_photo_counts")
@patch("core.get_active_requests")
def test_build_report_with_tasks(mock_get, mock_photos):
    """Отчёт с задачами"""
    mock_get.return_value = [
        {
            "name": "Кирпич",
            "quantity": 100.0,
            "unit": "шт",
            "request_date": "2023-01-01",
            "object": "Стройка",
            "initiator": "Иван",
            "notes": "",
            "done_date": None,
            "status": "new",
            "id": 1,
        },
        {
            "name": "Цемент",
            "quantity": 50.0,
            "unit": "кг",
            "request_date": "2023-01-01",
            "object": "Стройка",
            "initiator": "Петр",
            "notes": "",
            "done_date": None,
            "status": "ordered",
            "id": 2,
        },
    ]
    mock_photos.return_value = {}

    report, ids, all_ids = build_report(save_to_file=False)

    assert "Всего активных: <b>2</b>" in report
    assert "Новая: <b>1</b>" in report
    assert "Заказана: <b>1</b>" in report
    assert "Кирпич" in report
    assert "Цемент" in report


@patch("core.get_photo_counts")
@patch("core.get_active_requests")
def test_build_report_overdue_section(mock_get, mock_photos):
    """Раздел просроченных заявок по инициаторам"""
    mock_get.return_value = [
        {
            "name": "Старая заявка",
            "quantity": 1.0,
            "unit": "шт",
            "request_date": "2020-01-01",
            "object": "О",
            "initiator": "Иван",
            "notes": "",
            "done_date": None,
            "status": "new",
            "id": 1,
        },
    ]
    mock_photos.return_value = {}

    report, ids, all_ids = build_report(save_to_file=False)

    assert "ПРОСРОЧЕННЫЕ ЗАЯВКИ" in report
    assert "Иван" in report


@patch("core.get_photo_counts")
@patch("core.get_active_requests")
def test_build_report_with_links(mock_get, mock_photos):
    """Отчёт с ссылками в примечаниях"""
    mock_get.return_value = [
        {
            "name": "Материал",
            "quantity": 10.0,
            "unit": "шт",
            "request_date": "2023-01-01",
            "object": "Объект",
            "initiator": "Иван",
            "notes": "Каталог: https://example.com",
            "done_date": None,
            "status": "new",
            "id": 1,
        },
    ]
    mock_photos.return_value = {}

    report, ids, all_ids = build_report(save_to_file=False)

    assert "Примечание:" in report
    assert '<a href="https://example.com">' in report


@patch("core.get_photo_counts")
@patch("core.get_active_requests")
def test_build_report_photo_ids(mock_get, mock_photos):
    """Отчёт возвращает ID заявок с фото"""
    mock_get.return_value = [
        {
            "name": "Материал",
            "quantity": 10.0,
            "unit": "шт",
            "request_date": "2023-01-01",
            "object": "Объект",
            "initiator": "Иван",
            "notes": "",
            "done_date": None,
            "status": "new",
            "id": 1,
        },
        {
            "name": "Цемент",
            "quantity": 50.0,
            "unit": "кг",
            "request_date": "2023-01-01",
            "object": "Объект",
            "initiator": "Петр",
            "notes": "",
            "done_date": None,
            "status": "new",
            "id": 2,
        },
    ]
    mock_photos.return_value = {1: 3}

    report, ids, all_ids = build_report(save_to_file=False)

    assert 1 in ids
    assert 2 not in ids
    assert "📎 Фото: 3 шт." in report
