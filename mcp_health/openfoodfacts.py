import sqlite3
import time

from . import config
from .log import get_logger
from .metrics import OFF_DB_LATENCY, OFF_DB_QUERIES

_log = get_logger("mcp_health.off")

_conn: sqlite3.Connection | None = None


def _get_off_conn() -> sqlite3.Connection | None:
    """Lazy read-only connection to the OFF products database."""
    global _conn
    if _conn is not None:
        return _conn
    try:
        _conn = sqlite3.connect(
            f"file:{config.OFF_DB_PATH}?mode=ro", uri=True, check_same_thread=False
        )
        _conn.row_factory = sqlite3.Row
        return _conn
    except sqlite3.OperationalError:
        _log.warning(
            "OFF database not found, product search/lookup will return empty results",
            extra={"path": config.OFF_DB_PATH},
        )
        return None


def search(query: str, limit: int = 10, country: str | None = None) -> list[dict]:
    """Search local OFF database by product name using FTS5.

    When country is set (e.g. 'en:canada'), results are filtered to products
    sold in that country via the countries_tags column.
    """
    conn = _get_off_conn()
    if conn is None:
        return []

    start = time.monotonic()
    try:
        sql = (
            "SELECT p.code, p.product_name, p.brands, "
            "p.kcal_per_100, p.protein_per_100, p.fat_per_100, p.carbs_per_100 "
            "FROM products_fts fts "
            "JOIN products p ON p.rowid = fts.rowid "
            "WHERE products_fts MATCH ? "
        )
        params: list = [query]
        if country:
            sql += "AND p.countries_tags LIKE '%' || ? || '%' "
            params.append(country)
        sql += "ORDER BY rank LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        OFF_DB_QUERIES.labels(method="search").inc()
        _log.warning("OFF search error", extra={"query": query, "error": str(exc)})
        return []

    duration = time.monotonic() - start
    OFF_DB_LATENCY.labels(method="search").observe(duration)
    OFF_DB_QUERIES.labels(method="search").inc()

    return [
        {
            "name": row["product_name"],
            "brands": row["brands"],
            "kcal_per_100": round(float(row["kcal_per_100"]), 1),
            "protein_per_100": round(float(row["protein_per_100"]), 1),
            "fat_per_100": round(float(row["fat_per_100"]), 1),
            "carbs_per_100": round(float(row["carbs_per_100"]), 1),
            "barcode": row["code"],
        }
        for row in rows
    ]


def lookup_barcode(barcode: str) -> dict | None:
    """Look up product nutrients from local OFF database by barcode."""
    conn = _get_off_conn()
    if conn is None:
        return None

    start = time.monotonic()
    try:
        row = conn.execute(
            "SELECT code, product_name, brands, "
            "kcal_per_100, protein_per_100, fat_per_100, carbs_per_100 "
            "FROM products WHERE code = ?",
            (barcode,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        OFF_DB_QUERIES.labels(method="lookup").inc()
        _log.warning("OFF lookup error", extra={"barcode": barcode, "error": str(exc)})
        return None

    duration = time.monotonic() - start
    OFF_DB_LATENCY.labels(method="lookup").observe(duration)
    OFF_DB_QUERIES.labels(method="lookup").inc()

    if row is None:
        return None

    return {
        "name": row["product_name"],
        "brands": row["brands"],
        "kcal_per_100": round(float(row["kcal_per_100"]), 1),
        "protein_per_100": round(float(row["protein_per_100"]), 1),
        "fat_per_100": round(float(row["fat_per_100"]), 1),
        "carbs_per_100": round(float(row["carbs_per_100"]), 1),
        "barcode": row["code"],
    }
