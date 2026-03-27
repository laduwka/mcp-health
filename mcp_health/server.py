import json
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.fastmcp import FastMCP
from prometheus_client import make_asgi_app
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from . import calc, config, db
from .log import get_logger
from .metrics import (
    ACTIVITIES_LOGGED,
    CYCLE_EVENTS_LOGGED,
    HEALTH_IMPORT_LATENCY,
    HEALTH_IMPORTS,
    LOG_MEAL_ITEMS,
    LOG_MEAL_PHASE,
    LOG_MEAL_RESOLUTION,
    MEALS_LOGGED,
    PRODUCTS_CREATED,
    WEIGHT_ENTRIES,
    instrument_tool,
)

_log = get_logger("mcp_health.server")

_oauth_mode = bool(config.OAUTH_ISSUER)

if _oauth_mode:
    from .auth_provider import HealthOAuthProvider

    _oauth_provider = HealthOAuthProvider()
    auth_settings = AuthSettings(
        issuer_url=config.OAUTH_ISSUER,
        resource_server_url=config.OAUTH_ISSUER,
        required_scopes=[],
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    )
    mcp = FastMCP(
        "Fitness Coach",
        host=config.HOST,
        port=config.PORT,
        streamable_http_path="/",
        auth_server_provider=_oauth_provider,
        auth=auth_settings,
    )
else:
    _oauth_provider = None
    mcp = FastMCP(
        "Fitness Coach",
        host=config.HOST,
        port=config.PORT,
        streamable_http_path="/",
    )

_conn = None


def _get_conn():
    global _conn
    if _conn is None:
        _conn = db.get_connection()
        db.init_db(_conn)
    return _conn


def _today() -> str:
    """Today's date in user's configured timezone."""
    return datetime.now(ZoneInfo(config.TZ)).strftime("%Y-%m-%d")


def _now_utc() -> str:
    """Current time in UTC, ISO format."""
    return datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _utc_to_local_date(utc_str: str) -> str:
    """Convert a UTC timestamp string to a local date string."""
    dt = datetime.fromisoformat(utc_str)
    return dt.astimezone(ZoneInfo(config.TZ)).strftime("%Y-%m-%d")


# --- Tool 1: add_product ---


def _nutrition_differs(
    provided: dict, reference: dict, threshold: float = 0.15
) -> bool:
    """Check if nutrition values differ by more than threshold (relative)."""
    for key in ("kcal_per_100", "protein_per_100", "fat_per_100", "carbs_per_100"):
        ref = reference[key]
        val = provided[key]
        if ref > 0 and abs(val - ref) / ref > threshold:
            return True
    return False


@mcp.tool()
@instrument_tool
def add_product(
    name: str,
    kcal: float,
    protein: float,
    fat: float,
    carbs: float,
    per_amount: float = 100.0,
    per_unit: str = "g",
    barcode: str | None = None,
    notes: str | None = None,
    force: bool = False,
) -> dict:
    """Add a custom food product. Checks local DB for duplicates and
    OpenFoodFacts for similar products before creating. If matches found
    with different nutrition, returns them instead of creating.
    Use force=True for homemade/custom products not in any database."""
    normalized = calc.normalize_per_100(kcal, protein, fat, carbs, per_amount)
    warnings = calc.validate_nutrition(
        normalized["kcal_per_100"],
        normalized["protein_per_100"],
        normalized["fat_per_100"],
        normalized["carbs_per_100"],
    )
    conn = _get_conn()

    if not force:
        # Check local duplicates (exact name match)
        existing = db.search_products(conn, name, limit=3)
        exact = [p for p in existing if p["name"].lower() == name.lower()]
        if exact:
            return {
                "status": "already_exists",
                "product": exact[0],
                "message": f"Product '{name}' already exists. Use its product_id directly.",
            }

        # Check OFF products via FTS
        off_matches = db.search_products_fts(conn, name, limit=3)
        off_matches = [p for p in off_matches if p.get("source") == "off"]
        differing = [p for p in off_matches if _nutrition_differs(normalized, p)]
        if differing:
            return {
                "status": "off_matches_found",
                "off_products": [
                    {
                        "product_id": p["id"],
                        "name": p["name"],
                        "brand": p.get("brand"),
                        "kcal_per_100": p["kcal_per_100"],
                        "protein_per_100": p["protein_per_100"],
                        "fat_per_100": p["fat_per_100"],
                        "carbs_per_100": p["carbs_per_100"],
                    }
                    for p in differing
                ],
                "message": (
                    "Similar products found in database with different nutrition. "
                    "Use their product_id in log_meal, or call add_product with "
                    "force=True to create a custom product."
                ),
            }

    product_id = db.insert_product(
        conn,
        name=name,
        kcal_per_100=normalized["kcal_per_100"],
        protein_per_100=normalized["protein_per_100"],
        fat_per_100=normalized["fat_per_100"],
        carbs_per_100=normalized["carbs_per_100"],
        label_per_unit=per_unit,
        barcode=barcode,
        notes=notes,
        source="local",
    )
    PRODUCTS_CREATED.inc()
    return {
        "product_id": product_id,
        "status": "created",
        "normalized": normalized,
        "warnings": warnings,
    }


