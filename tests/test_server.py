import sqlite3
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

import db
import server


@pytest.fixture(autouse=True)
def setup_db(monkeypatch):
    """Use in-memory DB for all server tests."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(server, "_conn", conn)
    monkeypatch.setattr(server, "_get_conn", lambda: conn)
    return conn


class TestAddAndSearchProduct:
    def test_add_product(self):
        result = server.add_product("Chicken Breast", 165, 31, 3.6, 0, per_amount=100)
        assert result["status"] == "created"
        assert result["product_id"] is not None
        assert result["normalized"]["kcal_per_100"] == 165

    def test_add_product_with_normalization(self):
        result = server.add_product("Protein Bar", 200, 20, 8, 25, per_amount=60)
        assert result["normalized"]["kcal_per_100"] == pytest.approx(333.3, abs=0.1)

    def test_add_product_warnings(self):
        result = server.add_product("Pure Oil", 900, 0, 100, 0)
        assert result["warnings"] == []  # exactly 900 is fine
        result = server.add_product("Super Oil", 950, 0, 100, 0)
        assert "unusually_high_calories" in result["warnings"]

    def test_search_product(self):
        server.add_product("Greek Yogurt", 59, 10, 0.7, 3.6)
        results = server.search_product("yogurt", include_off=False)
        assert len(results) == 1
        assert results[0]["name"] == "Greek Yogurt"

    def test_search_empty(self):
        results = server.search_product("nonexistent_xyz")
        assert results == []


class TestLookupProduct:
    @patch("openfoodfacts.lookup_barcode")
    def test_lookup_from_off_and_cache(self, mock_lookup):
        mock_lookup.return_value = {
            "name": "Nutella",
            "brands": "Ferrero",
            "kcal_per_100": 539.0,
            "protein_per_100": 6.3,
            "fat_per_100": 30.9,
            "carbs_per_100": 57.5,
            "barcode": "3017620422003",
        }
        result = server.lookup_product("3017620422003", save=True)
        assert result["source"] == "openfoodfacts"
        assert result["product"]["name"] == "Nutella"
        assert result["product"]["id"] is not None

        # Second call should hit local DB
        result2 = server.lookup_product("3017620422003")
        assert result2["source"] == "local"
        assert result2["product"]["name"] == "Nutella"

    @patch("openfoodfacts.lookup_barcode")
    def test_lookup_not_found(self, mock_lookup):
        mock_lookup.return_value = None
        result = server.lookup_product("0000000000000")
        assert result["source"] is None
        assert result["product"] is None

    @patch("openfoodfacts.lookup_barcode")
    def test_lookup_no_save(self, mock_lookup):
        mock_lookup.return_value = {
            "name": "Test",
            "brands": None,
            "kcal_per_100": 100.0,
            "protein_per_100": 5.0,
            "fat_per_100": 3.0,
            "carbs_per_100": 10.0,
            "barcode": "999",
        }
        result = server.lookup_product("999", save=False)
        assert result["source"] == "openfoodfacts"
        assert "id" not in result["product"]


class TestSearchProductWithOFF:
    @patch("openfoodfacts.search")
    def test_includes_off_results(self, mock_search):
        mock_search.return_value = [
            {
                "name": "OFF Yogurt",
                "brands": "Brand",
                "kcal_per_100": 60.0,
                "protein_per_100": 4.0,
                "fat_per_100": 1.5,
                "carbs_per_100": 7.0,
                "barcode": "555",
            }
        ]
        results = server.search_product("yogurt", limit=5, include_off=True)
        sources = [r["source"] for r in results]
        assert "openfoodfacts" in sources

    @patch("openfoodfacts.search")
    def test_off_disabled(self, mock_search):
        results = server.search_product("yogurt", limit=5, include_off=False)
        mock_search.assert_not_called()
        assert all(r["source"] == "local" for r in results)

    @patch("openfoodfacts.search")
    def test_deduplicates_by_barcode(self, mock_search):
        server.add_product("Local Yogurt", 59, 10, 0.7, 3.6, barcode="111")
        mock_search.return_value = [
            {
                "name": "OFF Yogurt",
                "brands": "Brand",
                "kcal_per_100": 59.0,
                "protein_per_100": 10.0,
                "fat_per_100": 0.7,
                "carbs_per_100": 3.6,
                "barcode": "111",
            }
        ]
        results = server.search_product("yogurt", limit=5, include_off=True)
        assert len([r for r in results if r["barcode"] == "111"]) == 1


class TestLogMeal:
    def test_log_by_product_id(self):
        p = server.add_product("Rice", 130, 2.7, 0.3, 28)
        result = server.log_meal(
            items=[{"product_id": p["product_id"], "weight_grams": 200}],
            meal_type="lunch",
        )
        assert result["meal_id"] is not None
        assert result["meal_total"]["kcal"] == 260.0
        assert result["meal_total"]["protein"] == 5.4

    def test_log_adhoc(self):
        result = server.log_meal(
            items=[
                {
                    "name": "Homemade Soup",
                    "kcal": 80,
                    "protein": 5,
                    "fat": 3,
                    "carbs": 8,
                    "weight_grams": 300,
                    "per_amount": 100,
                }
            ],
        )
        assert result["meal_total"]["kcal"] == 240.0

    def test_log_adhoc_save_product(self):
        server.log_meal(
            items=[
                {
                    "name": "My Special Sauce",
                    "kcal": 50,
                    "protein": 1,
                    "fat": 3,
                    "carbs": 5,
                    "weight_grams": 30,
                    "per_amount": 100,
                    "save_product": True,
                }
            ],
        )
        found = server.search_product("special sauce", include_off=False)
        assert len(found) == 1

    def test_daily_totals_accumulate(self):
        p = server.add_product("Bread", 265, 9, 3.2, 49)
        server.log_meal(
            items=[{"product_id": p["product_id"], "weight_grams": 100}],
            timestamp="2026-03-20T08:00:00",
        )
        server.log_meal(
            items=[{"product_id": p["product_id"], "weight_grams": 50}],
            timestamp="2026-03-20T12:00:00",
        )
        result = server.get_daily_summary(date="2026-03-20")
        assert result["totals"]["kcal"] == pytest.approx(397.5, abs=0.1)


class TestLogWeight:
    def test_log_and_trend(self):
        server.log_weight(85.0, date="2026-03-01")
        server.log_weight(84.0, date="2026-03-08")
        result = server.log_weight(83.5, date="2026-03-08")
        assert result["trend"]["current"] == 83.5
        # week_ago is 2026-03-01
        assert result["trend"]["week_ago"] == 85.0
        assert result["trend"]["change_week"] == -1.5


class TestDailySummary:
    def test_with_goals(self):
        server.update_goals(daily_kcal=2000, protein_g=150, fat_g=70, carbs_g=200)
        p = server.add_product("Oats", 389, 16.9, 6.9, 66.3)
        server.log_meal(
            items=[{"product_id": p["product_id"], "weight_grams": 100}],
            timestamp="2026-03-20T08:00:00",
        )
        summary = server.get_daily_summary(date="2026-03-20")
        assert summary["targets"]["kcal"] == 2000
        assert summary["remaining"]["kcal"] == pytest.approx(1611.0, abs=0.1)


class TestUpdateGoals:
    def test_merge_with_existing(self):
        server.update_goals(daily_kcal=2000, protein_g=150)
        result = server.update_goals(protein_g=160)
        assert result["goals"]["daily_kcal"] == 2000  # preserved
        assert result["goals"]["protein_g"] == 160  # updated


class TestDeleteMeal:
    def test_delete_updates_totals(self):
        p = server.add_product("Pasta", 131, 5, 1.1, 25)
        r1 = server.log_meal(
            items=[{"product_id": p["product_id"], "weight_grams": 200}],
            timestamp="2026-03-20T12:00:00",
        )
        server.log_meal(
            items=[{"product_id": p["product_id"], "weight_grams": 100}],
            timestamp="2026-03-20T18:00:00",
        )
        # Delete first meal
        result = server.delete_meal(r1["meal_id"])
        assert result["status"] == "deleted"
        # Only second meal remains
        assert result["updated_daily_totals"]["kcal"] == pytest.approx(131.0, abs=0.1)

    def test_delete_nonexistent(self):
        result = server.delete_meal(9999)
        assert result["status"] == "not_found"


class TestWeeklyReportAndTrends:
    def test_weekly_report(self):
        server.update_goals(daily_kcal=2000)
        p = server.add_product("Egg", 155, 13, 11, 1.1)
        for day in range(16, 23):
            server.log_meal(
                items=[{"product_id": p["product_id"], "weight_grams": 200}],
                timestamp=f"2026-03-{day:02d}T08:00:00",
            )
            server.log_weight(85.0 - (day - 16) * 0.1, date=f"2026-03-{day:02d}")

        report = server.get_weekly_report(week_start="2026-03-16")
        assert report["period"]["start"] == "2026-03-16"
        assert len(report["daily_breakdown"]) == 7
        assert report["weight_trend"]["data_points"] == 7
        assert len(report["top_products"]) >= 1

    def test_trends(self):
        for i in range(5):
            server.log_weight(85.0 - i * 0.2, date=f"2026-03-{16 + i:02d}")
        result = server.get_trends(days=30)
        assert "weight" in result
        assert result["weight"]["change"] == pytest.approx(-0.8, abs=0.1)


class TestMetricsEndpoint:
    """Verify /metrics is accessible without auth."""

    def test_metrics_returns_prometheus_format(self):
        client = TestClient(server.app, raise_server_exceptions=False)
        resp = client.get("/metrics")
        assert resp.status_code == 200
        body = resp.text
        assert "mcp_tool_calls_total" in body or "mcp_health_info" in body

    def test_metrics_no_auth_required(self):
        client = TestClient(server.app, raise_server_exceptions=False)
        # No Authorization header — should still work
        resp = client.get("/metrics")
        assert resp.status_code == 200


class TestLegacyBearerAuth:
    """Verify legacy Bearer auth works when OAUTH_ISSUER is not set."""

    def test_no_token_returns_401(self):
        assert not server._oauth_mode, "These tests assume OAUTH_ISSUER is unset"
        client = TestClient(server.app, raise_server_exceptions=False)
        resp = client.get("/mcp")
        assert resp.status_code == 401

    def test_wrong_token_returns_401(self):
        client = TestClient(server.app, raise_server_exceptions=False)
        resp = client.get("/mcp", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

    def test_valid_token_passes(self):
        client = TestClient(server.app, raise_server_exceptions=False)
        resp = client.get(
            "/mcp", headers={"Authorization": f"Bearer {server.config.AUTH_TOKEN}"}
        )
        # Should not be 401 — it may be 405 or other depending on endpoint,
        # but auth passed
        assert resp.status_code != 401
