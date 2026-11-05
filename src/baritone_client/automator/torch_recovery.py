"""Retrieve local supplies before repeating an unsuccessful torch recipe."""

from __future__ import annotations

import math
from typing import Any, Mapping


def retrieve_torch_supplies(client: Any, state: Any, wanted: int, anchor) -> None:
    """Try home storage, using fresh carried counts after every withdrawal.

    Catalog entries only select candidates. Neither a catalog count nor an
    acknowledged transfer can satisfy the mining admission check.
    """
    from ..common.home_respawn import withdraw_from_home_containers
    from ..common.inventory import count_item

    if anchor is None or not isinstance(getattr(state, "custom_data", None), Mapping):
        return
    live = client.transport.dispatch("get_state", {})
    if not isinstance(live, Mapping) or live.get("is_dead"):
        return
    position = live.get("block_position", live.get("position", {}))
    try:
        health = float(live.get("health", 0))
        xyz = tuple(float(position[a]) for a in ("x", "y", "z"))
        if (
            "overworld" not in str(live.get("dimension", ""))
            or not all(math.isfinite(v) for v in (*xyz, health))
            or health < 16
            or int(live.get("food_level", live.get("food", 0))) < 14
            or math.dist(xyz, anchor) > 32
        ):
            return
    except (KeyError, TypeError, ValueError):
        return

    def fetch(item: str, target: int) -> None:
        if count_item(client, item) < target:
            withdraw_from_home_containers(
                client, state, item, target, origin=anchor,
                radius=32.0, max_vertical=8.0, recovery=True,
            )

    fetch("minecraft:torch", wanted)
    remaining = max(0, wanted - count_item(client, "minecraft:torch"))
    if not remaining:
        return
    batches = math.ceil(remaining / 4)
    fuel = count_item(client, "minecraft:coal") + count_item(client, "minecraft:charcoal")
    if fuel < batches:
        fetch("minecraft:coal", count_item(client, "minecraft:coal") + batches - fuel)
    fuel = count_item(client, "minecraft:coal") + count_item(client, "minecraft:charcoal")
    if fuel < batches:
        fetch("minecraft:charcoal", count_item(client, "minecraft:charcoal") + batches - fuel)
    fetch("minecraft:stick", batches)
    _fetch_stick_wood(client, batches - count_item(client, "minecraft:stick"), fetch)


def _fetch_stick_wood(client: Any, sticks_short: int, fetch) -> None:
    """Bring home wood back for sticks when none are stored.

    Iron preparation banks spare planks at home. Live A1 2026-10-15 had 52
    planks in its chests, 2 charcoal and no wood carried, so every iron trip
    deferred on 'needs torches' because no stick could be crafted.
    """
    from ..common.inventory import count_item
    from ..common.resources import LOG_TO_PLANKS, PLANK_ITEMS

    if sticks_short <= 0:
        return
    wanted = 2 * math.ceil(sticks_short / 4)  # two planks craft four sticks

    def planks() -> int:
        return sum(count_item(client, item) for item in PLANK_ITEMS)

    for item in PLANK_ITEMS:
        if planks() >= wanted:
            return
        fetch(item, count_item(client, item) + wanted - planks())
    if planks() >= wanted or any(count_item(client, log) > 0 for log in LOG_TO_PLANKS):
        return
    for log in LOG_TO_PLANKS:  # one log crafts four planks
        fetch(log, 1)
        if count_item(client, log) > 0:
            return