# --- Tool 2: search_product ---


@mcp.tool()
@instrument_tool
def search_product(query: str, limit: int = 5) -> list[dict]:
    """Search for products by name in the unified database (local + OpenFoodFacts).
    Returns matches sorted by usage frequency, with default_serving_grams when available.
    For known products (usage_count > 0), use product_id and default_serving directly.
    PREFER get_recent_meals or get_top_products when logging routine meals."""
    conn = _get_conn()
    return db.search_products_fts(conn, query, limit)


# --- Tool 3: log_meal ---


@mcp.tool()
@instrument_tool
def log_meal(
    items: list[dict],
    meal_type: str | None = None,
    notes: str | None = None,
    timestamp: str | None = None,
) -> dict:
    """Log a meal. Each item accepts EITHER product_id + weight_grams OR query + weight_grams.
    When query is provided, the server resolves it to a product automatically — no need to call
    search_product first. If a query is ambiguous (multiple candidates), those items are returned
    as ambiguous_items with candidates for you to pick from; re-call with product_id.
    NEVER provide nutrition values directly — always use product_id or query."""
    conn = _get_conn()
    calculated_items = []
    ambiguous_items = []
    not_found_items = []
    all_warnings = []

    LOG_MEAL_ITEMS.observe(len(items))

    # Phase 1: Resolve queries to product_ids
    phase_start = time.monotonic()
    resolved_items = []  # (original_item, product_id)
    query_items = []  # items that need resolution

    for item in items:
        if "product_id" in item:
            resolved_items.append((item, item["product_id"]))
        elif "query" in item:
            query_items.append(item)
        else:
            raise ValueError(
                "Each item must have either 'product_id' or 'query'. "
                "Never provide nutrition values directly."
            )

    # Resolve query items via FTS
    for item in query_items:
        candidates = db.resolve_product(conn, item["query"], limit=5)
        if not candidates:
            not_found_items.append({"query": item["query"]})
            LOG_MEAL_RESOLUTION.labels(outcome="not_found").inc()
            continue

        # Auto-resolve: top candidate with usage_count > 0
        top = candidates[0]
        if top.get("usage_count", 0) > 0:
            resolved_items.append((item, top["id"]))
            LOG_MEAL_RESOLUTION.labels(outcome="resolved").inc()
        elif len(candidates) == 1:
            # Only one candidate — use it
            resolved_items.append((item, top["id"]))
            LOG_MEAL_RESOLUTION.labels(outcome="resolved").inc()
        else:
            ambiguous_items.append(
                {
                    "query": item["query"],
                    "weight_grams": item["weight_grams"],
                    "candidates": [
                        {
                            "product_id": c["id"],
                            "name": c["name"],
                            "brand": c.get("brand"),
                            "source": c.get("source", "local"),
                            "kcal_per_100": c["kcal_per_100"],
                            "protein_per_100": c["protein_per_100"],
                            "fat_per_100": c["fat_per_100"],
                            "carbs_per_100": c["carbs_per_100"],
                        }
                        for c in candidates[:5]
                    ],
                }
            )
            LOG_MEAL_RESOLUTION.labels(outcome="ambiguous").inc()

    LOG_MEAL_PHASE.labels(phase="resolve").observe(time.monotonic() - phase_start)

    # Phase 2: Batch fetch products
    phase_start = time.monotonic()
    product_ids = [pid for _, pid in resolved_items]
    products = db.get_products_batch(conn, product_ids) if product_ids else {}

    # Validate all products exist
    for item, pid in resolved_items:
        if pid not in products:
            raise ValueError(f"Product {pid} not found")

    LOG_MEAL_PHASE.labels(phase="fetch").observe(time.monotonic() - phase_start)

    # Phase 3: Calculate portions
    phase_start = time.monotonic()
    for item, pid in resolved_items:
        product = products[pid]
        weight = item["weight_grams"]
        portion_warnings = calc.validate_portion_weight(weight)
        all_warnings.extend(portion_warnings)

        portion = calc.calculate_portion(
            weight,
            product["kcal_per_100"],
            product["protein_per_100"],
            product["fat_per_100"],
            product["carbs_per_100"],
        )
        calculated_items.append(
            {
                "product_id": pid,
                "name": product["name"],
                "weight_grams": weight,
                **portion,
            }
        )

    LOG_MEAL_PHASE.labels(phase="calculate").observe(time.monotonic() - phase_start)

    # Phase 4: Insert meal + batch updates (only if we have resolved items)
    result = {}
    if calculated_items:
        phase_start = time.monotonic()
        logged_ts = timestamp or _now_utc()
        meal_id = db.insert_meal(conn, meal_type, notes, logged_ts, calculated_items)
        MEALS_LOGGED.inc()

        # Batch increment usage
        db.increment_usage_batch(conn, product_ids)

        # Batch auto-learn serving sizes
        learnable = [
            pid
            for pid in product_ids
            if not products[pid].get("default_serving_grams")
            and products[pid].get("usage_count", 0) >= 2
        ]
        if learnable:
            servings = db.get_common_servings_batch(conn, learnable)
            for pid, info in servings.items():
                if info["ratio"] >= 0.6:
                    db.update_product_serving(conn, pid, info["weight_grams"])

        LOG_MEAL_PHASE.labels(phase="insert").observe(time.monotonic() - phase_start)

        phase_start = time.monotonic()
        meal_total = {
            "kcal": round(sum(i["kcal"] for i in calculated_items), 1),
            "protein": round(sum(i["protein"] for i in calculated_items), 1),
            "fat": round(sum(i["fat"] for i in calculated_items), 1),
            "carbs": round(sum(i["carbs"] for i in calculated_items), 1),
        }
        logged_date = _utc_to_local_date(logged_ts)
        daily_totals = db.get_daily_totals(conn, logged_date)
        goals = db.get_current_goals(conn)
        LOG_MEAL_PHASE.labels(phase="aggregate").observe(time.monotonic() - phase_start)

        result = {
            "meal_id": meal_id,
            "resolved_items": calculated_items,
            "meal_total": meal_total,
            "daily_totals": daily_totals,
            "warnings": all_warnings,
        }

        if goals:
            targets = {
                "kcal": goals.get("daily_kcal"),
                "protein": goals.get("protein_g"),
                "fat": goals.get("fat_g"),
                "carbs": goals.get("carbs_g"),
            }
            remaining = {}
            for k in ["kcal", "protein", "fat", "carbs"]:
                if targets[k] is not None:
                    remaining[k] = round(targets[k] - daily_totals[k], 1)
            result["daily_target"] = targets
            result["remaining"] = remaining

    if ambiguous_items:
        result["ambiguous_items"] = ambiguous_items
    if not_found_items:
        result["not_found_items"] = not_found_items

    if not calculated_items and not ambiguous_items and not not_found_items:
        raise ValueError("No items provided")

    return result


