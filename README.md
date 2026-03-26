# mcp-health

**Русский** | [English](README.en.md)

MCP-сервер для трекинга питания и веса. Работает как HTTP endpoint с OAuth 2.0 авторизацией — подключается к Claude Desktop, Claude.ai (web/mobile) или любому MCP-клиенту.

## Возможности

- **Продукты** — база продуктов с КБЖУ на 100г, поиск по имени, штрихкоды, размеры порций по умолчанию
- **OpenFoodFacts** — локальная база ~2M продуктов (SQLite + FTS5), мгновенный поиск и lookup по штрихкоду без внешних API, фильтрация по стране
- **Приёмы пищи** — логирование с расчётом порций, ad-hoc продукты с автосохранением
- **Быстрое логирование** — повторные приёмы пищи без уточняющих вопросов: история приёмов, автоматическое запоминание размеров порций
- **Вес** — трекинг с трендами (неделя/месяц)
- **Цели** — дневные целевые КБЖУ, остаток от нормы
- **Отчёты** — дневная сводка, недельный отчёт с adherence, тренды за произвольный период

## MCP Tools

| Tool | Описание |
|------|----------|
| `add_product` | Добавить продукт в базу (КБЖУ, штрихкод, заметки) |
| `search_product` | Поиск по имени (локально + OpenFoodFacts с фильтром по стране), сортировка по частоте, размеры порций |
| `lookup_product` | Найти продукт по штрихкоду (локальная БД → OpenFoodFacts → кэш) |
| `log_meal` | Записать приём пищи (по product_id или ad-hoc), автоизучение размеров порций |
| `get_recent_meals` | Недавние приёмы пищи с полными данными — для повторного логирования без поиска |
| `set_product_serving` | Задать размер порции по умолчанию (напр. 39г = 1 скуп протеина) |
| `log_weight` | Записать вес, получить тренд |
| `get_daily_summary` | Дневная сводка: приёмы, итоги, остаток от целей |
| `get_weekly_report` | Недельный отчёт: средние, adherence, топ продуктов |
| `get_trends` | Тренды питания и веса за N дней |
| `get_top_products` | Топ продуктов с product_id, КБЖУ и размерами порций — вызывать перед search для рутинных приёмов |
| `update_goals` | Установить/обновить дневные цели КБЖУ |
| `delete_meal` | Удалить приём пищи |
| `delete_meal_item` | Удалить отдельный продукт из приёма пищи (если последний — удаляет приём) |
| `update_meal_item` | Изменить вес продукта в приёме с пересчётом КБЖУ |

## Быстрый старт

```bash
cp .env.example .env
# отредактировать .env: AUTH_TOKEN, DB_PATH, TZ

# локально
pip install -r requirements.txt
uvicorn mcp_health.server:app --host 0.0.0.0 --port 8000

# или через Docker
docker compose up -d
```

### Импорт базы OpenFoodFacts

Для работы поиска и lookup по штрихкоду необходимо импортировать базу OFF:

```bash
# Полный импорт из CSV dump (~1.1 GB gz, ~2M продуктов → ~400-500 MB на диске)
python scripts/import_off.py

# Или с локальным файлом (если уже скачали)
python scripts/import_off.py --csv-path /path/to/en.openfoodfacts.org.products.csv.gz

# Инкрементальное обновление из дельт
python scripts/import_off.py --delta
```

Скрипт создаёт `data/off_products.db` — read-only SQLite с FTS5 индексом. Приложение работает и без этой базы (возвращает пустые результаты из OFF).

**Обновления** настроены через cron (Ansible):
- **Полный ре-импорт** — воскресенье 4:00 (атомарная замена файла)
- **Дельта-обновление** — понедельник–суббота 4:00 (инкрементальный upsert)

## Подключение

### Claude.ai / Mobile (iPhone, Android)

1. В claude.ai → Settings → Connectors → Add Custom Connector:
   - URL: `https://<your-domain>/mcp`
   - Пройти OAuth flow (ввести пароль = `AUTH_TOKEN`)

2. На iPhone/Android: коннектор автоматически доступен после настройки в web.

