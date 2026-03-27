import json
import sqlite3

import pytest
from starlette.testclient import TestClient

from mcp_health import db, server


@pytest.fixture(autouse=True)
def setup_db(monkeypatch):
    """Use in-memory DB for all server tests."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    monkeypatch.setattr(server, "_conn", conn)
    monkeypatch.setattr(server, "_get_conn", lambda: conn)
    return conn


class TestAddAndSearchProduct:
    def test_add_product(self):
        result = server.add_product(
            "Chicken Breast", 165, 31, 3.6, 0, per_amount=100, force=True
        )
        assert result["status"] == "created"
        assert result["product_id"] is not None
        assert result["normalized"]["kcal_per_100"] == 165

    def test_add_product_with_normalization(self):
        result = server.add_product(
            "Protein Bar", 200, 20, 8, 25, per_amount=60, force=True
        )
        assert result["normalized"]["kcal_per_100"] == pytest.approx(333.3, abs=0.1)

    def test_add_product_warnings(self):
        result = server.add_product("Pure Oil", 900, 0, 100, 0, force=True)
        assert result["warnings"] == []  # exactly 900 is fine
        result = server.add_product("Super Oil", 950, 0, 100, 0, force=True)
        assert "unusually_high_calories" in result["warnings"]

    def test_add_product_detects_duplicate(self):
        server.add_product("Greek Yogurt", 59, 10, 0.7, 3.6, force=True)
        result = server.add_product("Greek Yogurt", 59, 10, 0.7, 3.6)
        assert result["status"] == "already_exists"
        assert result["product"]["name"] == "Greek Yogurt"

    def test_add_product_force_skips_checks(self):
        server.add_product("Test Food", 100, 10, 5, 15, force=True)
        result = server.add_product("Test Food", 100, 10, 5, 15, force=True)
        assert result["status"] == "created"

    def test_add_product_off_cross_check(self, setup_db):
        """If OFF products exist with different nutrition, add_product returns them."""
        conn = setup_db
        # Insert an OFF product
        db.insert_product(
            conn,
            name="Lindt Excellence 70%",
            kcal_per_100=566,
            protein_per_100=9.5,
            fat_per_100=41,
            carbs_per_100=34,
            source="off",
            off_code="3046920028226",
        )
        # Try to add with hallucinated values
        result = server.add_product("Lindt Excellence 70%", 598, 7.9, 43, 41)
        assert result["status"] == "already_exists"

    def test_search_product(self):
        server.add_product("Greek Yogurt", 59, 10, 0.7, 3.6, force=True)
        results = server.search_product("yogurt")
        assert len(results) >= 1
        assert any(r["name"] == "Greek Yogurt" for r in results)

    def test_search_empty(self):
        results = server.search_product("nonexistent_xyz")
        assert results == []


class TestLogMeal:
    def test_log_by_product_id(self):
        p = server.add_product("Rice", 130, 2.7, 0.3, 28, force=True)
        result = server.log_meal(
            items=[{"product_id": p["product_id"], "weight_grams": 200}],
            meal_type="lunch",
        )
        assert result["meal_id"] is not None
        assert result["meal_total"]["kcal"] == 260.0
        assert result["meal_total"]["protein"] == 5.4

    def test_log_by_query(self):
        server.add_product("Гречка варёная", 110, 4, 1, 21, force=True)
        # Increment usage so it auto-resolves
        conn = server._get_conn()
        db.increment_product_usage(conn, 1)

        result = server.log_meal(
            items=[{"query": "Гречка", "weight_grams": 150}],
            meal_type="lunch",
        )
        assert result["meal_id"] is not None
        assert result["resolved_items"][0]["name"] == "Гречка варёная"

    def test_log_query_not_found(self):
        result = server.log_meal(
            items=[{"query": "xyznonexistent", "weight_grams": 100}],
        )
        assert "not_found_items" in result
        assert result["not_found_items"][0]["query"] == "xyznonexistent"

    def test_log_rejects_adhoc_nutrition(self):
        with pytest.raises(ValueError, match="product_id.*query"):
            server.log_meal(
                items=[{
                    "name": "Soup",
                    "kcal": 80, "protein": 5, "fat": 3, "carbs": 8,
                    "weight_grams": 300,
                }],
            )

    def test_daily_totals_accumulate(self):
        p = server.add_product("Bread", 265, 9, 3.2, 49, force=True)
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
        assert result["trend"]["week_ago"] == 85.0
        assert result["trend"]["change_week"] == -1.5


class TestDailySummary:
    def test_with_goals(self):
        server.update_goals(daily_kcal=2000, protein_g=150, fat_g=70, carbs_g=200)
        p = server.add_product("Oats", 389, 16.9, 6.9, 66.3, force=True)
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
        p = server.add_product("Pasta", 131, 5, 1.1, 25, force=True)
        r1 = server.log_meal(
            items=[{"product_id": p["product_id"], "weight_grams": 200}],
            timestamp="2026-03-20T12:00:00",
        )
        server.log_meal(
            items=[{"product_id": p["product_id"], "weight_grams": 100}],
            timestamp="2026-03-20T18:00:00",
        )
        result = server.delete_meal(r1["meal_id"])
        assert result["status"] == "deleted"
        assert result["updated_daily_totals"]["kcal"] == pytest.approx(131.0, abs=0.1)

    def test_delete_nonexistent(self):
        result = server.delete_meal(9999)
        assert result["status"] == "not_found"


class TestDeleteMealItem:
    def _log_two_item_meal(self):
        p1 = server.add_product("Apple", 52, 0.3, 0.2, 14, force=True)
        p2 = server.add_product("Banana", 89, 1.1, 0.3, 23, force=True)
        result = server.log_meal(
            items=[
                {"product_id": p1["product_id"], "weight_grams": 150},
                {"product_id": p2["product_id"], "weight_grams": 120},
            ],
            timestamp="2026-03-20T12:00:00",
        )
        conn = server._get_conn()
        items = conn.execute(
            "SELECT id FROM meal_items WHERE meal_id = ? ORDER BY id",
            (result["meal_id"],),
        ).fetchall()
        return result["meal_id"], [i["id"] for i in items]

    def test_delete_item_keeps_meal(self):
        meal_id, item_ids = self._log_two_item_meal()
        result = server.delete_meal_item(item_ids[0])
        assert result["status"] == "deleted"
        assert result["meal_deleted"] is False
        assert result["meal_id"] == meal_id

    def test_delete_last_item_deletes_meal(self):
        meal_id, item_ids = self._log_two_item_meal()
        server.delete_meal_item(item_ids[0])
        result = server.delete_meal_item(item_ids[1])
        assert result["status"] == "deleted"
        assert result["meal_deleted"] is True

    def test_delete_nonexistent_item(self):
        result = server.delete_meal_item(99999)
        assert result["status"] == "not_found"


class TestUpdateMealItem:
    def test_update_product_item(self):
        p = server.add_product("Rice", 130, 2.7, 0.3, 28, force=True)
        meal = server.log_meal(
            items=[{"product_id": p["product_id"], "weight_grams": 200}],
            timestamp="2026-03-20T12:00:00",
        )
        conn = server._get_conn()
        item_id = conn.execute(
            "SELECT id FROM meal_items WHERE meal_id = ?", (meal["meal_id"],)
        ).fetchone()["id"]
        result = server.update_meal_item(item_id, weight_grams=300)
        assert result["status"] == "updated"
        assert result["item"]["weight_grams"] == 300
        assert result["item"]["kcal"] == pytest.approx(390.0, abs=0.1)
        assert result["item"]["protein"] == pytest.approx(8.1, abs=0.1)

    def test_update_nonexistent_item(self):
        result = server.update_meal_item(99999, weight_grams=100)
        assert result["status"] == "not_found"


class TestWeeklyReportAndTrends:
    def test_weekly_report(self):
        server.update_goals(daily_kcal=2000)
        p = server.add_product("Egg", 155, 13, 11, 1.1, force=True)
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
        resp = client.get("/metrics")
        assert resp.status_code == 200


class TestGetRecentMeals:
    def test_returns_recent_breakfasts(self):
        p = server.add_product("Oats", 389, 16.9, 6.9, 66.3, force=True)
        server.log_meal(
            items=[{"product_id": p["product_id"], "weight_grams": 80}],
            meal_type="breakfast",
            timestamp="2026-03-24T12:00:00+00:00",
        )
        server.log_meal(
            items=[{"product_id": p["product_id"], "weight_grams": 80}],
            meal_type="breakfast",
            timestamp="2026-03-25T12:00:00+00:00",
        )
        server.log_meal(
            items=[{"product_id": p["product_id"], "weight_grams": 100}],
            meal_type="lunch",
            timestamp="2026-03-25T16:00:00+00:00",
        )
        result = server.get_recent_meals(meal_type="breakfast", days=7)
        assert len(result["meals"]) == 2
        assert all(m["meal_type"] == "breakfast" for m in result["meals"])
        items = result["meals"][0]["items"]
        assert items[0]["product_id"] == p["product_id"]
        assert items[0]["weight_grams"] == 80

    def test_no_filter_returns_all(self):
        p = server.add_product("Egg", 155, 13, 11, 1.1, force=True)
        server.log_meal(
            items=[{"product_id": p["product_id"], "weight_grams": 100}],
            meal_type="breakfast",
            timestamp="2026-03-25T12:00:00+00:00",
        )
        server.log_meal(
            items=[{"product_id": p["product_id"], "weight_grams": 200}],
            meal_type="lunch",
            timestamp="2026-03-25T16:00:00+00:00",
        )
        result = server.get_recent_meals(days=7)
        assert len(result["meals"]) == 2


class TestSetProductServing:
    def test_set_serving(self):
        p = server.add_product("Protein Powder", 400, 80, 5, 10, force=True)
        result = server.set_product_serving(p["product_id"], 39.0, "1 scoop")
        assert result["status"] == "updated"
        assert result["default_serving_grams"] == 39.0
        assert result["serving_label"] == "1 scoop"

        results = server.search_product("protein powder")
        matching = [r for r in results if r["name"] == "Protein Powder"]
        assert matching[0]["default_serving_grams"] == 39.0

    def test_not_found(self):
        result = server.set_product_serving(99999, 100.0)
        assert result["status"] == "not_found"


class TestAutoLearnServing:
    def test_auto_learns_after_3_uses(self):
        p = server.add_product("Protein Scoop", 400, 80, 5, 10, force=True)
        pid = p["product_id"]

        for i in range(3):
            server.log_meal(
                items=[{"product_id": pid, "weight_grams": 39}],
                timestamp=f"2026-03-{20 + i:02d}T12:00:00+00:00",
            )

        conn = server._get_conn()
        product = db.get_product(conn, pid)
        assert product["default_serving_grams"] == 39.0

    def test_no_auto_learn_with_varied_weights(self):
        p = server.add_product("Mixed Weight", 100, 10, 5, 15, force=True)
        pid = p["product_id"]

        server.log_meal(
            items=[{"product_id": pid, "weight_grams": 100}],
            timestamp="2026-03-20T12:00:00+00:00",
        )
        server.log_meal(
            items=[{"product_id": pid, "weight_grams": 200}],
            timestamp="2026-03-21T12:00:00+00:00",
        )
        server.log_meal(
            items=[{"product_id": pid, "weight_grams": 300}],
            timestamp="2026-03-22T12:00:00+00:00",
        )

        conn = server._get_conn()
        product = db.get_product(conn, pid)
        assert product["default_serving_grams"] is None


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
        assert resp.status_code != 401


class TestLogActivity:
    def test_log_activity(self):
        result = server.log_activity(
            activity_type="Running",
            start_at="2026-03-20T08:00:00+00:00",
            duration_min=45,
            kcal_burned=350,
            distance_m=5000,
        )
        assert result["status"] == "logged"
        assert result["activity_type"] == "Running"

    def test_activity_in_daily_summary(self):
        server.log_activity(
            activity_type="Running",
            start_at="2026-03-20T08:00:00+00:00",
            duration_min=45,
            kcal_burned=350,
        )
        summary = server.get_daily_summary(date="2026-03-20")
        assert "activity" in summary
        assert summary["activity"]["count"] == 1
        assert summary["activity"]["total_kcal_burned"] == 350

    def test_activity_summary(self):
        server.log_activity(
            activity_type="Running",
            start_at="2026-03-20T08:00:00+00:00",
            duration_min=45,
            kcal_burned=350,
        )
        server.log_activity(
            activity_type="Walking",
            start_at="2026-03-20T18:00:00+00:00",
            duration_min=30,
            kcal_burned=150,
        )
        result = server.get_activity_summary(date="2026-03-20")
        assert result["summary"]["count"] == 2
        assert len(result["activities"]) == 2


class TestCycleTracking:
    def test_log_cycle_event(self):
        result = server.log_cycle_event(
            event_type="flow", date="2026-03-01", value="medium"
        )
        assert result["status"] == "logged"
        assert result["event_type"] == "flow"

    def test_cycle_summary_with_prediction(self):
        for day in [1, 2, 3, 4]:
            server.log_cycle_event(
                event_type="flow", date=f"2026-03-{day:02d}", value="medium"
            )
        for day in [29, 30, 31]:
            server.log_cycle_event(
                event_type="flow", date=f"2026-03-{day:02d}", value="medium"
            )
        server.log_cycle_event(event_type="flow", date="2026-04-01", value="light")

        result = server.get_cycle_summary(months=3)
        assert result["cycles_detected"] >= 1
        assert result["avg_cycle_length"] is not None
        assert "predicted_next_period" in result

    def test_cycle_in_weekly_report(self):
        server.log_cycle_event(event_type="flow", date="2026-03-17", value="medium")
        report = server.get_weekly_report(week_start="2026-03-16")
        assert "cycle_events" in report
        assert len(report["cycle_events"]) == 1


class TestHealthImportEndpoint:
    def _post_import(self, payload, token=None):
        client = TestClient(server.app, raise_server_exceptions=False)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return client.post(
            "/api/health-import", content=json.dumps(payload), headers=headers
        )

    def test_no_auth_returns_401(self):
        resp = self._post_import({"data": {}})
        assert resp.status_code == 401

    def test_wrong_auth_returns_401(self):
        resp = self._post_import({"data": {}}, token="wrong")
        assert resp.status_code == 401

    def test_invalid_json(self):
        client = TestClient(server.app, raise_server_exceptions=False)
        resp = client.post(
            "/api/health-import",
            content="not json",
            headers={
                "Authorization": f"Bearer {server.config.AUTH_TOKEN}",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 400

    def test_import_weight(self):
        payload = {
            "data": {
                "metrics": [
                    {
                        "name": "Body Mass",
                        "units": "kg",
                        "data": [
                            {"date": "2026-03-20T08:00:00+00:00", "qty": 72.5},
                        ],
                    }
                ]
            }
        }
        resp = self._post_import(payload, token=server.config.AUTH_TOKEN)
        assert resp.status_code == 200
        body = resp.json()
        assert body["imported"]["weight"] == 1

        conn = server._get_conn()
        w = db.get_weight_for_date(conn, "2026-03-20")
        assert w is not None
        assert w["weight_kg"] == 72.5

    def test_import_workouts(self):
        payload = {
            "data": {
                "workouts": [
                    {
                        "name": "Running",
                        "start": "2026-03-20T08:00:00+00:00",
                        "end": "2026-03-20T08:45:00+00:00",
                        "duration": 2700,
                        "activeEnergyBurned": {"qty": 350, "units": "kcal"},
                        "distance": {"qty": 5000, "units": "m"},
                        "heartRate": {
                            "avg": {"qty": 145, "units": "bpm"},
                            "min": {"qty": 120, "units": "bpm"},
                            "max": {"qty": 170, "units": "bpm"},
                        },
                    }
                ]
            }
        }
        resp = self._post_import(payload, token=server.config.AUTH_TOKEN)
        assert resp.status_code == 200
        body = resp.json()
        assert body["imported"]["activities"] == 1

    def test_import_cycle_tracking(self):
        payload = {
            "data": {
                "cycleTracking": [
                    {
                        "start": "2026-03-01 12:00:00 -0400",
                        "name": "Menstrual Flow",
                        "value": "Heavy",
                        "isCycleStart": True,
                    },
                    {
                        "start": "2026-03-10T00:00:00+00:00",
                        "name": "Ovulation Test Result",
                        "value": "Positive",
                    },
                ]
            }
        }
        resp = self._post_import(payload, token=server.config.AUTH_TOKEN)
        assert resp.status_code == 200
        body = resp.json()
        assert body["imported"]["cycle_events"] == 2

    def test_method_not_allowed(self):
        client = TestClient(server.app, raise_server_exceptions=False)
        resp = client.get("/api/health-import")
        assert resp.status_code == 405

    def test_hae_date_format(self):
        payload = {
            "data": {
                "metrics": [
                    {
                        "name": "Body Mass",
                        "units": "kg",
                        "data": [
                            {"date": "2026-03-20 08:00:00 -0400", "qty": 72.5},
                        ],
                    }
                ]
            }
        }
        resp = self._post_import(payload, token=server.config.AUTH_TOKEN)
        assert resp.status_code == 200
        assert resp.json()["imported"]["weight"] == 1

    def test_import_workout_with_flat_heart_rate(self):
        payload = {
            "data": {
                "workouts": [
                    {
                        "name": "Walking",
                        "start": "2026-03-20T18:00:00+00:00",
                        "duration": 1800,
                        "activeEnergyBurned": 150,
                        "distance": 2000,
                        "heartRate": {"avg": 110},
                    }
                ]
            }
        }
        resp = self._post_import(payload, token=server.config.AUTH_TOKEN)
        assert resp.status_code == 200
        assert resp.json()["imported"]["activities"] == 1

    def test_import_cycle_with_cycle_start(self):
        payload = {
            "data": {
                "cycleTracking": [
                    {
                        "start": "2026-03-15T00:00:00+00:00",
                        "name": "Menstrual Flow",
                        "value": "Unspecified",
                        "isCycleStart": True,
                    },
                ]
            }
        }
        resp = self._post_import(payload, token=server.config.AUTH_TOKEN)
        assert resp.status_code == 200
        assert resp.json()["imported"]["cycle_events"] == 1