# --- Tool 4: log_weight ---


@mcp.tool()
@instrument_tool
def log_weight(weight_kg: float, date: str | None = None) -> dict:
    """Log body weight for a date (defaults to today). Returns trend data."""
    conn = _get_conn()
    date_str = date or _today()
    entry_id = db.upsert_weight(conn, weight_kg, date_str)
    WEIGHT_ENTRIES.inc()

    today_dt = datetime.strptime(date_str, "%Y-%m-%d")
    week_ago = (today_dt - timedelta(days=7)).strftime("%Y-%m-%d")
    month_ago = (today_dt - timedelta(days=30)).strftime("%Y-%m-%d")

    w_week = db.get_weight_for_date(conn, week_ago)
    w_month = db.get_weight_for_date(conn, month_ago)

    trend = {"current": weight_kg}
    if w_week:
        trend["week_ago"] = w_week["weight_kg"]
        trend["change_week"] = round(weight_kg - w_week["weight_kg"], 1)
    if w_month:
        trend["month_ago"] = w_month["weight_kg"]
        trend["change_month"] = round(weight_kg - w_month["weight_kg"], 1)

    return {"entry_id": entry_id, "date": date_str, "trend": trend}


# --- Tool 5: get_daily_summary ---


@mcp.tool()
@instrument_tool
def get_daily_summary(date: str | None = None) -> dict:
    """Get full daily nutrition summary: meals, totals, targets, remaining, activity."""
    conn = _get_conn()
    date_str = date or _today()
    meals = db.get_meals_for_date(conn, date_str)
    totals = db.get_daily_totals(conn, date_str)
    goals = db.get_current_goals(conn)
    weight = db.get_weight_for_date(conn, date_str)
    activity = db.get_activity_summary(conn, date_str)

    result = {
        "date": date_str,
        "meals": meals,
        "totals": totals,
    }

    if activity["count"] > 0:
        result["activity"] = activity

    if goals:
        targets = {
            "kcal": goals.get("daily_kcal"),
            "protein": goals.get("protein_g"),
            "fat": goals.get("fat_g"),
            "carbs": goals.get("carbs_g"),
        }
        remaining = {}
        for k in ["kcal", "protein", "fat", "carbs"]:
            if targets[k] is not None:
                remaining[k] = round(targets[k] - totals[k], 1)
        result["targets"] = targets
        result["remaining"] = remaining

    if weight:
        result["weight"] = weight["weight_kg"]

    return result


# --- Tool 6: get_weekly_report ---


