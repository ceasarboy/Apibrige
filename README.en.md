# APIBridge

<div align="center">

**Data exchange platform · continuous source-to-target sync · built-in BI dashboards**

[简体中文](README.md) | **English**

![BI Production Dashboard](docs/images/bi-production-board.png)

*BI production dashboard: KPIs + multi-dimension charts + aggregate pushdown (million rows in milliseconds)*

</div>

---

## What is APIBridge

**APIBridge is a data exchange platform**: business data between source and target systems stays **continuously synchronized** — inserts, updates and deletes are all captured and applied to the target. It is not a one-shot API debugger or a copy script, but a complete chain of *connect → transform correctly → keep aligned*:

1. **Connect**: unified management of API and database connections, credentials encrypted at rest, probes verify before a bridge is created;
2. **Transform correctly**: field mapping + field dictionary + per-record transformation, with honest L1/L2/L3 verification semantics;
3. **Keep aligned**: full, incremental (watermark) and delete-reconciliation modes, with Cron scheduling and webhook triggers.

On top of sync, a built-in **BI dashboard & analysis** suite turns tables in your connections into KPI cards, chart boards and public projection pages — no third-party BI required.

## Features

### 1. Sync core

![Connections](docs/images/connections.png)

- **Connection management**: REST / SOAP / GraphQL / WebSocket / gRPC / Dubbo / Mingdao (HAP) APIs, plus Oracle / MySQL / PostgreSQL / SQLite databases; credentials encrypted (`enc:`) and redacted on output.
- **Sync tasks**: full, incremental (watermark triple-check + rounds/lease/checkpoint retry), **delete reconciliation** (key-based diff cleanup with full-source-set guards); Cron scheduling, webhook triggers (secret enforced), failover-to-async, idempotent short-circuit.
- **Field mapping & dictionary**: dual-pane field selection, type-compatibility checks, per-field readable/writable control.
- **Execution logs**: per-request request/response/error details with row- and field-level failure drill-down.

### 2. Bridge workflow (probe → review queue)

The only way to create a bridge is **propose → verify → human approval**:

- **L1** connectivity / **L2** transformation correctness (no target writes) / **L3** end-to-end (requires human authorization, writes 1 real record and reads it back);
- Schema inference + mapping draft (falls back to name+type matching without a model);
- Approval creates connection + mapping + task in a single transaction — agents and probes cannot create bridges directly.

### 3. BI dashboards & analysis

![Projection mode](docs/images/bi-screen-projection.png)

- **Metric definitions**: dimensions / measures / filters / calculated fields (row-level expression engine) / aggregate ratios / dimension grouping / TopN;
- **Aggregation engine**: time-bucket pre-aggregation with watermark-based incremental advance; automatic **aggregate pushdown** for large DB tables (million rows in milliseconds; single SQL execution point with identifier whitelist + bound values, can be disabled);
- **Boards**: 72×36 free canvas, multi-board, dimension filters, layout editor;
- **Projection mode**: public big-screen pages (dark/light themes, four viewport-fit modes, auto-scrolling detail table) with **read-only access grants** — hashed tokens + board allowlist + row filters + column hiding, enforced server-side;
- **Analysis workbench**: field tree + drag-and-drop ad-hoc queries, save as board card.

![Analysis workbench](docs/images/bi-analysis.png)

*Analysis workbench: aggregate pushdown in 2ms; save as board card*

### 4. Monitoring & operations

![Monitoring](docs/images/monitoring.png)

- Monitoring board: task/connection health, latency percentiles, normalized failure Top N;
- Prometheus `/metrics` endpoint + Grafana example configs;
- Execution logs, send/receive comparison, probe session management.

### 5. Security

- Fernet-encrypted credentials (`enc:`); keys only in local `.env`;
- Admin token + separate BI read-only tokens; write endpoints denied by default;
- Webhook secret enforced (constant-time comparison); probe SSRF guard (DNS pre-resolution + private-range denial + allowlist).

## Quick start

### Option 1: Windows portable package (batteries included)

Ships with a Python 3.12 runtime, Redis, Oracle Instant Client 19 and a prebuilt frontend:

```cmd
start.cmd       rem start (backend + Redis + frontend)
status.cmd      rem status
stop.cmd        rem stop
```

Open `http://127.0.0.1:3000` and paste the value of `APIBRIDGE_ADMIN_TOKEN=` from `.env` into the "Token" box. First start generates a random token, encryption key and a fresh database; back up `.env`, `launcher.json` and `data\` together.

### Option 2: run from source

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000          # backend
cd frontend-source && npm ci && npm run dev                 # frontend (dev)
cd frontend-source && npm ci && npm run build               # frontend (build to web/)
```

### Option 3: Docker

`docker-compose.yml` orchestrates app + PostgreSQL + Redis.

## Tests & quality

```bash
python -m pytest tests/ -q     # 1144 passed / 13 skipped (self-contained)
```

- Five rounds of code review closed (4 P0 + 23 P1 + 38 P2/P3, including a re-review of the fixes themselves) — see `MERGE-REPORT.md` §3.1–3.4;
- Live-PostgreSQL deployment verification and a Mingdao API parameter matrix — see `VALIDATION.md`;
- Honest verification semantics: `L2_partial_no_contract` never counts as passing.

## Docs

| Doc | Content |
|-----|---------|
| `AGENTS.md` | Engineering notes: hard constraints, environment traps, code map |
| `docs/bi-dashboard-guide.md` | BI dashboard guide |
| `docs/trial-guide.md` | Trial-phase guide (current phase: **feature freeze, trial**) |
| `MERGE-REPORT.md` / `VALIDATION.md` | Review & verification records |
| `openspec/` | Spec baseline for 13 capability domains |

## Repository layout

```
app/               backend (FastAPI: api / adapters / connectors / probe / services / security)
frontend-source/   frontend source (React 19 + antd v5 + ECharts)
tests/             full pytest suite
openspec/          spec baseline
docs/              design & usage docs (with screenshots)
web/ runtime/ redis/ oracle/   portable-package build & runtimes
```

## Windows portable package notes

- Unzip into a **writable English path**; install `prerequisites\VC_redist.x64.exe` if the VC++ runtime is missing;
- Oracle uses thick mode by default for 11g compatibility (Instant Client 19 included); switch `.env` to thin for 12.1+;
- Probes into intranet targets require `APIBRIDGE_PROBE_ALLOWED_HOSTS` in `.env`;
- Upgrading: stop → back up `.env` / `launcher.json` / `data` → copy into the new package → `migrate.cmd` → start; never overwrite an old `.env` with a new key;
- Use `sync` task mode; `async` / auto-with-fallback requires your own RabbitMQ + Celery worker;
- The portable package targets single-machine intranet use; the Redis Windows port is dev/test grade.

## Current phase

**Feature freeze, public trial** (since 2026-10-01): issues found during the trial are recorded and batch-fixed — see `docs/trial-guide.md`.
