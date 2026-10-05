import logging
import shutil
import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Optional, List, Dict
from contextlib import contextmanager

from config import DB_PATH, BACKUP_DIR

logger = logging.getLogger(__name__)

# ===== Схема БД =====
_SCHEMA = """
CREATE TABLE IF NOT EXISTS lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    list_type TEXT NOT NULL,
    value TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0,
    UNIQUE(list_type, value)
);

CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    quantity REAL,
    unit TEXT,
    request_date DATE NOT NULL,
    done_date DATE,
    initiator TEXT DEFAULT '',
    status TEXT DEFAULT 'new',
    object TEXT DEFAULT '',
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS initiator_users (
    initiator_name TEXT PRIMARY KEY,
    telegram_id INTEGER NOT NULL,
    object_name TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS request_photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES requests(id),
    file_id TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
    request_id INTEGER,
    user_id INTEGER,
    user_name TEXT DEFAULT '',
    action TEXT NOT NULL,
    details TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS conversation_state (
    user_id INTEGER PRIMARY KEY,
    state_data TEXT NOT NULL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_requests_active ON requests(done_date, request_date);
CREATE INDEX IF NOT EXISTS idx_requests_object ON requests(object);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
CREATE INDEX IF NOT EXISTS idx_request_photos_req ON request_photos(request_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_req ON audit_log(request_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(timestamp);
"""

# Значения настроек по умолчанию (НЕ перезаписывают существующие)
_DEFAULT_SETTINGS = {
    "buttons_per_row_object": "2",
    "buttons_per_row_initiator": "4",
    "buttons_per_row_unit": "3",
}

# Значения по умолчанию для справочников (только для пустой БД)
_DEFAULT_LISTS = {
    "initiator": ["Алексей", "Анатолий", "Михаил", "Игорь"],
    "object": ["Солнечное", "Привилегия"],
    "unit": ["шт", "кг", "л", "м2", "м3", "лист", "м.п.", "комп"],
}

# Допустимые статусы заявок
VALID_STATUSES = ("new", "ordered", "delivered", "done", "archived")
STATUS_LABELS = {
    "new": "Новая",
    "ordered": "Заказана",
    "delivered": "Доставлена",
    "done": "Выполнена",
    "archived": "Архив",
}


@contextmanager
def get_conn():
    """Контекстный менеджер для соединения с БД."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _table_exists(conn, table_name: str) -> bool:
    """Проверяет существование таблицы."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _column_exists(conn, table_name: str, column_name: str) -> bool:
    """Проверяет существование колонки в таблице."""
    try:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return any(row["name"] == column_name for row in rows)
    except Exception:
        return False


def init_db():
    """Инициализирует БД: создаёт таблицы, мигрирует схему, заполняет пустые справочники."""
    with get_conn() as conn:
        conn.executescript(_SCHEMA)

        # Миграция: добавляем status если нет
        if not _column_exists(conn, "requests", "status"):
            conn.execute("ALTER TABLE requests ADD COLUMN status TEXT DEFAULT 'new'")
            logger.info("Миграция: добавлена колонка status в requests")

        # Миграция: UNIQUE на telegram_id в initiator_users
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_initiator_tg_id "
                "ON initiator_users(telegram_id)"
            )
        except sqlite3.IntegrityError:
            # Если уже есть дубли — логируем, но не крашим
            logger.warning(
                "Не удалось создать UNIQUE INDEX на telegram_id — "
                "возможно, есть дубли в initiator_users"
            )

        # Заполняем справочники ТОЛЬКО если таблица пустая
        for list_type, values in _DEFAULT_LISTS.items():
            count = conn.execute(
                "SELECT COUNT(*) FROM lists WHERE list_type = ?", (list_type,)
            ).fetchone()[0]
            if count == 0:
                for i, value in enumerate(values):
                    conn.execute(
                        "INSERT INTO lists (list_type, value, sort_order) VALUES (?, ?, ?)",
                        (list_type, value, i),
                    )

        # Заполняем настройки по умолчанию ТОЛЬКО если ключа нет
        for key, value in _DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )

    logger.info(f"БД инициализирована: {DB_PATH}")


# ===== Audit Log =====


