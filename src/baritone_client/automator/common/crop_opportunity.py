"""Delta-verified bounded crop work."""

from __future__ import annotations

import time
from typing import Any, Callable

from ...common.inventory import get_inventory
from ...common.navigation import find_nearby_block, goto
from ...common.home_surface import protect_home_route
from ..local_opportunity import LocalOpportunity

CROP_BLOCKS = ("minecraft:wheat", "minecraft:carrots", "minecraft:potatoes", "minecraft:beetroots")
CROP_ITEMS = ("minecraft:wheat", "minecraft:carrot", "minecraft:potato", "minecraft:beetroot")
PLANTABLE_ITEMS = ("minecraft:wheat_seeds", "minecraft:carrot", "minecraft:potato", "minecraft:beetroot_seeds")


def run_crop_opportunity(
    client: Any, opportunity: LocalOpportunity, timeout: float,
    *, inventory_reader: Callable[[Any], Any] = get_inventory,
    traveler: Callable[..., bool] = goto,
    block_finder: Callable[..., Any] = find_nearby_block,
    sleeper: Callable[[float], None] = time.sleep,
    state: Any = None,
) -> tuple[bool, str, int, int]:
    """Hold home digging protection across the entire native crop process."""
    if opportunity.location is None:
        return False, "crop patch was unreachable", 0, 0
    result = _run_crop_opportunity(
        client, *opportunity.location, opportunity, timeout,
        inventory_reader=inventory_reader, traveler=traveler,
        block_finder=block_finder, sleeper=sleeper, state=state,
    )
    if state is not None and isinstance(result, tuple):
        custom = getattr(state, "custom_data", None)
        if isinstance(custom, dict):
            holds = custom.setdefault("crop_site_cooldowns", {})
            key = ",".join(str(int(v)) for v in opportunity.location)
            if result[0]:
                holds.pop(key, None)
            else:
                holds[key] = time.time() + 900.0
    return result if isinstance(result, tuple) else (
        False, "home crop digging protection was unavailable", 0, 0
    )


@protect_home_route(surface_work=True)
def _run_crop_opportunity(
    client: Any, x: int, y: int, z: int,
    opportunity: LocalOpportunity, timeout: float,
    *, inventory_reader: Callable[[Any], Any] = get_inventory,
    traveler: Callable[..., bool] = goto,
    block_finder: Callable[..., Any] = find_nearby_block,
    sleeper: Callable[[float], None] = time.sleep,
    state: Any = None,
) -> tuple[bool, str, int, int]:
    """Farm once, accepting only a harvest or world-verified replant delta.

    The Baritone ``#farm`` command above only replants tiles that are
    already farmland -- it does not till new soil. Confirmed live on A1
    2026-08-14: a farm harvested down to bare ground (soil itself gone in
    one tile, plain grass_block instead of farmland in the rest) left every
    later cycle reporting "no harvest or planting change" forever, since
    there was nothing left for ``#farm`` to act on. Falling back to
    ``establish_wheat_farm`` -- already idempotent, already used to build
    this same farm the first time -- re-tills and replants from scratch
    when the quick path finds nothing.
    """
    location = opportunity.location
    if location is None or not traveler(client, *location, timeout=90, tolerance=5.0):
        return False, "crop patch was unreachable", 0, 0
    before_inventory = inventory_reader(client)
    before = sum(int(before_inventory.get(item, 0) or 0) for item in CROP_ITEMS)
    before_plantable = sum(int(before_inventory.get(item, 0) or 0) for item in PLANTABLE_ITEMS)
    client.transport.dispatch("farm", {"range": 8, "x": location[0], "y": location[1], "z": location[2], "replant": True})
    deadline = time.monotonic() + max(1.0, float(timeout))
    after = before
    safe = True
    try:
        while time.monotonic() < deadline:
            sleeper(min(2.0, max(0.05, float(timeout))))
            from ...common.farming import farm_surface_safe
            safe = farm_surface_safe(client)
            if not safe:
                return False, "crop process left the safe surface", before, after
            current = inventory_reader(client)
            after = sum(int(current.get(item, 0) or 0) for item in CROP_ITEMS)
            plantable = sum(int(current.get(item, 0) or 0) for item in PLANTABLE_ITEMS)
            if after > before:
                return True, "crop produce increased", before, after
            if plantable < before_plantable and block_finder(client, list(CROP_BLOCKS), radius=12):
                return True, "planting was verified in the world", before, plantable
    finally:
        stopped = False
        try:
            client.transport.dispatch("cancel", {})
            stopped = True
        except Exception:
            pass
        if stopped and safe and state is not None:
            from ..food_recovery_state import checkpointed_wheat_farm_origin
            if checkpointed_wheat_farm_origin(state) == tuple(location):
                from ...common.farming import replant_empty_wheat_tiles
                replant_empty_wheat_tiles(client, *location)

    # If the farm command didn't change inventories but crop blocks are present
    # in the world, the patch is already planted and waiting to mature. Accept
    # it as productive instead of cycling through rebuild attempts forever.
    # The player may have moved during the farm command; re-center at the farm
    # location so the block search covers the actual patch.
    if traveler(client, *location, timeout=30, tolerance=3.0):
        if block_finder(client, list(CROP_BLOCKS), radius=12):
            return True, "crop blocks present; awaiting maturation", before, after

    from ...common.farming import (
        establish_wheat_farm,
        harvest_wheat_farm,
        reestablish_wheat_farm,
        relocate_wheat_farm,
    )

    # Check if the farm is already established and working
    if harvest_wheat_farm(client, location[0], location[1], location[2], range_=8):
        return True, "farm is already producing", before, after

    rebuilt = establish_wheat_farm(client, *location, state=state)
    if rebuilt is not None:
        return True, "no mature crop; re-tilled and replanted the patch", before, after
    # A farm harvested down to bare ground has zero tillable tiles, so
    # establish_wheat_farm declines. Re-till the same site in place (soil
    # reverted to dirt/grass, water still beside it) before abandoning it.
    reestablished = reestablish_wheat_farm(client, *location, state=state)
    if reestablished is not None:
        return True, "bare-ground farm was re-tilled and replanted in place", before, after
    relocated = relocate_wheat_farm(client, *location, state=state)
    if relocated is not None:
        return True, "unusable crop patch was relocated and replanted", before, after
    return False, "no harvest, planting, or rebuild change was observed", before, after
