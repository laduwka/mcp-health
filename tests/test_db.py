import sqlite3

import pytest
from mcp_health.db import (
    delete_meal,
    get_activities,
    get_activity_summary,
    get_current_goals,
    get_cycle_events,
    get_cycle_flow_dates,
    get_daily_totals,
    get_date_range_totals,
    get_meal,
    get_meals_for_date,
    get_most_common_serving,
    get_product,
    get_product_by_barcode,
    get_recent_meals_by_type,
    get_top_products,
    get_weight_for_date,
    get_weight_range,
    increment_product_usage,
    init_db,
    insert_goals,
    insert_meal,
    insert_product,
    search_products,
    update_product_serving,
    upsert_activity,
    upsert_cycle_event,
    upsert_weight,
)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys=ON")
    c.row_factory = sqlite3.Row
    init_db(c)
    return c


class TestProducts:
    def test_insert_and_get(self, conn):
        pid = insert_product(
            conn,
            name="Chicken Breast",
            kcal_per_100=165,
            protein_per_100=31,
            fat_per_100=3.6,
            carbs_per_100=0,
            created_at="2026-01-01T00:00:00",
        )
        assert pid is not None
        product = get_product(conn, pid)
        assert product["name"] == "Chicken Breast"
        assert product["name_lower"] == "chicken breast"
        assert product["kcal_per_100"] == 165

    def test_search(self, conn):
        insert_product(
            conn,
            name="Greek Yogurt",
            kcal_per_100=59,
            protein_per_100=10,
            fat_per_100=0.7,
            carbs_per_100=3.6,
            created_at="2026-01-01T00:00:00",
        )
        insert_product(
            conn,
            name="Yogurt Drink",
            kcal_per_100=45,
            protein_per_100=3,
            fat_per_100=1,
            carbs_per_100=7,
            created_at="2026-01-01T00:00:00",
        )
        results = search_products(conn, "yogurt")
        assert len(results) == 2

    def test_get_by_barcode(self, conn):
        insert_product(
            conn,
            name="Nutella",
            kcal_per_100=539,
            protein_per_100=6.3,
            fat_per_100=30.9,
            carbs_per_100=57.5,
            barcode="3017620422003",
            created_at="2026-01-01T00:00:00",
        )
        result = get_product_by_barcode(conn, "3017620422003")
        assert result is not None
        assert result["name"] == "Nutella"
        assert result["barcode"] == "3017620422003"

    def test_get_by_barcode_not_found(self, conn):
        assert get_product_by_barcode(conn, "0000000000000") is None

    def test_increment_usage(self, conn):
        pid = insert_product(
            conn,
            name="Rice",
            kcal_per_100=130,
            protein_per_100=2.7,
            fat_per_100=0.3,
            carbs_per_100=28,
            created_at="2026-01-01T00:00:00",
        )
        increment_product_usage(conn, pid)
        product = get_product(conn, pid)
        assert product["usage_count"] == 1


class TestMeals:
    def test_insert_and_get(self, conn):
        insert_meal(
            conn,
            "lunch",
            "test meal",
            "2026-03-20T12:00:00+00:00",
            [
                {
                    "name": "Rice",
                    "weight_grams": 200,
                    "kcal": 260,
                    "protein": 5.4,
                    "fat": 0.6,
                    "carbs": 56,
                },
            ],
        )
        meals = get_meals_for_date(conn, "2026-03-20")
        assert len(meals) == 1
        assert len(meals[0]["items"]) == 1
        assert meals[0]["items"][0]["kcal"] == 260

    def test_delete_cascade(self, conn):
        meal_id = insert_meal(
            conn,
            "dinner",
            None,
            "2026-03-20T18:00:00+00:00",
            [
                {
                    "name": "Pasta",
                    "weight_grams": 150,
                    "kcal": 200,
                    "protein": 7,
                    "fat": 1,
                    "carbs": 40,
                },
            ],
        )
        assert delete_meal(conn, meal_id)
        assert get_meal(conn, meal_id) is None
        # meal_items should also be gone
        row = conn.execute(
            "SELECT COUNT(*) as c FROM meal_items WHERE meal_id = ?", (meal_id,)
        ).fetchone()
        assert row["c"] == 0

    def test_delete_nonexistent(self, conn):
        assert not delete_meal(conn, 9999)


