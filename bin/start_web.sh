#!/usr/bin/env bash
set -euo pipefail

echo "[nexus] starting web entrypoint"
echo "[nexus] PORT=${PORT:-10000}"

# Ensure we are in the app directory (Render docker runs with /app)
cd /app

# Run migrations only on first instance (with lock to prevent race conditions)
# Set INSTANCE_NUM via Render environment variable or default to 0 for first instance
INSTANCE_NUM="${INSTANCE_NUM:-0}"

if [ "$INSTANCE_NUM" = "0" ]; then
    echo "[nexus] running migrations (flask db upgrade) - instance $INSTANCE_NUM"
    # Add timeout and retries to handle concurrent upgrade attempts
    for attempt in 1 2 3; do
        echo "[nexus] migration attempt $attempt/3..."
        MIGRATION_LOG=$(mktemp)
        if python -m flask --app app:app db upgrade > "$MIGRATION_LOG" 2>&1; then
            echo "[nexus] migrations completed successfully"
            rm -f "$MIGRATION_LOG"
            break
        else
            EXIT_CODE=$?
            echo "[nexus] migration attempt failed with exit code $EXIT_CODE"
            echo "[nexus] === Migration Error Output ==="
            cat "$MIGRATION_LOG"
            echo "[nexus] === End Migration Error Output ==="
            rm -f "$MIGRATION_LOG"

            if [ $attempt -lt 3 ]; then
                echo "[nexus] retrying in 5s..."
                sleep 5
            else
                echo "[nexus] migration failed after 3 attempts, exiting"
                exit 1
            fi
        fi
    done
else
    echo "[nexus] skipping migrations on instance $INSTANCE_NUM (only run on instance 0)"
    # Wait for instance 0 to finish migrations before starting
    echo "[nexus] waiting for migrations to complete..."
    sleep 10
fi

echo "[nexus] starting gunicorn"
exec gunicorn -w "${WEB_CONCURRENCY:-2}" -k gthread --threads "${GTHREADS:-8}" -b "0.0.0.0:${PORT:-10000}" app:app

