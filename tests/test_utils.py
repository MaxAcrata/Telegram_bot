import pytest
from datetime import datetime
from utils import parse_date, format_date, validate_quantity, split_message


def test_parse_date_formats():
    """Тест различных форматов дат"""
    assert parse_date("31.12.2023") == datetime(2023, 12, 31).date()
    assert parse_date("12/31/2023") == datetime(2023, 12, 31).date()
    assert parse_date("2023-12-31") == datetime(2023, 12, 31).date()


def test_format_date():
    """Тест форматирования даты"""
    date = datetime(2023, 5, 15).date()
    assert format_date(date) == "15.05.2023"
    assert format_date(None) == "нет даты"
    assert format_date("") == "нет даты"


def test_validate_quantity_valid():
    """Тест валидации корректных количеств"""
    assert validate_quantity("100") is True
    assert validate_quantity("10.5") is True
    assert validate_quantity("0.001") is True


def test_validate_quantity_invalid():
    """Тест валидации некорректных количеств"""
    assert validate_quantity("abc") is False
    assert validate_quantity("") is False
    assert validate_quantity("10a") is False
    assert validate_quantity("-10") is False
    assert validate_quantity("$") is False


def test_split_message():
    """Тест разбиения длинных сообщений"""
    text = "a" * 10000
    parts = split_message(text, max_length=4000)

    assert len(parts) == 3
    assert len(parts[0]) == 4000
    assert len(parts[2]) == 2000
