# Railway topology (spec §13.1)

| Service | Source | Start command | Notes |
|---|---|---|---|
| `web` | this repo, Dockerfile | `python -m backend.app.main` | `railway.json` at repo root. Pre-deploy runs `scripts/migrate.sh`. Health `/readyz`. Serves the built PWA from `frontend/dist` and the API. |
| `worker` | this repo, Dockerfile | `python -m backend.worker` | Copy `deploy/railway/worker.railway.json` as the service's config (Railway → Settings → Config-as-code path). Exactly one replica; leases make a second replica safe but unnecessary at launch. |
| `postgres` | Railway Postgres | – | Set `DATABASE_URL` on both services (use the private network URL). `pg_trgm` is created by the migration; `vector` is optional and feature-detected. |
| storage | Railway bucket or local volume | – | `STORAGE_BACKEND=s3` + `S3_*` for a bucket; otherwise mount a volume at `DATA_DIR`. |

Environment variables: see `.env.example`. Separate staging and production projects with their own
databases, Telegram bots, Square sandbox, WordPress staging URL and reminder recipient (spec §13.4).
Production refuses to start without `ENCRYPTION_KEY` and treats module import errors as fatal.
