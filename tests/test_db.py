"""Тесты для db.py"""

import pytest
import tempfile
import os
from datetime import date
from unittest.mock import patch

import db
from db import (
    get_conn,
    init_db,
    add_request,
    get_active_requests,
    get_all_lists,
    add_list_value,
    add_request_photo,
    get_request_photos,
    get_photo_counts,
    complete_request,
    update_request_status,
    edit_request,
    get_request_by_id,
    audit_log,
    get_audit_log,
    backup_db,
    save_conversation_state,
    load_conversation_state,
    clear_conversation_state,
    VALID_STATUSES,
)


@pytest.fixture(autouse=True)
def temp_db(tmp_path):
    """Создаёт временную БД для каждого теста."""
    db_path = str(tmp_path / "test.db")
    backup_dir = str(tmp_path / "backups")
    with patch.object(db, "DB_PATH", db_path), patch.object(
        db, "BACKUP_DIR", backup_dir
    ):
        init_db()
        yield db_path


def test_init_db_creates_tables():
    """Таблицы создаются при инициализации"""
    with get_conn() as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        assert "requests" in tables
        assert "lists" in tables
        assert "audit_log" in tables
        assert "conversation_state" in tables


def test_init_db_populates_defaults():
    """Справочники заполняются значениями по умолчанию"""
    lists = get_all_lists()
    assert "initiator" in lists
    assert "object" in lists
    assert "unit" in lists
    assert "шт" in lists["unit"]
    assert "Другое" in lists["initiator"]


def test_init_db_does_not_overwrite_edited_defaults():
    """init_db не перезаписывает отредактированные справочники"""
    # Удаляем значение
    db.delete_list_value("initiator", "Алексей")
    lists = get_all_lists()
    assert "Алексей" not in lists.get("initiator", [])

    # Повторная инициализация — не должна вернуть удалённое
    init_db()
    lists = get_all_lists()
    assert "Алексей" not in lists.get("initiator", [])


def test_add_request():
    """Заявка добавляется и возвращается"""
    row_id = add_request(
        name="Кирпич",
        quantity=100.0,
        unit="шт",
        request_date=date(2025, 1, 15),
        initiator="Иван",
        object_name="Стройка",
        notes="срочно",
    )
    assert row_id > 0

    active = get_active_requests()
    assert len(active) == 1
    assert active[0]["name"] == "Кирпич"
    assert active[0]["quantity"] == 100.0
    assert active[0]["initiator"] == "Иван"
    assert active[0]["status"] == "new"


def test_get_active_requests_filters_done():
    """Завершённые заявки не возвращаются"""
    add_request("A", 1, "шт", date(2025, 1, 1), "И", "О")
    add_request("B", 2, "кг", date(2025, 1, 2), "И", "О")

    with get_conn() as conn:
        conn.execute(
            "UPDATE requests SET done_date = ? WHERE name = ?",
            (date(2025, 1, 5).isoformat(), "A"),
        )

    active = get_active_requests()
    assert len(active) == 1
    assert active[0]["name"] == "B"


def test_get_active_requests_sorted_by_object():
    """Заявки сортируются по объекту"""
    add_request("A", 1, "шт", date(2025, 1, 1), "И", "Б-объект")
    add_request("B", 2, "кг", date(2025, 1, 2), "И", "А-объект")

    active = get_active_requests()
    assert active[0]["object"] == "А-объект"
    assert active[1]["object"] == "Б-объект"


def test_get_all_lists():
    """Справочники возвращаются корректно"""
    lists = get_all_lists()
    assert isinstance(lists, dict)
    assert "Алексей" in lists["initiator"]
    assert "шт" in lists["unit"]


def test_add_list_value():
    """Новое значение добавляется в справочник"""
    result = add_list_value("object", "Новый объект")
    assert result is True

    lists = get_all_lists()
    assert "Новый объект" in lists["object"]


def test_add_list_value_duplicate():
    """Дубликат не добавляется"""
    add_list_value("object", "Тест")
    result = add_list_value("object", "Тест")
    assert result is False


def test_init_db_creates_photos_table():
    """Таблица request_photos создаётся при инициализации"""
    with get_conn() as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        assert "request_photos" in tables


