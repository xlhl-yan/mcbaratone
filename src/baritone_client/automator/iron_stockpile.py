"""A standing iron stockpile: mine when stock runs low, smelt, and bank the rest.

Iron used to come from one phase (`FOOD_AND_IRON`) that is marked done once and
never reopens, so a bot that died and lost its kit could not get iron back, and
the only repeatable path (armour recovery) mined only with armour already worn.
Live A1 2026-10-01: 400+ iron ore within 96 blocks of the base, 3 ingots held,
0 armour, no iron-getting step eligible at all.

The repo's other iron job, `common.iron_supply.run_iron_cycle`, belongs to the
dedicated fleet *iron supplier* role: it mines with Baritone's `mine` and needs
a sibling roster to be assigned to a bot at all. A lone balanced bot such as A1
never gets it, so this module is separate on purpose and mines with the
bounded `tunnel_miner` instead of `mine`.

This is that step. It is offered through the armour-upkeep opportunity whenever
carried plus banked iron falls below ``LOW_STOCK``. A trip digs one staircase
from a safe surface entrance (reused on later trips), mines the nearest iron
with ``tunnel_miner`` and its planner, retraces its own trail, smelts, and keeps
only an armour set's worth on the bot: a bot that dies 50 times a day must not
carry the stockpile.
"""

from __future__ import annotations

import time
from typing import Any, List, Optional, Tuple

IRON_INGOT = "minecraft:iron_ingot"
RAW_IRON = "minecraft:raw_iron"
TORCH = "minecraft:torch"
KEY = "iron_stockpile"

#: Refill below this many ingots (carried + raw + banked), up to ``TARGET_STOCK``.
LOW_STOCK = 32
TARGET_STOCK = 64
MAX_RAW_PER_TRIP = 48
#: One armour set's worth stays carried so armour can be crafted at once.
CARRY_RESERVE = 24
HEALTH_MIN = 18.0
FOOD_MIN = 14
TRIP_SECONDS = 1200
#: Without enough torches the tunnel is dark: a sealed 1x2 tunnel is a poor spawn
#: area, but the trip is shorter and leaves sooner when a mob shows.
DARK_TRIP_SECONDS = 600
DARK_PATIENCE = 5.0
TORCH_RETRY_SECONDS = 3600.0
ROOM_WANTED = 10
STORAGE_FOOD_RESERVE = 64
#: Clutter worth banking before a trip. Never tools, food, torches or armour.
CLUTTER = frozenset(
    "minecraft:" + name
    for name in (
        "dirt", "coarse_dirt", "netherrack", "cobblestone_slab", "feather",
        "rabbit_hide", "andesite", "diorite", "granite", "tuff", "gravel", "sand",
        "bone", "rotten_flesh", "spider_eye", "string", "gunpowder", "flint",
        "poisonous_potato", "cobbled_deepslate", "moss_block", "wildflowers",
        "oak_sapling", "birch_sapling", "acacia_sapling", "spruce_sapling",
    )
)
MIN_Y = 12
MIN_TORCHES = 4
WANT_TORCHES = 12
SUCCESS_REST = 300.0
FAIL_BACKOFF = (600.0, 1800.0, 5400.0, 14400.0)
BANK_RADIUS = 96.0
BANK_VERTICAL = 16.0
RING = (16, 20, 24)
HEADINGS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1))
GROUND = frozenset(
    "minecraft:" + name for name in ("grass_block", "dirt", "coarse_dirt", "podzol", "stone")
)
AIR = frozenset({"minecraft:air", "minecraft:cave_air"})


def _record(state: Any) -> dict:
    custom = getattr(state, "custom_data", None)
    if not isinstance(custom, dict):
        return {}
    record = custom.get(KEY)
    if not isinstance(record, dict):
        record = {}
        custom[KEY] = record
    return record


def _count(client: Any, item: str) -> int:
    from ..common.inventory import count_item

    try:
        return max(0, int(count_item(client, item) or 0))
    except Exception:
        return 0


def _live(client: Any) -> dict:
    try:
        live = client.transport.dispatch("get_state", {})
    except Exception:
        return {}
    live = live.get("data", live) if isinstance(live, dict) else {}
    return live if isinstance(live, dict) else {}