def audit_log(
    request_id: Optional[int],
    user_id: Optional[int],
    user_name: str,
    action: str,
    details: str = "",
):
    """Записывает действие в журнал аудита."""
    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO audit_log (request_id, user_id, user_name, action, details)
                   VALUES (?, ?, ?, ?, ?)""",
                (request_id, user_id, user_name, action, details),
            )
    except Exception as e:
        logger.warning(f"audit_log ошибка: {e}")


def get_audit_log(request_id: Optional[int] = None, limit: int = 50) -> List[dict]:
    """Возвращает записи журнала аудита."""
    try:
        with get_conn() as conn:
            if request_id:
                rows = conn.execute(
                    """SELECT * FROM audit_log WHERE request_id = ?
                       ORDER BY timestamp DESC LIMIT ?""",
                    (request_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"get_audit_log ошибка: {e}")
        return []


# ===== Backup =====


def backup_db() -> Optional[str]:
    """Создаёт резервную копию БД через SQLite backup API. Возвращает путь к копии."""
    try:
        backup_dir = Path(BACKUP_DIR)
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"bot_backup_{timestamp}.db"

        source = sqlite3.connect(DB_PATH)
        dest = sqlite3.connect(str(backup_path))
        source.backup(dest)
        dest.close()
        source.close()

        logger.info(f"Резервная копия создана: {backup_path}")
        return str(backup_path)
    except Exception as e:
        logger.error(f"Ошибка создания резервной копии: {e}")
        return None


def verify_backup(backup_path: str) -> bool:
    """Проверяет целостность резервной копии."""
    try:
        conn = sqlite3.connect(backup_path)
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        return result[0] == "ok"
    except Exception as e:
        logger.error(f"Ошибка проверки резервной копии: {e}")
        return False


# ===== Persistent Conversation State =====


def save_conversation_state(user_id: int, state_data: str):
    """Сохраняет состояние диалога пользователя."""
    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO conversation_state (user_id, state_data, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       state_data = excluded.state_data,
                       updated_at = excluded.updated_at""",
                (user_id, state_data, datetime.now().isoformat()),
            )
    except Exception as e:
        logger.warning(f"save_conversation_state ошибка: {e}")


