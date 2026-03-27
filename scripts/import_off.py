#!/usr/bin/env python3
"""Import OpenFoodFacts CSV dump or delta updates into the unified products table.

Full import (default):
    python scripts/import_off.py --db data/fitness.db [--csv-path local.csv.gz]

Delta update:
    python scripts/import_off.py --db data/fitness.db --delta
"""

import argparse
import csv
import gzip
import io
import json
import os
import sqlite3
import sys
import time
import urllib.request

csv.field_size_limit(10 * 1024 * 1024)  # 10 MB — OFF has very long fields

CSV_URL = "https://static.openfoodfacts.org/data/en.openfoodfacts.org.products.csv.gz"
DELTA_INDEX_URL = "https://static.openfoodfacts.org/data/delta/index.txt"
DELTA_BASE_URL = "https://static.openfoodfacts.org/data/delta"

BATCH_SIZE = 10_000
PROGRESS_EVERY = 100_000

# CSV column names in the OFF dump
COL_CODE = "code"
COL_NAME = "product_name"
COL_BRANDS = "brands"
COL_KCAL = "energy-kcal_100g"
COL_PROTEIN = "proteins_100g"
COL_FAT = "fat_100g"
COL_CARBS = "carbohydrates_100g"
COL_COUNTRIES = "countries_tags"

REQUIRED_COLS = {COL_CODE, COL_NAME, COL_KCAL, COL_PROTEIN, COL_FAT, COL_CARBS}

UPSERT_SQL = """\
INSERT INTO products (
    name, name_lower, brand, kcal_per_100, protein_per_100,
    fat_per_100, carbs_per_100, off_code, source, countries_tags, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'off', ?, datetime('now'))
ON CONFLICT(off_code) DO UPDATE SET
    name = excluded.name,
    name_lower = excluded.name_lower,
    brand = excluded.brand,
    kcal_per_100 = excluded.kcal_per_100,
    protein_per_100 = excluded.protein_per_100,
    fat_per_100 = excluded.fat_per_100,
    carbs_per_100 = excluded.carbs_per_100,
    countries_tags = excluded.countries_tags
"""

USER_AGENT = "mcp-health/1.0 (https://github.com/laduwka/mcp-health)"


def _safe_float(val: str | None) -> float | None:
    if not val or val.strip() == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _download_stream(url: str):
    """Return a streaming response object for a URL."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=60)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Ensure the unified products table has OFF columns and FTS."""
    # Check if off_code column exists
    cols = {r[1] for r in conn.execute("PRAGMA table_info(products)").fetchall()}
    migrations = []
    if "source" not in cols:
        migrations.append(
            "ALTER TABLE products ADD COLUMN source TEXT NOT NULL DEFAULT 'local'"
        )
    if "off_code" not in cols:
        migrations.append("ALTER TABLE products ADD COLUMN off_code TEXT")
    if "brand" not in cols:
        migrations.append("ALTER TABLE products ADD COLUMN brand TEXT")
    if "countries_tags" not in cols:
        migrations.append("ALTER TABLE products ADD COLUMN countries_tags TEXT")

    for sql in migrations:
        conn.execute(sql)

    conn.execute("DROP INDEX IF EXISTS idx_products_off_code")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_products_off_code ON products(off_code)"
    )
    conn.commit()


