#!/usr/bin/env python3
"""
Migration validator - checks for common Alembic migration errors before deployment.

Prevents:
- Mismatched revision IDs vs filenames
- Broken down_revision chains
- Orphaned migrations
- Circular dependencies
"""

import os
import re
import sys
from pathlib import Path
from collections import defaultdict

def extract_migration_info(filepath):
    """Extract revision and down_revision from migration file"""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    # Extract revision ID
    revision_match = re.search(r'revision\s*=\s*["\']([^"\']+)["\']', content)
    revision = revision_match.group(1) if revision_match else None

    # Extract down_revision
    down_match = re.search(r'down_revision\s*=\s*["\']?([^"\';\n]+)["\']?', content)
    down_revision = down_match.group(1) if down_match else None
    if down_revision == 'None':
        down_revision = None

    # Get filename without extension
    filename = Path(filepath).stem

    return {
        'filepath': filepath,
        'filename': filename,
        'revision': revision,
        'down_revision': down_revision,
    }

def validate_migrations(migration_dir='migrations/versions'):
    """Validate all migrations and report errors"""

    if not os.path.exists(migration_dir):
        print(f"Error: Migration directory '{migration_dir}' not found")
        return False

    # Load all migrations
    migrations = {}
    errors = []
    warnings = []

    migration_files = sorted([
        f for f in os.listdir(migration_dir)
        if f.endswith('.py') and not f.startswith('__')
    ])

    if not migration_files:
        print("Warning: No migration files found")
        return True

    # Parse all migrations
    for mfile in migration_files:
        filepath = os.path.join(migration_dir, mfile)
        try:
            info = extract_migration_info(filepath)
            migrations[info['revision']] = info
        except Exception as e:
            errors.append(f"Failed to parse {mfile}: {e}")
            continue

    # Validation checks
    print("=" * 70)
    print("MIGRATION VALIDATION")
    print("=" * 70)

    # 1. Check revision IDs match filenames
    print("\n[1] Checking revision IDs match filenames...")
    for rev_id, info in migrations.items():
        if rev_id != info['filename']:
            errors.append(
                f"Mismatch in {info['filename']}.py:\n"
                f"    Filename: {info['filename']}\n"
                f"    Revision: {rev_id}\n"
                f"    Fix: Change revision = \"{rev_id}\" to revision = \"{info['filename']}\""
            )
        else:
            print(f"    {rev_id}: OK")

    # 2. Check down_revision chains are valid
    print("\n[2] Checking down_revision chains...")
    for rev_id, info in migrations.items():
        if info['down_revision'] and info['down_revision'] != 'None':
            if info['down_revision'] not in migrations:
                errors.append(
                    f"Invalid down_revision in {info['filename']}.py:\n"
                    f"    Points to: {info['down_revision']}\n"
                    f"    But migration not found!"
                )
            else:
                print(f"    {rev_id} -> {info['down_revision']}: OK")
        else:
            if len(migrations) == 1 or all(m['down_revision'] is None for m in migrations.values()):
                print(f"    {rev_id} (head): OK")
            else:
                warnings.append(
                    f"Migration {rev_id} has no down_revision parent\n"
                    f"    Could be intentional for the first migration, but verify chain"
                )

    # 3. Check for circular dependencies
    print("\n[3] Checking for circular dependencies...")
    visited = set()
    rec_stack = set()

    def has_cycle(rev_id, path=[]):
        if rev_id in rec_stack:
            cycle_path = " -> ".join(path + [rev_id])
            errors.append(f"Circular dependency detected: {cycle_path}")
            return True

        if rev_id in visited:
            return False

        if rev_id not in migrations:
            return False

        visited.add(rev_id)
        rec_stack.add(rev_id)

        down_rev = migrations[rev_id]['down_revision']
        if down_rev and down_rev != 'None':
            if has_cycle(down_rev, path + [rev_id]):
                return True

        rec_stack.remove(rev_id)
        return False

    for rev_id in migrations:
        if rev_id not in visited:
            has_cycle(rev_id)

    if not any("Circular" in e for e in errors):
        print("    No circular dependencies detected: OK")

    # 4. Check chain continuity
    print("\n[4] Checking migration chain continuity...")
    # Build a map of children
    children = defaultdict(list)
    heads = []
    tails = []

    for rev_id, info in migrations.items():
        if info['down_revision'] and info['down_revision'] != 'None':
            children[info['down_revision']].append(rev_id)
        else:
            tails.append(rev_id)

    # Find heads (migrations with no children)
    for rev_id in migrations:
        if rev_id not in children or len(children[rev_id]) == 0:
            heads.append(rev_id)

    print(f"    Chain heads: {heads}")
    print(f"    Chain tails: {tails}")

    # Verify linear chain (each migration has at most 1 child)
    branches = [rev for rev, child_list in children.items() if len(child_list) > 1]
    if branches:
        warnings.append(f"Branched migrations found (multiple children): {branches}")
    else:
        print(f"    Linear progression: OK")

    # Summary
    print("\n" + "=" * 70)
    if errors:
        print(f"ERRORS FOUND: {len(errors)}")
        print("=" * 70)
        for i, error in enumerate(errors, 1):
            print(f"\n[ERROR {i}]")
            print(error)
        return False
    elif warnings:
        print(f"WARNINGS: {len(warnings)} (non-blocking)")
        print("=" * 70)
        for i, warning in enumerate(warnings, 1):
            print(f"\n[WARNING {i}]")
            print(warning)
        return True
    else:
        print("SUCCESS: All migrations validated!")
        print("=" * 70)
        return True

if __name__ == "__main__":
    success = validate_migrations()
    sys.exit(0 if success else 1)
