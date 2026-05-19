#!/usr/bin/env bash
# One-command deploy/reload on the droplet. Idempotent. Run as the service
# user from /opt/azkt-command. Your droplet agent can run this whenever you
# say "redeploy" — no manual git pulling.
set -euo pipefail

BRANCH="${BRANCH:-main}"
ROOT="${ROOT:-/opt/azkt-command}"
cd "$ROOT"

echo "▸ Fetching $BRANCH"
git fetch origin "$BRANCH"
BEFORE="$(git rev-parse HEAD)"
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"
AFTER="$(git rev-parse HEAD)"

echo "▸ Python deps"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
if ! git diff --quiet "$BEFORE" "$AFTER" -- backend/requirements.txt 2>/dev/null; then
  pip install -q -r backend/requirements.txt
else
  pip install -q -r backend/requirements.txt >/dev/null 2>&1 || true
fi

echo "▸ Frontend build"
if command -v npm >/dev/null; then
  ( cd frontend && npm ci --silent && npm run build --silent )
else
  echo "  npm not found — skipping frontend build (install Node 18+)."
fi

echo "▸ Restarting services"
# System units (API). Adjust if you run them as --user.
sudo systemctl restart azkt-api 2>/dev/null || systemctl --user restart azkt-api || true
for a in watcher scout inventory inbox main; do
  sudo systemctl restart "azkt-shim@$a" 2>/dev/null \
    || systemctl --user restart "azkt-shim@$a" 2>/dev/null || true
done
sudo systemctl restart azkt-usage-collector 2>/dev/null \
  || systemctl --user restart azkt-usage-collector || true

echo "▸ Reloading nginx"
sudo nginx -t && sudo systemctl reload nginx || echo "  (skipped nginx reload)"

echo "✓ Deployed $AFTER"
echo "  Health: curl -s 127.0.0.1:8787/healthz"