@mcp.tool()
@instrument_tool
def get_weekly_report(week_start: str | None = None) -> dict:
    """Get weekly nutrition report with averages, adherence, weight trend, and top products."""
    conn = _get_conn()
    if week_start:
        start_dt = datetime.strptime(week_start, "%Y-%m-%d")
    else:
        today = datetime.now(ZoneInfo(config.TZ))
        start_dt = today - timedelta(days=today.weekday() + 7)

    start = start_dt.strftime("%Y-%m-%d")
    end = (start_dt + timedelta(days=6)).strftime("%Y-%m-%d")

    daily_breakdown = db.get_date_range_totals(conn, start, end)
    goals = db.get_current_goals(conn)
    weight_data = db.get_weight_range(conn, start, end)
    top_products = db.get_top_products(conn, start, end)

    days_with_data = len(daily_breakdown)
    if days_with_data > 0:
        daily_averages = {
            "kcal": round(sum(d["kcal"] for d in daily_breakdown) / days_with_data, 1),
            "protein": round(
                sum(d["protein"] for d in daily_breakdown) / days_with_data, 1
            ),
            "fat": round(sum(d["fat"] for d in daily_breakdown) / days_with_data, 1),
            "carbs": round(
                sum(d["carbs"] for d in daily_breakdown) / days_with_data, 1
            ),
        }
    else:
        daily_averages = {"kcal": 0, "protein": 0, "fat": 0, "carbs": 0}

    adherence = {"on_target": 0, "over": 0, "under": 0}
    if goals and goals.get("daily_kcal"):
        target = goals["daily_kcal"]
        for day in daily_breakdown:
            ratio = day["kcal"] / target if target else 0
            if 0.9 <= ratio <= 1.1:
                adherence["on_target"] += 1
            elif ratio > 1.1:
                adherence["over"] += 1
            else:
                adherence["under"] += 1

    weight_trend = None
    if weight_data:
        weight_trend = {
            "start": weight_data[0]["weight_kg"],
            "end": weight_data[-1]["weight_kg"],
            "change": round(
                weight_data[-1]["weight_kg"] - weight_data[0]["weight_kg"], 1
            ),
            "data_points": len(weight_data),
        }

    targets = None
    if goals:
        targets = {
            "daily_kcal": goals.get("daily_kcal"),
            "protein_g": goals.get("protein_g"),
            "fat_g": goals.get("fat_g"),
            "carbs_g": goals.get("carbs_g"),
        }

    activity_summary = db.get_activity_range_summary(conn, start, end)
    cycle_events = db.get_cycle_events(conn, start, end)

    result = {
        "period": {"start": start, "end": end},
        "daily_averages": daily_averages,
        "daily_breakdown": daily_breakdown,
        "targets": targets,
        "adherence": adherence,
        "weight_trend": weight_trend,
        "top_products": top_products,
    }

    if activity_summary["count"] > 0:
        result["activity"] = activity_summary

    if cycle_events:
        result["cycle_events"] = cycle_events

    return result


# --- Tool 7: get_trends ---


@mcp.tool()
@instrument_tool
def get_trends(days: int = 30, metrics: list[str] | None = None) -> dict:
    """Get nutrition and weight trends over specified period."""
    conn = _get_conn()
    if metrics is None:
        metrics = ["weight", "nutrition"]

    today = _today()
    start = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days)).strftime(
        "%Y-%m-%d"
    )

    result = {"period": {"start": start, "end": today, "days": days}}

    if "weight" in metrics:
        weight_data = db.get_weight_range(conn, start, today)
        if weight_data:
            weights = [w["weight_kg"] for w in weight_data]
            result["weight"] = {
                "start": weights[0],
                "current": weights[-1],
                "min": min(weights),
                "max": max(weights),
                "change": round(weights[-1] - weights[0], 1),
                "data_points": len(weights),
            }

    if "nutrition" in metrics:
        daily_data = db.get_date_range_totals(conn, start, today)
        if daily_data:
            n = len(daily_data)
            avg_daily = {
                "kcal": round(sum(d["kcal"] for d in daily_data) / n, 1),
                "protein": round(sum(d["protein"] for d in daily_data) / n, 1),
                "fat": round(sum(d["fat"] for d in daily_data) / n, 1),
                "carbs": round(sum(d["carbs"] for d in daily_data) / n, 1),
            }
            result["nutrition"] = {
                "avg_daily": avg_daily,
                "days_tracked": n,
            }

    return result


# --- Tool 8: get_top_products ---


@mcp.tool()
@instrument_tool
def get_top_products(days: int = 30, limit: int = 20) -> dict:
    """Get most frequently consumed products with product_id and nutrition totals. Call this BEFORE search_product when logging routine meals. Returns products sorted by usage frequency with last_used date."""
    conn = _get_conn()
    today = _today()
    start = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days)).strftime(
        "%Y-%m-%d"
    )
    products = db.get_top_products(conn, start, today, limit)
    for p in products:
        if p.get("last_used"):
            p["last_used"] = _utc_to_local_date(p["last_used"])
    return {
        "period": {"start": start, "end": today, "days": days},
        "products": products,
    }


