"""Small daylight charcoal batches without native unrestricted tree mining."""

import math
import time

from ..common.home_surface import protect_home_route


GROUND = {"minecraft:dirt", "minecraft:grass_block", "minecraft:podzol", "minecraft:coarse_dirt"}
AIR = {"minecraft:air", "minecraft:cave_air"}
FOOT_SPACE = AIR | {"minecraft:leaf_litter", "minecraft:short_grass", "minecraft:fern"}


def _safe(client, anchor):
    from ..common.combat import scan_for_threats

    live = client.transport.dispatch("get_state", {})
    position = live.get("block_position", live.get("position", {}))
    xyz = tuple(float(position[a]) for a in ("x", "y", "z"))
    health = float(live.get("health", 0))
    return (
        live.get("is_dead") is False and live.get("success") is not False
        and str(live.get("status", "")).lower() != "error"
        and "overworld" in str(live.get("dimension", ""))
        and math.isfinite(health) and health >= 18
        and int(live.get("food_level", live.get("food", 0))) >= 14
        and int(live.get("world_time", 11000)) % 24000 < 11000
        and all(math.isfinite(v) for v in xyz)
        and math.dist(xyz, anchor) <= 48
        and not scan_for_threats(client, radius=16, raise_on_error=True, player_state=live)
    )


def _block(client, cell):
    return client.transport.dispatch("get_block", dict(zip(("x", "y", "z"), cell))).get("id", "")


@protect_home_route(surface_work=True, safe_movement=True)
def _travel(client, x, y, z):
    from ..common.navigation import goto

    return goto(client, x, y, z, timeout=45, tolerance=1.25)


def _break_log(client, cell, log, anchor):
    """A dig acknowledgement starts progressive mining; it is not removal."""
    client.transport.dispatch("dig_block", dict(zip(("x", "y", "z"), cell), max_ticks=200))
    deadline = time.monotonic() + 11.0
    removed = False
    try:
        for _ in range(44):
            time.sleep(0.25)
            observed = _block(client, cell)
            if observed in AIR:
                removed = True
                return True
            if observed != log or time.monotonic() >= deadline or not _safe(client, anchor):
                break
    finally:
        if not removed:
            client.transport.dispatch("stop", {})
    return False


def gather_charcoal_logs(client, state, wanted, anchor):
    """Harvest at most one observed trunk and return toward the saved home.

    Natural ground, a vertical trunk, and matching leaves identify a tree.
    Every dig retains real ray validation; failures cool down that candidate.
    Temporary travel settings are restored only after a verified stop.
    """
    from ..common.inventory import count_item, select_item
    from ..common.resources import LOG_TO_PLANKS
    from ..common.harness_ops import place_block_exact
    from ..common.tasks import PlayerDeathDetected, SurvivalRecoveryRequired

    if anchor is None or getattr(client, "_protected_home_anchor", None) is None:
        return 0
    anchor = tuple(client._protected_home_anchor)  # a lighting-zone centre is not home
    rec = state.custom_data.setdefault("charcoal_wood", {})
    blocked = rec.setdefault("rejected_until", {})
    now = time.time()
    blocked_keys = list(blocked)
    for key in blocked_keys:
        if blocked[key] <= now:
            blocked.pop(key)
    moved = False
    gained = 0
    try:
        if not _safe(client, anchor):
            return 0
        found = client.transport.dispatch("find_blocks", {
            "blocks": list(LOG_TO_PLANKS), "radius": 48, "limit": 100,
        }).get("found", [])
        candidates = sorted(found, key=lambda row: (row.get("distance", float("inf")), row["y"]))
        for row in candidates[:100]:
            x, y, z = (int(row[a]) for a in ("x", "y", "z"))
            key = f"{x},{y},{z}"
            if (key in blocked or not 10 < math.hypot(x - anchor[0], z - anchor[2]) <= 48
                    or not anchor[1] - 2 <= y <= anchor[1] + 4):
                continue
            log = _block(client, (x, y, z))
            if log not in LOG_TO_PLANKS or _block(client, (x, y - 1, z)) not in GROUND:
                continue
            trunk = [(x, y + dy, z) for dy in range(6) if _block(client, (x, y + dy, z)) == log]
            leaves = log.replace("_log", "_leaves")
            if len(trunk) < 3 or not any(_block(client, (x + dx, y + 4, z + dz)) == leaves
                                        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                continue
            stands = [(x + dx, y, z + dz) for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1))
                      if _block(client, (x + dx, y - 1, z + dz)) in GROUND
                      and _block(client, (x + dx, y, z + dz)) in FOOT_SPACE
                      and _block(client, (x + dx, y + 1, z + dz)) in AIR]
            if not stands:
                continue
            blocked[key] = now + 1200
            moved = True
            if not _safe(client, anchor) or not _travel(client, *stands[0]):
                return 0
            before = count_item(client, log)
            for cell in trunk[:min(6, max(1, int(wanted)))]:
                if not _safe(client, anchor) or _block(client, cell) != log:
                    break
                for axe in ("minecraft:iron_axe", "minecraft:stone_axe", "minecraft:wooden_axe"):
                    if count_item(client, axe) > 0:
                        select_item(client, axe, allow_swap=True)
                        break
                if not _break_log(client, cell, log, anchor):
                    break
            if _block(client, (x, y, z)) in AIR and _safe(client, anchor):
                _travel(client, x, y, z)  # collect drops on the verified former trunk floor
                time.sleep(0.75)
                sapling = log.replace("_log", "_sapling")
                if count_item(client, sapling) > 0:
                    place_block_exact(client, x, y, z, sapling, allow_break=False)
            gained = max(0, count_item(client, log) - before)
            rec["logs_harvested"] = int(rec.get("logs_harvested", 0)) + gained
            return gained
        print(f"CHARCOAL WOOD: no natural trunk 10-48 blocks from home near its height ({len(candidates)} log(s) seen)")
    except (PlayerDeathDetected, SurvivalRecoveryRequired):
        raise
    except Exception as exc:
        print(f"CHARCOAL WOOD: bounded harvest stopped ({exc})")
    finally:
        if moved:
            try:
                if not client.transport.dispatch("get_state", {}).get("is_dead", True):
                    _travel(client, *anchor)
            except Exception as exc:
                print(f"CHARCOAL WOOD: return could not be verified ({exc})")
    return gained
