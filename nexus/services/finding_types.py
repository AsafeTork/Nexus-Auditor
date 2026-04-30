from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Finding:
    key: str
    category: str
    failure: str
    proof: str
    explanation: str
    loss: str
    solution: str
    priority: str
    complexity: str