def banked_iron(client: Any, state: Any) -> int:
    """Ingots cataloged in home storage."""
    from math import hypot

    from ..common.storage_catalog import catalog_for
    from .armor_recovery import _home

    anchor = _home(state)
    if anchor is None:
        return 0
    try:
        rows = catalog_for(client, state).find_item(IRON_INGOT)
    except Exception:
        return 0
    return sum(
        int(row.get("count", 0) or 0)
        for row in rows
        if "overworld" in str(row.get("dimension", "overworld"))
        and hypot(row["x"] - anchor[0], row["z"] - anchor[2]) <= BANK_RADIUS
        and abs(row["y"] - anchor[1]) <= BANK_VERTICAL
    )


def iron_stock(client: Any, state: Any) -> int:
    return _count(client, IRON_INGOT) + _count(client, RAW_IRON) + banked_iron(client, state)


def supply_due(client: Any, state: Any, signals: Any = None, *, now: Optional[float] = None) -> bool:
    """Whether a mining trip should run now."""
    if state is None:
        return False
    current = time.time() if now is None else float(now)
    if current < float(_record(state).get("next_trip", 0) or 0):
        return False
    if signals is not None:
        if float(getattr(signals, "health", 0.0) or 0.0) < HEALTH_MIN:
            return False
        if int(getattr(signals, "nearby_hostiles", 0) or 0) > 0:
            return False
    live = _live(client)
    try:
        if (
            live.get("is_dead")
            or "overworld" not in str(live.get("dimension", "minecraft:overworld"))
            or float(live.get("health", 0) or 0) < HEALTH_MIN
            or int(live.get("food_level", live.get("food", 0)) or 0) < FOOD_MIN
        ):
            return False
    except (TypeError, ValueError):
        return False
    from .weapon_upkeep import kit_gaps

    # Never go underground unarmed: a missing sword/pickaxe is crafted first.
    if kit_gaps(client):
        return False
    return iron_stock(client, state) < LOW_STOCK


# -- entrance ---------------------------------------------------------------
def _block(client: Any, x: int, y: int, z: int) -> str:
    try:
        return str(client.transport.dispatch("get_block", {"x": x, "y": y, "z": z}).get("id", ""))
    except Exception:
        return ""


def _surface(client: Any, x: int, z: int, y_hint: int) -> Optional[int]:
    """Y of the first solid block from above, or None when it is unusable."""
    for y in range(y_hint + 12, y_hint - 13, -1):
        block = _block(client, x, y, z)
        if not block or block == "minecraft:void_air":
            return None
        if block in AIR:
            continue
        if block in GROUND and _block(client, x, y + 1, z) in AIR and _block(client, x, y + 2, z) in AIR:
            return y
        return None
    return None


def choose_entrance(client: Any, state: Any) -> Optional[Tuple[int, int, int]]:
    """A feet cell on open, plain ground just outside the base's built zone."""
    from .armor_recovery import _home
    from .base_lighting import lighting_zone

    anchor = _home(state)
    if anchor is None:
        return None
    zone = lighting_zone(state)
    bad = {tuple(c) for c in _record(state).get("bad_entrances", [])}
    for radius in RING:
        for dx, dz in HEADINGS:
            x = anchor[0] + dx * radius // (2 if dx and dz else 1)
            z = anchor[2] + dz * radius // (2 if dx and dz else 1)
            if zone and zone[0] - 3 <= x <= zone[2] + 3 and zone[1] - 3 <= z <= zone[3] + 3:
                continue
            ground = _surface(client, x, z, anchor[1])
            if ground is not None and (x, ground + 1, z) not in bad:
                return (x, ground + 1, z)
    return None


# -- the trip ---------------------------------------------------------------
def _supply_chest(state: Any) -> Optional[Tuple[int, int, int]]:
    custom = getattr(state, "custom_data", {}) or {}
    structures = custom.get("structures", {}) if isinstance(custom, dict) else {}
    for name in ("starter_house", "bootstrap_base"):
        chest = (structures.get(name) or {}).get("supply_chest") if isinstance(structures, dict) else None
        if isinstance(chest, (list, tuple)) and len(chest) == 3:
            return tuple(int(v) for v in chest)
    return None


def _bank(client: Any, state: Any) -> int:
    """Keep an armour set carried; put the rest in the home chest."""
    chest = _supply_chest(state)
    if chest is None or _count(client, IRON_INGOT) <= CARRY_RESERVE:
        return 0
    from ..common.inventory import deposit_excess_to_chest

    before = _count(client, IRON_INGOT)
    try:
        deposit_excess_to_chest(
            client, chest, deposit_items={IRON_INGOT}, retain_counts={IRON_INGOT: CARRY_RESERVE}
        )
    except Exception as exc:
        print(f"IRON SUPPLY: banking failed ({exc})")
    return max(0, before - _count(client, IRON_INGOT))


