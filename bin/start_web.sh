#!/usr/bin/env bash
set -euo pipefail

echo "[nexus] starting web entrypoint"
echo "[nexus] PORT=${PORT:-10000}"

# Ensure we are in the app directory (Render docker runs with /app)
cd /app

# Ensure DATABASE_URL is set (required for migrations)
if [ -z "${DATABASE_URL:-}" ]; then
    echo "[nexus] ERROR: DATABASE_URL is not set!"
    echo "[nexus] Set DATABASE_URL in Render environment variables"
    exit 1
fi

# Ensure app.py exists
if [ ! -f "app.py" ]; then
    echo "[nexus] ERROR: app.py not found!"
    exit 1
fi

# Run migrations only on first instance (with lock to prevent race conditions)
# Set INSTANCE_NUM via Render environment variable or default to 0 for first instance
INSTANCE_NUM="${INSTANCE_NUM:-0}"

if [ "$INSTANCE_NUM" = "0" ]; then
    echo "[nexus] running migrations (flask db upgrade) - instance $INSTANCE_NUM"
    echo "[nexus] DATABASE_URL: ${DATABASE_URL:0:50}..."
    echo "[nexus] SQLALCHEMY_DATABASE_URI: ${SQLALCHEMY_DATABASE_URI:-not set (using DATABASE_URL)}"

    # Add timeout and retries to handle concurrent upgrade attempts
    for attempt in 1 2 3; do
        echo "[nexus] migration attempt $attempt/3..."

        # Debug: Test if Python/Flask works
        echo "[nexus] testing Python import..."
        python -c "import flask; print('[nexus] Flask import OK')" 2>&1 || echo "[nexus] Flask import FAILED"

        echo "[nexus] testing app import..."
        python -c "from app import app; print('[nexus] App import OK')" 2>&1 || echo "[nexus] App import FAILED"

        MIGRATION_LOG=$(mktemp)
        MIGRATION_LOG_STDERR=$(mktemp)
        MIGRATION_LOG_STDOUT=$(mktemp)

        echo "[nexus] starting flask db upgrade..."
        # Run migration with verbose output and separate stderr/stdout
        timeout 30 python -u -c "
import sys
import os
print('[nexus] Python started', flush=True)
print(f'[nexus] CWD: {os.getcwd()}', flush=True)
print(f'[nexus] DATABASE_URL: {os.getenv(\"DATABASE_URL\", \"NOT SET\")[:50]}...', flush=True)

from flask_migrate import upgrade
from app import app

print('[nexus] About to create app context', flush=True)
with app.app_context():
    print('[nexus] App context created, starting upgrade', flush=True)
    upgrade()
    print('[nexus] Upgrade complete', flush=True)
" > "$MIGRATION_LOG_STDOUT" 2> "$MIGRATION_LOG_STDERR" || EXIT_CODE=$?

        EXIT_CODE=${EXIT_CODE:-$?}

        echo "[nexus] migration attempt failed with exit code $EXIT_CODE"
        echo "[nexus] === STDOUT ==="
        cat "$MIGRATION_LOG_STDOUT"
        echo "[nexus] === STDERR ==="
        cat "$MIGRATION_LOG_STDERR"
        echo "[nexus] === END ==="

        # Check if migration succeeded (exit code 0)
        if [ "$EXIT_CODE" = "0" ]; then
            echo "[nexus] migrations completed successfully"
            rm -f "$MIGRATION_LOG_STDOUT" "$MIGRATION_LOG_STDERR"
            break
        else
            if [ $attempt -lt 3 ]; then
                echo "[nexus] retrying in 5s..."
                sleep 5
            else
                echo "[nexus] migration failed after 3 attempts, exiting"
                rm -f "$MIGRATION_LOG_STDOUT" "$MIGRATION_LOG_STDERR"
                exit 1
            fi
        fi

        rm -f "$MIGRATION_LOG_STDOUT" "$MIGRATION_LOG_STDERR"
    done
else
    echo "[nexus] skipping migrations on instance $INSTANCE_NUM (only run on instance 0)"
    # Wait for instance 0 to finish migrations before starting
    echo "[nexus] waiting for migrations to complete..."
    sleep 10
fi

echo "[nexus] starting gunicorn"
exec gunicorn -w "${WEB_CONCURRENCY:-2}" -k gthread --threads "${GTHREADS:-8}" -b "0.0.0.0:${PORT:-10000}" app:app

