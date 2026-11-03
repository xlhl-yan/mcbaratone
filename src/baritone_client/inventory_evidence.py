"""Validate inventory evidence without turning unavailable reads into losses."""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional


_SECTIONS = ("inventory", "armor", "offhand")
_GENERIC_SECTIONS = ("items", "slots")


def unwrap_inventory(payload: Any) -> Optional[Dict[str, Any]]:
    """Return the inventory data object from a bridge response envelope."""
    if not isinstance(payload, dict):
        return None
    if payload.get("error") or payload.get("status") == "error":
        return None
    # The bridge marks reads that were not obtained from the current player
    # snapshot explicitly.  Such a response must not become inventory
    # evidence merely because its data shape happens to look valid.
    if payload.get("snapshot_valid") is False:
        return None
    data = payload.get("data")
    if isinstance(data, dict) and any(
        key in data for key in (*_SECTIONS, *_GENERIC_SECTIONS)
    ):
        if data.get("snapshot_valid") is False:
            return None
        return data
    return payload


def _valid_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    item_id = item.get("id", item.get("item", item.get("name")))
    if not isinstance(item_id, str) or not item_id:
        return False
    count = item.get("count", item.get("quantity"))
    return isinstance(count, int) and not isinstance(count, bool) and count >= 0


def _valid_section(value: Any) -> bool:
    if isinstance(value, list):
        return all(_valid_item(item) for item in value)
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, dict):
                if item.get("id", item.get("item", key)) is not None and not isinstance(
                    item.get("id", item.get("item", key)), str
                ):
                    return False
                count = item.get("count", item.get("quantity"))
            else:
                count = item
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                return False
        return True
    return False


def valid_inventory(payload):
    payload = unwrap_inventory(payload)
    if payload is None:
        return False
    present = [key for key in (*_SECTIONS, *_GENERIC_SECTIONS) if key in payload]
    if not present:
        return False
    # An empty inventory is valid when all advertised sections are well formed.
    return all(_valid_section(payload[key]) for key in present)


def _carried_slots(payload: Any) -> Optional[list[Dict[str, Any]]]:
    """Validate the current slot layout before deriving any capacity."""
    data = unwrap_inventory(payload)
    if data is None or not valid_inventory(payload):
        return None
    if any(part.get("success") is False or part.get("error")
           or part.get("status") == "error" for part in (payload, data)):
        return None
    slots = data.get("inventory")
    if not isinstance(slots, list):
        return None  # Aggregated item counts cannot establish slot capacity.
    seen = set()
    for item in slots:
        slot = item.get("slot")
        if (type(slot) is not int or not 0 <= slot < 36 or slot in seen
                or not isinstance(item.get("id"), str) or type(item.get("count")) is not int
                or (item["id"] == "minecraft:air" and item["count"] != 0)):
            return None
        seen.add(slot)
    return slots


def free_carried_slots(payload: Any) -> int:
    """Fail closed when a read cannot establish carried-slot occupancy."""
    slots = _carried_slots(payload)
    return 0 if slots is None else 36 - sum(item["count"] > 0 for item in slots)


def carried_output_capacity(payload: Any, item_id: str, max_count: int = 64) -> int:
    """Room for plain recipe output; offhand and custom stacks cannot merge."""
    slots = _carried_slots(payload)
    if slots is None or type(max_count) is not int or max_count <= 0:
        return 0
    room = (36 - sum(item["count"] > 0 for item in slots)) * max_count
    for item in slots:
        if (item["id"] != item_id or item["count"] <= 0
                or any(item.get(key) for key in ("components", "enchantments", "stored_enchantments", "damage"))):
            continue
        limit = item.get("max_count", max_count)
        if type(limit) is not int or limit != max_count or item["count"] > limit:
            return 0
        room += limit - item["count"]
    return room


def _section_items(value: Any) -> Iterable[Dict[str, Any]]:
    """Normalize list and item-id/count-map inventory section shapes."""
    if isinstance(value, list):
        return (item for item in value if isinstance(item, dict))
    if isinstance(value, dict):
        normalized = []
        for key, item in value.items():
            if isinstance(item, dict):
                entry = dict(item)
                entry.setdefault("id", entry.get("item", key))
                normalized.append(entry)
            else:
                normalized.append({"id": key, "count": item})
        return normalized
    return ()


def inventory_counts(payload: Any) -> Dict[str, int]:
    """Aggregate one validated snapshot without double-counting aliases.

    Canonical player sections take precedence over generic ``items``/``slots``
    aliases.  A bridge response that only supplies a generic section uses
    ``items`` before ``slots``.  Both list entries and count maps are accepted.
    """
    data = unwrap_inventory(payload)
    if data is None:
        return {}
    sections = [key for key in _SECTIONS if key in data]
    if not sections:
        sections = [key for key in _GENERIC_SECTIONS if key in data][:1]
    counts: Dict[str, int] = {}
    for section in sections:
        for item in _section_items(data.get(section)):
            item_id = item.get("id", item.get("item", item.get("name")))
            count = item.get("count", item.get("quantity"))
            if isinstance(item_id, str) and item_id and isinstance(count, int) and count > 0:
                counts[item_id] = counts.get(item_id, 0) + count
    return counts