def load_conversation_state(user_id: int) -> Optional[str]:
    """Загружает состояние диалога пользователя."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT state_data FROM conversation_state WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return row["state_data"] if row else None
    except Exception as e:
        logger.warning(f"load_conversation_state ошибка: {e}")
        return None


def clear_conversation_state(user_id: int):
    """Удаляет сохранённое состояние диалога."""
    try:
        with get_conn() as conn:
            conn.execute("DELETE FROM conversation_state WHERE user_id = ?", (user_id,))
    except Exception as e:
        logger.warning(f"clear_conversation_state ошибка: {e}")


# ===== Запросы (requests) =====


def add_request(
    name: str,
    quantity: float,
    unit: str,
    request_date: date,
    initiator: str,
    object_name: str,
    notes: str = "",
    user_id: Optional[int] = None,
    user_name: str = "",
) -> int:
    """Добавляет заявку и возвращает её ID."""
    with get_conn() as conn:
        cursor = conn.execute(
            """INSERT INTO requests
               (name, quantity, unit, request_date, initiator, object, notes, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'new')""",
            (
                name,
                quantity,
                unit,
                request_date.isoformat(),
                initiator,
                object_name,
                notes,
            ),
        )
        row_id = cursor.lastrowid
    logger.info(f"Заявка #{row_id} добавлена: {name}")
    audit_log(row_id, user_id, user_name, "created", f"Наименование: {name}")
    return row_id


def add_request_with_photos(
    name: str,
    quantity: float,
    unit: str,
    request_date: date,
    initiator: str,
    object_name: str,
    notes: str = "",
    photo_file_ids: Optional[List[str]] = None,
    user_id: Optional[int] = None,
    user_name: str = "",
) -> int:
    """Добавляет заявку и фото в одной транзакции. Возвращает ID заявки."""
    with get_conn() as conn:
        cursor = conn.execute(
            """INSERT INTO requests
               (name, quantity, unit, request_date, initiator, object, notes, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'new')""",
            (
                name,
                quantity,
                unit,
                request_date.isoformat(),
                initiator,
                object_name,
                notes,
            ),
        )
        row_id = cursor.lastrowid
        if photo_file_ids:
            conn.executemany(
                "INSERT INTO request_photos (request_id, file_id) VALUES (?, ?)",
                [(row_id, fid) for fid in photo_file_ids],
            )
    logger.info(
        f"Заявка #{row_id} добавлена: {name} (фото: {len(photo_file_ids or [])})"
    )
    audit_log(
        row_id,
        user_id,
        user_name,
        "created",
        f"Наименование: {name}, фото: {len(photo_file_ids or [])}",
    )
    return row_id


def update_request_status(
    request_id: int,
    new_status: str,
    user_id: Optional[int] = None,
    user_name: str = "",
) -> bool:
    """Обновляет статус заявки. Возвращает True если успешно.
    Использует optimistic locking: UPDATE ... WHERE id=? для атомарности."""
    if new_status not in VALID_STATUSES:
        logger.warning(f"Недопустимый статус: {new_status}")
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM requests WHERE id = ?", (request_id,)
        ).fetchone()
        if not row:
            return False
        old_status = row["status"]
        if old_status == new_status:
            return True
        cursor = conn.execute(
            "UPDATE requests SET status = ? WHERE id = ?",
            (new_status, request_id),
        )
        if cursor.rowcount == 0:
            return False
    audit_log(
        request_id,
        user_id,
        user_name,
        "status_changed",
        f"{old_status} → {new_status}",
    )
    return True


def edit_request(
    request_id: int,
    name: Optional[str] = None,
    quantity: Optional[float] = None,
    unit: Optional[str] = None,
    object_name: Optional[str] = None,
    notes: Optional[str] = None,
    user_id: Optional[int] = None,
    user_name: str = "",
) -> bool:
    """Редактирует заявку. Обновляет только переданные поля."""
    fields = []
    values = []
    changes = []
    if name is not None:
        fields.append("name = ?")
        values.append(name)
        changes.append(f"наименование: {name}")
    if quantity is not None:
        fields.append("quantity = ?")
        values.append(quantity)
        changes.append(f"кол-во: {quantity}")
    if unit is not None:
        fields.append("unit = ?")
        values.append(unit)
        changes.append(f"ед.изм: {unit}")
    if object_name is not None:
        fields.append("object = ?")
        values.append(object_name)
        changes.append(f"объект: {object_name}")
    if notes is not None:
        fields.append("notes = ?")
        values.append(notes)
        changes.append(f"примечание: {notes}")

    if not fields:
        return False

    values.append(request_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE requests SET {', '.join(fields)} WHERE id = ?", values)
    audit_log(request_id, user_id, user_name, "edited", "; ".join(changes))
    return True


def clear_all_requests():
    """Удаляет все заявки и связанные фото. Сначала создаёт резервную копию."""
    backup_path = backup_db()
    if backup_path:
        if verify_backup(backup_path):
            logger.info(f"Резервная копия подтверждена: {backup_path}")
        else:
            logger.error("Резервная копия не прошла проверку — очистка отменена")
            raise RuntimeError("Резервная копия БД не прошла проверку целостности")

    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        conn.execute("DELETE FROM request_photos")
        conn.execute("DELETE FROM requests")
        conn.execute("DELETE FROM audit_log")
    logger.warning(f"Очищена БД: удалено {count} заявок")
    audit_log(None, None, "system", "db_cleared", f"Удалено заявок: {count}")
    return count


def delete_request(
    request_id: int,
    user_id: Optional[int] = None,
    user_name: str = "",
) -> bool:
    """Удаляет заявку и связанные фото. Возвращает True если успешно."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM requests WHERE id = ?", (request_id,)
        ).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM request_photos WHERE request_id = ?", (request_id,))
        conn.execute("DELETE FROM requests WHERE id = ?", (request_id,))
    audit_log(request_id, user_id, user_name, "deleted", "")
    logger.info(f"Заявка #{request_id} удалена")
    return True


