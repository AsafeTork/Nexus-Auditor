from __future__ import annotations

import json
from typing import Any, Dict

from ..models import MonitoringRun


def get_site_agent_state(org_id: str, site_id: str) -> Dict[str, Any]:
    """
    Minimal Agent Control Plane (read-only).
    Returns the latest decision_json for a given (org_id, site_id).

    - No side effects
    - No metrics
    - No policy/learning changes
    """
    if not org_id or not site_id:
        return {}

    mr = (
        MonitoringRun.query.filter_by(org_id=org_id, site_id=site_id)
        .order_by(MonitoringRun.created_utc.desc())
        .first()
    )
    if not mr:
        return {}

    raw = (mr.decision_json or "").strip()
    if not raw:
        return {}

    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

