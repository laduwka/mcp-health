# mcp-health

**Русский** | [English](README.en.md)

MCP-сервер для трекинга питания и веса. Работает как HTTP endpoint с OAuth 2.0 авторизацией — подключается к Claude Desktop, Claude.ai (web/mobile) или любому MCP-клиенту.

## Возможности

- **Единая база продуктов** — локальные продукты + ~700K продуктов из OpenFoodFacts в одной таблице с FTS5 поиском
- **Server-side resolution** — `log_meal` принимает названия продуктов (`query`), сервер сам находит продукт в базе. LLM никогда не передаёт значения КБЖУ — галлюцинации невозможны
- **Быстрое логирование** — один вызов `log_meal` вместо цепочки search → lookup → log. Повторные приёмы пищи без уточняющих вопросов: история приёмов, автоматическое запоминание размеров порций
- **Защита от дубликатов** — `add_product` проверяет локальную базу и OpenFoodFacts перед созданием, сравнивает КБЖУ
- **Вес** — трекинг с трендами (неделя/месяц)
- **Цели** — дневные целевые КБЖУ, остаток от нормы
- **Отчёты** — дневная сводка, недельный отчёт с adherence, тренды за произвольный период
- **Apple Health** — автоматический импорт данных через [Health Auto Export](https://www.healthyapps.dev/) (вес, тренировки, менструальный цикл)
- **Активность** — логирование тренировок (тип, длительность, калории, дистанция, пульс)
- **Менструальный цикл** — трекинг событий, расчёт средней длины цикла, прогноз следующей менструации

## MCP Tools

| Tool | Описание |
|------|----------|
| `add_product` | Добавить продукт. Проверяет дубликаты и OFF перед созданием. `force=True` для кастомных продуктов |
| `search_product` | Поиск по имени в единой базе (локальные + OFF), сортировка по частоте использования |
| `log_meal` | Записать приём пищи. Принимает `product_id` или `query` (название) — сервер резолвит автоматически |
| `get_recent_meals` | Недавние приёмы пищи с полными данными — для повторного логирования без поиска |
| `set_product_serving` | Задать размер порции по умолчанию (напр. 39г = 1 скуп протеина) |
| `get_top_products` | Топ продуктов по частоте — вызывать перед search для рутинных приёмов |
| `log_weight` | Записать вес, получить тренд |
| `get_daily_summary` | Дневная сводка: приёмы, итоги, остаток от целей |
| `get_weekly_report` | Недельный отчёт: средние, adherence, топ продуктов |
| `get_trends` | Тренды питания и веса за N дней |
| `update_goals` | Установить/обновить дневные цели КБЖУ |
| `delete_meal` | Удалить приём пищи |
| `delete_meal_item` | Удалить отдельный продукт из приёма пищи (если последний — удаляет приём) |
| `update_meal_item` | Изменить вес продукта в приёме с пересчётом КБЖУ |
| `log_activity` | Записать тренировку (тип, длительность, калории, дистанция, пульс) |
| `get_activity_summary` | Сводка активности за день |
| `log_cycle_event` | Записать событие цикла (flow, cervical_mucus, ovulation_test, basal_temp) |
| `get_cycle_summary` | Аналитика цикла: средняя длина, прогноз следующей менструации |

### Workflow логирования

```
# Типичный обед — один вызов:
log_meal(items=[
    {"query": "гречка варёная", "weight_grams": 150},
    {"query": "куриная грудка", "weight_grams": 107},
    {"query": "фета", "weight_grams": 25},
], meal_type="lunch")

# Если query неоднозначен (напр. "молоко" → 2%, 3.2%):
# → сервер возвращает ambiguous_items с candidates
# → повторный вызов с product_id

# По product_id (как раньше):
log_meal(items=[{"product_id": 12, "weight_grams": 200}])
```

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

OFF продукты импортируются прямо в основную базу `fitness.db`:

```bash
# Полный импорт из CSV dump (~1.1 GB gz, ~700K продуктов)
python scripts/import_off.py --db data/fitness.db

# С локальным файлом
python scripts/import_off.py --db data/fitness.db --csv-path /path/to/en.openfoodfacts.org.products.csv.gz

# Инкрементальное обновление из дельт
python scripts/import_off.py --db data/fitness.db --delta
```

Продукты из OFF сохраняются с `source='off'` и `off_code` (штрихкод). Локальные продукты (`source='local'`) всегда приоритетнее в поиске за счёт `usage_count`.

**Обновления** настроены через cron (Ansible):
- **Полный ре-импорт** — воскресенье 4:00
- **Дельта-обновление** — понедельник–суббота 4:00

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

## Apple Health интеграция

Данные из Apple Health импортируются автоматически через iOS-приложение [Health Auto Export](https://www.healthyapps.dev/) (Premium, ~$3/мес).

### Настройка

Создайте 3 автоматизации в Health Auto Export (каждая — отдельный POST-запрос):

| Автоматизация | Тип данных | Что импортируется |
|---------------|-----------|-------------------|
| Body Mass | Health Metrics | Вес |
| Workouts | Workouts | Тренировки (тип, длительность, калории, дистанция, пульс) |
| Cycle Tracking | Cycle Tracking | Менструальный цикл (flow, cervical mucus, ovulation и т.д.) |

Для каждой автоматизации:
- **URL**: `https://<your-domain>/api/health-import`
- **Method**: POST
- **Header**: `Authorization: Bearer <AUTH_TOKEN>`
- **Format**: JSON
- **Периодичность**: по желанию (напр. каждые 6 часов)

### Endpoint

`POST /api/health-import` — принимает JSON от Health Auto Export, парсит метрики/тренировки/цикл, дедуплицирует и сохраняет в БД. Возвращает:

```json
{"status": "ok", "imported": {"weight": 1, "activities": 0, "cycle_events": 1}}
```

## Переменные окружения

| Переменная | По умолчанию | Описание |
|-----------|-------------|----------|
| `AUTH_TOKEN` | `changeme` | Пароль для OAuth авторизации (также используется как Bearer-токен в legacy-режиме) |
| `DB_PATH` | `data/fitness.db` | Путь к SQLite базе (продукты, приёмы пищи, OFF, всё в одной БД) |
| `TZ` | `America/Toronto` | Часовой пояс |
| `OAUTH_ISSUER` | _(пусто)_ | URL сервера для OAuth (напр. `https://your-domain.com`). Если не задан — fallback на legacy Bearer auth |
| `LOG_LEVEL` | `INFO` | Уровень логирования (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

## Observability

### Метрики

Приложение экспортирует Prometheus-метрики на `/metrics` (доступен только с localhost, nginx возвращает 403 снаружи):

- **MCP Tools** — `mcp_tool_calls_total`, `mcp_tool_latency_seconds` (по каждому tool)
- **Database** — `db_operations_total`, `db_latency_seconds` (по operation)
- **Business** — `meals_logged_total`, `products_created_total`, `weight_entries_total`, `activities_logged_total`, `cycle_events_logged_total`
- **Health Import** — `health_import_total` (по data_type: weight/activity/cycle), `health_import_latency_seconds`

### Логирование

Structured JSON logs на stderr:

```json
{"ts": "2026-03-24T12:00:00", "level": "INFO", "logger": "mcp_health.db", "msg": "Database connection opened"}
```

### Grafana

Dashboard `grafana/mcp-health-dashboard.json` — деплоится автоматически в `/var/lib/grafana/dashboards/`.

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
  db.py                # SQLite: продукты (локальные + OFF), приёмы, вес, цели, активность, цикл, FTS5
  calc.py              # Расчёты КБЖУ, нормализация, валидация
  config.py            # Конфигурация из env vars
  metrics.py           # Prometheus-метрики и декораторы инструментации
  log.py               # Structured JSON logging
scripts/import_off.py  # Импорт OFF в единую таблицу products (full CSV + delta updates)
grafana/               # Grafana dashboard
ansible/               # Ansible-роли для деплоя на VPS (app, nginx, monitoring, backup)
.github/workflows/     # CI (ruff + pytest) и CD (GHCR + SSH deploy)
```

## Тесты

```bash
pytest tests/ -v
```