# --- Tool 9: update_goals ---


@mcp.tool()
@instrument_tool
def update_goals(
    daily_kcal: float | None = None,
    protein_g: float | None = None,
    fat_g: float | None = None,
    carbs_g: float | None = None,
    target_weight: float | None = None,
) -> dict:
    """Set or update daily nutrition goals. Unspecified fields keep their current values."""
    conn = _get_conn()
    current = db.get_current_goals(conn)

    new_goals = {
        "daily_kcal": daily_kcal
        if daily_kcal is not None
        else (current.get("daily_kcal") if current else None),
        "protein_g": protein_g
        if protein_g is not None
        else (current.get("protein_g") if current else None),
        "fat_g": fat_g
        if fat_g is not None
        else (current.get("fat_g") if current else None),
        "carbs_g": carbs_g
        if carbs_g is not None
        else (current.get("carbs_g") if current else None),
        "target_weight": target_weight
        if target_weight is not None
        else (current.get("target_weight") if current else None),
    }

    db.insert_goals(conn, **new_goals)
    return {"goals": new_goals, "status": "updated"}


# --- Tool 10: delete_meal ---


@mcp.tool()
@instrument_tool
def delete_meal(meal_id: int) -> dict:
    """Delete a logged meal by ID and return updated daily totals."""
    conn = _get_conn()
    meal = db.get_meal(conn, meal_id)
    if not meal:
        return {"status": "not_found", "meal_id": meal_id}

    meal_date = _utc_to_local_date(meal["logged_at"])
    db.delete_meal(conn, meal_id)
    updated_totals = db.get_daily_totals(conn, meal_date)

    return {
        "status": "deleted",
        "meal_id": meal_id,
        "updated_daily_totals": updated_totals,
    }


# --- Tool 11: delete_meal_item ---


@mcp.tool()
@instrument_tool
def delete_meal_item(item_id: int) -> dict:
    """Delete a single item from a meal by item ID. If it was the last item, the meal is deleted too."""
    conn = _get_conn()
    item = db.get_meal_item(conn, item_id)
    if not item:
        return {"status": "not_found", "item_id": item_id}

    meal_id = item["meal_id"]
    meal = db.get_meal(conn, meal_id)
    meal_date = _utc_to_local_date(meal["logged_at"])

    db.delete_meal_item(conn, item_id)

    meal_deleted = False
    if db.count_meal_items(conn, meal_id) == 0:
        db.delete_meal(conn, meal_id)
        meal_deleted = True

    updated_totals = db.get_daily_totals(conn, meal_date)
    return {
        "status": "deleted",
        "item_id": item_id,
        "meal_id": meal_id,
        "meal_deleted": meal_deleted,
        "updated_daily_totals": updated_totals,
    }


# --- Tool 12: update_meal_item ---


@mcp.tool()
@instrument_tool
def update_meal_item(item_id: int, weight_grams: float) -> dict:
    """Update the weight of a meal item and recalculate its nutrition. Works with both product-based and ad-hoc items."""
    if weight_grams <= 0:
        return {"status": "error", "message": "weight_grams must be > 0"}

    conn = _get_conn()
    item = db.get_meal_item(conn, item_id)
    if not item:
        return {"status": "not_found", "item_id": item_id}

    product = None
    if item["product_id"]:
        product = db.get_product(conn, item["product_id"])

    if product:
        portion = calc.calculate_portion(
            weight_grams,
            product["kcal_per_100"],
            product["protein_per_100"],
            product["fat_per_100"],
            product["carbs_per_100"],
        )
    else:
        old_weight = item["weight_grams"]
        if old_weight == 0:
            return {
                "status": "error",
                "message": "cannot scale ad-hoc item with zero weight",
            }
        ratio = weight_grams / old_weight
        portion = {
            "kcal": round(item["kcal"] * ratio, 1),
            "protein": round(item["protein"] * ratio, 1),
            "fat": round(item["fat"] * ratio, 1),
            "carbs": round(item["carbs"] * ratio, 1),
        }

    db.update_meal_item(
        conn,
        item_id,
        weight_grams,
        portion["kcal"],
        portion["protein"],
        portion["fat"],
        portion["carbs"],
    )

    meal = db.get_meal(conn, item["meal_id"])
    meal_date = _utc_to_local_date(meal["logged_at"])
    updated_totals = db.get_daily_totals(conn, meal_date)

    return {
        "status": "updated",
        "item": {
            "id": item_id,
            "name": item["name"],
            "weight_grams": weight_grams,
            **portion,
        },
        "updated_daily_totals": updated_totals,
    }


# --- Tool: get_recent_meals ---


