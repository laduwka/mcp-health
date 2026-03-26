# mcp-health

[Русский](README.md) | **English**

MCP server for nutrition and weight tracking. Works as an HTTP endpoint with OAuth 2.0 authorization — connects to Claude Desktop, Claude.ai (web/mobile) or any MCP client.

## Features

- **Products** — product database with calories/protein/fat/carbs per 100g, name search, barcodes, default serving sizes
- **OpenFoodFacts** — local database of ~2M products (SQLite + FTS5), instant search and barcode lookup without external APIs, country filtering
- **Meals** — logging with portion calculation, ad-hoc products with auto-save
- **Quick logging** — repeat meals without clarification questions: meal history, auto-learned serving sizes
- **Weight** — tracking with trends (week/month)
- **Goals** — daily calorie/macro targets, remaining intake
- **Reports** — daily summary, weekly report with adherence, trends over any period

## MCP Tools

| Tool | Description |
|------|-------------|
| `add_product` | Add a product to the database (calories, macros, barcode, notes) |
| `search_product` | Search by name (local + country-filtered OpenFoodFacts), sorted by usage, with serving sizes |
| `lookup_product` | Look up product by barcode (local DB → OpenFoodFacts → cache) |
| `log_meal` | Log a meal (by product_id or ad-hoc), auto-learns serving sizes |
| `get_recent_meals` | Recent meals with full item data — for re-logging without search |
| `set_product_serving` | Set default serving size (e.g. 39g = 1 protein scoop) |
| `log_weight` | Log weight, get trend |
| `get_daily_summary` | Daily summary: meals, totals, remaining vs goals |
| `get_weekly_report` | Weekly report: averages, adherence, top products |
| `get_trends` | Nutrition and weight trends over N days |
| `get_top_products` | Top products with product_id, macros, and serving sizes — call before search for routine meals |
| `update_goals` | Set/update daily calorie and macro goals |
| `delete_meal` | Delete a logged meal |
| `delete_meal_item` | Delete a single item from a meal (deletes meal if last item) |
| `update_meal_item` | Update item weight with automatic macro recalculation |

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

Product search and barcode lookup require importing the OFF database:

```bash
# Full import from CSV dump (~1.1 GB gz, ~2M products → ~400-500 MB on disk)
python scripts/import_off.py

# Or from a local file (if already downloaded)
python scripts/import_off.py --csv-path /path/to/en.openfoodfacts.org.products.csv.gz

# Incremental delta update
python scripts/import_off.py --delta
```

The script creates `data/off_products.db` — a read-only SQLite with FTS5 index. The application works without this database (returns empty OFF results).

**Updates** are configured via cron (Ansible):
- **Full re-import** — Sunday 4:00 AM (atomic file replacement)
- **Delta update** — Monday–Saturday 4:00 AM (incremental upsert)

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

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTH_TOKEN` | `changeme` | Password for OAuth authorization (also used as Bearer token in legacy mode) |
| `DB_PATH` | `data/fitness.db` | Path to user data SQLite database |
| `OFF_DB_PATH` | `data/off_products.db` | Path to OpenFoodFacts SQLite database (read-only) |
| `OFF_COUNTRY` | _(empty)_ | Country filter for OFF search (e.g. `en:canada`). If not set — no filtering |
| `TZ` | `America/Toronto` | Timezone |
| `OAUTH_ISSUER` | _(empty)_ | OAuth server URL (e.g. `https://your-domain.com`). If not set — falls back to legacy Bearer auth |
| `LOG_LEVEL` | `INFO` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

## Observability

### Metrics

The application exports Prometheus metrics at `/metrics` (accessible only from localhost, nginx returns 403 externally):

- **MCP Tools** — `mcp_tool_calls_total`, `mcp_tool_latency_seconds` (per tool)
- **OpenFoodFacts** — `off_db_queries_total`, `off_db_latency_seconds` (per method: search/lookup)
- **Database** — `db_operations_total`, `db_latency_seconds` (per operation)
- **Business** — `meals_logged_total`, `products_created_total`, `weight_entries_total`

### Logging

Structured JSON logs to stderr:

```json
{"ts": "2026-03-24T12:00:00", "level": "WARNING", "logger": "mcp_health.off", "msg": "OFF database not found", "path": "data/off_products.db"}
```

### Grafana

Dashboard `grafana/mcp-health-dashboard.json` — auto-deployed to `/var/lib/grafana/dashboards/`. Panels: overview, MCP tools, OpenFoodFacts (DB queries/latency), business metrics, database.

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
  db.py                # SQLite: products, meals, weight, goals
  calc.py              # Calorie/macro calculations, normalization, validation
  config.py            # Configuration from env vars
  openfoodfacts.py     # Local OpenFoodFacts search (SQLite + FTS5)
  metrics.py           # Prometheus metrics and instrumentation decorators
  log.py               # Structured JSON logging
scripts/import_off.py  # OFF database import (full CSV + delta updates)
grafana/               # Grafana dashboard
ansible/               # Ansible roles for VPS deployment (app, nginx, monitoring, backup)
.github/workflows/     # CI (ruff + pytest) and CD (GHCR + SSH deploy)
```

## Tests

```bash
pytest tests/ -v
```
