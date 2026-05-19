# AZKT Command

Personal command center for Arizona Kei Trucks. Single user. Postgres is the
source of truth; Notion is a bidirectional mirror. Backend + OpenClaw agents
stay on loopback — only the reverse proxy is public.

> **Status: full stack built** — backend + agent shims + token-usage
> retrofit + installable iPhone PWA (Face ID, pull-to-refresh, quick-add,
> Kanban, per-agent chat with persistent history). One-command deploy:
> `scripts/deploy.sh`.

## You must fill these before it works (nothing is guessed)

| File | What goes in it | Source |
|---|---|---|
| `.env` | secrets, DB URL, Notion token + DB ids, setup token | copy `.env.example` |
| `config/agents.yaml` | each agent's `agent_id`, `workspace`, loopback `port` | context doc |
| `config/notion_schema.json` | **exact** Notion property names, char-for-char | copy from Notion |
| `config/notifications.yaml` | notification tiers + thresholds | context doc |
| `config/pricing.json` | per-model $ rates (defaults provided, verify) | Anthropic pricing |

The code refuses to start an unconfigured agent and reports every unfilled
Notion mapping on the health page rather than syncing wrong data silently.

## Architecture

- **Shims** (`shim/`): one loopback FastAPI sidecar per OpenClaw agent —
  `/health` `/state` `/run` `/chat` and git-versioned `SOUL.md` editing with
  rollback. Drives OpenClaw via its CLI + loopback Gateway (`:18789`).
- **Usage retrofit** (`shim/usage_ledger.py`, `usage/collector.py`):
  crash-safe append-only JSONL. The collector backfills from OpenClaw session
  transcripts since the agents don't self-report tokens.
- **Backend** (`backend/`): FastAPI + Postgres. Passkey-only auth (first
  registration gated by `SETUP_TOKEN`). Every business row carries
  `business_id` (default `AZKT`). `shipping_events` table exists but empty.
  `forbidden_recipient` validator enforced now.

## Run (dev)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt
cp .env.example .env   # then edit
createdb azkt_command
PYTHONPATH=. python -m backend.app.main          # api on 127.0.0.1:8787
PYTHONPATH=. python -m shim.run_shim watcher     # once agents.yaml is filled
PYTHONPATH=. python -m usage.collector --watch
```

Frontend (dev): `cd frontend && npm install && npm run dev` (proxies to the
loopback API). Production build: `npm run build` → `frontend/dist` served by
nginx at `dash.arizonakeitrucks.com`.

Production deploy/reload (run on the droplet, or have your agent run it):

```bash
ROOT=/opt/azkt-command scripts/deploy.sh   # pull, build, restart, reload nginx
```

Units: `deploy/systemd/*` + `deploy/nginx.conf.example`.

## Out of scope for v1 (by design)

Customs/transport email parsing (manual stage flips; `shipping_events` left
empty for the future parser), GoHighLevel/Twilio/Square (shown "Not connected",
form to add later), historical backfill, photo/VIN/pricing/sourcing agents.
