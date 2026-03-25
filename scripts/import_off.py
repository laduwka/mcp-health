#!/usr/bin/env python3
"""Import OpenFoodFacts CSV dump or delta updates into local SQLite database.

Full import (default):
    python scripts/import_off.py [--output data/off_products.db] [--csv-path local.csv.gz]

Delta update:
    python scripts/import_off.py --delta [--output data/off_products.db]
"""

import argparse
import csv
import gzip
import io
import json
import os
import sqlite3
import sys
import tempfile
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

REQUIRED_COLS = {COL_CODE, COL_NAME, COL_KCAL, COL_PROTEIN, COL_FAT, COL_CARBS}

CREATE_SCHEMA = """\
CREATE TABLE products (
    code TEXT PRIMARY KEY,
    product_name TEXT NOT NULL,
    brands TEXT,
    kcal_per_100 REAL NOT NULL,
    protein_per_100 REAL NOT NULL,
    fat_per_100 REAL NOT NULL,
    carbs_per_100 REAL NOT NULL
);

CREATE VIRTUAL TABLE products_fts USING fts5(
    product_name,
    content='products',
    content_rowid='rowid',
    tokenize='unicode61'
);
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


def _create_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA cache_size=-200000")  # 200 MB cache
    conn.executescript(CREATE_SCHEMA)
    return conn


def full_import(output_path: str, csv_path: str | None = None):
    """Download CSV dump and create a fresh database."""
    # Write to a temp file, then atomically rename
    output_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(output_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(suffix=".db", dir=output_dir)
    os.close(fd)

    try:
        conn = _create_db(tmp_path)
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

                batch.append((code, name, brands, kcal, protein, fat, carbs))

                if len(batch) >= BATCH_SIZE:
                    conn.executemany(
                        "INSERT OR IGNORE INTO products VALUES (?, ?, ?, ?, ?, ?, ?)",
                        batch,
                    )
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
                conn.executemany(
                    "INSERT OR IGNORE INTO products VALUES (?, ?, ?, ?, ?, ?, ?)",
                    batch,
                )
                conn.commit()
                inserted += len(batch)

        print("Building FTS5 index...")
        conn.execute("INSERT INTO products_fts(products_fts) VALUES('rebuild')")
        conn.commit()

        print("Running VACUUM...")
        conn.execute("VACUUM")
        conn.close()

        # Atomic rename
        os.replace(tmp_path, output_path)

        elapsed = time.monotonic() - start_time
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(
            f"Done: {inserted:,} products imported, "
            f"{skipped:,} skipped, "
            f"{size_mb:.0f} MB, "
            f"{elapsed:.0f}s"
        )

    except BaseException:
        # Clean up temp file on any failure
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def delta_update(output_path: str):
    """Apply incremental delta updates from OpenFoodFacts."""
    if not os.path.exists(output_path):
        print(f"Database not found at {output_path}, run full import first.")
        sys.exit(1)

    cursor_path = os.path.join(os.path.dirname(output_path), ".off_delta_cursor")

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
        # No cursor or cursor not found — process all available deltas
        new_deltas = delta_files

    if not new_deltas:
        print("Already up to date.")
        return

    print(f"Processing {len(new_deltas)} delta file(s)...")

    conn = sqlite3.connect(output_path)
    conn.execute("PRAGMA journal_mode=WAL")

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

                batch.append((code, name, brands, kcal, protein, fat, carbs))

                if len(batch) >= BATCH_SIZE:
                    _upsert_batch(conn, batch)
                    upserted += len(batch)
                    batch.clear()

        if batch:
            _upsert_batch(conn, batch)
            upserted += len(batch)

        total_upserted += upserted
        print(f"    {upserted:,} products upserted")

        # Update cursor after each successfully processed file
        with open(cursor_path, "w") as f:
            f.write(delta_file)

    # Rebuild FTS after all deltas
    if total_upserted > 0:
        print("Rebuilding FTS5 index...")
        conn.execute("INSERT INTO products_fts(products_fts) VALUES('rebuild')")
        conn.commit()

    conn.close()
    print(f"Done: {total_upserted:,} products upserted from {len(new_deltas)} delta(s)")


def _upsert_batch(conn: sqlite3.Connection, batch: list[tuple]):
    conn.executemany(
        "INSERT OR REPLACE INTO products "
        "(code, product_name, brands, kcal_per_100, protein_per_100, fat_per_100, carbs_per_100) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        batch,
    )
    conn.commit()


def main():
    parser = argparse.ArgumentParser(
        description="Import OpenFoodFacts data into SQLite"
    )
    parser.add_argument(
        "--output",
        default=os.path.join(
            os.path.dirname(__file__), "..", "data", "off_products.db"
        ),
        help="Output database path (default: data/off_products.db)",
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

    output = os.path.abspath(args.output)

    if args.delta:
        delta_update(output)
    else:
        full_import(output, csv_path=args.csv_path)


if __name__ == "__main__":
    main()
