"""Grow food at the farm a starving, wounded player is already standing beside.

Health only regenerates at food 18, and survival recovery refuses blind
exploration while health is critical. Where no passive animal is in range
that is a closed loop: no food, so no health, so no search for food. A
checkpointed farm a short walk away is the one food source that needs
neither: bread from carried wheat, mature wheat from the plot, and seeds
replanted from where the player stands.
"""

from __future__ import annotations

import time
from math import floor, hypot, isfinite
from typing import Any, Mapping, Optional, Tuple

from .navigation import allow_recovery_navigation

#: Only tend a farm the player can reach without a real journey.
SURVIVAL_FARM_REACH = 16.0
#: The survival loop calls this every few seconds; the farm needs minutes.
SURVIVAL_FARM_INTERVAL = 60.0
#: Below the 14-food work gate a bot with no carried food could never reach
#: the farm that bakes its bread: A1 sat in that loop from 2026-10-18 06:00
#: for two days. Healthy and not starving is enough for the home farm.
HOME_FARM_HEALTH = 12.0
HOME_FARM_FOOD = 6


def fed_enough_for_home_farm(client: Any) -> bool:
    """Whether a bot that could not eat may still tend its own farm."""
    live = client.transport.dispatch("get_state", {})
    try:
        return (float(live.get("health", 0) or 0) >= HOME_FARM_HEALTH
                and int(live.get("food_level", live.get("food", 0)) or 0) >= HOME_FARM_FOOD)
    except (AttributeError, TypeError, ValueError):
        return False
#: Plots are 5x5 around a water center (see ``establish_wheat_farm``).
_HALF = 2
_MATURE_WHEAT_AGE = "7"


def _position(live: Mapping[str, Any]) -> Optional[Tuple[float, float, float]]:
    position = live.get("block_position", live.get("position", {}))
    try:
        result = tuple(float(position[key]) for key in ("x", "y", "z"))
        return result if all(isfinite(value) for value in result) else None
    except (KeyError, TypeError, ValueError):
        return None


def _nearest_plot(live: Mapping[str, Any], state: Any):
    """Return the closest checkpointed plot center and its horizontal distance."""
    from .food_supply import _plots

    here = _position(live)
    plots = _plots(state)
    if here is None or not plots:
        return None
    plots = [p for p in plots if abs(p[1] - here[1]) <= 3]
    if not plots:
        return None
    plot = min(plots, key=lambda p: hypot(p[0] - here[0], p[2] - here[2]))
    return plot, hypot(plot[0] - here[0], plot[2] - here[2])


def _hostile_close(client: Any, live: Mapping[str, Any]) -> bool:
    from .combat import _threat_can_reach_player, scan_for_threats

    try:
        threats = scan_for_threats(client, radius=16, player_state=dict(live))
    except Exception:
        return True
    return any(
        threat.get("distance", 999) <= 12 and _threat_can_reach_player(threat, live)
        for threat in threats
    )


def _bake_and_eat(client: Any) -> bool:
    """Eat carried food, baking bread from wheat first when there is none."""
    from .combat import eat_until_hunger
    from .emergency_food import (
        craft_emergency_bread_from_carried_wheat,
        emergency_food_count,
    )
    from .inventory import count_item

    if emergency_food_count(client) == 0:
        if count_item(client, "minecraft:wheat") < 3:
            return False
        craft_emergency_bread_from_carried_wheat(client)
        if emergency_food_count(client) == 0:
            return False
    return bool(eat_until_hunger(client, minimum_food=20))


@allow_recovery_navigation
def tend_local_farm_for_food(
    client: Any,
    state: Any,
    *,
    now: Optional[float] = None,
) -> bool:
    """Bake, harvest and replant at a nearby farm; True once food was eaten."""
    from .farming import _block_data, _till_and_plant_tile, harvest_wheat_farm
    from .inventory import count_item
    from .navigation import goto
    from .tasks import PlayerDeathDetected

    current = time.monotonic() if now is None else float(now)
    last = getattr(client, "_survival_farm_last", None)
    if isinstance(last, (int, float)) and current - last < SURVIVAL_FARM_INTERVAL:
        return False
    client._survival_farm_last = current

    live = client.transport.dispatch("get_state", {})
    if not isinstance(live, Mapping):
        return False
    if int(live.get("food_level", live.get("food", 0)) or 0) >= 18:
        return False  # health is already regenerating; nothing to grow for
    found = _nearest_plot(live, state)
    if found is None or found[1] > SURVIVAL_FARM_REACH:
        return False
    if _hostile_close(client, live):
        return False
    if _bake_and_eat(client):
        print("RECOVERY: ate bread baked from carried wheat")
        return True

    (cx, cy, cz), distance = found
    if distance > 3.0:
        if not goto(client, cx, cy, cz, timeout=45, tolerance=3.5, radius=2):
            return False
    tiles = [
        (cx + dx, cy, cz + dz)
        for dx in range(-_HALF, _HALF + 1)
        for dz in range(-_HALF, _HALF + 1)
        if dx or dz
    ]
    crops = {tile: _block_data(client, tile[0], cy + 1, tile[2]) for tile in tiles}
    wheat_before = count_item(client, "minecraft:wheat")
    if any(
        "wheat" in str(data.get("id", ""))
        and str((data.get("state") or {}).get("age")) == _MATURE_WHEAT_AGE
        for data in crops.values()
    ):
        harvest_wheat_farm(client, cx, cy, cz, range_=_HALF + 1)
    harvested = max(0, count_item(client, "minecraft:wheat") - wheat_before)

    planted, refused = 0, 0
    for tile, data in crops.items():
        if count_item(client, "minecraft:wheat_seeds") < 1:
            break
        if "wheat" in str(data.get("id", "")):
            continue
        # The bridge only acts on a block the eye-to-center ray actually hits.
        # From the plot's water hole the eye sits a block low and that ray
        # clips the nearer tile, so plant each tile from a stand beside it.
        # One refused tile must not abort the rest of survival recovery.
        try:
            if not goto(client, tile[0], cy + 1, tile[2], timeout=15, tolerance=1.5, radius=1):
                refused += 1
                continue
            if _till_and_plant_tile(client, *tile):
                planted += 1
        except PlayerDeathDetected:
            raise
        except Exception:
            refused += 1
    print(
        f"RECOVERY: tended local farm at {(cx, cy, cz)}: "
        f"harvested {harvested} wheat, planted {planted} tile(s)"
        + (f", {refused} refused" if refused else "")
    )
    if _bake_and_eat(client):
        print("RECOVERY: ate bread baked from farm wheat")
        return True
    return False