def get_requests_by_status(status: Optional[str] = None, limit: int = 50) -> List[dict]:
    """Возвращает заявки по статусу. Если status=None — все неархивные."""
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                """SELECT id, name, quantity, unit, request_date, done_date,
                          initiator, status, object, notes
                   FROM requests
                   WHERE status = ?
                   ORDER BY request_date DESC LIMIT ?""",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, name, quantity, unit, request_date, done_date,
                          initiator, status, object, notes
                   FROM requests
                   WHERE status != 'archived'
                   ORDER BY request_date DESC LIMIT ?""",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_dashboard_counts() -> dict:
    """Возвращает счётчики для дашборда."""
    with get_conn() as conn:
        total_active = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE done_date IS NULL AND status != 'archived'"
        ).fetchone()[0]
        by_status = {}
        for st in ("new", "ordered", "delivered"):
            count = conn.execute(
                "SELECT COUNT(*) FROM requests WHERE status = ?", (st,)
            ).fetchone()[0]
            by_status[st] = count
        overdue = conn.execute("""SELECT COUNT(*) FROM requests
               WHERE done_date IS NULL AND status != 'archived'
               AND status NOT IN ('done', 'delivered')
               AND request_date < date('now', '-' || (
                   SELECT COALESCE(value, '3') FROM settings WHERE key = 'overdue_days'
               ) || ' days')""").fetchone()[0]
        today_str = date.today().isoformat()
        done_today = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE done_date = ?", (today_str,)
        ).fetchone()[0]
    return {
        "total_active": total_active,
        "new": by_status.get("new", 0),
        "ordered": by_status.get("ordered", 0),
        "delivered": by_status.get("delivered", 0),
        "overdue": overdue,
        "done_today": done_today,
    }


def archive_completed_requests(days_old: int = 90) -> int:
    """Архивирует заявки старше указанного количества дней (помечает status='archived').
    Возвращает количество архивированных заявок."""
    cutoff = date.today().isoformat()
    with get_conn() as conn:
        cursor = conn.execute(
            """UPDATE requests SET status = 'archived'
               WHERE done_date IS NOT NULL
               AND status = 'done'
               AND done_date < date(?, '-' || ? || ' days')""",
            (cutoff, days_old),
        )
        count = cursor.rowcount
    if count:
        logger.info(f"Архивировано {count} заявок старше {days_old} дней")
        audit_log(None, None, "system", "archived", f"Архивировано: {count}")
    return count


def get_active_requests() -> List[dict]:
    """Возвращает активные заявки (без done_date, status != archived), отсортированные по объекту."""
    with get_conn() as conn:
        rows = conn.execute("""SELECT id, name, quantity, unit, request_date, done_date,
                      initiator, status, object, notes
               FROM requests
               WHERE done_date IS NULL AND request_date IS NOT NULL
               AND status != 'archived'
               ORDER BY object, request_date""").fetchall()
    return [dict(r) for r in rows]


def get_all_lists() -> Dict[str, List[str]]:
    """Возвращает все справочники в виде {тип: [значения]}."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT list_type, value FROM lists ORDER BY sort_order"
        ).fetchall()

    result = {}
    for row in rows:
        lt = row["list_type"]
        if lt not in result:
            result[lt] = []
        result[lt].append(row["value"])

    # Добавляем "Другое" в инициаторы
    if "initiator" in result and "Другое" not in result["initiator"]:
        result["initiator"].append("Другое")

    # Добавляем "Другое" в объекты
    if "object" in result and "Другое" not in result["object"]:
        result["object"].append("Другое")

    # Заполняем пропущенные типы значениями по умолчанию
    for lt, defaults in _DEFAULT_LISTS.items():
        if lt not in result:
            result[lt] = defaults

    return result


