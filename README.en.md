# mcp-health

[Русский](README.md) | **English**

MCP server for nutrition and weight tracking. Works as an HTTP endpoint with OAuth 2.0 authorization — connects to Claude Desktop, Claude.ai (web/mobile) or any MCP client.

## Features

- **Unified product database** — local products + ~700K OpenFoodFacts products in a single table with FTS5 search
- **Server-side resolution** — `log_meal` accepts product names (`query`), the server resolves them automatically. The LLM never provides nutrition values — hallucinations are structurally impossible
- **Quick logging** — single `log_meal` call instead of search → lookup → log chain. Repeat meals without clarification questions: meal history, auto-learned serving sizes
- **Duplicate protection** — `add_product` checks local DB and OpenFoodFacts before creating, compares nutrition values
- **Weight** — tracking with trends (week/month)
- **Goals** — daily calorie/macro targets, remaining intake
- **Reports** — daily summary, weekly report with adherence, trends over any period
- **Apple Health** — automatic data import via [Health Auto Export](https://www.healthyapps.dev/) (weight, workouts, menstrual cycle)
- **Activity** — workout logging (type, duration, calories, distance, heart rate)
- **Menstrual cycle** — event tracking, average cycle length, next period prediction

## MCP Tools

| Tool | Description |
|------|-------------|
| `add_product` | Add a product. Checks for duplicates and OFF before creating. Use `force=True` for custom products |
| `search_product` | Search by name in the unified database (local + OFF), sorted by usage frequency |
| `log_meal` | Log a meal. Accepts `product_id` or `query` (product name) — server resolves automatically |
| `get_recent_meals` | Recent meals with full item data — for re-logging without search |
| `set_product_serving` | Set default serving size (e.g. 39g = 1 protein scoop) |
| `get_top_products` | Top products by frequency — call before search for routine meals |
| `log_weight` | Log weight, get trend |
| `get_daily_summary` | Daily summary: meals, totals, remaining vs goals |
| `get_weekly_report` | Weekly report: averages, adherence, top products |
| `get_trends` | Nutrition and weight trends over N days |
| `update_goals` | Set/update daily calorie and macro goals |
| `delete_meal` | Delete a logged meal |
| `delete_meal_item` | Delete a single item from a meal (deletes meal if last item) |
| `update_meal_item` | Update item weight with automatic macro recalculation |
| `log_activity` | Log a workout (type, duration, calories, distance, heart rate) |
| `get_activity_summary` | Activity summary for a day |
| `log_cycle_event` | Log a cycle event (flow, cervical_mucus, ovulation_test, basal_temp) |
| `get_cycle_summary` | Cycle analytics: average length, next period prediction |

### Meal logging workflow

```
# Typical lunch — single call:
log_meal(items=[
    {"query": "buckwheat boiled", "weight_grams": 150},
    {"query": "chicken breast", "weight_grams": 107},
    {"query": "feta", "weight_grams": 25},
], meal_type="lunch")

# If query is ambiguous (e.g. "milk" → 2%, 3.2%):
# → server returns ambiguous_items with candidates
# → re-call with product_id

# By product_id (as before):
log_meal(items=[{"product_id": 12, "weight_grams": 200}])
```

## Quick Start

```bash
cp .env.example .env
# edit .env: AUTH_TOKEN, DB_PATH, TZ

# locally
pip install -r requirements.txt
uvicorn mcp_health.server:app --host 0.0.0.0 --port 8000

# or via Docker
docker compose up -d
```

### OpenFoodFacts Database Import

OFF products are imported directly into the main `fitness.db` database:

```bash
# Full import from CSV dump (~1.1 GB gz, ~700K products)
python scripts/import_off.py --db data/fitness.db

# From a local file
python scripts/import_off.py --db data/fitness.db --csv-path /path/to/en.openfoodfacts.org.products.csv.gz

# Incremental delta update
python scripts/import_off.py --db data/fitness.db --delta
```

OFF products are saved with `source='off'` and `off_code` (barcode). Local products (`source='local'`) always rank higher in search results via `usage_count`.

**Updates** are configured via cron (Ansible):
- **Full re-import** — Sunday 4:00 AM
- **Delta update** — Monday–Saturday 4:00 AM

## Connecting

### Claude.ai / Mobile (iPhone, Android)

1. In claude.ai → Settings → Connectors → Add Custom Connector:
   - URL: `https://<your-domain>/mcp`
   - Complete the OAuth flow (enter password = `AUTH_TOKEN`)

2. On iPhone/Android: the connector is automatically available after web setup.

### Claude Desktop

Claude Desktop supports OAuth — connection is the same as web. You can also use legacy Bearer auth by disabling OAuth (`OAUTH_ISSUER=`):

```json
{
  "mcpServers": {
    "fitness": {
      "type": "streamable-http",
      "url": "https://<your-domain>/mcp",
      "headers": {
        "Authorization": "Bearer <AUTH_TOKEN>"
      }
    }
  }
}
```

## Apple Health Integration

Health data is automatically imported from Apple Health via the [Health Auto Export](https://www.healthyapps.dev/) iOS app (Premium, ~$3/mo).

### Setup

Create 3 automations in Health Auto Export (each sends a separate POST request):

| Automation | Data Type | What's Imported |
|------------|-----------|-----------------|
| Body Mass | Health Metrics | Weight |
| Workouts | Workouts | Workouts (type, duration, calories, distance, heart rate) |
| Cycle Tracking | Cycle Tracking | Menstrual cycle (flow, cervical mucus, ovulation, etc.) |

For each automation:
- **URL**: `https://<your-domain>/api/health-import`
- **Method**: POST
- **Header**: `Authorization: Bearer <AUTH_TOKEN>`
- **Format**: JSON
- **Frequency**: as desired (e.g. every 6 hours)

### Endpoint

`POST /api/health-import` — accepts JSON from Health Auto Export, parses metrics/workouts/cycle events, deduplicates and stores in DB. Returns:

```json
{"status": "ok", "imported": {"weight": 1, "activities": 0, "cycle_events": 1}}
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTH_TOKEN` | `changeme` | Password for OAuth authorization (also used as Bearer token in legacy mode) |
| `DB_PATH` | `data/fitness.db` | Path to SQLite database (products, meals, OFF — all in one DB) |
| `TZ` | `America/Toronto` | Timezone |
| `OAUTH_ISSUER` | _(empty)_ | OAuth server URL (e.g. `https://your-domain.com`). If not set — falls back to legacy Bearer auth |
| `LOG_LEVEL` | `INFO` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

## Observability

### Metrics

The application exports Prometheus metrics at `/metrics` (accessible only from localhost, nginx returns 403 externally):

- **MCP Tools** — `mcp_tool_calls_total`, `mcp_tool_latency_seconds` (per tool)
- **Database** — `db_operations_total`, `db_latency_seconds` (per operation)
- **Business** — `meals_logged_total`, `products_created_total`, `weight_entries_total`, `activities_logged_total`, `cycle_events_logged_total`
- **Health Import** — `health_import_total` (by data_type: weight/activity/cycle), `health_import_latency_seconds`

### Logging

Structured JSON logs to stderr:

```json
{"ts": "2026-03-24T12:00:00", "level": "INFO", "logger": "mcp_health.db", "msg": "Database connection opened"}
```

### Grafana

Dashboard `grafana/mcp-health-dashboard.json` — auto-deployed to `/var/lib/grafana/dashboards/`.

VictoriaMetrics scrape job is added via the `monitoring` Ansible role.

## Deployment

The project deploys to a VPS via GitHub Actions:
1. Push to `main` → build Docker image → push to GHCR
2. SSH to server → `docker compose pull && up -d`
3. Nginx reverse proxy with SSL (Let's Encrypt)

For manual deployment via Ansible:

```bash
cd ansible
ansible-playbook -i inventory/hosts.yml playbook.yml --ask-vault-pass
```

Individual roles:

```bash
ansible-playbook -i inventory/hosts.yml playbook.yml --tags app         # application only
ansible-playbook -i inventory/hosts.yml playbook.yml --tags nginx       # nginx/SSL only
ansible-playbook -i inventory/hosts.yml playbook.yml --tags backup      # backups only
ansible-playbook -i inventory/hosts.yml playbook.yml --tags monitoring  # monitoring only
```

## Project Structure

```
mcp_health/            # Main application package
  server.py            # MCP server (FastMCP + Starlette + OAuth/Bearer auth)
  auth_provider.py     # OAuth provider (OAuthAuthorizationServerProvider + login flow)
  db.py                # SQLite: products (local + OFF), meals, weight, goals, activity, cycle, FTS5
  calc.py              # Calorie/macro calculations, normalization, validation
  config.py            # Configuration from env vars
  metrics.py           # Prometheus metrics and instrumentation decorators
  log.py               # Structured JSON logging
scripts/import_off.py  # OFF import into unified products table (full CSV + delta updates)
grafana/               # Grafana dashboard
ansible/               # Ansible roles for VPS deployment (app, nginx, monitoring, backup)
.github/workflows/     # CI (ruff + pytest) and CD (GHCR + SSH deploy)
```

## Tests

```bash
pytest tests/ -v
```
