# Deep Debugging Guide for Migration Failures

## If migrations still fail silently

This guide helps diagnose the "Exited with status 1" issue in production.

### Step 1: Check the Render Log More Carefully

Look for patterns in the timestamps:
```
2026-05-02T19:49:36.634438701Z [nexus] running migrations (flask db upgrade)
2026-05-02T19:49:38.862347776Z INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
2026-05-02T19:49:40.307487351Z ==> Exited with status 1
```

**Time gap = 1.4 seconds** between logs and failure. This is suspicious - either:
- A database operation is timing out
- An import is failing
- A configuration is missing

### Step 2: Add Emergency Logging to env.py

If you still see failures, we can add STDERR output:

```bash
# Render allows accessing recent logs, check for stderr output
# It might have printed an error to stderr that wasn't captured
```

### Step 3: Test Locally with Production Database

Create a test script:

```bash
# test_migration.py
import os
import sys

# Set production database URL
os.environ["DATABASE_URL"] = "postgresql://user:pass@host:port/db"
os.environ["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL")

try:
    print("1. Importing app...")
    from app import app
    print("   ✓ App imported")
    
    print("2. Creating app context...")
    with app.app_context():
        print("   ✓ App context created")
        
        print("3. Getting database...")
        db = app.extensions.get("sqlalchemy")
        print(f"   ✓ Database: {db}")
        
        print("4. Testing connection...")
        from sqlalchemy import text
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("   ✓ Connection works")
        
        print("5. Running migrations...")
        from flask_migrate import upgrade
        upgrade()
        print("   ✓ Migrations complete")

except Exception as e:
    import traceback
    print(f"✗ ERROR: {e}")
    traceback.print_exc()
    sys.exit(1)
```

Run it:
```bash
python test_migration.py
```

### Step 4: Common Silent Failures

#### Issue: PostgreSQL Connection String Invalid
```
DATABASE_URL=postgresql://user:pass@localhost:5432/db
# vs
DATABASE_URL=postgres://user:pass@localhost:5432/db
```
PostgreSQL deprecated `postgres://` in favor of `postgresql://`

**Fix:** Ensure DATABASE_URL uses `postgresql://` not `postgres://`

#### Issue: Database Doesn't Exist
Migration tries to connect to a database that doesn't exist.

**Fix:** Create database first, or use a SQL init script

#### Issue: Table Already Locked
Another migration is running concurrently.

**Check:** Look for lock warnings in logs

**Fix:** Ensure only one instance runs migrations at startup

#### Issue: Bad Default Value in Migration
A migration tries to set a default that's invalid.

**Example:**
```python
op.add_column('sites', sa.Column('status', sa.String, server_default='invalid'))
# PostgreSQL rejects invalid string for String column
```

**Fix:** Make sure server_defaults are valid for the column type

### Step 5: Enable Debug Mode

Modify your startup to show more info:

```bash
# In Render's build command or deploy script
echo "=== PRE-MIGRATION DEBUG ==="
echo "DATABASE_URL: $DATABASE_URL"
echo "FLASK_ENV: $FLASK_ENV"
echo "Python: $(python --version)"
echo "=== RUNNING MIGRATIONS ==="
python -u -m flask db upgrade 2>&1
echo "=== MIGRATION COMPLETE ==="
```

The `-u` flag disables buffering so all output appears.

### Step 6: Check alembic_version Table

After deployment fails, if you have DB access:

```sql
-- Check what migrations are recorded
SELECT * FROM alembic_version;

-- If there's a bad migration
DELETE FROM alembic_version WHERE version_num = 'bad_migration_id';

-- Try upgrade again
-- flask db upgrade
```

### Step 7: Nuclear Option - Reset Migrations

If nothing else works:

```sql
-- WARNING: This drops migration history
DROP TABLE alembic_version;

-- Then re-run migrations from scratch
-- flask db upgrade
```

### Step 8: Check Flask App Initialization

The real problem might be app.py failing during import:

```bash
# Test if app loads
python -c "from app import app; print('App loaded OK')"
```

If this fails, there's an error in:
- `app.py`
- `nexus/__init__.py`
- `nexus/models.py`
- Any imported routes

### Common app.py Import Errors

1. **Missing environment variable**
   ```
   KeyError: 'SECRET_KEY'
   ```
   Fix: Set `SECRET_KEY` env var

2. **Invalid database URL**
   ```
   sqlalchemy.exc.ArgumentError: Invalid database URL
   ```
   Fix: Verify `DATABASE_URL` format

3. **Module not found**
   ```
   ModuleNotFoundError: No module named 'flask'
   ```
   Fix: Ensure all dependencies installed (pip install -r requirements.txt)

4. **Circular import**
   ```
   ImportError: cannot import name 'X' from partially initialized module 'Y'
   ```
   Fix: Check for circular imports between models and routes

## If You See This Pattern

```
[nexus] running migrations (flask db upgrade)
==> Exited with status 1
```

With NO Alembic logs before it = **app.py import failure**

With Alembic logs = **migration execution failure**

## Contact Info for Next Debug Round

When sharing logs next time, include:

1. Full deployment log (all lines)
2. Output of: `echo $DATABASE_URL && echo $SECRET_KEY`
3. Recent git commits: `git log --oneline -5`
4. Python version in Dockerfile
5. Any custom startup scripts

This will help diagnose exactly where the failure happens.