class TestWeight:
    def test_upsert(self, conn):
        upsert_weight(conn, 85.0, "2026-03-20")
        w = get_weight_for_date(conn, "2026-03-20")
        assert w["weight_kg"] == 85.0

        upsert_weight(conn, 84.5, "2026-03-20")
        w = get_weight_for_date(conn, "2026-03-20")
        assert w["weight_kg"] == 84.5

    def test_range(self, conn):
        upsert_weight(conn, 85.0, "2026-03-18")
        upsert_weight(conn, 84.5, "2026-03-19")
        upsert_weight(conn, 84.0, "2026-03-20")
        data = get_weight_range(conn, "2026-03-18", "2026-03-20")
        assert len(data) == 3
        assert data[0]["weight_kg"] == 85.0
        assert data[2]["weight_kg"] == 84.0


class TestGoals:
    def test_insert_and_get(self, conn):
        insert_goals(
            conn,
            daily_kcal=2000,
            protein_g=150,
            fat_g=70,
            carbs_g=200,
            set_at="2026-03-20T00:00:00",
        )
        goals = get_current_goals(conn)
        assert goals["daily_kcal"] == 2000

    def test_latest_wins(self, conn):
        insert_goals(conn, daily_kcal=2000, set_at="2026-03-19T00:00:00")
        insert_goals(conn, daily_kcal=1800, set_at="2026-03-20T00:00:00")
        goals = get_current_goals(conn)
        assert goals["daily_kcal"] == 1800


class TestAggregation:
    def test_daily_totals(self, conn):
        insert_meal(
            conn,
            "lunch",
            None,
            "2026-03-20T12:00:00+00:00",
            [
                {
                    "name": "A",
                    "weight_grams": 100,
                    "kcal": 200,
                    "protein": 10,
                    "fat": 5,
                    "carbs": 30,
                },
                {
                    "name": "B",
                    "weight_grams": 50,
                    "kcal": 100,
                    "protein": 5,
                    "fat": 3,
                    "carbs": 15,
                },
            ],
        )
        totals = get_daily_totals(conn, "2026-03-20")
        assert totals["kcal"] == 300
        assert totals["protein"] == 15

    def test_date_range_totals(self, conn):
        insert_meal(
            conn,
            "lunch",
            None,
            "2026-03-19T12:00:00+00:00",
            [
                {
                    "name": "A",
                    "weight_grams": 100,
                    "kcal": 200,
                    "protein": 10,
                    "fat": 5,
                    "carbs": 30,
                },
            ],
        )
        insert_meal(
            conn,
            "lunch",
            None,
            "2026-03-20T12:00:00+00:00",
            [
                {
                    "name": "B",
                    "weight_grams": 100,
                    "kcal": 300,
                    "protein": 15,
                    "fat": 8,
                    "carbs": 40,
                },
            ],
        )
        data = get_date_range_totals(conn, "2026-03-19", "2026-03-20")
        assert len(data) == 2

    def test_top_products(self, conn):
        for i in range(3):
            insert_meal(
                conn,
                "snack",
                None,
                f"2026-03-20T{10 + i}:00:00+00:00",
                [
                    {
                        "name": "Apple",
                        "product_id": None,
                        "weight_grams": 150,
                        "kcal": 78,
                        "protein": 0.4,
                        "fat": 0.2,
                        "carbs": 19,
                    },
                ],
            )
        top = get_top_products(conn, "2026-03-20", "2026-03-20")
        assert len(top) >= 1
        assert top[0]["name"] == "Apple"
        assert top[0]["times_used"] == 3


class TestProductServing:
    def test_update_and_search(self, conn):
        pid = insert_product(
            conn,
            name="Protein Powder",
            kcal_per_100=400,
            protein_per_100=80,
            fat_per_100=5,
            carbs_per_100=10,
            created_at="2026-01-01T00:00:00",
        )
        update_product_serving(conn, pid, 39.0, "1 scoop")
        product = get_product(conn, pid)
        assert product["default_serving_grams"] == 39.0
        assert product["serving_label"] == "1 scoop"

        results = search_products(conn, "protein")
        assert results[0]["default_serving_grams"] == 39.0
        assert results[0]["serving_label"] == "1 scoop"

    def test_schema_migration_idempotent(self, conn):
        # Calling init_db twice should not fail
        init_db(conn)
        pid = insert_product(
            conn,
            name="Test",
            kcal_per_100=100,
            protein_per_100=10,
            fat_per_100=5,
            carbs_per_100=15,
            created_at="2026-01-01T00:00:00",
        )
        assert get_product(conn, pid) is not None


