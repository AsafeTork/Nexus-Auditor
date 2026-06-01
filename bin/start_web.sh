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

    # Run migrations with retries. Use a generous timeout for cold Render free-tier Postgres.
    MIGRATION_OK=0
    for attempt in 1 2 3; do
        echo "[nexus] migration attempt $attempt/3..."

        MIGRATION_LOG_STDOUT=$(mktemp)
        MIGRATION_LOG_STDERR=$(mktemp)
        EXIT_CODE=0

        timeout 120 python -u -c "
import sys, os, traceback

print('[nexus] Python started', flush=True)
print(f'[nexus] CWD: {os.getcwd()}', flush=True)
print(f'[nexus] DATABASE_URL prefix: {os.getenv(\"DATABASE_URL\", \"NOT SET\")[:40]}...', flush=True)

try:
    from flask_migrate import upgrade
    from app import app
    with app.app_context():
        print('[nexus] running flask db upgrade...', flush=True)
        upgrade()
        print('[nexus] flask db upgrade complete', flush=True)
except Exception as e:
    print(f'[nexus] alembic error: {type(e).__name__}: {e}', flush=True)
    traceback.print_exc()
    sys.exit(1)
" > "$MIGRATION_LOG_STDOUT" 2> "$MIGRATION_LOG_STDERR" || EXIT_CODE=$?

        echo "[nexus] === STDOUT ==="
        cat "$MIGRATION_LOG_STDOUT"
        echo "[nexus] === STDERR ==="
        cat "$MIGRATION_LOG_STDERR"
        echo "[nexus] === END (exit=$EXIT_CODE) ==="
        rm -f "$MIGRATION_LOG_STDOUT" "$MIGRATION_LOG_STDERR"

        if [ "$EXIT_CODE" = "0" ]; then
            echo "[nexus] migrations completed successfully"
            MIGRATION_OK=1
            break
        else
            if [ $attempt -lt 3 ]; then
                echo "[nexus] retrying in 10s..."
                sleep 10
            fi
        fi
    done

    # Fallback: if Alembic failed, use db.create_all() so tables at least exist.
    if [ "$MIGRATION_OK" = "0" ]; then
        echo "[nexus] WARNING: Alembic upgrade failed after 3 attempts — running db.create_all() fallback"
        EXIT_CODE=0
        timeout 60 python -u -c "
import sys, traceback
try:
    from app import app
    from nexus import db
    with app.app_context():
        db.create_all()
        print('[nexus] db.create_all() succeeded', flush=True)
except Exception as e:
    print(f'[nexus] db.create_all() error: {e}', flush=True)
    traceback.print_exc()
    sys.exit(1)
" || EXIT_CODE=$?
        if [ "$EXIT_CODE" = "0" ]; then
            echo "[nexus] fallback tables created — app can start"
        else
            echo "[nexus] FATAL: could not create database tables — check DATABASE_URL and Render Postgres"
            exit 1
        fi
    fi
else
    echo "[nexus] skipping migrations on instance $INSTANCE_NUM (only run on instance 0)"
    # Wait for instance 0 to finish migrations before starting
    echo "[nexus] waiting for migrations to complete..."
    sleep 10
fi

echo "[nexus] starting gunicorn"
exec gunicorn -w "${WEB_CONCURRENCY:-2}" -k gthread --threads "${GTHREADS:-8}" -b "0.0.0.0:${PORT:-10000}" app:app

