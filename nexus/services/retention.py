from __future__ import annotations

import time

from .. import db
from ..models import AuditEvent


def cleanup_old_audit_events(*, keep_days: int = 30, batch_size: int = 5000) -> int:
    """
    Deletes old AuditEvent rows to prevent unbounded DB growth.
    Uses ts_ms which is indexed and safe to compare.
    Returns number of deleted rows (approx).
    """
    cutoff_ms = int(time.time() * 1000) - int(keep_days) * 24 * 60 * 60 * 1000
    deleted_total = 0
    while True:
        ids = (
            db.session.query(AuditEvent.id)
            .filter(AuditEvent.ts_ms < cutoff_ms)
            .order_by(AuditEvent.id.asc())
            .limit(batch_size)
            .all()
        )
        ids = [x[0] for x in ids]
        if not ids:
            break
        db.session.query(AuditEvent).filter(AuditEvent.id.in_(ids)).delete(synchronize_session=False)
        db.session.commit()
        deleted_total += len(ids)
    return deleted_total

