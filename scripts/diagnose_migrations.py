#!/usr/bin/env python3
"""
Diagnose migration issues by checking database state vs migration files.

Run this when migrations fail to understand what's happening.
"""

import sys
import os

# Set up environment
os.environ.setdefault("FLASK_APP", "app.py")
os.environ.setdefault("DATABASE_URL", "sqlite:///nexus_dev.db")

def diagnose():
    """Diagnose migration issues"""
    print("=" * 70)
    print("MIGRATION DIAGNOSTICS")
    print("=" * 70)

    print("\n[1] Checking migration files...")
    migrations_dir = "migrations/versions"
    migration_files = sorted([
        f for f in os.listdir(migrations_dir)
        if f.endswith('.py') and not f.startswith('__')
    ])

    print(f"    Found {len(migration_files)} migration files:")
    for f in migration_files:
        print(f"      - {f}")

    print("\n[2] Checking migration file syntax...")
    import py_compile
    for mfile in migration_files:
        filepath = os.path.join(migrations_dir, mfile)
        try:
            py_compile.compile(filepath, doraise=True)
            print(f"    {mfile}: OK")
        except py_compile.PyCompileError as e:
            print(f"    {mfile}: SYNTAX ERROR")
            print(f"      {e}")
            return False

    print("\n[3] Running validate_migrations.py...")
    try:
        from scripts.validate_migrations import validate_migrations
        if not validate_migrations(migrations_dir):
            print("    Migration validation failed!")
            return False
    except Exception as e:
        print(f"    Error: {e}")
        return False

    print("\n[4] Checking Flask app...")
    try:
        from app import app
        with app.app_context():
            print("    Flask app initialized: OK")

            print("\n[5] Checking database connection...")
            try:
                from flask_sqlalchemy import SQLAlchemy
                db = app.extensions.get('sqlalchemy')
                if db:
                    with db.engine.connect() as conn:
                        conn.execute("SELECT 1")
                        print("    Database connection: OK")
                else:
                    print("    Warning: SQLAlchemy not initialized in app")
            except Exception as e:
                print(f"    Database connection: FAILED - {e}")

            print("\n[6] Checking Alembic version table...")
            try:
                from sqlalchemy import text
                db = app.extensions.get('sqlalchemy')
                if db:
                    with db.engine.connect() as conn:
                        result = conn.execute(text("SELECT * FROM alembic_version"))
                        rows = result.fetchall()
                        print(f"    Alembic records in database: {len(rows)}")
                        for row in rows:
                            print(f"      - {row[0]}")
            except Exception as e:
                print(f"    Cannot query alembic_version: {e}")

    except ImportError as e:
        print(f"    Cannot import app: {e}")
        print("    (This is OK if Flask is not installed locally)")

    print("\n" + "=" * 70)
    print("DIAGNOSTICS COMPLETE")
    print("=" * 70)
    return True

if __name__ == "__main__":
    success = diagnose()
    sys.exit(0 if success else 1)