HOME_RANGE = 4.0


def _near(client: Any, anchor: Tuple[int, int, int], distance: float) -> bool:
    from math import hypot

    position = _live(client).get("block_position") or {}
    try:
        return (hypot(float(position["x"]) - anchor[0], float(position["z"]) - anchor[2]) <= distance
                and abs(float(position["y"]) - anchor[1]) <= 2)
    except (KeyError, TypeError, ValueError):
        return False


def _at_cell(client: Any, target: Tuple[int, int, int]) -> bool:
    position = _live(client).get("block_position") or {}
    try:
        return tuple(int(float(position[axis])) for axis in ("x", "y", "z")) == target
    except (KeyError, TypeError, ValueError):
        return False


def _make_room(client: Any, state: Any) -> int:
    """Bank clutter in the home chest so a trip never starts with a full pack."""
    from ..common.inventory import deposit_excess_to_chest
    from ..common.tunnel_miner import DIGGING_JUNK, free_slots

    chest = _supply_chest(state)
    if chest is None:
        return 0
    try:
        if free_slots(client) >= ROOM_WANTED:
            return 0
    except Exception:
        return 0
    try:
        # Mining rubble is safe to bank, and can otherwise occupy a slot for
        # every block type collected on prior trips before preparation runs.
        moved = deposit_excess_to_chest(
            client, chest, deposit_items=set(CLUTTER) | set(DIGGING_JUNK),
            retain_counts={"minecraft:cobblestone": 64},
        )
    except Exception as exc:
        print(f"IRON SUPPLY: could not bank clutter ({exc})")
        return 0
    print(f"IRON SUPPLY: banked {max(0, moved)} stack(s) of clutter to make room")
    return max(0, int(moved or 0))


def _schedule(rec: dict, now: float, ok: bool) -> None:
    if ok:
        rec["failures"] = 0
        rec["next_trip"] = now + SUCCESS_REST
        return
    streak = int(rec.get("failures", 0) or 0) + 1
    rec["failures"] = streak
    rec["next_trip"] = now + FAIL_BACKOFF[min(streak - 1, len(FAIL_BACKOFF) - 1)]