def test_add_and_get_request_photos():
    """Фото добавляются и возвращаются для заявки"""
    req_id = add_request("Тест", 1, "шт", date(2025, 1, 1), "И", "О")

    add_request_photo(req_id, "file_id_1")
    add_request_photo(req_id, "file_id_2")
    add_request_photo(req_id, "file_id_3")

    photos = get_request_photos(req_id)
    assert len(photos) == 3
    assert photos[0] == "file_id_1"
    assert photos[2] == "file_id_3"


def test_get_request_photos_empty():
    """Для заявки без фото возвращается пустой список"""
    req_id = add_request("Тест", 1, "шт", date(2025, 1, 1), "И", "О")
    photos = get_request_photos(req_id)
    assert photos == []


def test_get_photo_counts():
    """Подсчёт фото для нескольких заявок"""
    req1 = add_request("A", 1, "шт", date(2025, 1, 1), "И", "О")
    req2 = add_request("B", 2, "кг", date(2025, 1, 2), "И", "О")
    req3 = add_request("C", 3, "л", date(2025, 1, 3), "И", "О")

    add_request_photo(req1, "photo_a1")
    add_request_photo(req1, "photo_a2")
    add_request_photo(req3, "photo_c1")

    counts = get_photo_counts([req1, req2, req3])
    assert counts[req1] == 2
    assert req2 not in counts or counts.get(req2, 0) == 0
    assert counts[req3] == 1


def test_get_photo_counts_empty_list():
    """Пустой список ID возвращает пустой словарь"""
    result = get_photo_counts([])
    assert result == {}


def test_complete_request():
    """Заявка завершается"""
    req_id = add_request("Тест", 1, "шт", date(2025, 1, 1), "И", "О")
    result = complete_request(req_id, date(2025, 1, 10))
    assert result is not None
    assert result["done_date"] == "2025-01-10"
    assert result["status"] == "done"


def test_complete_request_idempotent():
    """Повторное завершение не перезаписывает done_date"""
    req_id = add_request("Тест", 1, "шт", date(2025, 1, 1), "И", "О")
    complete_request(req_id, date(2025, 1, 10))
    result = complete_request(req_id, date(2025, 2, 1))
    assert result["done_date"] == "2025-01-10"  # Оригинальная дата


def test_update_request_status():
    """Статус заявки обновляется"""
    req_id = add_request("Тест", 1, "шт", date(2025, 1, 1), "И", "О")
    assert update_request_status(req_id, "ordered") is True
    req = get_request_by_id(req_id)
    assert req["status"] == "ordered"


def test_update_request_status_invalid():
    """Недопустимый статус отклоняется"""
    req_id = add_request("Тест", 1, "шт", date(2025, 1, 1), "И", "О")
    assert update_request_status(req_id, "invalid") is False


def test_edit_request():
    """Заявка редактируется"""
    req_id = add_request("Тест", 1, "шт", date(2025, 1, 1), "И", "О")
    assert edit_request(req_id, name="Новое имя", quantity=5.0) is True
    req = get_request_by_id(req_id)
    assert req["name"] == "Новое имя"
    assert req["quantity"] == 5.0


def test_audit_log():
    """Записи аудита создаются и читаются"""
    audit_log(1, 100, "Тест", "created", "Тестовая заявка")
    audit_log(2, 100, "Тест", "completed", "")
    entries = get_audit_log()
    assert len(entries) >= 2
    assert entries[0]["action"] == "completed"  # DESC order


def test_audit_log_by_request():
    """Фильтр аудита по ID заявки"""
    audit_log(10, 100, "Тест", "created", "")
    audit_log(20, 100, "Тест", "completed", "")
    entries = get_audit_log(request_id=10)
    assert all(e["request_id"] == 10 for e in entries)


def test_backup_db():
    """Резервная копия создаётся"""
    path = backup_db()
    assert path is not None
    assert os.path.exists(path)


def test_conversation_state():
    """Сохранение и загрузка состояния диалога"""
    save_conversation_state(123, '{"step": "NAME", "name": "Кирпич"}')
    state = load_conversation_state(123)
    assert "Кирпич" in state

    clear_conversation_state(123)
    assert load_conversation_state(123) is None
