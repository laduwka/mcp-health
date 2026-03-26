import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config
from .log import get_logger
from .metrics import timed_db

_log = get_logger("mcp_health.db")


def _now_utc() -> str:
    """Current time in UTC, ISO format."""
    return datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _today_local() -> str:
    """Today's date in the user's configured timezone."""
    return datetime.now(ZoneInfo(config.TZ)).strftime("%Y-%m-%d")


def _tz_offset_sql() -> str:
    """Return the current UTC offset for config.TZ as a SQLite modifier string.

    Example: '-04:00' or '+03:00'. Used in date(logged_at, offset) for grouping.
    """
    now = datetime.now(ZoneInfo(config.TZ))
    offset = now.utcoffset()
    total_seconds = int(offset.total_seconds())
    sign = "+" if total_seconds >= 0 else "-"
    hours, remainder = divmod(abs(total_seconds), 3600)
    minutes = remainder // 60
    return f"{sign}{hours:02d}:{minutes:02d}"


def _date_range_utc(date_str: str) -> tuple[str, str]:
    """Convert a local date to UTC start/end boundaries.

    Returns (start_utc, end_utc) where start is inclusive and end is exclusive.
    Example: '2026-03-24' in America/Toronto (-04:00)
      -> ('2026-03-24T04:00:00+00:00', '2026-03-25T04:00:00+00:00')
    """
    tz = ZoneInfo(config.TZ)
    local_start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    utc = ZoneInfo("UTC")
    return (
        local_start.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        local_end.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
    )


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or config.DB_PATH
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    _log.info("Database connection opened", extra={"operation": "connect"})
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    _log.info("Initializing database schema", extra={"operation": "init_db"})
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            name_lower TEXT NOT NULL,
            kcal_per_100 REAL NOT NULL,
            protein_per_100 REAL NOT NULL,
            fat_per_100 REAL NOT NULL,
            carbs_per_100 REAL NOT NULL,
            label_per_unit TEXT DEFAULT 'g',
            barcode TEXT,
            notes TEXT,
            usage_count INTEGER DEFAULT 0,
            last_used TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_products_name_lower ON products(name_lower);
        CREATE INDEX IF NOT EXISTS idx_products_barcode ON products(barcode);

        CREATE TABLE IF NOT EXISTS meals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meal_type TEXT,
            notes TEXT,
            logged_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_meals_logged_at ON meals(logged_at);

        CREATE TABLE IF NOT EXISTS meal_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meal_id INTEGER NOT NULL REFERENCES meals(id) ON DELETE CASCADE,
            product_id INTEGER REFERENCES products(id),
            name TEXT NOT NULL,
            weight_grams REAL NOT NULL,
            kcal REAL NOT NULL,
            protein REAL NOT NULL,
            fat REAL NOT NULL,
            carbs REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_meal_items_meal_id ON meal_items(meal_id);

        CREATE TABLE IF NOT EXISTS weight_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            weight_kg REAL NOT NULL,
            date TEXT NOT NULL UNIQUE,
            logged_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_weight_log_date ON weight_log(date);

        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            daily_kcal REAL,
            protein_g REAL,
            fat_g REAL,
            carbs_g REAL,
            target_weight REAL,
            set_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS oauth_clients (
            client_id TEXT PRIMARY KEY,
            client_info TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS oauth_tokens (
            token TEXT PRIMARY KEY,
            token_type TEXT NOT NULL,
            client_id TEXT NOT NULL,
            data TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_oauth_tokens_type ON oauth_tokens(token_type);
        CREATE INDEX IF NOT EXISTS idx_oauth_tokens_expires ON oauth_tokens(expires_at);
    """)
    conn.commit()

    # Migration: add serving size columns to products
    try:
        conn.execute("ALTER TABLE products ADD COLUMN default_serving_grams REAL")
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        conn.execute("ALTER TABLE products ADD COLUMN serving_label TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()


# --- Products ---


@timed_db
def insert_product(conn: sqlite3.Connection, **kwargs) -> int:
    kwargs["name_lower"] = kwargs["name"].lower()
    kwargs.setdefault("created_at", _now_utc())
    cols = ", ".join(kwargs.keys())
    placeholders = ", ".join(["?"] * len(kwargs))
    cur = conn.execute(
        f"INSERT INTO products ({cols}) VALUES ({placeholders})",
        list(kwargs.values()),
    )
    conn.commit()
    return cur.lastrowid


@timed_db
def search_products(conn: sqlite3.Connection, query: str, limit: int = 5) -> list[dict]:
    rows = conn.execute(
        """SELECT id, name, kcal_per_100, protein_per_100, fat_per_100, carbs_per_100,
                  barcode, usage_count, last_used, default_serving_grams, serving_label
           FROM products
           WHERE name_lower LIKE '%' || ? || '%'
           ORDER BY usage_count DESC, last_used DESC
           LIMIT ?""",
        (query.lower(), limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_product(conn: sqlite3.Connection, product_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    return dict(row) if row else None


def get_product_by_barcode(conn: sqlite3.Connection, barcode: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM products WHERE barcode = ?", (barcode,)
    ).fetchone()
    return dict(row) if row else None


def increment_product_usage(conn: sqlite3.Connection, product_id: int) -> None:
    conn.execute(
        "UPDATE products SET usage_count = usage_count + 1, last_used = ? WHERE id = ?",
        (_now_utc(), product_id),
    )
    conn.commit()


def update_product_serving(
    conn: sqlite3.Connection,
    product_id: int,
    grams: float,
    label: str | None = None,
) -> None:
    conn.execute(
        "UPDATE products SET default_serving_grams = ?, serving_label = ? WHERE id = ?",
        (grams, label, product_id),
    )
    conn.commit()


def get_most_common_serving(conn: sqlite3.Connection, product_id: int) -> dict | None:
    """Return the most common weight_grams for a product and its frequency ratio."""
    rows = conn.execute(
        """SELECT weight_grams, COUNT(*) as cnt
           FROM meal_items
           WHERE product_id = ?
           GROUP BY weight_grams
           ORDER BY cnt DESC""",
        (product_id,),
    ).fetchall()
    if not rows:
        return None
    total = sum(r["cnt"] for r in rows)
    top = rows[0]
    return {
        "weight_grams": top["weight_grams"],
        "count": top["cnt"],
        "total": total,
        "ratio": top["cnt"] / total,
    }


# --- Meals ---


@timed_db
def insert_meal(
    conn: sqlite3.Connection,
    meal_type: str | None,
    notes: str | None,
    logged_at: str | None,
    items: list[dict],
) -> int:
    logged_at = logged_at or _now_utc()
    cur = conn.execute(
        "INSERT INTO meals (meal_type, notes, logged_at) VALUES (?, ?, ?)",
        (meal_type, notes, logged_at),
    )
    meal_id = cur.lastrowid
    for item in items:
        conn.execute(
            """INSERT INTO meal_items (meal_id, product_id, name, weight_grams, kcal, protein, fat, carbs)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                meal_id,
                item.get("product_id"),
                item["name"],
                item["weight_grams"],
                item["kcal"],
                item["protein"],
                item["fat"],
                item["carbs"],
            ),
        )
    conn.commit()
    return meal_id