def run_supply_trip(client: Any, state: Any, *, now: Optional[float] = None) -> Tuple[bool, str, int, int]:
    """One bounded trip. Returns the scheduler's (success, detail, before, after)."""
    from ..common.navigation import goto, recovery_navigation_defense
    from ..common.tasks import PlayerDeathDetected, SurvivalRecoveryRequired
    from ..common.tunnel_miner import MineAbort, TunnelMiner, route_travel_seconds
    from ..common.tunnel_planner import loop_erase
    from .armor_recovery import _home, _smelt_raw_iron
    from .mining_checkpoint import checkpoint_progress

    current = time.time() if now is None else float(now)
    rec = _record(state)
    rec["last_attempt"] = current
    before = iron_stock(client, state)
    anchor = _home(state)

    def done(ok: bool, detail: str, *, retry_after: Optional[float] = None):
        _schedule(rec, current, ok)
        if retry_after is not None and not ok:
            rec["next_trip"] = current + retry_after
        after = iron_stock(client, state)
        print(f"IRON SUPPLY: {detail} (stock {before}->{after})")
        return ok, detail, before, after

    if anchor is not None and not _near(client, anchor, HOME_RANGE):
        # Storage preparation needs actual home arrival, even after a short flee.
        if (not goto(client, *anchor, timeout=300, tolerance=2.0,
                     on_defense=lambda: recovery_navigation_defense(client))
                or not _near(client, anchor, HOME_RANGE)):
            return done(False, "could not get home to start a mining trip", retry_after=600.0)
    _make_room(client, state)
    from .iron_preparation import prepare_iron_inventory

    if reason := prepare_iron_inventory(client, state):
        return done(False, reason, retry_after=600.0)
    if _count(client, TORCH) < MIN_TORCHES and anchor is not None and current >= float(rec.get("torch_retry", 0) or 0):
        from .base_lighting import ensure_torches

        rec["torch_retry"] = current + TORCH_RETRY_SECONDS  # never loop on this
        try:
            ensure_torches(client, state, WANT_TORCHES, anchor)
        except (PlayerDeathDetected, SurvivalRecoveryRequired):
            raise
        except Exception as exc:
            print(f"IRON SUPPLY: could not make torches ({exc})")
    lit = _count(client, TORCH) >= MIN_TORCHES
    saved_spine = rec.get("spine", [])
    if not lit and saved_spine and min(c[1] for c in saved_spine) < saved_spine[0][1] - 12:
        # The miner refuses deep unlit work. Do not spend the entire trip
        # walking down a saved route whose prerequisite is still missing.
        rec["torch_retry"] = min(float(rec.get("torch_retry", current) or current), current + 600.0)
        return done(False, "deep saved mine needs torches before descent; route preserved", retry_after=600.0)
    if not lit:
        print("IRON SUPPLY: no torches; running a shorter dark trip")
    entrance = rec.get("entrance")
    entrance = tuple(entrance) if isinstance(entrance, (list, tuple)) and len(entrance) == 3 else choose_entrance(client, state)
    if entrance is None:
        return done(False, "no safe surface entrance near the base")
    from ..common.house_door_travel import prepare_house_door_for_departure

    if not prepare_house_door_for_departure(client, state, anchor):
        return done(False, "saved-house doorway is not safe for mine travel", retry_after=600.0)
    rec["entrance"] = list(entrance)
    spine: List[Tuple[int, int, int]] = [tuple(c) for c in rec.get("spine", [])] or [entrance]
    from ..common.tunnel_travel_defense import intact_route_start

    start = intact_route_start(spine, lambda cell: _block(client, *cell))
    if start:
        # Walk to the intact part; the entrance height still anchors the miner.
        print(f"IRON SUPPLY: saved route lost its floor near the entrance; starting at step {start} {spine[start]}")
        spine = spine[start:]
    if (
        not goto(client, *spine[0], timeout=150, tolerance=0.8)
        or not _at_cell(client, spine[0])
    ):
        rec["approach_failures"] = int(rec.get("approach_failures", 0) or 0) + 1
        # Interrupted travel does not establish bad terrain or depleted ore.
        # Retain the proven route; fresh navigation/terrain checks still gate it.
        return done(False, "could not reach the mine entrance", retry_after=600.0)
    rec["approach_failures"] = 0

    miner = TunnelMiner(
        client, surface_y=entrance[1],
        deadline=time.monotonic() + route_travel_seconds(spine),
        spine=spine, entrance=entrance, min_y=MIN_Y,
        patience=12.0 if lit else DARK_PATIENCE,
        on_progress=checkpoint_progress(client, state, rec),
    )
    raw_start = _count(client, RAW_IRON)
    goal = raw_start + max(1, min(MAX_RAW_PER_TRIP, TARGET_STOCK - before))
    reason = ""
    try:
        miner.descend(spine)
        miner.check()  # expired/unsafe travel cannot renew the work deadline
        miner.deadline = time.monotonic() + (TRIP_SECONDS if lit else DARK_TRIP_SECONDS)
        reason = miner.mine(goal, lambda: _count(client, RAW_IRON))
    except MineAbort as abort:
        reason = abort.reason
    except (PlayerDeathDetected, SurvivalRecoveryRequired):
        raise
    finally:
        rec["spine"] = [list(c) for c in loop_erase(miner.trail)][-400:]
    returned = reason != "died" and miner.retreat()
    mined = max(0, _count(client, RAW_IRON) - raw_start)
    if mined:
        rec["dry_trips"] = 0
    elif reason == "no reachable iron":
        # Only a completed search with no reachable ore says anything about
        # geology. Inventory pressure and failed tunnel navigation are
        # operational failures; they must not retire a proven entrance.
        rec["dry_trips"] = int(rec.get("dry_trips", 0) or 0) + 1
        if rec["dry_trips"] >= 3:
            rec.setdefault("bad_entrances", []).append(list(entrance))
            for stale in ("entrance", "spine", "dry_trips"):
                rec.pop(stale, None)
    smelted = _smelt_raw_iron(client, state) if mined and returned else 0
    banked = _bank(client, state) if smelted else 0
    return_detail = "" if returned else "; return route not verified, raw iron retained"
    detail = f"mined {mined} raw iron ({reason or 'done'}); smelted {smelted}; banked {banked}; {miner.stats.moves} tunnel moves{return_detail}"
    return done(mined > 0 and returned, detail,
                retry_after=600.0 if reason != "no reachable iron" else None)


__all__ = [
    "LOW_STOCK", "banked_iron", "choose_entrance", "iron_stock", "run_supply_trip", "supply_due",
]
