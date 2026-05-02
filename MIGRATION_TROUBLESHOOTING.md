# Migration Troubleshooting Guide

## Quick Diagnostics

### Before Deploying
```bash
# Validate all migration files
python scripts/validate_migrations.py

# Run full diagnostics (local environment)
python scripts/diagnose_migrations.py
```

### During Deployment Failure

If migrations fail during deployment:

1. **Check logs for error message** - The error should appear in the deployment logs
2. **Run diagnostics locally** (if possible)
3. **Check database state** - Which migrations are applied?

## Common Issues and Solutions

### Issue 1: "Table X already defined"
**Cause:** Multiple imports of SQLAlchemy models

**Solution:** ✅ Already fixed with `extend_existing=True` on all models

### Issue 2: KeyError on Migration ID
**Cause:** Migration file has mismatched revision ID vs filename

**Solution:** Run validation:
```bash
python scripts/validate_migrations.py
```

If errors found, the validator will show exactly what to fix.

### Issue 3: Orphaned Migration in Database
**Cause:** A migration was deleted from code but still exists in `alembic_version` table

**Example Error:** `KeyError: '0014_add_site_financial_context'`

**Solution:** We've created a stub migration. If this happens again:
```bash
# Create a stub migration automatically
python scripts/fix_migration_mismatch.py <migration_id>
```

### Issue 4: Failed Migration Chain
**Cause:** `down_revision` points to non-existent migration

**Solution:** The validator will detect this immediately and show which migration to fix:
```
ERROR: Invalid down_revision in XXXXX.py:
    Points to: YYYYY
    But migration not found!
```

## Migration File Structure (Correct Format)

```python
"""Clear description of what this migration does

Revision ID: XXXXX_descriptive_name
Revises: YYYYY_previous_migration_name
Create Date: YYYY-MM-DD
"""

from __future__ import annotations
from alembic import op
import sqlalchemy as sa

# IMPORTANT: revision ID must match filename!
revision = "XXXXX_descriptive_name"
down_revision = "YYYYY_previous_migration_name"  # Must point to actual migration
branch_labels = None
depends_on = None

def upgrade() -> None:
    # Your schema changes here
    pass

def downgrade() -> None:
    # Reverse the upgrade changes
    pass
```

## Key Rules

✅ **DO:**
- Keep `revision ID` matching the filename
- Keep `down_revision` pointing to the previous migration
- Test locally before committing
- Run validation before pushing: `python scripts/validate_migrations.py`

❌ **DON'T:**
- Modify an applied migration (create a new one instead)
- Use `pass` without `op.execute()` in migrations
- Leave orphaned `down_revision` references
- Commit migrations without validating

## Recovery Steps (If Something Breaks)

### For Local Development
```bash
# Drop and recreate database
rm nexus_dev.db

# Recreate from scratch
flask db upgrade
```

### For Production (PostgreSQL)

1. **Check current migration state:**
   ```sql
   SELECT * FROM alembic_version;
   ```

2. **If stuck on a failed migration:**
   ```sql
   DELETE FROM alembic_version WHERE version_num = 'XXX_bad_migration';
   ```

3. **Try again:**
   ```bash
   flask db upgrade
   ```

4. **If migration files are corrupted:**
   - Revert to previous commit
   - Run diagnostics
   - Fix and redeploy

## Files That Help

- `scripts/validate_migrations.py` - Validates all migrations
- `scripts/fix_migration_mismatch.py` - Creates stubs for orphaned migrations
- `scripts/diagnose_migrations.py` - Full diagnostics (requires Flask)
- `VERIFICATION_REPORT.md` - Initial verification summary

## Current Status (as of 2026-05-02)

- ✅ 14 migrations validated
- ✅ No circular dependencies
- ✅ Linear chain: 0001 → 0014
- ✅ All models have `extend_existing=True`
- ✅ Orphaned migration (0014) has stub

## After Deployment

Once deployed successfully:

1. The app should initialize without SQLAlchemy errors
2. All 14 migrations should be marked `complete` in `alembic_version`
3. Database schema should be up to date
4. Future migrations should follow the validated pattern

For new migrations, use:
```bash
flask db migrate -m "descriptive name"
```

Then validate before committing:
```bash
python scripts/validate_migrations.py
```
