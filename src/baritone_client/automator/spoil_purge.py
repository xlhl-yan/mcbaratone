"""Throw digging spoil out of full home chests so banking can work again.

Live A1 2026-10-21: every home chest was 27/27, mostly with spoil (about
3674 cobblestone, 2896 seeds, 950 granite/diorite/andesite/tuff, 654 cobbled
deepslate, 547 dirt and 488 leaf litter). Nothing carried could be banked, so
the iron trip's ten-slot gate refused 45 times in 14 hours while the pack
held four stacks of surplus carrots.

Spoil is thrown straight out of the open chest (a container-slot THROW), so
it needs no pack space. Callers run this only when a trip is about to leave,
so the thrown stacks despawn while the bot is away instead of being picked
back up at home. Storage-wide floors keep a working reserve of each item.
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Tuple

#: Item -> amount to keep across all cataloged storage. Everything above the
#: floor is spoil; stone variants and leaf litter have no use at all.
SPOIL_FLOORS: Dict[str, int] = {
    "minecraft:cobblestone": 512,
    "minecraft:cobbled_deepslate": 128,
    "minecraft:dirt": 128,
    "minecraft:wheat_seeds": 128,
    "minecraft:gravel": 64,
    "minecraft:granite": 0,
    "minecraft:diorite": 0,
    "minecraft:andesite": 0,
    "minecraft:tuff": 0,
    "minecraft:leaf_litter": 0,
}
HOME_RADIUS = 32.0
HOME_VERTICAL = 8.0


def _home_spoil_chests(catalog, anchor, spare):
    """Full home chests that hold spare spoil, the most spoil first."""
    chests = []
    for row in catalog.list_containers():
        try:
            position = (int(row["x"]), int(row["y"]), int(row["z"]))
            capacity, occupied = int(row.get("capacity_slots") or 0), int(row.get("occupied_slots") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if ("overworld" not in str(row.get("dimension", ""))
                or math.hypot(position[0] - anchor[0], position[2] - anchor[2]) > HOME_RADIUS
                or abs(position[1] - anchor[1]) > HOME_VERTICAL
                or capacity <= 0 or occupied < capacity - 1):
            continue
        held = catalog.container_inventory(position, dimension=str(row.get("dimension")))
        spoil = sum(count for item, count in held.items() if spare.get(item, 0) > 0)
        if spoil:
            chests.append((spoil, position))
    return [position for _spoil, position in sorted(chests, reverse=True)]


def _slot_emptied(client, slot: int) -> Optional[Dict[str, Any]]:
    """Return the fresh screen once ``slot`` reads empty, else None."""
    for _ in range(10):
        raw = client.transport.dispatch("get_screen", {})
        data = raw.get("data", raw) if isinstance(raw, dict) else {}
        entry = next((s for s in data.get("slots", []) if s.get("slot") == slot), None)
        if entry is not None and (entry.get("id") in (None, "", "minecraft:air") or int(entry.get("count", 0)) <= 0):
            return raw
        time.sleep(0.1)
    return None


def purge_home_spoil(client: Any, state: Any, anchor: Tuple[float, float, float], max_stacks: int) -> List[Tuple[int, int, int]]:
    """Throw up to ``max_stacks`` spoil stacks out of full home chests.

    Returns the chests that now have room, so the caller banks into them.
    """
    from ..common.harness_ops import close_container, open_container
    from ..common.storage_catalog import catalog_for, observe_open_container

    if max_stacks <= 0 or anchor is None:
        return []
    catalog = catalog_for(client, state)
    totals = catalog.inventory_totals()
    spare = {item: max(0, totals.get(item, 0) - floor) for item, floor in SPOIL_FLOORS.items()}
    thrown = 0
    emptied_chests: List[Tuple[int, int, int]] = []
    for position in _home_spoil_chests(catalog, anchor, spare):
        if thrown >= max_stacks:
            break
        if not open_container(client, position, timeout=4.0):
            continue
        try:
            raw = client.transport.dispatch("get_screen", {})
            data = raw.get("data", raw) if isinstance(raw, dict) else {}
            slots = data.get("slots", [])
            chest_slots = int(data.get("total_slots") or len(slots)) - 36
            if chest_slots not in (27, 54):
                continue
            sync_id = data.get("sync_id")
            final = raw
            for entry in list(slots):
                slot, item, count = int(entry.get("slot", -1)), entry.get("id"), int(entry.get("count", 0) or 0)
                if thrown >= max_stacks:
                    break
                if not 0 <= slot < chest_slots or count <= 0 or spare.get(item, 0) < count:
                    continue  # whole stacks only, never below the storage floor
                payload = {"slot": slot, "type": "THROW", "button": 1}
                if sync_id is not None:
                    payload["sync_id"] = sync_id
                client.transport.dispatch("inventory_click", payload)
                emptied = _slot_emptied(client, slot)
                if emptied is None:
                    break  # an unverified throw is not disposal
                spare[item] -= count
                thrown += 1
                final = emptied
                if position not in emptied_chests:
                    emptied_chests.append(position)
            try:
                observe_open_container(client, position, final, state=state)
            except Exception as exc:
                print(f"STORAGE: catalog refresh after spoil purge failed ({exc})")
        finally:
            close_container(client)
    if thrown:
        print(f"STORAGE: threw {thrown} spoil stack(s) out of {len(emptied_chests)} full home chest(s)")
    return emptied_chests