### Claude Desktop

Claude Desktop поддерживает OAuth — подключение аналогично web. Также можно использовать legacy Bearer auth, отключив OAuth (`OAUTH_ISSUER=`):

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

## Переменные окружения

| Переменная | По умолчанию | Описание |
|-----------|-------------|----------|
| `AUTH_TOKEN` | `changeme` | Пароль для OAuth авторизации (также используется как Bearer-токен в legacy-режиме) |
| `DB_PATH` | `data/fitness.db` | Путь к SQLite базе пользовательских данных |
| `OFF_DB_PATH` | `data/off_products.db` | Путь к SQLite базе OpenFoodFacts (read-only) |
| `OFF_COUNTRY` | _(пусто)_ | Фильтр страны для OFF поиска (напр. `en:canada`). Если не задан — без фильтрации |
| `TZ` | `America/Toronto` | Часовой пояс |
| `OAUTH_ISSUER` | _(пусто)_ | URL сервера для OAuth (напр. `https://your-domain.com`). Если не задан — fallback на legacy Bearer auth |
| `LOG_LEVEL` | `INFO` | Уровень логирования (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

## Observability

### Метрики

Приложение экспортирует Prometheus-метрики на `/metrics` (доступен только с localhost, nginx возвращает 403 снаружи):

- **MCP Tools** — `mcp_tool_calls_total`, `mcp_tool_latency_seconds` (по каждому tool)
- **OpenFoodFacts** — `off_db_queries_total`, `off_db_latency_seconds` (по method: search/lookup)
- **Database** — `db_operations_total`, `db_latency_seconds` (по operation)
- **Business** — `meals_logged_total`, `products_created_total`, `weight_entries_total`

### Логирование

Structured JSON logs на stderr. Формат:

```json
{"ts": "2026-03-24T12:00:00", "level": "WARNING", "logger": "mcp_health.off", "msg": "OFF database not found", "path": "data/off_products.db"}
```

### Grafana

Dashboard `grafana/mcp-health-dashboard.json` — деплоится автоматически в `/var/lib/grafana/dashboards/`. Панели: overview, MCP tools, OpenFoodFacts (DB queries/latency), business metrics, database.

VictoriaMetrics scrape job добавляется через ansible role `monitoring`.

## Деплой

Проект деплоится на VPS через GitHub Actions:
1. Push в `main` → сборка Docker-образа → push в GHCR
2. SSH на сервер → `docker compose pull && up -d`
3. Nginx reverse proxy с SSL (Let's Encrypt)

Для ручного деплоя через Ansible:

```bash
cd ansible
ansible-playbook -i inventory/hosts.yml playbook.yml --ask-vault-pass
```

Отдельные роли:

```bash
ansible-playbook -i inventory/hosts.yml playbook.yml --tags app    # только приложение
ansible-playbook -i inventory/hosts.yml playbook.yml --tags nginx   # только nginx/SSL
ansible-playbook -i inventory/hosts.yml playbook.yml --tags backup     # только бэкапы
ansible-playbook -i inventory/hosts.yml playbook.yml --tags monitoring  # только мониторинг
```

## Структура

```
mcp_health/            # Основной пакет приложения
  server.py            # MCP-сервер (FastMCP + Starlette + OAuth/Bearer auth)
  auth_provider.py     # OAuth provider (OAuthAuthorizationServerProvider + login flow)
  db.py                # SQLite: продукты, приёмы, вес, цели
  calc.py              # Расчёты КБЖУ, нормализация, валидация
  config.py            # Конфигурация из env vars
  openfoodfacts.py     # Локальный поиск по базе OpenFoodFacts (SQLite + FTS5)
  metrics.py           # Prometheus-метрики и декораторы инструментации
  log.py               # Structured JSON logging
scripts/import_off.py  # Импорт базы OFF (full CSV + delta updates)
grafana/               # Grafana dashboard
ansible/               # Ansible-роли для деплоя на VPS (app, nginx, monitoring, backup)
.github/workflows/     # CI (ruff + pytest) и CD (GHCR + SSH deploy)
```

## Тесты

```bash
pytest tests/ -v
```