@mcp.tool()
@instrument_tool
def get_recent_meals(
    meal_type: str | None = None, days: int = 7, limit: int = 5
) -> dict:
    """Get recent meals with full items (product_id, name, weight_grams, default_serving). Enables "same as yesterday's breakfast" without any search calls. Returns meals newest-first."""
    conn = _get_conn()
    today = _today()
    start = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days)).strftime(
        "%Y-%m-%d"
    )
    start_utc, _ = db._date_range_utc(start)
    _, end_utc = db._date_range_utc(today)
    meals = db.get_recent_meals_by_type(conn, meal_type, start_utc, end_utc, limit)
    # Convert timestamps to local dates for display
    for meal in meals:
        meal["date"] = _utc_to_local_date(meal["logged_at"])
    return {
        "period": {"start": start, "end": today, "days": days},
        "meal_type_filter": meal_type,
        "meals": meals,
    }


# --- Tool: set_product_serving ---


@mcp.tool()
@instrument_tool
def set_product_serving(
    product_id: int, serving_grams: float, label: str | None = None
) -> dict:
    """Store default serving size for a product so it doesn't need to be asked every time. E.g. set_product_serving(42, 39, "1 scoop") for a protein powder."""
    conn = _get_conn()
    product = db.get_product(conn, product_id)
    if not product:
        return {"status": "not_found", "product_id": product_id}
    db.update_product_serving(conn, product_id, serving_grams, label)
    return {
        "status": "updated",
        "product_id": product_id,
        "product_name": product["name"],
        "default_serving_grams": serving_grams,
        "serving_label": label,
    }


# --- Tool: log_activity ---


@mcp.tool()
@instrument_tool
def log_activity(
    activity_type: str,
    start_at: str,
    duration_min: float | None = None,
    kcal_burned: float | None = None,
    distance_m: float | None = None,
    avg_heart_rate: float | None = None,
    notes: str | None = None,
) -> dict:
    """Log a physical activity (workout, walk, etc.). start_at in ISO format."""
    conn = _get_conn()
    start_at = _normalize_ts(start_at)
    end_at = None
    if duration_min and start_at:
        try:
            start_dt = datetime.fromisoformat(start_at)
            end_at = (start_dt + timedelta(minutes=duration_min)).isoformat()
        except ValueError:
            pass
    entry_id = db.upsert_activity(
        conn,
        activity_type=activity_type,
        start_at=start_at,
        end_at=end_at,
        duration_min=duration_min,
        kcal_burned=kcal_burned,
        distance_m=distance_m,
        avg_heart_rate=avg_heart_rate,
        notes=notes,
    )
    ACTIVITIES_LOGGED.inc()
    return {"entry_id": entry_id, "status": "logged", "activity_type": activity_type}


# --- Tool: get_activity_summary ---


@mcp.tool()
@instrument_tool
def get_activity_summary(date: str | None = None) -> dict:
    """Get activity summary for a day: total duration, calories burned, distance."""
    conn = _get_conn()
    date_str = date or _today()
    summary = db.get_activity_summary(conn, date_str)
    activities = db.get_activities(conn, date_str, date_str)
    return {
        "date": date_str,
        "summary": summary,
        "activities": activities,
    }


# --- Tool: log_cycle_event ---


@mcp.tool()
@instrument_tool
def log_cycle_event(
    event_type: str,
    date: str | None = None,
    value: str | None = None,
    notes: str | None = None,
) -> dict:
    """Log a menstrual cycle event. event_type: 'flow', 'cervical_mucus', 'ovulation_test', 'basal_temp', 'spotting'. value: 'light'/'medium'/'heavy' for flow, temperature for basal_temp, etc."""
    conn = _get_conn()
    date_str = date or _today()
    entry_id = db.upsert_cycle_event(
        conn, event_type=event_type, date=date_str, value=value, notes=notes
    )
    CYCLE_EVENTS_LOGGED.inc()
    return {
        "entry_id": entry_id,
        "status": "logged",
        "event_type": event_type,
        "date": date_str,
    }


# --- Tool: get_cycle_summary ---


