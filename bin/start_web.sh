#!/usr/bin/env bash
set -euo pipefail

echo "[nexus] starting web entrypoint"
echo "[nexus] PORT=${PORT:-10000}"

# Ensure we are in the app directory (Render docker runs with /app)
cd /app

echo "[nexus] running migrations (flask db upgrade)"
python -m flask --app app:app db upgrade

echo "[nexus] starting gunicorn"
exec gunicorn -w "${WEB_CONCURRENCY:-2}" -k gthread --threads "${GTHREADS:-8}" -b "0.0.0.0:${PORT:-10000}" app:app

