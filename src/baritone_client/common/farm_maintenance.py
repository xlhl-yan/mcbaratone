"""Fresh-world postconditions and scheduling for surface crop maintenance."""

import time
from collections.abc import Mapping

from .home_surface import below_home_surface


_CROP_ITEMS = {
    "minecraft:wheat": "minecraft:wheat_seeds",
    "minecraft:carrots": "minecraft:carrot",
    "minecraft:potatoes": "minecraft:potato",
    "minecraft:beetroots": "minecraft:beetroot_seeds",
}


def plot_on_cooldown(state, plot) -> bool:
    custom = getattr(state, "custom_data", {})
    holds = custom.get("crop_site_cooldowns", {}) if isinstance(custom, Mapping) else {}
    key = ",".join(str(int(v)) for v in plot)
    try:
        return isinstance(holds, Mapping) and float(holds.get(key, 0) or 0) > time.time()
    except (TypeError, ValueError):
        return True


def farm_surface_safe(client) -> bool:
    """Stop native crop work on an observed drop or unverifiable safety."""
    guard = getattr(client, "_protected_surface_work", None)
    if guard is None:
        return True
    try:
        live = client.transport.dispatch("get_state", {})
        return (not live.get("is_dead") and float(live["health"]) >= 14
                and int(live["air_supply"]) > 0
                and not below_home_surface(guard, live["block_position"]))
    except (KeyError, TypeError, ValueError, RuntimeError):
        return False


def replant_empty_wheat_tiles(client, x: int, y: int, z: int, size: int = 5) -> int:
    """Replant only empty soil, matching the nearest crop seen in this plot.

    The historical function name is kept for callers, but crop type comes
    from fresh world blocks. This prevents a wheat maintenance pass from
    replacing bare tiles in a carrot or potato plot with wheat.
    """
    from . import farming

    half = size // 2
    crops = {}
    empty = []
    for dx in range(-half, half + 1):
        for dz in range(-half, half + 1):
            if not (dx or dz):
                continue
            ground = (x + dx, y, z + dz)
            soil = farming._block_id(client, *ground)
            above = farming._block_id(client, ground[0], y + 1, ground[2])
            if above in _CROP_ITEMS:
                crops[ground] = above
            elif (
                soil in {"minecraft:farmland", "minecraft:dirt", "minecraft:grass_block"}
                and above in {"minecraft:air", "minecraft:cave_air"}
            ):
                empty.append(ground)

    # This legacy entry point is called only by the wheat-farm path, so a
    # completely harvested wheat plot retains its original explicit default.
    # Any surviving crop blocks above take precedence over that default.
    if not crops:
        crops[(x, y, z)] = "minecraft:wheat"
    if not empty:
        return 0

    planted = 0
    for tile in empty:
        # Preserve local rows where possible; ties are resolved by the
        # crop's prevalence in the observed plot, never checkpoint metadata.
        nearest_distance = min(
            abs(tile[0] - crop[0]) + abs(tile[2] - crop[2])
            for crop in crops
        )
        nearest = [
            crop for crop in crops
            if abs(tile[0] - crop[0]) + abs(tile[2] - crop[2]) == nearest_distance
        ]
        counts = {crop_id: sum(value == crop_id for value in crops.values()) for crop_id in set(crops.values())}
        chosen = max(
            (crops[crop] for crop in nearest),
            key=lambda crop_id: (counts[crop_id], crop_id),
        )
        planted += farming.plant_farm_tiles(
            client, [tile], crop_item=_CROP_ITEMS[chosen]
        )
    return planted
