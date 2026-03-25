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

# --- OpenFoodFacts ---

OFF_DB_QUERIES = Counter(
    "off_db_queries_total",
    "OpenFoodFacts local DB queries",
    ["method"],
)
OFF_DB_LATENCY = Histogram(
    "off_db_latency_seconds",
    "OpenFoodFacts local DB query duration",
    ["method"],
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
