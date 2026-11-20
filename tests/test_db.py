"""Тесты для db.py"""

import pytest
import sqlite3
import tempfile
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from unittest.mock import patch

import db
from db import (
    get_conn,
    init_db,
    add_request,
    add_request_with_photos,
    get_active_requests,
    get_all_lists,
    add_list_value,
    add_request_photo,
    get_request_photos,
    get_photo_counts,
    complete_request,
    complete_requests_batch,
    cancel_request,
    update_request_status,
    edit_request,
    get_request_by_id,
    get_requests_by_status,
    get_completed_requests,
    get_dashboard_counts,
    get_user_requests,
    audit_log,
    get_audit_log,
    backup_db,
    save_conversation_state,
    load_conversation_state,
    clear_conversation_state,
    VALID_STATUSES,
    STATUS_LABELS,
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


def test_complete_request_writes_audit():
    """18: завершение записывается в audit_log (один раз, идемпотентно)"""
    req_id = add_request("Тест", 1, "шт", date(2025, 1, 1), "И", "О")
    complete_request(req_id, date(2025, 1, 10), user_id=7, user_name="Админ")
    complete_request(req_id, date(2025, 2, 1), user_id=7, user_name="Админ")

    entries = [
        e for e in get_audit_log(request_id=req_id) if e["action"] == "completed"
    ]
    assert len(entries) == 1  # повторное завершение не дублирует audit
    assert entries[0]["user_id"] == 7
    assert entries[0]["details"] == "Дата: 2025-01-10"


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


# ===== B4: статус cancelled =====


def _mk_request(name="Цемент", request_date=date(2026, 1, 10), **kw):
    kwargs = dict(
        name=name,
        quantity=1.0,
        unit="шт",
        request_date=request_date,
        initiator="Иван",
        object_name="Склад",
        notes="",
    )
    kwargs.update(kw)
    return add_request(**kwargs)


def test_cancel_request_status():
    """19: активная заявка получает status=cancelled"""
    rid = _mk_request()
    row, changed = cancel_request(rid, user_id=1, user_name="Тест")

    assert changed is True
    assert row["status"] == "cancelled"
    assert STATUS_LABELS["cancelled"] == "Отменена"
    assert "cancelled" in VALID_STATUSES


def test_cancel_keeps_row_and_photos():
    """20: строка и фотографии остаются в БД"""
    rid = _mk_request()
    add_request_photo(rid, "file-abc")
    cancel_request(rid, user_id=1, user_name="Тест")

    assert get_request_by_id(rid) is not None
    assert get_request_photos(rid) == ["file-abc"]


def test_cancel_keeps_done_date_null():
    """21: done_date остаётся NULL (отмена не выставляет дату выполнения)"""
    rid = _mk_request()
    cancel_request(rid, user_id=1, user_name="Тест")

    row = get_request_by_id(rid)
    assert row["done_date"] is None


def test_cancel_excluded_from_active():
    """22: отменённая заявка исключается из get_active_requests и счётчиков"""
    rid = _mk_request(request_date=date(2020, 1, 1))  # ещё и просроченная
    assert len(get_active_requests()) == 1
    assert get_dashboard_counts()["overdue"] == 1

    cancel_request(rid, user_id=1, user_name="Тест")

    assert get_active_requests() == []
    counts = get_dashboard_counts()
    assert counts["total_active"] == 0
    assert counts["overdue"] == 0


def test_cancel_not_in_completion_and_batch():
    """23: не появляется в завершении и не участвует в batch complete"""
    rid_cancelled = _mk_request(name="Отменить")
    rid_active = _mk_request(name="Оставить")
    cancel_request(rid_cancelled, user_id=1, user_name="Тест")

    # Списки завершения не содержат отменённую
    assert all(r["id"] != rid_cancelled for r in get_active_requests())
    assert all(r["id"] != rid_cancelled for r in get_completed_requests())

    # Завершение отменённой невозможно
    result = complete_request(rid_cancelled, date.today())
    assert result is None
    assert get_request_by_id(rid_cancelled)["done_date"] is None
    assert get_request_by_id(rid_cancelled)["status"] == "cancelled"

    # Batch не трогает отменённую, трогает активную
    completed = complete_requests_batch(
        [rid_cancelled, rid_active], date.today(), user_id=1, user_name="Тест"
    )
    assert completed == 1
    assert get_request_by_id(rid_cancelled)["status"] == "cancelled"
    assert get_request_by_id(rid_active)["status"] == "done"


def test_cancelled_filter_and_all_list():
    """24: отдельный фильтр отменённых + видна в «Все»"""
    rid1 = _mk_request(name="Отменённая")
    rid2 = _mk_request(name="Живая")
    cancel_request(rid1, user_id=1, user_name="Тест")

    cancelled = get_requests_by_status(status="cancelled")
    assert [r["id"] for r in cancelled] == [rid1]

    all_list = get_requests_by_status(status=None)
    ids = [r["id"] for r in all_list]
    assert rid1 in ids and rid2 in ids

    # История пользователя показывает отменённую, активные — нет
    active = get_user_requests("Иван", status_filter="active")
    assert all(r["id"] != rid1 for r in active)
    history = get_user_requests("Иван")
    assert any(r["id"] == rid1 for r in history)


def test_cancel_writes_audit():
    """25: действие записывается в audit_log с комментарием и пользователем"""
    rid = _mk_request()
    cancel_request(rid, user_id=42, user_name="Админ", comment="Не актуально")

    entries = [e for e in get_audit_log(request_id=rid) if e["action"] == "cancelled"]
    assert len(entries) == 1
    assert entries[0]["user_id"] == 42
    assert entries[0]["user_name"] == "Админ"
    assert "Не актуально" in entries[0]["details"]


def test_cancel_idempotent_repeat():
    """26: повторная отмена идемпотентна — без второго audit"""
    rid = _mk_request()
    _, changed_first = cancel_request(rid, user_id=1, user_name="Тест")
    row, changed_second = cancel_request(rid, user_id=1, user_name="Тест")

    assert changed_first is True
    assert changed_second is False
    assert row["status"] == "cancelled"
    entries = [e for e in get_audit_log(request_id=rid) if e["action"] == "cancelled"]
    assert len(entries) == 1


def test_cancel_done_and_archived_rejected():
    """done/archived отменить нельзя"""
    rid_done = _mk_request(name="Выполненная")
    complete_request(rid_done, date.today())
    row, changed = cancel_request(rid_done, user_id=1, user_name="Тест")
    assert changed is False
    assert row["status"] == "done"
    assert not any(
        e["action"] == "cancelled" for e in get_audit_log(request_id=rid_done)
    )

    rid_arch = _mk_request(name="Архивная")
    update_request_status(rid_arch, "archived")
    row, changed = cancel_request(rid_arch, user_id=1, user_name="Тест")
    assert changed is False
    assert row["status"] == "archived"


def test_update_request_status_blocks_cancelled():
    """cancelled недоступен через generic-смену статуса; из cancelled — тоже"""
    rid = _mk_request()
    assert update_request_status(rid, "cancelled") is False
    assert get_request_by_id(rid)["status"] == "new"

    cancel_request(rid, user_id=1, user_name="Тест")
    # Любой переход из cancelled через generic-UI запрещён
    assert update_request_status(rid, "new") is False
    assert get_request_by_id(rid)["status"] == "cancelled"


# ===== B5: миграция существующей БД =====


def _remove_db_files(path):
    for suffix in ("", "-wal", "-shm"):
        candidate = path + suffix
        if os.path.exists(candidate):
            os.remove(candidate)


def _create_old_schema_db(path):
    """БД в старой схеме: requests БЕЗ колонки status (до первой миграции)."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            quantity REAL,
            unit TEXT,
            request_date DATE NOT NULL,
            done_date DATE,
            initiator TEXT DEFAULT '',
            object TEXT DEFAULT '',
            notes TEXT DEFAULT ''
        );
        CREATE TABLE lists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            list_type TEXT NOT NULL,
            value TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            UNIQUE(list_type, value)
        );
        CREATE TABLE request_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO requests (name, request_date, initiator, object) VALUES
            ('Старая 1', '2024-01-01', 'Иван', 'Склад'),
            ('Старая 2', '2024-02-01', 'Пётр', 'Дом');
        """)
    conn.commit()
    conn.close()


