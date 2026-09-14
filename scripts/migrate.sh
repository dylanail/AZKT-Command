#!/usr/bin/env bash
# Apply database migrations (Railway release command / manual).
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHONPATH=. alembic -c backend/alembic.ini upgrade head
