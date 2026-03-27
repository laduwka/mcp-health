import functools
import time

from prometheus_client import Counter, Histogram, Info

# --- MCP Tools ---

TOOL_CALLS = Counter(
    "mcp_tool_calls_total",
    "Total MCP tool invocations",
    ["tool", "status"],
)
TOOL_LATENCY = Histogram(
    "mcp_tool_latency_seconds",
    "MCP tool call duration",
    ["tool"],
)

# --- Database ---

DB_OPS = Counter(
    "db_operations_total",
    "Database operations",
    ["operation"],
)
DB_LATENCY = Histogram(
    "db_latency_seconds",
    "Database operation duration",
    ["operation"],
)

# --- Business ---

MEALS_LOGGED = Counter("meals_logged_total", "Total meals logged")
PRODUCTS_CREATED = Counter("products_created_total", "Total products created")
WEIGHT_ENTRIES = Counter("weight_entries_total", "Total weight entries logged")
ACTIVITIES_LOGGED = Counter("activities_logged_total", "Total activity entries logged")
CYCLE_EVENTS_LOGGED = Counter("cycle_events_logged_total", "Total cycle events logged")
HEALTH_IMPORTS = Counter(
    "health_import_total",
    "Total health data imports from external sources",
    ["data_type"],
)
HEALTH_IMPORT_LATENCY = Histogram(
    "health_import_latency_seconds",
    "Health data import duration",
)

# --- log_meal pipeline ---

LOG_MEAL_PHASE = Histogram(
    "log_meal_phase_seconds",
    "Time spent in each phase of log_meal",
    ["phase"],
)
LOG_MEAL_ITEMS = Histogram(
    "log_meal_items_count",
    "Number of items per log_meal call",
    buckets=[1, 2, 3, 5, 8, 13],
)
LOG_MEAL_RESOLUTION = Counter(
    "log_meal_resolution_total",
    "Product resolution outcomes in log_meal",
    ["outcome"],
)

# --- App info ---

APP_INFO = Info("mcp_health", "MCP Health application info")
APP_INFO.info({"version": "1.0.0", "name": "mcp-health"})


# --- Decorators ---


def instrument_tool(fn):
    """Decorator for MCP tools. Place UNDER @mcp.tool()."""
    name = fn.__name__

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        start = time.monotonic()
        try:
            result = fn(*args, **kwargs)
            TOOL_CALLS.labels(tool=name, status="ok").inc()
            return result
        except Exception:
            TOOL_CALLS.labels(tool=name, status="error").inc()
            raise
        finally:
            TOOL_LATENCY.labels(tool=name).observe(time.monotonic() - start)

    return wrapper


def timed_db(fn):
    """Decorator for DB functions: counter + histogram."""
    name = fn.__name__

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        start = time.monotonic()
        try:
            result = fn(*args, **kwargs)
            DB_OPS.labels(operation=name).inc()
            return result
        except Exception:
            DB_OPS.labels(operation=name).inc()
            raise
        finally:
            DB_LATENCY.labels(operation=name).observe(time.monotonic() - start)

    return wrapper
