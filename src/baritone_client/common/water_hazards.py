"""Keep a starving search out of water and away from known drowning spots.

Live A1 2026-10-04..15: 29 drownings, seven at one riverbed (-411, 61, -142)
at the foot of a cliff on the main northbound route. Each time the emergency
food search walked into the river, the dry-search guard cancelled Baritone in
the water, the bot sank to the bed under two blocks of water, and the bounded
surfacing attempts ran out of air one block from dry grass.
"""

from __future__ import annotations

import math
import time
from functools import wraps
from typing import Any, Callable, Optional, Tuple

HAZARD_KEY = "drowning_hazards"
#: Exploration polls every few seconds at walking pace, so the fence must be
#: wider than one poll's travel or the bot is in the water before it is seen.
FENCE_RADIUS = 16.0
#: Two drownings this close are the same trap.
_MERGE_RADIUS = 4.0
_MAX_HAZARDS = 32
#: Waterlogged plants occupy water blocks, so the pathfinder must avoid them too.
#: The list replaces Baritone's default (tripwire), so it repeats that entry.
_WATER_BLOCKS = (
    "minecraft:tripwire",
    "minecraft:water",
    "minecraft:seagrass",
    "minecraft:tall_seagrass",
    "minecraft:kelp",
    "minecraft:kelp_plant",
)

Hazard = Tuple[int, int, int]


def _hazard(entry: Any) -> Optional[Hazard]:
    if not isinstance(entry, dict):
        return None
    try:
        values = tuple(entry[axis] for axis in ("x", "y", "z"))
        if any(isinstance(v, bool) or not math.isfinite(float(v)) for v in values):
            return None
        return tuple(int(v) for v in values)
    except (KeyError, TypeError, ValueError):
        return None


def drowned(state: Any) -> bool:
    """Return whether a death snapshot names drowning as the last damage."""
    source = state.get("last_damage_source") if isinstance(state, dict) else None
    return isinstance(source, dict) and source.get("type") == "drown"


def record_drowning(state: Any, position: Any, dimension: str = "minecraft:overworld") -> Optional[Hazard]:
    """Persist where a drowning happened so later searches fence it off."""
    custom = getattr(state, "custom_data", None)
    if not isinstance(custom, dict) or "overworld" not in str(dimension).lower():
        return None
    point = _hazard(position) if isinstance(position, dict) else _hazard(
        dict(zip(("x", "y", "z"), position)) if isinstance(position, (list, tuple)) and len(position) == 3 else None
    )
    if point is None:
        return None
    hazards = [entry for entry in custom.get(HAZARD_KEY, []) if _hazard(entry) is not None]
    for entry in hazards:
        if (math.hypot(entry["x"] - point[0], entry["z"] - point[2]) <= _MERGE_RADIUS
                and abs(entry["y"] - point[1]) <= _MERGE_RADIUS):
            entry["deaths"] = int(entry.get("deaths", 1) or 1) + 1
            break
    else:
        hazards.append({"x": point[0], "y": point[1], "z": point[2], "deaths": 1})
    hazards.sort(key=lambda entry: -int(entry.get("deaths", 1) or 1))
    custom[HAZARD_KEY] = hazards[:_MAX_HAZARDS]
    return point


def bind_drowning_hazards(client: Any, state: Any) -> None:
    """Install checkpointed hazards on the client, as the home anchor is."""
    custom = getattr(state, "custom_data", {})
    entries = custom.get(HAZARD_KEY, []) if isinstance(custom, dict) else []
    client._drowning_hazards = tuple(
        point for point in (_hazard(entry) for entry in entries or []) if point is not None
    )


def fenced_hazard(client: Any, x: float, z: float, radius: float = FENCE_RADIUS) -> Optional[Hazard]:
    """Return a known drowning spot within ``radius`` horizontally, if any."""
    for hazard in getattr(client, "_drowning_hazards", ()) or ():
        if math.hypot(hazard[0] - float(x), hazard[2] - float(z)) <= radius:
            return hazard
    return None


def avoid_water(client: Any, *, sleep: Callable[[float], None] = time.sleep) -> None:
    """Make Baritone route around every water block until ``allow_water``."""
    client.transport.dispatch(
        "chat", {"message": "#set blocksToAvoid " + ",".join(_WATER_BLOCKS)}
    )
    client._avoiding_water = True
    # Chat settings apply on a Minecraft tick; the next plan must see them.
    sleep(0.1)


def allow_water(client: Any, *, force: bool = False, sleep: Callable[[float], None] = time.sleep) -> None:
    """Restore water routing. Surfacing and recovery must be able to swim."""
    if not force and not getattr(client, "_avoiding_water", False):
        return
    client.transport.dispatch("chat", {"message": "#set reset blocksToAvoid"})
    client._avoiding_water = False
    sleep(0.1)


def restore_water_routes(function: Callable) -> Callable:
    """Restore water routing however a search ends, including by death.

    Baritone saves ``#set`` changes, so a leaked avoidance would outlive the
    search and even the controller, and block a grave or home route that swims.
    """
    @wraps(function)
    def wrapped(client: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return function(client, *args, **kwargs)
        finally:
            _best_effort(allow_water, client)
    return wrapped


def _best_effort(change: Callable, client: Any) -> None:
    """A routing change is a safety margin; never let it fail the caller."""
    try:
        change(client)
    except Exception as exc:
        print(f"WATER: could not {change.__name__.replace('_', ' ')} routes ({exc})")


def avoiding_water(function: Callable) -> Callable:
    """Route the wrapped approach around water; an animal across a river is
    unreachable for a starving bot."""
    @wraps(function)
    def wrapped(client: Any, *args: Any, **kwargs: Any) -> Any:
        _best_effort(avoid_water, client)
        try:
            return function(client, *args, **kwargs)
        finally:
            _best_effort(allow_water, client)
    return wrapped


def fence_drowning_death(state: Any, client: Any, death: Any, recovery: dict, position: Any, dimension: str) -> None:
    """Record a drowning once per death so food searches avoid the spot."""
    if not drowned(death) or not position:
        return
    try:
        spot = [int(position[axis]) for axis in ("x", "y", "z")]
    except (KeyError, TypeError, ValueError):
        return
    # Death recovery re-runs while the player is still dead; count it once.
    if recovery.get("drowning_recorded") == spot:
        return
    recovery["drowning_recorded"] = spot
    hazard = record_drowning(state, position, dimension)
    if hazard is not None:
        bind_drowning_hazards(client, state)
        print(f"RECOVERY: fenced drowning spot {hazard} for later food searches")


def turn_back_at_fence(exploration: Any, client: Any, state: dict) -> bool:
    """Leave a drowning fence the search walked into from outside.

    Seven drownings at one riverbed: turn back at the fence rather than
    trusting the next waypoint to route around it. A search that starts
    inside a fence (a pond at home) may still leave.
    """
    from .movement_recovery import block_position
    from .navigation import recovery_goto

    here = block_position(state)
    hazard = fenced_hazard(client, here[0], here[2])
    if hazard is None:
        exploration.clear_of_fences = here
        return False
    if not exploration.exploring or exploration.clear_of_fences is None:
        return False
    print(f"RECOVERY: food search reached the fence around drowning spot {hazard}; turning back")
    exploration.stop(client)
    recovery_goto(client, *exploration.clear_of_fences, timeout=20, check_interval=0.5, tolerance=2.0)
    exploration.start(client, client.transport.dispatch("get_state", {}))
    time.sleep(3)
    return True
