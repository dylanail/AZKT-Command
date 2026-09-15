# AZKT — Arizona Kei Trucks operations app

Desktop and mobile web app that runs AZKT's daily operations: inquiries, vehicles, import
requests, tasks and reminders, evidence, costs and payments, listings — with an AI Manager that
Dylan directs from the app, Telegram or an authorized external agent. Built from the v4 handoff in
[`docs/handoff/`](docs/handoff/) (full spec, acceptance tests, decisions, design references).

- Architecture and conventions: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Operations runbook (run, deploy, migrate, back up, restore, connection setup): [`docs/RUNBOOK.md`](docs/RUNBOOK.md)
- Railway topology: [`deploy/railway/README.md`](deploy/railway/README.md)
- Acceptance results: [`docs/RESULTS.md`](docs/RESULTS.md) · Route map (prototype screens → routes): [`docs/ROUTE_MAP.md`](docs/ROUTE_MAP.md)

## Stack

| Layer | Technology |
|---|---|
| API / web | Python 3.11, FastAPI, SQLAlchemy 2 (async), Postgres 16, Alembic |
| Worker | `backend/worker.py` — Postgres job queue with leases + fencing, outbox, sweeps |
| Agent runtime | Anthropic SDK (`claude-opus-5` default), typed tools over the command layer, MCP server at `/mcp` |
| Frontend | React 18 + TypeScript + Vite PWA, "Liquid Glass II" design tokens |
| Auth | Passkeys only (WebAuthn), roles owner / manager / mechanic (+ presets), per-person overrides |

Every business write goes through one command layer (`backend/app/domain/commands.py`) with
server-side policy decisions (Allowed / Needs review / Blocked), idempotency, versions, activity and
an event outbox. Customer sends, publication, bids, payments and terms changes require Dylan's
exact approval; routine internal work runs automatically.

## Quick start (development)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements-dev.txt
cp .env.example .env                      # set DATABASE_URL, SETUP_TOKEN, SESSION_SECRET, ENCRYPTION_KEY
createdb azkt_command && createdb azkt_test
scripts/migrate.sh
PYTHONPATH=. python -m backend.app.main   # API (dev mode also serves /enroll for the first passkey)
PYTHONPATH=. python -m backend.worker     # durable worker (reminders, jobs, sync)
cd frontend && npm ci && npm run dev
PYTHONPATH=. pytest -q                    # tests run against a real Postgres (TEST_DATABASE_URL)
```

## Repository layout

```
backend/app/core        settings, time, money, errors, crypto
backend/app/db.py       engine + BusinessRow mixin (id, business_id, version, audit)
backend/app/models      one module per domain (auto-imported)
backend/app/domain      commands (single write path), policy, access, jobs, events, actors
backend/app/services    domain services and commands per area
backend/app/adapters    provider clients: model, google_oauth, gmail, drive, sheets, square, telegram, wordpress, email
backend/app/agent       agent runtime, tools, Manager chat, MCP server
backend/app/routers     HTTP API (auto-discovered), webhooks
backend/worker.py       always-on worker
backend/alembic         migrations
frontend/src            PWA (app shell, ui kit, screens, lib)
deploy/, Dockerfile     Railway services; legacy droplet units kept under deploy/systemd
docs/                   handoff spec, architecture, runbook, results
shim/, usage/, config/  legacy OpenClaw shims and token ledger (retained as legacy adapters)
```

## Legacy (droplet era)

The Notion mirror, OpenClaw shims and token-usage ledger from the previous build are retained under
`/api/legacy/*`, `shim/` and `usage/` and are off by default (`LEGACY_BACKGROUND_LOOP=false`).