def add_list_value(list_type: str, value: str) -> bool:
    """Добавляет значение в справочник. Возвращает True если добавлено."""
    with get_conn() as conn:
        try:
            max_order = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) FROM lists WHERE list_type = ?",
                (list_type,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO lists (list_type, value, sort_order) VALUES (?, ?, ?)",
                (list_type, value, max_order + 1),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def rename_list_value(list_type: str, old_value: str, new_value: str) -> bool:
    """Переименовывает значение в справочнике. Возвращает True если успешно."""
    with get_conn() as conn:
        try:
            conn.execute(
                "UPDATE lists SET value = ? WHERE list_type = ? AND value = ?",
                (new_value, list_type, old_value),
            )
            return conn.total_changes > 0
        except sqlite3.IntegrityError:
            return False


def delete_list_value(list_type: str, value: str) -> bool:
    """Удаляет значение из справочника. Возвращает True если удалено."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM lists WHERE list_type = ? AND value = ?",
            (list_type, value),
        )
        return conn.total_changes > 0


def get_setting(key: str, default: str = "") -> str:
    """Возвращает значение настройки."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default
    except Exception as e:
        logger.warning(f"get_setting({key!r}) ошибка: {e}")
        return default


def set_setting(key: str, value: str):
    """Устанавливает значение настройки."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )


def complete_request(
    request_id: int,
    done_date: date,
    user_id: Optional[int] = None,
    user_name: str = "",
) -> Optional[dict]:
    """Отмечает заявку как выполненную. Идемпотентна — не перезаписывает done_date."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM requests WHERE id = ?", (request_id,)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        # Идемпотентность: не перезаписываем, если уже завершена
        if row["done_date"]:
            return result
        conn.execute(
            "UPDATE requests SET done_date = ?, status = 'done' WHERE id = ?",
            (done_date.isoformat(), request_id),
        )
    result["done_date"] = done_date.isoformat()
    result["status"] = "done"
    logger.info(f"Заявка #{request_id} завершена")
    audit_log(
        request_id, user_id, user_name, "completed", f"Дата: {done_date.isoformat()}"
    )
    return result


def complete_requests_batch(
    request_ids: List[int],
    done_date: date,
    user_id: Optional[int] = None,
    user_name: str = "",
) -> int:
    """Завершает несколько заявок одним SQL. Возвращает количество завершённых.
    Атомарно: UPDATE ... WHERE id IN (...) AND done_date IS NULL."""
    if not request_ids:
        return 0
    placeholders = ",".join("?" * len(request_ids))
    done_str = done_date.isoformat()
    with get_conn() as conn:
        cursor = conn.execute(
            f"""UPDATE requests
                SET done_date = ?, status = 'done'
                WHERE id IN ({placeholders}) AND done_date IS NULL""",
            [done_str] + list(request_ids),
        )
        completed = cursor.rowcount
    if completed:
        audit_log(
            None,
            user_id,
            user_name,
            "batch_completed",
            f"Завершено {completed} заявок: {request_ids}",
        )
    return completed


def get_completed_requests(limit: int = 50) -> List[dict]:
    """Возвращает завершённые заявки (есть done_date), отсортированные по дате завершения."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, name, quantity, unit, request_date, done_date,
                      initiator, status, object, notes
               FROM requests
               WHERE done_date IS NOT NULL AND status != 'archived'
               ORDER BY done_date DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_request_by_id(request_id: int) -> Optional[dict]:
    """Возвращает заявку по ID."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM requests WHERE id = ?", (request_id,)
        ).fetchone()
    return dict(row) if row else None


def is_request_owner(request_id: int, telegram_id: int) -> bool:
    """Проверяет, является ли пользователь инициатором заявки."""
    try:
        with get_conn() as conn:
            # Получаем инициатора заявки
            req = conn.execute(
                "SELECT initiator FROM requests WHERE id = ?", (request_id,)
            ).fetchone()
            if not req or not req["initiator"]:
                return False
            # Проверяем, привязан ли telegram_id к этому инициатору
            link = conn.execute(
                "SELECT 1 FROM initiator_users WHERE initiator_name = ? AND telegram_id = ?",
                (req["initiator"], telegram_id),
            ).fetchone()
            return link is not None
    except Exception as e:
        logger.warning(f"is_request_owner({request_id}, {telegram_id}) ошибка: {e}")
        return False