def full_import(db_path: str, csv_path: str | None = None):
    """Import OFF CSV dump into the unified products table."""
    if not os.path.exists(db_path):
        print(
            f"Database not found at {db_path}. Run the server first to create the schema."
        )
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA cache_size=-200000")  # 200 MB cache
    _ensure_schema(conn)

    # Delete existing OFF products before full import
    print("Deleting existing OFF products...")
    cur = conn.execute("DELETE FROM products WHERE source = 'off'")
    deleted = cur.rowcount
    conn.commit()
    print(f"  Deleted {deleted:,} existing OFF products")

    # Disable FTS triggers during bulk import for performance
    conn.execute("DROP TRIGGER IF EXISTS products_fts_insert")
    conn.execute("DROP TRIGGER IF EXISTS products_fts_delete")
    conn.execute("DROP TRIGGER IF EXISTS products_fts_update")

    inserted = 0
    skipped = 0
    start_time = time.monotonic()

    if csv_path:
        print(f"Reading local file: {csv_path}")
        raw_stream = open(csv_path, "rb")
    else:
        print(f"Downloading: {CSV_URL}")
        raw_stream = _download_stream(CSV_URL)

    with raw_stream:
        gz = gzip.GzipFile(fileobj=raw_stream)
        text_stream = io.TextIOWrapper(gz, encoding="utf-8", errors="replace")
        reader = csv.DictReader(text_stream, delimiter="\t")

        batch = []
        for row in reader:
            code = (row.get(COL_CODE) or "").strip()
            name = (row.get(COL_NAME) or "").strip()
            if not code or not name:
                skipped += 1
                continue

            kcal = _safe_float(row.get(COL_KCAL))
            if kcal is None:
                skipped += 1
                continue

            protein = _safe_float(row.get(COL_PROTEIN)) or 0.0
            fat = _safe_float(row.get(COL_FAT)) or 0.0
            carbs = _safe_float(row.get(COL_CARBS)) or 0.0
            brands = (row.get(COL_BRANDS) or "").strip() or None
            countries = (row.get(COL_COUNTRIES) or "").strip() or None

            batch.append(
                (
                    name,
                    name.lower(),
                    brands,
                    kcal,
                    protein,
                    fat,
                    carbs,
                    code,
                    countries,
                )
            )

            if len(batch) >= BATCH_SIZE:
                conn.executemany(UPSERT_SQL, batch)
                conn.commit()
                inserted += len(batch)
                batch.clear()

                if inserted % PROGRESS_EVERY == 0:
                    elapsed = time.monotonic() - start_time
                    rate = inserted / elapsed if elapsed > 0 else 0
                    print(
                        f"  {inserted:,} rows inserted, "
                        f"{skipped:,} skipped, "
                        f"{rate:,.0f} rows/sec"
                    )

        # Final batch
        if batch:
            conn.executemany(UPSERT_SQL, batch)
            conn.commit()
            inserted += len(batch)

    # Rebuild FTS index
    print("Rebuilding FTS5 index...")
    try:
        conn.execute("INSERT INTO products_fts(products_fts) VALUES('rebuild')")
        conn.commit()
    except sqlite3.OperationalError as e:
        print(f"  FTS rebuild warning: {e}")

    # Restore FTS triggers
    conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS products_fts_insert
        AFTER INSERT ON products BEGIN
            INSERT INTO products_fts(rowid, name, brand)
            VALUES (new.rowid, new.name, new.brand);
        END;

        CREATE TRIGGER IF NOT EXISTS products_fts_delete
        AFTER DELETE ON products BEGIN
            INSERT INTO products_fts(products_fts, rowid, name, brand)
            VALUES ('delete', old.rowid, old.name, old.brand);
        END;

        CREATE TRIGGER IF NOT EXISTS products_fts_update
        AFTER UPDATE OF name, brand ON products BEGIN
            INSERT INTO products_fts(products_fts, rowid, name, brand)
            VALUES ('delete', old.rowid, old.name, old.brand);
            INSERT INTO products_fts(rowid, name, brand)
            VALUES (new.rowid, new.name, new.brand);
        END;
    """)

    conn.close()

    elapsed = time.monotonic() - start_time
    print(f"Done: {inserted:,} products imported, {skipped:,} skipped, {elapsed:.0f}s")


def delta_update(db_path: str):
    """Apply incremental delta updates from OpenFoodFacts."""
    if not os.path.exists(db_path):
        print(f"Database not found at {db_path}, run full import first.")
        sys.exit(1)

    cursor_path = os.path.join(os.path.dirname(db_path), ".off_delta_cursor")

    # Read last processed delta file
    last_cursor = ""
    if os.path.exists(cursor_path):
        with open(cursor_path) as f:
            last_cursor = f.read().strip()

    # Fetch delta index
    print("Fetching delta index...")
    resp = _download_stream(DELTA_INDEX_URL)
    with resp:
        index_text = resp.read().decode("utf-8")
    delta_files = [line.strip() for line in index_text.splitlines() if line.strip()]

    if not delta_files:
        print("No delta files available.")
        return

    # Find new deltas after cursor
    if last_cursor and last_cursor in delta_files:
        start_idx = delta_files.index(last_cursor) + 1
        new_deltas = delta_files[start_idx:]
    else:
        new_deltas = delta_files

    if not new_deltas:
        print("Already up to date.")
        return

    print(f"Processing {len(new_deltas)} delta file(s)...")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_schema(conn)

    total_upserted = 0

    for delta_file in new_deltas:
        url = f"{DELTA_BASE_URL}/{delta_file}"
        print(f"  Downloading {delta_file}...")

        try:
            resp = _download_stream(url)
        except urllib.error.URLError as e:
            print(f"  Warning: failed to download {delta_file}: {e}")
            continue

        upserted = 0
        batch = []

        with resp:
            gz = gzip.GzipFile(fileobj=resp)
            text_stream = io.TextIOWrapper(gz, encoding="utf-8", errors="replace")

            for line in text_stream:
                line = line.strip()
                if not line:
                    continue

                try:
                    doc = json.loads(line)
                except json.JSONDecodeError:
                    continue

                code = (doc.get("code") or "").strip()
                name = (doc.get("product_name") or "").strip()
                if not code or not name:
                    continue

                n = doc.get("nutriments", {})
                kcal = _safe_float(str(n.get("energy-kcal_100g", "")))
                if kcal is None:
                    continue

                protein = _safe_float(str(n.get("proteins_100g", ""))) or 0.0
                fat = _safe_float(str(n.get("fat_100g", ""))) or 0.0
                carbs = _safe_float(str(n.get("carbohydrates_100g", ""))) or 0.0
                brands = (doc.get("brands") or "").strip() or None
                countries = doc.get("countries_tags") or ""
                if isinstance(countries, list):
                    countries = ",".join(countries)
                countries = countries.strip() or None

                batch.append(
                    (
                        name,
                        name.lower(),
                        brands,
                        kcal,
                        protein,
                        fat,
                        carbs,
                        code,
                        countries,
                    )
                )

                if len(batch) >= BATCH_SIZE:
                    conn.executemany(UPSERT_SQL, batch)
                    conn.commit()
                    upserted += len(batch)
                    batch.clear()

        if batch:
            conn.executemany(UPSERT_SQL, batch)
            conn.commit()
            upserted += len(batch)

        total_upserted += upserted
        print(f"    {upserted:,} products upserted")

        # Update cursor after each successfully processed file
        with open(cursor_path, "w") as f:
            f.write(delta_file)

    # Rebuild FTS after all deltas
    if total_upserted > 0:
        print("Rebuilding FTS5 index...")
        try:
            conn.execute("INSERT INTO products_fts(products_fts) VALUES('rebuild')")
            conn.commit()
        except sqlite3.OperationalError as e:
            print(f"  FTS rebuild warning: {e}")

    conn.close()
    print(f"Done: {total_upserted:,} products upserted from {len(new_deltas)} delta(s)")


def main():
    parser = argparse.ArgumentParser(
        description="Import OpenFoodFacts data into unified products table"
    )
    parser.add_argument(
        "--db",
        default=os.path.join(os.path.dirname(__file__), "..", "data", "fitness.db"),
        help="Path to fitness database (default: data/fitness.db)",
    )
    parser.add_argument(
        "--csv-path",
        default=None,
        help="Path to local CSV.gz file (skip download)",
    )
    parser.add_argument(
        "--delta",
        action="store_true",
        help="Run incremental delta update instead of full import",
    )
    args = parser.parse_args()

    db_path = os.path.abspath(args.db)

    if args.delta:
        delta_update(db_path)
    else:
        full_import(db_path, csv_path=args.csv_path)


if __name__ == "__main__":
    main()
