"""Safety mode handling for write operations."""
import os
from enum import Enum
from typing import Any


class SafetyMode(str, Enum):
    STRICT = "strict"       # All writes require confirmation
    STANDARD = "standard"   # Low-risk auto, destructive requires confirmation
    AUTONOMOUS = "autonomous"  # Agent decides (no forced confirmations)


class RiskLevel(str, Enum):
    LOW = "low"         # tags, notes, create, update fields, attach/link
    HIGH = "high"       # merge, delete, bulk destructive ops


def get_safety_mode() -> SafetyMode:
    raw = os.environ.get("SAFETY_MODE", "standard").lower().strip()
    try:
        return SafetyMode(raw)
    except ValueError:
        return SafetyMode.STANDARD


def requires_confirmation(risk: RiskLevel) -> bool:
    mode = get_safety_mode()
    if mode == SafetyMode.STRICT:
        return True
    elif mode == SafetyMode.STANDARD:
        return risk == RiskLevel.HIGH
    else:  # AUTONOMOUS
        return False