@mcp.tool()
@instrument_tool
def get_cycle_summary(months: int = 3) -> dict:
    """Get menstrual cycle summary: recent events, average cycle length, last period start."""
    conn = _get_conn()
    today = _today()
    start = (
        datetime.strptime(today, "%Y-%m-%d") - timedelta(days=months * 30)
    ).strftime("%Y-%m-%d")
    events = db.get_cycle_events(conn, start, today)
    flow_dates = db.get_cycle_flow_dates(conn, months)

    # Calculate cycle lengths from flow dates
    cycles = []
    if flow_dates:
        period_starts = []
        prev_date = None
        for d in flow_dates:
            dt = datetime.strptime(d, "%Y-%m-%d")
            if prev_date is None or (dt - prev_date).days > 3:
                period_starts.append(d)
            prev_date = dt

        for i in range(1, len(period_starts)):
            d1 = datetime.strptime(period_starts[i - 1], "%Y-%m-%d")
            d2 = datetime.strptime(period_starts[i], "%Y-%m-%d")
            cycles.append((d2 - d1).days)

    avg_cycle_length = round(sum(cycles) / len(cycles), 1) if cycles else None

    result = {
        "period": {"start": start, "end": today, "months": months},
        "events": events,
        "flow_dates": flow_dates,
        "cycles_detected": len(cycles),
        "cycle_lengths": cycles,
        "avg_cycle_length": avg_cycle_length,
    }

    if avg_cycle_length and flow_dates:
        # Predict next period
        last_period_start = None
        prev_date = None
        for d in flow_dates:
            dt = datetime.strptime(d, "%Y-%m-%d")
            if prev_date is None or (dt - prev_date).days > 3:
                last_period_start = d
            prev_date = dt
        if last_period_start:
            result["last_period_start"] = last_period_start
            next_dt = datetime.strptime(last_period_start, "%Y-%m-%d") + timedelta(
                days=round(avg_cycle_length)
            )
            result["predicted_next_period"] = next_dt.strftime("%Y-%m-%d")

    return result


# --- ASGI app composition ---

_metrics_app = make_asgi_app()


# --- Health Auto Export webhook ---


async def _handle_health_import(request: Request) -> Response:
    """Handle POST /api/health-import from Health Auto Export app."""
    source_ip = request.client.host if request.client else "unknown"
    automation_id = request.headers.get("automation-id", "")

    _log.info(
        "Health import request received",
        extra={
            "source_ip": source_ip,
            "automation_id": automation_id,
            "path": "/api/health-import",
        },
    )

    # Auth check
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != config.AUTH_TOKEN:
        _log.warning(
            "Health import auth failed",
            extra={"source_ip": source_ip, "status": "401"},
        )
        return Response("Unauthorized", status_code=401)

    start_time = time.monotonic()
    try:
        body = await request.body()
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        _log.warning(
            "Health import invalid JSON",
            extra={"source_ip": source_ip, "status": "400"},
        )
        return Response(
            '{"error": "invalid JSON"}', status_code=400, media_type="application/json"
        )

    conn = _get_conn()
    data = payload.get("data", payload)
    imported = {"weight": 0, "activities": 0, "cycle_events": 0}

    # Debug: log what sections are present and their sizes
    sections = {
        k: len(v) if isinstance(v, list) else type(v).__name__
        for k, v in data.items()
        if v and (not isinstance(v, list) or len(v) > 0)
    }
    _log.info(
        "Health import payload sections",
        extra={"imported": sections, "automation_id": automation_id},
    )

    # Process metrics (weight, etc.)
    for metric in data.get("metrics", []):
        metric_name = metric.get("name", "").lower().replace(" ", "_")
        if metric_name in ("weight", "body_mass", "weight_body_mass"):
            for entry in metric.get("data", []):
                qty = entry.get("qty")
                if qty is None:
                    continue
                date_str = _parse_date(entry.get("date", ""))
                if date_str:
                    db.upsert_weight(conn, round(qty, 2), date_str)
                    WEIGHT_ENTRIES.inc()
                    HEALTH_IMPORTS.labels(data_type="weight").inc()
                    imported["weight"] += 1

    # Process workouts
    for workout in data.get("workouts", []):
        activity_type = workout.get("name", "Unknown")
        start_at = _normalize_ts(workout.get("start", ""))
        end_at = _normalize_ts(workout.get("end")) if workout.get("end") else None
        duration = workout.get("duration")  # seconds
        duration_min = round(duration / 60, 1) if duration else None

        energy_raw = workout.get("activeEnergyBurned") or workout.get("activeEnergy")
        kcal = energy_raw.get("qty") if isinstance(energy_raw, dict) else energy_raw

        dist_raw = workout.get("distance")
        distance = dist_raw.get("qty") if isinstance(dist_raw, dict) else dist_raw

        hr_raw = workout.get("heartRate")
        hr = None
        if isinstance(hr_raw, dict):
            avg_raw = hr_raw.get("avg")
            hr = avg_raw.get("qty") if isinstance(avg_raw, dict) else avg_raw

        if start_at:
            db.upsert_activity(
                conn,
                activity_type=activity_type,
                start_at=start_at,
                end_at=end_at,
                duration_min=duration_min,
                kcal_burned=kcal,
                distance_m=distance,
                avg_heart_rate=hr,
                source="health_auto_export",
            )
            ACTIVITIES_LOGGED.inc()
            HEALTH_IMPORTS.labels(data_type="activity").inc()
            imported["activities"] += 1

    # Process cycle tracking
    # HAE sends individual events with name/value:
    # {"name": "Menstrual Flow", "value": "Unspecified", "start": "...", "isCycleStart": true}
    _CYCLE_NAME_MAP = {
        "menstrual flow": "flow",
        "cervical mucus quality": "cervical_mucus",
        "ovulation test result": "ovulation_test",
        "basal body temperature": "basal_temp",
        "sexual activity": "sexual_activity",
        "spotting": "spotting",
        "intermenstrual bleeding": "spotting",
    }
    for event in data.get("cycleTracking", []):
        date_str = _parse_date(event.get("start") or event.get("date", ""))
        if not date_str:
            continue
        raw_name = event.get("name", "").lower().strip()
        event_type = _CYCLE_NAME_MAP.get(raw_name, raw_name.replace(" ", "_"))
        value = event.get("value")
        if isinstance(value, (int, float)):
            value = str(value)
        elif isinstance(value, dict):
            value = str(value.get("qty", ""))
        notes = "cycle_start" if event.get("isCycleStart") else None
        db.upsert_cycle_event(
            conn,
            event_type=event_type,
            date=date_str,
            value=value,
            notes=notes,
            source="health_auto_export",
        )
        CYCLE_EVENTS_LOGGED.inc()
        HEALTH_IMPORTS.labels(data_type="cycle").inc()
        imported["cycle_events"] += 1

    HEALTH_IMPORT_LATENCY.observe(time.monotonic() - start_time)
    _log.info(
        "Health data imported",
        extra={
            "imported": imported,
            "source_ip": source_ip,
            "automation_id": automation_id,
            "duration_s": round(time.monotonic() - start_time, 3),
        },
    )

    return Response(
        json.dumps({"status": "ok", "imported": imported}),
        status_code=200,
        media_type="application/json",
    )