def _enclosure_bounds(client, live, *, strict=False):
    """Require a roof and four body-height walls; open doors are not walls."""
    from .farming import _block_data

    here = _position(live)
    if here is None:
        return False
    x, y, z = map(floor, here)
    # Farmland lowers the feet by 1/16 block; use body-height wall cells,
    # not the soil layer, when precise position telemetry is available.
    precise = live.get("position", {}).get("y")
    if precise is not None:
        y = floor(float(precise) + 0.2)

    cache = {}

    def data_at(tx, ty, tz):
        key = (tx, ty, tz)
        if key not in cache:
            cache[key] = _block_data(client, *key)
            if strict and not cache[key].get("id"):
                raise ValueError("unknown enclosure block")
        return cache[key]

    def solid(tx, ty, tz):
        data = data_at(tx, ty, tz)
        block = data.get("id", "")
        if block == "minecraft:grass_block":
            return True
        if not block or any(token in block for token in (
            "air", "water", "lava", "grass", "wheat", "torch", "flower", "leaves", "vine",
            "slab", "stairs", "fence", "pane", "trapdoor",
        )):
            return False
        return "door" not in block or str((data.get("state") or {}).get("open")) == "false"

    edges = []
    for dx, dz in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        edge = next((d for d in range(1, 6) if
                     solid(x + dx * d, y, z + dz * d) and
                     solid(x + dx * d, y + 1, z + dz * d)), None)
        if edge is None:
            return False
        edges.append(edge)
    west, east, north, south = edges
    for tx in range(x - west, x + east + 1):
        for tz in range(z - north, z + south + 1):
            if tx in (x - west, x + east) or tz in (z - north, z + south):
                if not (solid(tx, y, tz) and solid(tx, y + 1, tz)):
                    return False
            else:
                if not any(solid(tx, y + dy, tz) for dy in range(2, 5)):
                    return False
                floor_data = data_at(tx, y - 1, tz).get("id")
                covered_water = (floor_data == "minecraft:water" and
                                 "slab" in data_at(tx, y, tz).get("id", ""))
                if not (solid(tx, y - 1, tz) or floor_data == "minecraft:farmland" or covered_water):
                    return False
    return x - west, x + east, y, z - north, z + south


def _wait_enclosure(client, live):
    """Observe the entire room even from the edge of a five-wide plot."""
    return bool(_enclosure_bounds(client, live))


def local_farm_wait_reason(client, state, live):
    """Fresh shelter/crop evidence permits waiting, never credits food output.

    A checkpoint or a cooldown alone is insufficient. Unknown blocks, threats,
    missing irrigation, or a player below the farm all disqualify this hold.
    """
    from .farming import _block_data
    from .tasks import PlayerDeathDetected

    try:
        if live.get("is_dead") or _hostile_close(client, live) or not _wait_enclosure(client, live):
            return None
        if (float(live.get("health", 0) or 0) < 12 and
                int(live.get("food_level", live.get("food", 0)) or 0) >= 18):
            return "recovering health in a verified enclosure"
        found = _nearest_plot(live, state)
        if found is None or found[1] > 4:
            return None
        (cx, cy, cz), _ = found
        # A repaired or inherited plot may have off-centre irrigation. Verify
        # a real source in its footprint, not an assumed checkpoint geometry.
        irrigated = any(
            (data := _block_data(client, cx + dx, cy, cz + dz)).get("id") == "minecraft:water"
            and str((data.get("state") or {}).get("level")) == "0"
            for dx in range(-_HALF, _HALF + 1)
            for dz in range(-_HALF, _HALF + 1)
        )
        if not irrigated:
            return None
        for dx in range(-_HALF, _HALF + 1):
            for dz in range(-_HALF, _HALF + 1):
                crop = _block_data(client, cx + dx, cy + 1, cz + dz)
                if crop.get("id") != "minecraft:wheat":
                    continue
                age = str((crop.get("state") or {}).get("age"))
                soil = _block_data(client, cx + dx, cy, cz + dz)
                if (age in "0123456" and len(age) == 1 and
                        soil.get("id") == "minecraft:farmland" and
                        str((soil.get("state") or {}).get("moisture")) == "7"):
                    return "waiting for growing wheat in a verified farm enclosure"
    except PlayerDeathDetected:
        raise
    except Exception:
        return None
    return None


__all__ = [
    "SURVIVAL_FARM_INTERVAL",
    "SURVIVAL_FARM_REACH",
    "tend_local_farm_for_food",
]
