"""Persistently pace unchanged production so other work gets a turn."""

from __future__ import annotations

from typing import Any


_PACED_KINDS = {
    "animal_farm", "crop_farm", "wood_farm", "food_production",
    "iron_mine", "enchanting_material", "storage_maintenance",
}


def retry_cooldown(state: Any, kind: str, base: float) -> float:
    """Back off repeated zero-output work, with a bounded automatic retry.

    Survival and equipment repair keep their normal urgent admission rules.
    This reads the per-kind ledger: unrelated harvesting must not erase a
    failing producer's history, and restarting must not erase its pacing.
    """
    if kind not in _PACED_KINDS:
        return base
    custom = getattr(state, "custom_data", {})
    try:
        ledger = custom.get("productive_work", {})
        records = ledger.get("by_kind", {})
        record = records.get(kind, {})
        streak = max(0, int(record.get("no_progress_streak", 0)))
    except (AttributeError, TypeError, ValueError):
        return base
    return max(base, min(900.0, base * 2 ** min(4, streak // 2)))
