"""Bounded, non-destructive home capacity for a mining expedition."""

import math

from ..common.home_surface import bind_home_surface, protect_home_route


AIR = {"minecraft:air", "minecraft:cave_air"}


def _block(client, cell):
    data = client.transport.dispatch("get_block", dict(zip(("x", "y", "z"), cell)))
    while isinstance(data, dict):
        if (data.get("success") is False or data.get("error")
                or str(data.get("status", "")).lower() in {"error", "failed", "failure"}):
            raise ValueError("mining storage block read failed")
        if "data" not in data:
            return data.get("id", "")
        data = data["data"]
    raise ValueError("mining storage block read is unavailable")


def _at_home(client, anchor):
    from ..common.food_workstation import _fresh_safe_position
    from ..common.combat import scan_for_threats

    position = _fresh_safe_position(client, require_grounded=True)
    if position is None or math.dist(position, anchor) > 4 or abs(position[1] - anchor[1]) > 1:
        return False
    live = client.transport.dispatch("get_state", {})
    return (live.get("is_dead") is False and live.get("is_pathing") is False
            and live.get("dimension") == "minecraft:overworld" and live.get("game_mode") == "survival"
            and live.get("is_on_ground") is True and float(live.get("health", 0)) >= 18
            and int(live.get("food_level", 0)) >= 14
            and not scan_for_threats(client, radius=8, raise_on_error=True, player_state=live))


def _house_cells(state, anchor):
    """Bounded wall storage, keeping the doorway and its interior aisle clear."""
    house = state.custom_data.get("structures", {}).get("starter_house", {})
    origin = house.get("origin")
    if not isinstance(origin, (list, tuple)) or len(origin) != 3:
        return []
    if not all(type(v) is int for v in origin):
        return []
    ox, oy, oz = map(int, origin)
    excluded = {tuple(v) for v in house.values() if isinstance(v, (list, tuple)) and len(v) == 3}
    corners = ((5, 1), (5, 5), (1, 5), (1, 1))
    edges = tuple((dx, dz) for dx in range(1, 6) for dz in range(1, 6)
                  if (dx in (1, 5) or dz in (1, 5)) and dx != 3)
    cells = []
    for dx, dz in dict.fromkeys(corners + edges):
        cell = (ox + dx, oy + 1, oz + dz)
        if cell in excluded or math.dist(cell, anchor) > 4.5:
            continue
        if dx in (1, 5):
            wall = (ox + (6 if dx == 5 else 0), oy + 1, oz + dz)
        else:
            wall = (ox + dx, oy + 1, oz + (6 if dz == 5 else 0))
        cells.append((cell, wall))
    return cells


def _spot(client, state, anchor):
    from ..common.base import _ALL_PLANKS

    house_blocks = {"minecraft:cobblestone", "minecraft:stone"} | set(_ALL_PLANKS)
    for cell, wall in _house_cells(state, anchor):
        if (_block(client, cell) in AIR
                and _block(client, (cell[0], cell[1] + 1, cell[2])) in AIR
                and _block(client, (cell[0], cell[1] - 1, cell[2])) in house_blocks
                and _block(client, wall) in house_blocks):
            return cell
    return None


def _local_chests(client, state, anchor, saved):
    """A checkpoint is a hint; observe each nearby chest before reusing it."""
    candidates = []
    if (isinstance(saved, (list, tuple)) and len(saved) == 3
            and all(type(value) is int for value in saved)
            and math.dist(saved, anchor) <= 4.5):
        candidates.append(tuple(saved))
    candidates.extend(cell for cell, _wall in _house_cells(state, anchor))
    return [cell for cell in dict.fromkeys(candidates) if _block(client, cell) == "minecraft:chest"]


def _full(client, chest):
    """Only a complete freshly opened chest snapshot can justify expansion."""
    from ..common.harness_ops import open_container, close_container

    try:
        if not open_container(client, chest, timeout=4.0):
            return False
        raw = client.transport.dispatch("get_screen", {})
        if not isinstance(raw, dict):
            return False
        data = raw.get("data", raw) if isinstance(raw, dict) else {}
        if not isinstance(data, dict):
            return False
        slots, total = data.get("slots"), data.get("total_slots")
        if (raw.get("error") or raw.get("status") == "error" or data.get("error")
                or raw.get("success") is False or data.get("success") is False
                or data.get("status") == "error" or type(data.get("sync_id")) is not int
                or data["sync_id"] <= 0 or type(total) is not int or total not in (63, 90) or not isinstance(slots, list)
                or len(slots) != total or any(not isinstance(s, dict) or type(s.get("slot")) is not int for s in slots)
                or {s["slot"] for s in slots} != set(range(total))):
            return False
        return all(isinstance(s.get("id"), str) and s["id"] != "minecraft:air"
                   and type(s.get("count")) is int and s["count"] > 0
                   for s in slots if s["slot"] < total - 36)
    finally:
        close_container(client)