def _normalize_ts(ts: str) -> str:
    """Normalize a timestamp string to canonical UTC ISO format for consistent storage."""
    if not ts:
        return ts
    ts = ts.replace("Z", "+00:00")
    # HAE space-separated: "2025-01-20 08:00:00 -0500"
    if "T" not in ts and len(ts) > 10:
        ts = ts[:10] + "T" + ts[11:]
        ts = ts.replace(" -", "-").replace(" +", "+")
    # Insert colon in offset: -0500 → -05:00
    ts = re.sub(r"([+-])(\d{2})(\d{2})$", r"\1\2:\3", ts)
    try:
        dt = datetime.fromisoformat(ts)
        return dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    except ValueError:
        return ts


def _parse_date(date_str: str) -> str | None:
    """Extract YYYY-MM-DD from a datetime string (ISO 8601 or HAE space-separated)."""
    if not date_str:
        return None
    normalized = _normalize_ts(date_str)
    try:
        dt = datetime.fromisoformat(normalized)
        return dt.astimezone(ZoneInfo(config.TZ)).strftime("%Y-%m-%d")
    except ValueError:
        if len(date_str) >= 10:
            return date_str[:10]
        return None


class BearerAuthMiddleware:
    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            auth = headers.get(b"authorization", b"").decode()
            if not auth.startswith("Bearer ") or auth[7:] != self.token:
                response = Response("Unauthorized", status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class AppRouter:
    """Unified ASGI router: /metrics (no auth), /api/health-import, /login (OAuth), everything else to MCP."""

    def __init__(self, mcp_app, login_router=None):
        self.mcp_app = mcp_app
        self.login_router = login_router

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path == "/metrics":
                await _metrics_app(scope, receive, send)
                return
            if path == "/api/health-import":
                request = Request(scope, receive)
                if request.method != "POST":
                    response = Response("Method Not Allowed", status_code=405)
                    await response(scope, receive, send)
                    return
                response = await _handle_health_import(request)
                await response(scope, receive, send)
                return
            if path == "/login" and self.login_router:
                await self.login_router(scope, receive, send)
                return
        await self.mcp_app(scope, receive, send)


if _oauth_mode:
    from starlette.routing import Route, Router

    _starlette_app = mcp.streamable_http_app()

    async def _login_get(request: Request) -> HTMLResponse:
        state = request.query_params.get("state", "")
        return HTMLResponse(_oauth_provider.get_login_page(state))

    async def _login_post(request: Request) -> Response:
        form = await request.form()
        state = form.get("state", "")
        password = form.get("password", "")
        redirect_url = await _oauth_provider.handle_login_callback(state, password)
        if redirect_url is None:
            html = _oauth_provider.get_login_page(state)
            html = html.replace(
                "</form>",
                '<p class="error">Invalid password. Try again.</p></form>',
            )
            return HTMLResponse(html, status_code=401)
        return RedirectResponse(redirect_url, status_code=302)

    _login_routes = [
        Route("/login", endpoint=_login_get, methods=["GET"]),
        Route("/login", endpoint=_login_post, methods=["POST"]),
    ]
    _login_router = Router(routes=_login_routes)

    app = AppRouter(_starlette_app, _login_router)
else:
    app = AppRouter(BearerAuthMiddleware(mcp.streamable_http_app(), config.AUTH_TOKEN))


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