class TestMostCommonServing:
    def test_returns_dominant_weight(self, conn):
        pid = insert_product(
            conn,
            name="Rice",
            kcal_per_100=130,
            protein_per_100=2.7,
            fat_per_100=0.3,
            carbs_per_100=28,
            created_at="2026-01-01T00:00:00",
        )
        # Log 3x 200g, 1x 100g
        for i in range(3):
            insert_meal(
                conn,
                "lunch",
                None,
                f"2026-03-{20 + i:02d}T12:00:00+00:00",
                [
                    {
                        "product_id": pid,
                        "name": "Rice",
                        "weight_grams": 200,
                        "kcal": 260,
                        "protein": 5.4,
                        "fat": 0.6,
                        "carbs": 56,
                    }
                ],
            )
        insert_meal(
            conn,
            "dinner",
            None,
            "2026-03-23T18:00:00+00:00",
            [
                {
                    "product_id": pid,
                    "name": "Rice",
                    "weight_grams": 100,
                    "kcal": 130,
                    "protein": 2.7,
                    "fat": 0.3,
                    "carbs": 28,
                }
            ],
        )

        common = get_most_common_serving(conn, pid)
        assert common is not None
        assert common["weight_grams"] == 200
        assert common["count"] == 3
        assert common["total"] == 4
        assert common["ratio"] == 0.75

    def test_no_meals(self, conn):
        pid = insert_product(
            conn,
            name="Empty",
            kcal_per_100=100,
            protein_per_100=10,
            fat_per_100=5,
            carbs_per_100=15,
            created_at="2026-01-01T00:00:00",
        )
        assert get_most_common_serving(conn, pid) is None


class TestRecentMealsByType:
    def test_returns_meals_with_items(self, conn):
        pid = insert_product(
            conn,
            name="Oats",
            kcal_per_100=389,
            protein_per_100=16.9,
            fat_per_100=6.9,
            carbs_per_100=66.3,
            created_at="2026-01-01T00:00:00",
        )
        insert_meal(
            conn,
            "breakfast",
            None,
            "2026-03-24T12:00:00+00:00",
            [
                {
                    "product_id": pid,
                    "name": "Oats",
                    "weight_grams": 80,
                    "kcal": 311.2,
                    "protein": 13.5,
                    "fat": 5.5,
                    "carbs": 53.0,
                }
            ],
        )
        insert_meal(
            conn,
            "breakfast",
            None,
            "2026-03-25T12:00:00+00:00",
            [
                {
                    "product_id": pid,
                    "name": "Oats",
                    "weight_grams": 80,
                    "kcal": 311.2,
                    "protein": 13.5,
                    "fat": 5.5,
                    "carbs": 53.0,
                }
            ],
        )
        insert_meal(
            conn,
            "lunch",
            None,
            "2026-03-25T16:00:00+00:00",
            [
                {
                    "product_id": pid,
                    "name": "Oats",
                    "weight_grams": 100,
                    "kcal": 389,
                    "protein": 16.9,
                    "fat": 6.9,
                    "carbs": 66.3,
                }
            ],
        )

        meals = get_recent_meals_by_type(
            conn,
            "breakfast",
            "2026-03-24T00:00:00+00:00",
            "2026-03-26T00:00:00+00:00",
            limit=5,
        )
        assert len(meals) == 2
        assert all(m["meal_type"] == "breakfast" for m in meals)
        assert len(meals[0]["items"]) == 1

    def test_no_filter(self, conn):
        insert_meal(
            conn,
            "breakfast",
            None,
            "2026-03-25T12:00:00+00:00",
            [
                {
                    "name": "A",
                    "weight_grams": 100,
                    "kcal": 200,
                    "protein": 10,
                    "fat": 5,
                    "carbs": 30,
                }
            ],
        )
        insert_meal(
            conn,
            "lunch",
            None,
            "2026-03-25T16:00:00+00:00",
            [
                {
                    "name": "B",
                    "weight_grams": 100,
                    "kcal": 300,
                    "protein": 15,
                    "fat": 8,
                    "carbs": 40,
                }
            ],
        )

        meals = get_recent_meals_by_type(
            conn,
            None,
            "2026-03-25T00:00:00+00:00",
            "2026-03-26T00:00:00+00:00",
            limit=5,
        )
        assert len(meals) == 2