@protect_home_route(surface_work=True, safe_movement=True)
def _bank(client, state, x, y, z, required, items, retains):
    from ..core.exceptions import CommandError
    from ..common.harness_ops import place_block_exact
    from ..common.inventory import count_item, craft, deposit_excess_to_chest, get_inventory, drop_items
    from ..common.resources import LOG_TO_PLANKS, PLANK_ITEMS
    from ..common.storage_catalog import catalog_for
    from ..common.tunnel_miner import free_slots, DIGGING_JUNK
    from .charcoal_wood import gather_charcoal_logs

    anchor = (x, y, z)
    record = state.custom_data.setdefault("mining_storage", {})
    may_expand = True
    for chest in _local_chests(client, state, anchor, record.get("chest")):
        if not _at_home(client, anchor):
            return False
        try:
            deposited = deposit_excess_to_chest(client, chest, deposit_items=items, retain_counts=retains, state=state)
        except CommandError as exc:
            print(f"IRON PREPARATION: local chest interaction rejected ({exc})")
            may_expand = False
            continue
        if free_slots(client) >= required:
            return True
        if not _at_home(client, anchor):
            return False
        # Reuse another observed chest after an inaccessible target, but an
        # uncertain interaction must never justify building more capacity.
        if deposited < 0 or not _full(client, chest):
            may_expand = False
    if not may_expand:
        return False
    chest = None  # Retain the old checkpoint landmark until new placement is verified.
    if chest is None:
        chest = _spot(client, state, anchor)
        if chest is None:
            return False
        if count_item(client, "minecraft:chest") < 1:
            if sum(count_item(client, p) for p in PLANK_ITEMS) < 8:
                if free_slots(client) < 1:
                    drop_items(client, sorted(DIGGING_JUNK), max_stacks=2,
                               retain_counts={"minecraft:cobblestone": 64})
                    if free_slots(client) < 1 or not _at_home(client, anchor):
                        return False
                if sum(count_item(client, log) for log in LOG_TO_PLANKS) < 2:
                    gather_charcoal_logs(client, state, 2, anchor)
                if not _at_home(client, anchor):
                    return False
                for log, plank in LOG_TO_PLANKS.items():
                    missing = 8 - sum(count_item(client, p) for p in PLANK_ITEMS)
                    if missing > 0 and count_item(client, log) > 0:
                        craft(client, plank, min(missing, count_item(client, log) * 4))
            if sum(count_item(client, p) for p in PLANK_ITEMS) < 8:
                return False
            craft(client, "minecraft:chest", 1)
        if count_item(client, "minecraft:chest") < 1:
            return False
        if not _at_home(client, anchor):
            from ..common.food_workstation import _fresh_safe_position
            from ..common.food_return import _return
            from ..common.harness_ops import close_container

            # Crafting can approach a workstation on another house level.
            # Return before placement, then require the original home margins.
            close_container(client)
            position = _fresh_safe_position(client, require_grounded=True)
            if (position is None or math.dist(position, anchor) > 32
                    or abs(position[1] - anchor[1]) > 8):
                return False
            _return(client, *anchor, state)
            if not _at_home(client, anchor):
                return False
        # Recheck the target after gathering/crafting; never clear an obstruction.
        if _spot(client, state, anchor) != chest:
            return False
        place_block_exact(client, *chest, "minecraft:chest", allow_break=False)
        if _block(client, chest) != "minecraft:chest":
            return False
        record["chest"] = list(chest)
        catalog_for(client, state).register_container(chest, dimension="minecraft:overworld", purpose="mining_overflow")
        writer = getattr(state, "save_checkpoint", None)
        if callable(writer):
            writer(get_inventory(client))
    if not _at_home(client, anchor):
        return False
    deposit_excess_to_chest(client, chest, deposit_items=items, retain_counts=retains, state=state)
    return free_slots(client) >= required


def bank_mining_overflow(client, state, anchor, required, items, retains):
    """Reuse capacity or add one verified home chest, then prove free slots."""
    from ..common.tasks import PlayerDeathDetected, SurvivalRecoveryRequired

    bind_home_surface(client, state)
    try:
        if not _at_home(client, anchor):
            from ..common.food_workstation import _fresh_safe_position
            from ..common.food_return import _return

            position = _fresh_safe_position(client, require_grounded=True)
            if (position is None or math.dist(position, anchor) > 32
                    or abs(position[1] - anchor[1]) > 2):
                return False
            _return(client, *anchor, state)
            if not _at_home(client, anchor):
                return False
        return _bank(client, state, *anchor, required, items, retains)
    except (PlayerDeathDetected, SurvivalRecoveryRequired):
        raise
    except Exception as exc:
        print(f"IRON PREPARATION: local overflow deferred ({exc})")
        return False
