"""Light the base the bot actually lives in, and keep it lit.

Monsters spawn only at block light 0. The homestead's `light_perimeter` step
lit a four-torch ring around a legacy anchor twenty blocks from the house
and was then marked verified for good; on 2026-09-27 only one of those
torches stood and the live base (house, bed, furnaces, farm) had none, while
skeletons and zombies spawned in the shade beside it all day.

This step defines the base from what the bot relies on (house, bed,
furnaces, chests, crafting tables, farm plots), lays a grid of torch spots
over it, and places a bounded batch per run on solid ground wherever no
torch already lights the cell. Torches are made locally: sticks from
planks, charcoal from logs at the home furnace, and a few logs gathered near
base in daylight when needed. It re-checks periodically so broken or missing
torches are replaced.
"""

from __future__ import annotations

import math
import time
from typing import Any, Mapping, Optional, Sequence, Tuple

LIGHTING_KEY = "base_lighting"
#: Grid spacing between torch spots. Torch light 14 falls by one per block,
#: so any spot within about 12 blocks stays above light 0; 8 leaves margin.
SPACING = 8
#: Extra blocks lit beyond the outermost base feature.
MARGIN = 5
#: A zone never grows past this, so one stray landmark cannot sprawl it.
MAX_SPAN = 48
#: A grid cell with a torch this close (horizontally) already counts as lit.
LIT_RADIUS = 5
#: Torches placed per run: bounded work, the rest follows next time.
BATCH = 8
#: How often to re-check while unfinished, and once the base is fully lit.
RECHECK_PENDING = 300.0
RECHECK_DONE = 1800.0
MIN_HEALTH = 14.0
MAX_HOSTILES = 2
#: Covered floors are lit only down to this far below the base's level: the
#: house interior, never the hollow under it. Live A1 2026-09-28 (#32 allowed
#: 6) walked into the cavity below its house to place a torch; the cavity
#: opened into a cave and a skeleton shot it dead at y=70.6. A torch does not
#: make a cave safe, and descending into one at 14 HP is the danger itself.
COVERED_DEPTH = 1

TORCHES = ("minecraft:torch", "minecraft:wall_torch", "minecraft:lantern", "minecraft:soul_torch")
_AIRLIKE = {
    "minecraft:air", "minecraft:cave_air", "minecraft:short_grass", "minecraft:tall_grass",
    "minecraft:fern", "minecraft:large_fern", "minecraft:snow", "minecraft:leaf_litter",
    "minecraft:dandelion", "minecraft:poppy", "minecraft:pink_petals", "minecraft:dead_bush",
}
_NOT_GROUND_TOKENS = ("leaves", "log", "wood", "water", "lava", "farmland", "torch", "glass",
                      "fence", "door", "bed", "chest", "furnace", "crafting_table", "powder_snow")


def _custom(state: Any) -> dict:
    custom = getattr(state, "custom_data", None)
    return custom if isinstance(custom, dict) else {}


def _xyz(value: Any) -> Optional[Tuple[int, int, int]]:
    if isinstance(value, Mapping):
        value = value.get("origin", value.get("location", value.get("bed")))
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        try:
            return (int(value[0]), int(value[1]), int(value[2]))
        except (TypeError, ValueError):
            return None
    return None


def base_points(state: Any) -> list:
    """Positions the bot relies on at home."""
    custom = _custom(state)
    points = []
    for key in ("base_location", "homestead_anchor"):
        if (p := _xyz(custom.get(key))) is not None:
            points.append(p)
    respawn = custom.get("home_respawn")
    if isinstance(respawn, Mapping) and (p := _xyz(respawn.get("bed"))) is not None:
        points.append(p)
    structures = custom.get("structures", {})
    if isinstance(structures, Mapping):
        house = structures.get("starter_house", {})
        if isinstance(house, Mapping):
            for key in ("origin", "bed", "furnace", "crafting_table", "supply_chest", "door"):
                if (p := _xyz(house.get(key))) is not None:
                    points.append(p)
        if (p := _xyz(structures.get("food_source"))) is not None:
            points.append(p)
    worker = custom.get("food_worker", {})
    if isinstance(worker, Mapping):
        for plot in worker.get("farm_plots", []) or []:
            if (p := _xyz(plot)) is not None:
                points.append(p)
    if not points:
        return []
    # Drop outliers (a stale record far away must not stretch the zone).
    anchor = points[0]
    return [p for p in points if math.hypot(p[0] - anchor[0], p[2] - anchor[2]) <= MAX_SPAN]