def get_user_requests(
    initiator: str, status_filter: Optional[str] = None, limit: int = 50
) -> List[dict]:
    """Возвращает заявки конкретного инициатора."""
    with get_conn() as conn:
        if status_filter and status_filter == "active":
            rows = conn.execute(
                """SELECT id, name, quantity, unit, request_date, done_date,
                          initiator, status, object, notes
                   FROM requests
                   WHERE initiator = ? AND done_date IS NULL AND status != 'archived'
                   ORDER BY request_date DESC LIMIT ?""",
                (initiator, limit),
            ).fetchall()
        elif status_filter and status_filter == "completed":
            rows = conn.execute(
                """SELECT id, name, quantity, unit, request_date, done_date,
                          initiator, status, object, notes
                   FROM requests
                   WHERE initiator = ? AND done_date IS NOT NULL AND status != 'archived'
                   ORDER BY done_date DESC LIMIT ?""",
                (initiator, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, name, quantity, unit, request_date, done_date,
                          initiator, status, object, notes
                   FROM requests
                   WHERE initiator = ? AND status != 'archived'
                   ORDER BY request_date DESC LIMIT ?""",
                (initiator, limit),
            ).fetchall()
    return [dict(r) for r in rows]


def search_requests(
    query: str,
    object_filter: Optional[str] = None,
    initiator_filter: Optional[str] = None,
    limit: int = 20,
) -> List[dict]:
    """Поиск заявок по наименованию/примечанию с фильтрами."""
    conditions = ["status != 'archived'"]
    params: list = []

    if query:
        conditions.append("(name LIKE ? OR notes LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%"])

    if object_filter:
        conditions.append("object = ?")
        params.append(object_filter)

    if initiator_filter:
        conditions.append("initiator = ?")
        params.append(initiator_filter)

    where = " AND ".join(conditions)
    params.append(limit)

    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT id, name, quantity, unit, request_date, done_date,
                       initiator, status, object, notes
                FROM requests
                WHERE {where}
                ORDER BY request_date DESC LIMIT ?""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


# ===== Фото к заявкам =====


def add_request_photo(request_id: int, file_id: str):
    """Сохраняет file_id фото для заявки."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO request_photos (request_id, file_id) VALUES (?, ?)",
            (request_id, file_id),
        )


def get_request_photos(request_id: int) -> List[str]:
    """Возвращает список file_id фото для заявки."""
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT file_id FROM request_photos WHERE request_id = ? ORDER BY id",
                (request_id,),
            ).fetchall()
        return [row["file_id"] for row in rows]
    except Exception as e:
        logger.warning(f"get_request_photos({request_id}) ошибка: {e}")
        return []


def get_photo_counts(request_ids: List[int]) -> Dict[int, int]:
    """Возвращает {request_id: photo_count} для списка заявок."""
    if not request_ids:
        return {}
    try:
        result = {}
        batch_size = 900
        for i in range(0, len(request_ids), batch_size):
            batch = request_ids[i : i + batch_size]
            placeholders = ",".join("?" * len(batch))
            with get_conn() as conn:
                rows = conn.execute(
                    f"""SELECT request_id, COUNT(*) as cnt
                        FROM request_photos
                        WHERE request_id IN ({placeholders})
                        GROUP BY request_id""",
                    batch,
                ).fetchall()
            result.update({row["request_id"]: row["cnt"] for row in rows})
        return result
    except Exception as e:
        logger.warning(f"get_photo_counts ошибка: {e}")
        return {}


# ===== Привязка инициаторов к Telegram =====


def link_initiator(initiator_name: str, telegram_id: int):
    """Привязывает инициатора к Telegram-ID."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO initiator_users (initiator_name, telegram_id)
               VALUES (?, ?)
               ON CONFLICT(initiator_name) DO UPDATE SET telegram_id = excluded.telegram_id""",
            (initiator_name, telegram_id),
        )
    logger.info(f"Инициатор «{initiator_name}» привязан к tg_id={telegram_id}")


def get_initiator_tg_id(initiator_name: str) -> Optional[int]:
    """Возвращает telegram_id инициатора или None."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT telegram_id FROM initiator_users WHERE initiator_name = ?",
                (initiator_name,),
            ).fetchone()
        return row["telegram_id"] if row else None
    except Exception as e:
        logger.warning(f"get_initiator_tg_id({initiator_name!r}) ошибка: {e}")
        return None


def get_initiator_by_tg_id(telegram_id: int) -> Optional[str]:
    """Возвращает имя инициатора по telegram_id или None."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT initiator_name FROM initiator_users WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
        return row["initiator_name"] if row else None
    except Exception as e:
        logger.warning(f"get_initiator_by_tg_id({telegram_id}) ошибка: {e}")
        return None


def get_all_initiator_links() -> Dict[str, int]:
    """Возвращает все привязки {имя: telegram_id}."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT initiator_name, telegram_id FROM initiator_users"
        ).fetchall()
    return {row["initiator_name"]: row["telegram_id"] for row in rows}