class TestActivity:
    def test_insert_and_get(self, conn):
        entry_id = upsert_activity(
            conn,
            activity_type="Running",
            start_at="2026-03-20T08:00:00+00:00",
            end_at="2026-03-20T08:45:00+00:00",
            duration_min=45,
            kcal_burned=350,
            distance_m=5000,
            avg_heart_rate=145,
        )
        assert entry_id is not None
        activities = get_activities(conn, "2026-03-20", "2026-03-20")
        assert len(activities) == 1
        assert activities[0]["activity_type"] == "Running"
        assert activities[0]["kcal_burned"] == 350

    def test_upsert_deduplicates(self, conn):
        upsert_activity(
            conn,
            activity_type="Running",
            start_at="2026-03-20T08:00:00+00:00",
            duration_min=45,
            kcal_burned=350,
        )
        # Same type + start_at + source → should update
        upsert_activity(
            conn,
            activity_type="Running",
            start_at="2026-03-20T08:00:00+00:00",
            duration_min=50,
            kcal_burned=400,
        )
        activities = get_activities(conn, "2026-03-20", "2026-03-20")
        assert len(activities) == 1
        assert activities[0]["kcal_burned"] == 400
        assert activities[0]["duration_min"] == 50

    def test_summary(self, conn):
        upsert_activity(
            conn,
            activity_type="Running",
            start_at="2026-03-20T08:00:00+00:00",
            duration_min=45,
            kcal_burned=350,
            distance_m=5000,
        )
        upsert_activity(
            conn,
            activity_type="Walking",
            start_at="2026-03-20T18:00:00+00:00",
            duration_min=30,
            kcal_burned=150,
            distance_m=2000,
        )
        summary = get_activity_summary(conn, "2026-03-20")
        assert summary["count"] == 2
        assert summary["total_duration_min"] == 75
        assert summary["total_kcal_burned"] == 500
        assert summary["total_distance_m"] == 7000

    def test_empty_summary(self, conn):
        summary = get_activity_summary(conn, "2026-03-20")
        assert summary["count"] == 0
        assert summary["total_kcal_burned"] == 0


class TestCycle:
    def test_insert_and_get(self, conn):
        entry_id = upsert_cycle_event(
            conn, event_type="flow", date="2026-03-01", value="medium"
        )
        assert entry_id is not None
        events = get_cycle_events(conn, "2026-03-01", "2026-03-31")
        assert len(events) == 1
        assert events[0]["event_type"] == "flow"
        assert events[0]["value"] == "medium"

    def test_upsert_deduplicates(self, conn):
        upsert_cycle_event(conn, event_type="flow", date="2026-03-01", value="light")
        upsert_cycle_event(conn, event_type="flow", date="2026-03-01", value="heavy")
        events = get_cycle_events(conn, "2026-03-01", "2026-03-01")
        assert len(events) == 1
        assert events[0]["value"] == "heavy"

    def test_flow_dates(self, conn):
        for day in [1, 2, 3, 4, 29, 30, 31]:
            upsert_cycle_event(conn, event_type="flow", date=f"2026-03-{day:02d}", value="medium")
        dates = get_cycle_flow_dates(conn, months=1)
        assert len(dates) == 7
        assert dates[0] == "2026-03-01"

    def test_multiple_event_types(self, conn):
        upsert_cycle_event(conn, event_type="flow", date="2026-03-01", value="heavy")
        upsert_cycle_event(conn, event_type="cervical_mucus", date="2026-03-10", value="egg_white")
        upsert_cycle_event(conn, event_type="basal_temp", date="2026-03-10", value="36.6")
        events = get_cycle_events(conn, "2026-03-01", "2026-03-31")
        assert len(events) == 3
