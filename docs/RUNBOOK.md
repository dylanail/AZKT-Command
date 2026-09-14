# AZKT operations runbook

This is the manual operations reference for the AZKT app (spec §13). It describes how to run,
deploy, migrate, back up, restore and recover the system, and what each connection needs before
a feature can be activated. Nothing here connects an account by itself.

## 1. Services

| Service | Command | Health |
|---|---|---|
| web/api | `python -m backend.app.main` | `GET /healthz` (process) · `GET /readyz` (database) |
| worker | `python -m backend.worker` (`--once` for a single pass) | `GET /api/settings/health` shows worker heartbeat, queue depth, outbox lag |
| postgres | managed (Railway) or local `pg_ctl` | `SELECT 1` via `/readyz` |

The API serves the built PWA from `frontend/dist` when present. Production refuses to start without
`ENCRYPTION_KEY`, and treats any module import error as fatal.

## 2. Local development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements-dev.txt
cp .env.example .env            # fill DATABASE_URL, SETUP_TOKEN, SESSION_SECRET, ENCRYPTION_KEY
createdb azkt_command && createdb azkt_test
scripts/migrate.sh              # alembic upgrade head
PYTHONPATH=. python -m backend.app.main     # API on 127.0.0.1:8787
PYTHONPATH=. python -m backend.worker       # durable worker
cd frontend && npm ci && npm run dev        # Vite dev server proxying /api and /auth
```

First owner bootstrap: open `/` (or `/enroll`), enter `SETUP_TOKEN`, create the first passkey.
Invite people from Settings → Team; they register a passkey via `/invite/<token>`.

Tests (real Postgres): `PYTHONPATH=. pytest -q`.

## 3. Deploying on Railway

See `deploy/railway/README.md`. Two services from one Dockerfile (`web`, `worker`), one Postgres,
one bucket or volume. `railway.json` runs `scripts/migrate.sh` before each web deploy. Set every
variable from `.env.example` per environment; staging and production must have separate
databases, Telegram bots, Square environments, WordPress targets and reminder recipients (H08).

Rollback: redeploy the previous image. Migrations are backward compatible for queued work; if a
migration must be reverted, run `PYTHONPATH=. alembic -c backend/alembic.ini downgrade -1` on the
previous image, then redeploy.

## 4. Backups and restore (RPO ≤ 24h, RTO ≤ 4h)

- Database: Railway daily backups plus `pg_dump -Fc "$DATABASE_URL" > azkt-$(date +%F).dump` from a
  scheduled job. Keep 30 days.
- Files: bucket contents under `assets/` are content-addressed and immutable; sync them to a second
  bucket or download nightly (`aws s3 sync` with the S3-compatible endpoint).
- Restore drill (staging): create an empty database, `pg_restore -d "$URL" azkt-<date>.dump`, point a
  staging web+worker at it with the restored bucket, then run `python -m backend.worker --once` —
  startup recovers expired leases, re-queues due deliveries and leaves `result_unknown` external
  actions for reconciliation (H09). Verify `/api/settings/health` before enabling outbound work.
- A DB-only backup is incomplete if `assets/` cannot be restored: the health page reports assets
  whose storage key is missing.

## 5. Operating checks

Watch (Settings → Recovery / `/api/settings/health`): job lag, failed/unknown external actions,
provider freshness (`warn`/`expired`), reminder lateness, webhook rejections, matching exceptions,
model spend versus budget, last successful backup. Alerts to the owner are limited to actionable
issues (connection expired, unknown action, budget cap, reminder delivery failure).

Global pause: Settings → Automation → Pause all (or `POST /api/settings/pause {"key":"global"}`).
Pausing shows pending/unknown external actions; resuming revalidates queued approvals.

## 6. Connection setup (what each feature needs)

| Feature | Setup item | Held until provided |
|---|---|---|
| Business email ingest/reply drafts | Google OAuth client + connect `info@azkeitrucks.com` (S01/S13) | Inbox shows "Not connected"; drafts can still be prepared from manual/pasted threads |
| Personal Sebastian/port mail | Connect personal Google account + explicit sender/thread allowlist (S02) | No personal ingestion |
| Ledger import | Connect Sheets, choose sheet/tab, map columns, preview, activate (S03) | Costs entered manually or from emails only |
| Importer photos | Connect Drive, choose `Dylan Nail Shipments` folder id (S04) | Photos via app/Telegram intake only |
| Website publication | WP/Woo base URL + app password / consumer keys, discovery, staging preview (S05/S06) | Listing packages prepared locally; publication "unsupported" |
| Square reconciliation | Square token + webhook signature key + notification URL (S07) | Email signals stay "Payment reported — needs confirmation"; manual confirmed payments work |
| Telegram | Bot token, webhook secret, bot username; owner pairs from Settings (S10) | Email-only reminders |
| Reminder email | SMTP or Gmail send scope + verified owner address (S10) | Reminders logged, not sent |
| AI Manager / intake analysis | `ANTHROPIC_API_KEY` + `MODEL_DAILY_BUDGET_USD` (S14) | Deterministic fallbacks: reminders, lists, approvals, intake text splitting |
| Voice transcription | `TRANSCRIBE_URL` (self-hosted STT) or browser transcript | Audio kept; "transcript needed" with typed entry |
| External agents | Register a client in Settings → Connections → External agents (S17) | `/mcp` and `/api/integrations/v1` reject unknown tokens |

## 7. Incident playbook

- **Gmail history cursor invalid** → the worker starts a bounded resync of the approved scope; Inbox shows the coverage gap until catch-up completes. Do not mark all-clear manually.
- **Result unknown on a send/publish** → open the approval; reconcile from the provider (message id / product id) before any retry. Retry reuses the same logical action.
- **Telegram bot blocked/token rotated** → Settings → Connections → Telegram: update token, re-set webhook, owner re-pairs. Email fallback carries reminders meanwhile.
- **Budget cap reached** → discretionary model work stops; reminders/ingestion/approvals continue. Raise the cap in variables and redeploy the worker.
- **Access removal** → Settings → Team → Disable: the person's sessions and queued authorizations are invalidated immediately.
