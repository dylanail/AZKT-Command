# Railway topology (spec §13.1)

**Setting it up from nothing: [SETUP.md](SETUP.md).** This file is the topology reference.

| Service | Source | Start command | Notes |
|---|---|---|---|
| `web` | this repo, Dockerfile | `python -m backend.app.main` | `railway.json` at repo root. Pre-deploy runs `scripts/migrate.sh`. Health `/readyz`. Serves the built PWA from `frontend/dist` and the API. |
| `worker` | this repo, Dockerfile | `python -m backend.worker` | Copy `deploy/railway/worker.railway.json` as the service's config (Railway → Settings → Config-as-code path). Exactly one replica; leases make a second replica safe but unnecessary at launch. |
| `postgres` | Railway Postgres | – | Set `DATABASE_URL` on both services (use the private network URL). `pg_trgm` is created by the migration; `vector` is optional and feature-detected. |
| storage | Railway bucket **or** a mounted volume | – | `STORAGE_BACKEND=s3` + `S3_*` for a bucket; otherwise mount a volume and point `DATA_DIR` inside it. Not optional — see below. |

## Photo storage is not optional

A Railway service filesystem is replaced on every deploy. With the default
`STORAGE_BACKEND=local` and no mounted volume, every uploaded photo is thrown away the next time you
deploy. Nothing fails at that moment: it surfaces later as a listing that cannot be published because
its approved photos have no bytes left to upload to WooCommerce.

Pick one, on both `web` and `worker` (they share the files):

* **Bucket** — `STORAGE_BACKEND=s3`, `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, and `S3_ENDPOINT`
  for a non-AWS S3-compatible bucket. Objects are content-addressed under `assets/` and immutable.
* **Volume** — attach a Railway volume to both services and set `DATA_DIR` to a path inside the
  volume's mount (Railway exposes it as `RAILWAY_VOLUME_MOUNT_PATH`).

`GET /api/health` reports `storage.durable`. It is `false`, and the page is not "ok", whenever the
app is running on Railway with neither of the above. Check it after the first deploy.

## The port

Railway assigns each deploy a port and injects it as `PORT`. `backend.app.main` listens on that when
it is set, falling back to `API_PORT` for a droplet behind nginx. Do not set `PORT` yourself, and do
not set `API_PORT` on Railway — leave the injected value alone. `API_HOST` must be `0.0.0.0`; the
default `127.0.0.1` is only reachable from inside the container and the health check would never pass.

## Both services must run

The `worker` service is not optional either. It runs the sweeps: reminder delivery, provider
reconciliation, and the hourly `listings.availability_scan` that notices a price or stock change made
on the website outside AZKT. With only `web` deployed, listings are published but never re-verified.

Environment variables: see `.env.example`. Separate staging and production projects with their own
databases, Telegram bots, Square sandbox, WordPress staging URL and reminder recipient (spec §13.4).
Production refuses to start without `ENCRYPTION_KEY` and treats module import errors as fatal.
