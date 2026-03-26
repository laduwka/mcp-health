import sqlite3
import tempfile
import os

from mcp_health import openfoodfacts, config


def _create_test_db(path: str):
    """Create a minimal OFF database with test data."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """\
        CREATE TABLE products (
            code TEXT PRIMARY KEY,
            product_name TEXT NOT NULL,
            brands TEXT,
            kcal_per_100 REAL NOT NULL,
            protein_per_100 REAL NOT NULL,
            fat_per_100 REAL NOT NULL,
            carbs_per_100 REAL NOT NULL,
            countries_tags TEXT
        );

        CREATE VIRTUAL TABLE products_fts USING fts5(
            product_name,
            content='products',
            content_rowid='rowid',
            tokenize='unicode61'
        );
        """
    )
    conn.executemany(
        "INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "3017620422003",
                "Nutella",
                "Ferrero",
                539.0,
                6.3,
                30.9,
                57.5,
                "en:france,en:canada",
            ),
            ("123", "Chocolate Bar", "Brand A", 545.0, 5.0, 30.0, 60.0, "en:canada"),
            (
                "789",
                "Another Chocolate",
                "Brand C",
                500.0,
                7.0,
                28.0,
                55.0,
                "en:russia",
            ),
            ("999", "Banana Chips", None, 520.0, 2.0, 28.0, 65.0, None),
        ],
    )
    conn.execute("INSERT INTO products_fts(products_fts) VALUES('rebuild')")
    conn.commit()
    conn.close()


class TestLookupBarcode:
    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db_path = os.path.join(self._tmpdir, "off_products.db")
        _create_test_db(self._db_path)
        self._orig_path = config.OFF_DB_PATH
        config.OFF_DB_PATH = self._db_path
        # Reset cached connection
        openfoodfacts._conn = None

    def teardown_method(self):
        openfoodfacts._conn = None
        config.OFF_DB_PATH = self._orig_path
        os.unlink(self._db_path)
        os.rmdir(self._tmpdir)

    def test_success(self):
        result = openfoodfacts.lookup_barcode("3017620422003")
        assert result is not None
        assert result["name"] == "Nutella"
        assert result["kcal_per_100"] == 539.0
        assert result["protein_per_100"] == 6.3
        assert result["fat_per_100"] == 30.9
        assert result["carbs_per_100"] == 57.5
        assert result["barcode"] == "3017620422003"

    def test_not_found(self):
        result = openfoodfacts.lookup_barcode("0000000000000")
        assert result is None

    def test_no_database(self):
        config.OFF_DB_PATH = "/nonexistent/off_products.db"
        openfoodfacts._conn = None
        result = openfoodfacts.lookup_barcode("123")
        assert result is None


class TestSearch:
    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db_path = os.path.join(self._tmpdir, "off_products.db")
        _create_test_db(self._db_path)
        self._orig_path = config.OFF_DB_PATH
        config.OFF_DB_PATH = self._db_path
        openfoodfacts._conn = None

    def teardown_method(self):
        openfoodfacts._conn = None
        config.OFF_DB_PATH = self._orig_path
        os.unlink(self._db_path)
        os.rmdir(self._tmpdir)

    def test_finds_matching_products(self):
        results = openfoodfacts.search("chocolate", limit=10)
        assert len(results) == 2
        names = [r["name"] for r in results]
        assert "Chocolate Bar" in names
        assert "Another Chocolate" in names

    def test_respects_limit(self):
        results = openfoodfacts.search("chocolate", limit=1)
        assert len(results) == 1

    def test_no_database(self):
        config.OFF_DB_PATH = "/nonexistent/off_products.db"
        openfoodfacts._conn = None
        results = openfoodfacts.search("anything")
        assert results == []

    def test_no_results(self):
        results = openfoodfacts.search("xyznonexistent")
        assert results == []

    def test_country_filter(self):
        results = openfoodfacts.search("chocolate", limit=10, country="en:canada")
        assert len(results) == 1
        assert results[0]["name"] == "Chocolate Bar"

    def test_country_filter_no_match(self):
        results = openfoodfacts.search("banana", limit=10, country="en:canada")
        assert len(results) == 0

    def test_country_filter_none_passes_all(self):
        results = openfoodfacts.search("chocolate", limit=10, country=None)
        assert len(results) == 2