def test_migration_old_schema_preserves_rows_ids(temp_db):
    """27-28: старая схема -> init_db: строки и ID сохранены, status добавлен"""
    _remove_db_files(temp_db)
    _create_old_schema_db(temp_db)

    init_db()  # версия новой — идемпотентная миграция

    with get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 2
        rows = conn.execute(
            "SELECT id, name, status FROM requests ORDER BY id"
        ).fetchall()
    assert [r["id"] for r in rows] == [1, 2]
    assert rows[0]["name"] == "Старая 1"
    assert all(r["status"] == "new" for r in rows)

    # Фото и старые данные доступны после миграции
    add_request_photo(1, "file-old")
    assert get_request_photos(1) == ["file-old"]


def test_migration_creates_backup(temp_db):
    """Перед миграцией существующей БД создаётся резервная копия"""
    _remove_db_files(temp_db)
    _create_old_schema_db(temp_db)

    init_db()

    backup_dir = os.path.join(os.path.dirname(temp_db), "backups")
    assert os.path.isdir(backup_dir)
    backups = [f for f in os.listdir(backup_dir) if f.endswith(".db")]
    assert len(backups) >= 1


def test_migration_stops_when_backup_fails(temp_db):
    """Неудачный backup останавливает миграцию существующей БД"""
    _remove_db_files(temp_db)
    _create_old_schema_db(temp_db)

    with patch.object(db, "backup_db", return_value=None):
        with pytest.raises(RuntimeError, match="миграция остановлена"):
            init_db()

    # Схема не тронута: колонки status нет
    conn = sqlite3.connect(temp_db)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(requests)")]
    finally:
        conn.close()
    assert "status" not in cols


def test_init_db_rerun_safe(temp_db):
    """29-31: повторный init_db после миграции безопасен"""
    _remove_db_files(temp_db)
    _create_old_schema_db(temp_db)
    init_db()

    init_db()  # повторный запуск не падает
    init_db()

    with get_conn() as conn:
        rows = conn.execute("SELECT id FROM requests ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == [1, 2]


# ===== A7: WAL, busy_timeout, конкурентный доступ =====


def test_wal_set_on_init_db(temp_db):
    """journal_mode=WAL выставляется в init_db (постоянно для файла)"""
    with get_conn() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"


def test_concurrent_writes_and_reads(temp_db):
    """Конкурентные записи/чтения из потоков не падают (busy_timeout)"""

    def worker(i):
        return add_request(
            name=f"Заявка {i}",
            quantity=float(i),
            unit="шт",
            request_date=date(2026, 3, 1),
            initiator="Иван",
            object_name="Склад",
            notes="",
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(worker, range(20)))

    assert len(set(ids)) == 20
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    assert count == 20
    # Параллельное чтение после записей видит все 20
    assert len(get_active_requests()) == 20
