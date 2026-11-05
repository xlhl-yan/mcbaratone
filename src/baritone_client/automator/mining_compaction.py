"""Recover mining room by merging identical player stacks, without banking tools."""

import json
from collections import Counter

from ..inventory_evidence import unwrap_inventory, valid_inventory
from .mining_storage import _at_home


def _key(item):
    return json.dumps({k: v for k, v in item.items() if k not in {"slot", "count"}}, sort_keys=True)


def _snapshot(client):
    raw = client.transport.dispatch("get_inventory", {})
    data = unwrap_inventory(raw)
    if (not valid_inventory(raw) or not isinstance(data, dict) or data.get("snapshot_valid") is not True
            or raw.get("success") is False or data.get("success") is False):
        return None
    rows = data.get("inventory")
    if (not isinstance(rows, list) or len(rows) != 36
            or any(type(r.get("slot")) is not int for r in rows)
            or {r["slot"] for r in rows} != set(range(36))
            or type(data.get("selected_slot")) is not int or not 0 <= data["selected_slot"] < 9
            or any(not isinstance(data.get(section), list) for section in ("armor", "offhand"))):
        return None
    return data


def _totals(snapshot):
    totals = Counter()
    for section in ("inventory", "armor", "offhand"):
        for row in snapshot[section]:
            if row["id"] != "minecraft:air" and row["count"] > 0:
                totals[_key(row)] += row["count"]
    return totals


def _free(snapshot):
    return sum(row["id"] == "minecraft:air" or row["count"] == 0 for row in snapshot["inventory"])


def _screen_slot(slot):
    return slot + 36 if slot < 9 else slot


def _merge_source(snapshot):
    rows = sorted(snapshot["inventory"], key=lambda r: r["count"])
    for source in rows:
        maximum = source.get("max_count")
        if (type(maximum) is not int or maximum <= 1
                or not 0 < source["count"] < maximum or source["id"] == "minecraft:air"):
            continue
        for target in rows:
            # Native quick-move merges across the hotbar/main-inventory boundary.
            if ((source["slot"] < 9) == (target["slot"] < 9)
                    or _key(source) != _key(target) or target["count"] <= 0):
                continue
            if source["count"] + target["count"] <= maximum:
                return source, target
    return None


def compact_home_stacks(client, anchor, required, *, max_moves=4):
    """Use at most four cursor-free native merges; verify every item is retained."""
    recovered = 0
    for _ in range(min(4, max(0, int(max_moves)))):
        if not _at_home(client, anchor):
            break
        before = _snapshot(client)
        if before is None or _free(before) >= required:
            break
        pair = _merge_source(before)
        if pair is None:
            break
        screen = client.transport.dispatch("get_screen", {})
        if (not isinstance(screen, dict) or screen.get("error") or screen.get("success") is False
                or screen.get("status") == "error" or type(screen.get("sync_id")) is not int
                or screen.get("sync_id") != 0 or screen.get("type") not in {"InventoryMenu", "PlayerScreenHandler"}
                or screen.get("total_slots") != 46):
            break
        slots = screen.get("slots", [])
        if not isinstance(slots, list):
            break
        for row in pair:
            observed = next((s for s in slots if isinstance(s, dict) and s.get("slot") == _screen_slot(row["slot"])), None)
            if observed is None or _key(observed) != _key(row) or observed.get("count") != row["count"]:
                return recovered
        if not _at_home(client, anchor):
            break
        client.transport.dispatch("inventory_click", {"slot": _screen_slot(pair[0]["slot"]),
                                                       "type": "QUICK_MOVE", "button": 0, "sync_id": 0})
        after = _snapshot(client)
        if (after is None or _totals(after) != _totals(before)
                or after["armor"] != before["armor"] or after["offhand"] != before["offhand"]
                or after["selected_slot"] != before["selected_slot"]):
            raise RuntimeError("inventory compaction postcondition was not verified")
        freed = _free(after) - _free(before)
        if freed <= 0:
            break
        recovered += freed
    return recovered