@timed_db
def get_meals_for_date(conn: sqlite3.Connection, date_str: str) -> list[dict]:
    start_utc, end_utc = _date_range_utc(date_str)
    meals = conn.execute(
        "SELECT * FROM meals WHERE logged_at >= ? AND logged_at < ? ORDER BY logged_at",
        (start_utc, end_utc),
    ).fetchall()
    result = []
    for meal in meals:
        meal_dict = dict(meal)
        items = conn.execute(
            "SELECT * FROM meal_items WHERE meal_id = ?", (meal_dict["id"],)
        ).fetchall()
        meal_dict["items"] = [dict(i) for i in items]
        result.append(meal_dict)
    return result


def get_meal(conn: sqlite3.Connection, meal_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM meals WHERE id = ?", (meal_id,)).fetchone()
    return dict(row) if row else None


def delete_meal(conn: sqlite3.Connection, meal_id: int) -> bool:
    cur = conn.execute("DELETE FROM meals WHERE id = ?", (meal_id,))
    conn.commit()
    return cur.rowcount > 0


def get_meal_item(conn: sqlite3.Connection, item_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM meal_items WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def delete_meal_item(conn: sqlite3.Connection, item_id: int) -> bool:
    cur = conn.execute("DELETE FROM meal_items WHERE id = ?", (item_id,))
    conn.commit()
    return cur.rowcount > 0


def count_meal_items(conn: sqlite3.Connection, meal_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM meal_items WHERE meal_id = ?", (meal_id,)
    ).fetchone()
    return row["cnt"]


def update_meal_item(
    conn: sqlite3.Connection,
    item_id: int,
    weight_grams: float,
    kcal: float,
    protein: float,
    fat: float,
    carbs: float,
) -> None:
    conn.execute(
        """UPDATE meal_items
           SET weight_grams = ?, kcal = ?, protein = ?, fat = ?, carbs = ?
           WHERE id = ?""",
        (weight_grams, kcal, protein, fat, carbs, item_id),
    )
    conn.commit()


@timed_db
def get_recent_meals_by_type(
    conn: sqlite3.Connection,
    meal_type: str | None,
    start_utc: str,
    end_utc: str,
    limit: int = 5,
) -> list[dict]:
    """Return recent meals (with items) optionally filtered by meal_type."""
    if meal_type:
        meals = conn.execute(
            """SELECT * FROM meals
               WHERE meal_type = ? AND logged_at >= ? AND logged_at < ?
               ORDER BY logged_at DESC LIMIT ?""",
            (meal_type, start_utc, end_utc, limit),
        ).fetchall()
    else:
        meals = conn.execute(
            """SELECT * FROM meals
               WHERE logged_at >= ? AND logged_at < ?
               ORDER BY logged_at DESC LIMIT ?""",
            (start_utc, end_utc, limit),
        ).fetchall()

    result = []
    for meal in meals:
        meal_dict = dict(meal)
        items = conn.execute(
            """SELECT mi.id, mi.product_id, mi.name, mi.weight_grams,
                      mi.kcal, mi.protein, mi.fat, mi.carbs,
                      p.default_serving_grams, p.serving_label
               FROM meal_items mi
               LEFT JOIN products p ON mi.product_id = p.id
               WHERE mi.meal_id = ?
               ORDER BY mi.id""",
            (meal_dict["id"],),
        ).fetchall()
        meal_dict["items"] = [dict(i) for i in items]
        result.append(meal_dict)
    return result


# --- Weight ---


@timed_db
def upsert_weight(
    conn: sqlite3.Connection, weight_kg: float, date_str: str | None = None
) -> int:
    date_str = date_str or _today_local()
    cur = conn.execute(
        """INSERT INTO weight_log (weight_kg, date, logged_at) VALUES (?, ?, ?)
           ON CONFLICT(date) DO UPDATE SET weight_kg = excluded.weight_kg, logged_at = excluded.logged_at""",
        (weight_kg, date_str, _now_utc()),
    )
    conn.commit()
    return cur.lastrowid


def get_weight_range(
    conn: sqlite3.Connection, start_date: str, end_date: str
) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM weight_log WHERE date >= ? AND date <= ? ORDER BY date",
        (start_date, end_date),
    ).fetchall()
    return [dict(r) for r in rows]


def get_weight_for_date(conn: sqlite3.Connection, date_str: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM weight_log WHERE date = ?", (date_str,)
    ).fetchone()
    return dict(row) if row else None


# --- Goals ---


def get_current_goals(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM goals ORDER BY set_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def insert_goals(conn: sqlite3.Connection, **kwargs) -> int:
    kwargs.setdefault("set_at", _now_utc())
    cols = ", ".join(kwargs.keys())
    placeholders = ", ".join(["?"] * len(kwargs))
    cur = conn.execute(
        f"INSERT INTO goals ({cols}) VALUES ({placeholders})",
        list(kwargs.values()),
    )
    conn.commit()
    return cur.lastrowid


# --- Aggregation ---


@timed_db
def get_daily_totals(conn: sqlite3.Connection, date_str: str) -> dict:
    start_utc, end_utc = _date_range_utc(date_str)
    row = conn.execute(
        """SELECT
               COALESCE(SUM(mi.kcal), 0) as kcal,
               COALESCE(SUM(mi.protein), 0) as protein,
               COALESCE(SUM(mi.fat), 0) as fat,
               COALESCE(SUM(mi.carbs), 0) as carbs
           FROM meal_items mi
           JOIN meals m ON mi.meal_id = m.id
           WHERE m.logged_at >= ? AND m.logged_at < ?""",
        (start_utc, end_utc),
    ).fetchone()
    return dict(row)


@timed_db
def get_date_range_totals(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    start_utc, _ = _date_range_utc(start)
    _, end_utc = _date_range_utc(end)
    tz_offset = _tz_offset_sql()
    rows = conn.execute(
        f"""SELECT
               date(m.logged_at, '{tz_offset}') as date,
               COALESCE(SUM(mi.kcal), 0) as kcal,
               COALESCE(SUM(mi.protein), 0) as protein,
               COALESCE(SUM(mi.fat), 0) as fat,
               COALESCE(SUM(mi.carbs), 0) as carbs
           FROM meal_items mi
           JOIN meals m ON mi.meal_id = m.id
           WHERE m.logged_at >= ? AND m.logged_at < ?
           GROUP BY date(m.logged_at, '{tz_offset}')
           ORDER BY date(m.logged_at, '{tz_offset}')""",
        (start_utc, end_utc),
    ).fetchall()
    return [dict(r) for r in rows]


def get_top_products(
    conn: sqlite3.Connection, start: str, end: str, limit: int = 10
) -> list[dict]:
    start_utc, _ = _date_range_utc(start)
    _, end_utc = _date_range_utc(end)
    rows = conn.execute(
        """SELECT
               mi.name,
               mi.product_id,
               COUNT(*) as times_used,
               MAX(m.logged_at) as last_used,
               SUM(mi.kcal) as total_kcal,
               SUM(mi.protein) as total_protein,
               SUM(mi.fat) as total_fat,
               SUM(mi.carbs) as total_carbs
           FROM meal_items mi
           JOIN meals m ON mi.meal_id = m.id
           WHERE m.logged_at >= ? AND m.logged_at < ?
           GROUP BY COALESCE(mi.product_id, mi.name)
           ORDER BY times_used DESC
           LIMIT ?""",
        (start_utc, end_utc, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# --- OAuth ---


def save_oauth_client(
    conn: sqlite3.Connection, client_id: str, client_info_json: str
) -> None:
    conn.execute(
        """INSERT INTO oauth_clients (client_id, client_info, created_at)
           VALUES (?, ?, ?)
           ON CONFLICT(client_id) DO UPDATE SET client_info = excluded.client_info""",
        (client_id, client_info_json, _now_utc()),
    )
    conn.commit()


def get_oauth_client(conn: sqlite3.Connection, client_id: str) -> str | None:
    row = conn.execute(
        "SELECT client_info FROM oauth_clients WHERE client_id = ?", (client_id,)
    ).fetchone()
    return row["client_info"] if row else None


def save_oauth_token(
    conn: sqlite3.Connection,
    token: str,
    token_type: str,
    client_id: str,
    data: str,
    expires_at: str,
) -> None:
    conn.execute(
        """INSERT INTO oauth_tokens (token, token_type, client_id, data, expires_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(token) DO UPDATE SET data = excluded.data, expires_at = excluded.expires_at""",
        (token, token_type, client_id, data, expires_at, _now_utc()),
    )
    conn.commit()


def get_oauth_token(
    conn: sqlite3.Connection, token: str, token_type: str
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM oauth_tokens WHERE token = ? AND token_type = ?",
        (token, token_type),
    ).fetchone()
    return dict(row) if row else None


def delete_oauth_token(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM oauth_tokens WHERE token = ?", (token,))
    conn.commit()


def cleanup_expired_tokens(conn: sqlite3.Connection) -> int:
    cur = conn.execute("DELETE FROM oauth_tokens WHERE expires_at < ?", (_now_utc(),))
    conn.commit()
    return cur.rowcount
