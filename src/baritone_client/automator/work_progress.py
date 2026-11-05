"""Durable evidence ledger for the fleet-wide useful-work invariant.

Heartbeat freshness, coordinate movement, and a successful command response do
not prove that an autonomous worker improved the world.  This module records a
small, stable fingerprint of counters that represent resources, production,
construction, or verified objective completion.  Schedulers can use the
resulting no-progress streak to choose a different bounded recovery action.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping


_COUNTER_KEYS = {
    "plots",
    "crop_tiles",
    "wheat_harvested",
    "other_edible_crops_harvested",
    "crops_replanted",
    "bread_crafted",
    "prepared_food_banked",
    "logs_harvested",
    "saplings_planted",
    "logs_banked",
    "raw_iron_mined",
    "iron_ingots_smelted",
    "iron_banked",
    "blaze_rods_banked",
    "nether_wart_banked",
    "quartz_banked",
    "end_cities_looted",
    "shulker_boxes_banked",
    "elytra_banked",
    "xp_gained",
    "books_created",
    "items_enchanted",
    "deliveries_completed",
    "blocks_placed",
    "structures_completed",
    "chunks_completed",
    "districts_completed",
    "rings_completed",
    "double_chests_created",
    "items_moved",
    "stacks_moved",
    "free_slots_added",
    "legacy_chests_emptied",
    "hostiles_cleared",
}

_PRODUCTIVE_OPPORTUNITIES = {
    "animal_farm",
    "crop_farm",
    "wood_farm",
    "iron_mine",
    "food_production",
    "end_supply",
    "nether_supply",
    "enchanting_xp",
    "enchanting_material",
    "storage_maintenance",
    "armor_upkeep",
}


@dataclass(frozen=True)
class ProductiveAttempt:
    """Result of comparing durable evidence around one bounded action."""

    progressed: bool
    no_progress_streak: int
    escalation_level: int
    delta: dict[str, int]


def _number(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return max(0, int(value))
    return None


def productive_snapshot(state: Any) -> dict[str, int]:
    """Return only durable counters that constitute useful-world progress."""
    custom = getattr(state, "custom_data", {}) or {}
    if not isinstance(custom, Mapping):
        return {}
    snapshot: dict[str, int] = {}
    for namespace, raw in custom.items():
        if not isinstance(raw, Mapping):
            continue
        for key, value in raw.items():
            if key not in _COUNTER_KEYS:
                continue
            number = _number(value)
            if number is not None:
                snapshot[f"{namespace}.{key}"] = number

    completed = custom.get("completed_objectives")
    if isinstance(completed, (list, tuple, set)):
        snapshot["objectives.completed"] = len({str(item) for item in completed})
    verified = custom.get("verified_objective_completions")
    if isinstance(verified, Mapping):
        snapshot["objectives.verified"] = len(verified)

    scheduler = custom.get("adaptive_scheduler")
    scheduler = scheduler if isinstance(scheduler, Mapping) else {}
    opportunities = scheduler.get("opportunities", {})
    if isinstance(opportunities, Mapping):
        for kind, raw in opportunities.items():
            if (
                str(kind) not in _PRODUCTIVE_OPPORTUNITIES
                or not isinstance(raw, Mapping)
            ):
                continue
            for key in ("verified_delta_total",):
                number = _number(raw.get(key))
                if number is not None:
                    snapshot[f"adaptive_scheduler.{kind}.{key}"] = number
    return snapshot


def _positive_delta(
    before: Mapping[str, int], after: Mapping[str, int]
) -> dict[str, int]:
    keys = set(before) | set(after)
    return {
        key: int(after.get(key, 0)) - int(before.get(key, 0))
        for key in sorted(keys)
        if int(after.get(key, 0)) > int(before.get(key, 0))
    }


def record_productive_attempt(
    state: Any,
    kind: str,
    before: Mapping[str, int],
    after: Mapping[str, int],
    *,
    detail: str = "",
    now: float | None = None,
) -> ProductiveAttempt:
    """Persist progress or escalate an unchanged action without inventing work."""
    custom = getattr(state, "custom_data", None)
    if not isinstance(custom, dict):
        custom = {}
        state.custom_data = custom
    ledger = custom.setdefault("productive_work", {})
    if not isinstance(ledger, dict):
        ledger = {}
        custom["productive_work"] = ledger

    timestamp = time.time() if now is None else float(now)
    delta = _positive_delta(before, after)
    progressed = bool(delta)
    ledger["attempts"] = int(ledger.get("attempts", 0) or 0) + 1
    ledger["last_kind"] = str(kind)
    ledger["last_attempt_at"] = timestamp
    ledger["last_detail"] = str(detail)

    by_kind = ledger.setdefault("by_kind", {})
    if not isinstance(by_kind, dict):
        by_kind = {}
        ledger["by_kind"] = by_kind
    record = by_kind.setdefault(str(kind), {})
    if not isinstance(record, dict):
        record = {}
        by_kind[str(kind)] = record
    record["attempts"] = int(record.get("attempts", 0) or 0) + 1
    record["last_attempt_at"] = timestamp
    record["last_detail"] = str(detail)

    if progressed:
        ledger["progress_events"] = int(ledger.get("progress_events", 0) or 0) + 1
        ledger["last_progress_at"] = timestamp
        ledger["last_delta"] = delta
        ledger["no_progress_streak"] = 0
        ledger["escalation_level"] = 0
        record["progress_events"] = int(record.get("progress_events", 0) or 0) + 1
        record["no_progress_streak"] = 0
    else:
        streak = int(ledger.get("no_progress_streak", 0) or 0) + 1
        kind_streak = int(record.get("no_progress_streak", 0) or 0) + 1
        ledger["no_progress_streak"] = streak
        ledger["escalation_level"] = min(4, 1 + (streak - 1) // 2)
        record["no_progress_streak"] = kind_streak

    return ProductiveAttempt(
        progressed=progressed,
        no_progress_streak=int(ledger.get("no_progress_streak", 0) or 0),
        escalation_level=int(ledger.get("escalation_level", 0) or 0),
        delta=delta,
    )


__all__ = [
    "ProductiveAttempt",
    "productive_snapshot",
    "record_productive_attempt",
]
