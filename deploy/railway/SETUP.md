# Setting AZKT Command up on Railway, from nothing

Three services in one Railway project: **Postgres**, **web**, **worker**. Both app services build the
same `Dockerfile` from this repo and differ only in their start command.

Work through this in order. Each step says how to tell it worked.

---

## 1. Create the project and the database

1. Railway → **New Project** → **Deploy PostgreSQL**.
2. Nothing else to configure. The baseline migration creates the `pg_trgm` extension it needs;
   `vector` is optional and feature-detected, so a plain Postgres is fine.

## 2. Generate the three secrets

Run these locally and keep the output — you paste them in step 4, and the first two can never be
rotated casually afterwards (a changed `ENCRYPTION_KEY` makes every stored provider token unreadable).

```bash
python3 -c "from cryptography.fernet import Fernet; print('ENCRYPTION_KEY =', Fernet.generate_key().decode())"
python3 -c "import secrets; print('SESSION_SECRET =', secrets.token_hex(32))"
python3 -c "import secrets; print('SETUP_TOKEN    =', secrets.token_urlsafe(32))"
```

`SETUP_TOKEN` is single-use in practice: it gates the registration of the **first** passkey and
nothing else.

## 3. Create the web service

1. **New** → **GitHub Repo** → this repository, branch `main` (or the branch you are deploying).
2. Settings → **Config as code**: leave it at the repo root `railway.json`. It already sets the
   Dockerfile build, `python -m backend.app.main` as the start command, `scripts/migrate.sh` as the
   pre-deploy command and `/readyz` as the health check.
3. Settings → **Networking** → **Generate Domain** (or attach your own). Whatever the final public
   address is, it goes in `PUBLIC_ORIGIN` and `WEBAUTHN_RP_ID` below, and it must match exactly —
   passkeys are bound to the hostname and will silently refuse to register otherwise.

## 4. Set the web service's variables

Minimum to boot:

| Variable | Value |
|---|---|
| `ENV` | `production` |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` — the reference, as-is. The app rewrites the driver for you. |
| `API_HOST` | `0.0.0.0` — the default `127.0.0.1` is unreachable from outside the container |
| `PUBLIC_ORIGIN` | `https://<your domain>` |
| `WEBAUTHN_RP_ID` | `<your domain>`, hostname only, no scheme and no trailing slash |
| `WEBAUTHN_RP_NAME` | `AZKT` |
| `ENCRYPTION_KEY` | from step 2 — production refuses to start without it |
| `SESSION_SECRET` | from step 2 |
| `SETUP_TOKEN` | from step 2 |
| `BUSINESS_EMAIL` | `info@azkeitrucks.com` |

Do **not** set `PORT` or `API_PORT`. Railway injects `PORT` and the app listens on it.

Everything else in `.env.example` is a per-integration setup input: blank means that feature reports
`setup_blocked` in Settings and does nothing, which is a valid state to deploy in. Add them as you
connect each provider.

## 5. Photo storage — pick one, before you upload anything

Railway replaces a service filesystem on every deploy. With the default `STORAGE_BACKEND=local` and
no volume, every uploaded photo is thrown away the next time you deploy, and nothing fails at that
moment: it surfaces later as a listing that cannot be published because its approved photos have no
bytes left to send to WooCommerce.

**Either** a volume:

1. Web service → **Volumes** → **New Volume**, mount at `/data`.
2. Attach a volume to the **worker** service too — they share the same files.
3. Set `DATA_DIR=/data/azkt` on both.

**Or** a bucket:

- Set `STORAGE_BACKEND=s3`, `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` on both services, plus
  `S3_ENDPOINT` for any non-AWS S3-compatible bucket.

Check it: `GET /api/health` reports `storage.durable`. If it is `false`, the page is not "ok" and the
reason says what to fix.

## 6. Create the worker service

1. **New** → **GitHub Repo** → the same repository.
2. Settings → **Config as code** → path `deploy/railway/worker.railway.json`. That sets the start
   command to `python -m backend.worker` and one replica.
3. Give it the **same variables** as the web service. The simplest way is Railway's shared variables,
   or copy the block across.

The worker is not optional. It runs every sweep: reminder delivery, provider reconciliation, calendar
sync, callback delivery, and the hourly website scan. Without it, listings publish but are never
re-verified and no reminder is ever sent.

## 7. First deploy and first sign-in

1. Deploy both services. The web service runs migrations before it starts serving.
2. Open `https://<your domain>/readyz` — expect `{"ok": true}`.
3. Open `https://<your domain>/enroll`, present the `SETUP_TOKEN`, and register your passkey. That
   account is the owner.
4. Sign in and open **Settings → Recovery** (`/api/health`): worker heartbeat present, no unknown
   external actions, `storage.durable` true.

## 8. Connect the providers, one at a time

Each is independent and each reports honestly until it is done. In Settings → Connections unless
noted:

| Feature | What it needs |
|---|---|
| Business email, Drive, Sheets, Calendar | `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`, then connect each as `info@azkeitrucks.com`. The OAuth consent screen must list the scopes for whichever you connect; the Calendar API has to be enabled on the Google Cloud project. |
| Calendar events | After connecting: **Settings → Calendar** → turn on creating events. This is a separate capability on purpose; until it is on, nothing is written. |
| Website listings | WordPress application password (**with media upload permission** — photos go into your media library) and WooCommerce consumer key/secret. Then Settings → Website: read the site, preview on staging, activate. |
| Square | Access token, webhook signature key, and `SQUARE_NOTIFICATION_URL` pointing at `https://<domain>/api/webhooks/square`. |
| Telegram | Bot token, `TELEGRAM_WEBHOOK_SECRET`, bot username, then pair from Settings. |
| Reminder email | `EMAIL_TRANSPORT=smtp` with the SMTP block, or the Gmail send scope, plus `OWNER_REMINDER_EMAIL`. |
| AI Manager | `ANTHROPIC_API_KEY` and a non-zero `MODEL_DAILY_BUDGET_USD`. Zero budget keeps discretionary model work off. |
| Your personal agent | Settings → External agents: register the client, then set its https callback URL and signing secret. |

## Staging

Use a **separate Railway project** with its own database, its own Telegram bot, Square sandbox, the
WordPress staging URL and a test reminder address. Set `ENV=staging`, and list any address a
delivering transport may reach in `NON_PROD_DESTINATION_ALLOWLIST` — outside production, email,
website writes, Telegram, calendar writes and callbacks are all refused unless the destination is on
that list. That guard is what stops a staging deploy emailing a real customer.

## If something is wrong

| Symptom | Cause |
|---|---|
| Health check never passes, service restarts | `API_HOST` is not `0.0.0.0`, or `PORT`/`API_PORT` was set by hand |
| Startup fails on the database | Leave `DATABASE_URL` as the `${{Postgres.DATABASE_URL}}` reference; the driver is normalised in code |
| Startup fails with "ENCRYPTION_KEY must be set in production" | Step 2 |
| Passkey registration does nothing | `WEBAUTHN_RP_ID` must be the bare hostname and must match the address in the browser |
| Photos vanish after a deploy | Step 5; `/api/health` → `storage.durable` |
| Reminders never arrive, listings never verify | The worker service is not deployed or is crash-looping |