def lighting_zone(state: Any):
    """(x0, z0, x1, z1, y_hint) around the base, or None."""
    points = base_points(state)
    if not points:
        return None
    x0 = min(p[0] for p in points) - MARGIN
    x1 = max(p[0] for p in points) + MARGIN
    z0 = min(p[2] for p in points) - MARGIN
    z1 = max(p[2] for p in points) + MARGIN
    if x1 - x0 > MAX_SPAN:
        mid = (x0 + x1) // 2
        x0, x1 = mid - MAX_SPAN // 2, mid + MAX_SPAN // 2
    if z1 - z0 > MAX_SPAN:
        mid = (z0 + z1) // 2
        z0, z1 = mid - MAX_SPAN // 2, mid + MAX_SPAN // 2
    y_hint = sorted(p[1] for p in points)[len(points) // 2]
    return x0, z0, x1, z1, y_hint


def grid_points(zone) -> list:
    x0, z0, x1, z1, _y = zone
    half = SPACING // 2
    return [
        (x, z)
        for x in range(x0 + half, x1 + 1, SPACING)
        for z in range(z0 + half, z1 + 1, SPACING)
    ]


def interior_columns(state: Any) -> list:
    """Columns that a spaced grid can miss: inside the house and home chests."""
    columns = []
    structures = _custom(state).get("structures", {})
    house = structures.get("starter_house", {}) if isinstance(structures, Mapping) else {}
    if isinstance(house, Mapping):
        origin = _xyz(house.get("origin"))
        if origin is not None:
            columns.append((origin[0] + 3, origin[2] + 3))  # 7x7 house centre
        chest = _xyz(house.get("supply_chest"))
        if chest is not None:
            columns.append((chest[0], chest[2]))
    base = _xyz(_custom(state).get("base_location"))
    storage = _custom(state).get("storage", {})
    if isinstance(storage, Mapping):
        for value in storage.values():
            p = _xyz(value)
            if p is not None and (base is None or abs(p[1] - base[1]) <= COVERED_DEPTH):
                columns.append((p[0], p[2]))
    return columns


def lighting_due(state: Any, *, now: Optional[float] = None) -> bool:
    """Whether a lighting pass should be offered now."""
    if lighting_zone(state) is None:
        return False
    record = _custom(state).get(LIGHTING_KEY, {})
    try:
        next_check = float(record.get("next_check", 0) or 0) if isinstance(record, Mapping) else 0.0
    except (TypeError, ValueError):
        next_check = 0.0
    return (time.time() if now is None else float(now)) >= next_check


def lighting_allowed(signals: Any) -> bool:
    """Lighting is the fix for hostiles, so it must not wait for none."""
    return (
        float(getattr(signals, "health", 0.0) or 0.0) >= MIN_HEALTH
        and int(getattr(signals, "nearby_hostiles", 0) or 0) <= MAX_HOSTILES
    )


def _block(client: Any, x: int, y: int, z: int) -> str:
    try:
        return str(client.transport.dispatch("get_block", {"x": x, "y": y, "z": z}).get("id", ""))
    except Exception:
        return ""


def torch_spot(client: Any, x: int, z: int, y_hint: int) -> Optional[Tuple[int, int, int]]:
    """The first floor cell in this column (kept for callers and tests)."""
    spots = torch_spots(client, x, z, y_hint)
    return spots[0] if spots else None


def torch_spots(client: Any, x: int, z: int, y_hint: int) -> list:
    """Every floor cell in this column near the base's level, top first.

    A floor cell is air with air above and solid ground below. The first one
    under open sky is kept (lighting open ground); every *covered* one below
    a roof, floor or canopy is kept too. The house interior and the hollow
    under its floor are exactly those covered cells, and a surface-only grid
    never reached them: live A1 died in the dark hollow under its own house.
    """
    column = {}
    for y in range(y_hint + 12, y_hint - 11, -1):
        block = _block(client, x, y, z)
        if not block or block == "minecraft:void_air":
            return []  # unloaded or unknown: do not guess
        column[y] = block
    spots = []
    covered = False
    for y in range(y_hint + 11, y_hint - 10, -1):
        block = column[y]
        if block not in _AIRLIKE:
            covered = True  # anything overhead shades the cells below
            continue
        below = column.get(y - 1, "")
        above = column.get(y + 1, "")
        if (
            above in _AIRLIKE
            and below not in _AIRLIKE
            and not any(token in below for token in _NOT_GROUND_TOKENS)
            and (covered or not spots)
            and y >= y_hint - COVERED_DEPTH  # hollows, not the caves below them
        ):
            spots.append((x, y, z))
    return spots


def _existing_torches(client: Any) -> list:
    try:
        found = client.transport.dispatch(
            "find_blocks", {"blocks": list(TORCHES), "radius": 40, "limit": 512}
        ).get("found", [])
    except Exception:
        return []
    return [(int(b["x"]), int(b["y"]), int(b["z"])) for b in found]


def _lit(spot: Sequence[int], torches: Sequence[Sequence[int]]) -> bool:
    """A torch within LIT_RADIUS in 3D lights the spot (not one a floor away)."""
    return any(math.dist(t, spot) <= LIT_RADIUS for t in torches)


def _count(client: Any, item: str) -> int:
    from ..common.inventory import count_item

    try:
        return max(0, int(count_item(client, item) or 0))
    except Exception:
        return 0


def _fuel(client: Any) -> int:
    return _count(client, "minecraft:coal") + _count(client, "minecraft:charcoal")


def _carried_log(client: Any) -> Optional[Tuple[str, str]]:
    from ..common.resources import LOG_TO_PLANKS

    for log, plank in LOG_TO_PLANKS.items():
        if _count(client, log) > 0:
            return log, plank
    return None


def _planks(client: Any) -> Tuple[int, Optional[str]]:
    from ..common.resources import PLANK_ITEMS

    best = max(PLANK_ITEMS, key=lambda item: _count(client, item))
    return _count(client, best), (best if _count(client, best) > 0 else None)


def ensure_torches(client: Any, state: Any, wanted: int, anchor) -> int:
    """Make up to ``wanted`` torches from local materials; return carried torches."""
    from ..common import harness_ops
    from ..common.inventory import craft
    from ..common.livestock_food import _home_furnace
    from .charcoal_wood import gather_charcoal_logs

    def torches() -> int:
        return _count(client, "minecraft:torch")

    if torches() >= wanted:
        return torches()
    from .torch_recovery import retrieve_torch_supplies

    retrieve_torch_supplies(client, state, wanted, anchor)
    if torches() >= wanted:
        return torches()
    fuel_needed = math.ceil((wanted - torches()) / 4)
    if _fuel(client) < fuel_needed:
        logs_needed = fuel_needed - _fuel(client) + 2  # +2 for planks and sticks
        carried_logs = sum(_count(client, log) for log in _log_items())
        if carried_logs < logs_needed:
            gather_charcoal_logs(client, state, logs_needed - carried_logs, anchor)
        pair = _carried_log(client)
        planks, plank_item = _planks(client)
        if pair is not None and planks < 4:
            craft(client, pair[1], 4)
            planks, plank_item = _planks(client)
        pair = _carried_log(client)
        furnace = _home_furnace(client, state)
        if pair is not None and plank_item is not None and furnace is not None:
            logs = min(_count(client, pair[0]), fuel_needed)
            try:
                harness_ops.smelt_in_furnace(
                    client, furnace, pair[0], plank_item, "minecraft:charcoal", logs
                )
            except Exception as exc:
                print(f"BASE LIGHTING: charcoal smelting failed ({exc})")
    if _count(client, "minecraft:stick") < math.ceil(wanted / 4):
        planks, plank_item = _planks(client)
        if planks < 2 and (pair := _carried_log(client)) is not None:
            craft(client, pair[1], 4)
        craft(client, "minecraft:stick", 4)
    for _ in range(math.ceil(wanted / 4)):
        if torches() >= wanted or _fuel(client) < 1 or _count(client, "minecraft:stick") < 1:
            break
        before = torches()
        craft(client, "minecraft:torch", 4)
        if torches() <= before:
            break
    return torches()


def _log_items():
    from ..common.resources import LOG_TO_PLANKS

    return tuple(LOG_TO_PLANKS)


def light_base(client: Any, state: Any, *, now: Optional[float] = None) -> Tuple[int, int, str]:
    """Place one bounded batch of torches; return (placed, remaining, detail)."""
    from ..common import harness_ops
    from ..common.navigation import goto

    current = time.time() if now is None else float(now)
    custom = _custom(state)
    record = custom.setdefault(LIGHTING_KEY, {}) if isinstance(custom, dict) else {}
    zone = lighting_zone(state)
    if zone is None:
        return 0, 0, "no base to light"
    x0, z0, x1, z1, y_hint = zone
    center = ((x0 + x1) // 2, y_hint, (z0 + z1) // 2)
    goto(client, *center, timeout=120, tolerance=6.0, radius=4)

    torches = _existing_torches(client)
    columns = list(dict.fromkeys(grid_points(zone) + interior_columns(state)))
    spots = [
        spot
        for x, z in columns
        for spot in torch_spots(client, x, z, y_hint)
        if not _lit(spot, torches)
    ]
    remaining_before = len(spots)
    placed = 0
    if spots:
        batch = spots[:BATCH]
        ensure_torches(client, state, len(batch), center)
        for spot in batch:
            if _count(client, "minecraft:torch") < 1:
                break
            try:
                if harness_ops.place_block_exact(client, *spot, "minecraft:torch", allow_break=False):
                    placed += 1
            except Exception as exc:
                print(f"BASE LIGHTING: torch at {spot} failed ({exc})")
    remaining = max(0, remaining_before - placed)
    record.update(
        zone=[x0, z0, x1, z1],
        last_run=current,
        placed_total=int(record.get("placed_total", 0) or 0) + placed,
        remaining=remaining,
        next_check=current + (RECHECK_PENDING if remaining else RECHECK_DONE),
    )
    detail = (
        f"base lighting placed {placed} torch(es); {remaining} dark spot(s) remain "
        f"in {x1 - x0 + 1}x{z1 - z0 + 1} base zone"
    )
    print(f"BASE LIGHTING: {detail}")
    return placed, remaining, detail


__all__ = [
    "LIGHTING_KEY",
    "base_points",
    "ensure_torches",
    "grid_points",
    "light_base",
    "lighting_allowed",
    "lighting_due",
    "lighting_zone",
    "interior_columns",
    "torch_spot",
    "torch_spots",
]
