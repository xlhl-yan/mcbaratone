"""Bounded, observed opening of the saved starter-house doorway."""

import math
import time
from typing import Any, Mapping, Optional, Sequence


def _data(response):
    if not isinstance(response, Mapping):
        return None
    for envelope in (response, response.get("data")):
        if isinstance(envelope, Mapping) and (
            envelope.get("success") is False
            or str(envelope.get("status", "")).casefold() in {"error", "failed", "failure"}
            or envelope.get("error") not in (None, "", False)
        ):
            return None
    result = response.get("data", response)
    return result if isinstance(result, Mapping) else None


def _state(client):
    try:
        live = _data(client.transport.dispatch("get_state", {}))
    except Exception:
        return None
    if live is None:
        return None
    health = live.get("health")
    if (
        isinstance(health, bool)
        or not isinstance(health, (int, float))
        or not math.isfinite(float(health))
        or health < 12
        or live.get("is_dead") is not False
        or live.get("is_pathing") is not False
        or live.get("is_on_ground") is not True
        or str(live.get("dimension", "")).casefold() != "minecraft:overworld"
        or str(live.get("game_mode", "")).casefold() != "survival"
    ):
        return None
    position = live.get("block_position") or live.get("position")
    if not isinstance(position, Mapping):
        return None
    values = []
    for axis in ("x", "y", "z"):
        value = position.get(axis)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        if not math.isfinite(number):
            return None
        values.append(number)
    return live, tuple(values)


def _door_state(client, lower, upper):
    halves = []
    for pos, expected_half in ((lower, "lower"), (upper, "upper")):
        try:
            block = _data(client.transport.dispatch(
                "get_block", dict(zip(("x", "y", "z"), pos))
            ))
        except Exception:
            return None
        state = block.get("state") if block else None
        opened = state.get("open") if isinstance(state, Mapping) else None
        if (
            not isinstance(state, Mapping)
            or str(state.get("half", "")).casefold() != expected_half
            or str(state.get("facing", "")).casefold() != "south"
            or not isinstance(block.get("id"), str)
            or not block["id"].startswith("minecraft:")
            or not block["id"].endswith("_door")
            or not (type(opened) is bool or opened in ("true", "false"))
        ):
            return None
        halves.append((block["id"], opened in (True, "true")))
    if halves[0][0] != halves[1][0] or halves[0][1] != halves[1][1]:
        return None
    return halves[0][1]


def _position(client):
    try:
        live = _data(client.transport.dispatch("get_state", {}))
    except Exception:
        return None
    position = (live or {}).get("block_position") or (live or {}).get("position")
    if not isinstance(position, Mapping):
        return None
    values = []
    for axis in ("x", "y", "z"):
        value = position.get(axis)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        if not math.isfinite(number):
            return None
        values.append(number)
    return tuple(values)


def _in_house(position, origin):
    x, y, z = (math.floor(value) for value in position)
    ox, oy, oz = origin
    interior = oy + 1 == y and ox + 1 <= x <= ox + 5 and oz + 1 <= z <= oz + 5
    return interior or (x == ox + 3 and y == oy + 1 and z == oz)


def _inventory_snapshot(raw):
    from ..inventory_evidence import valid_inventory

    data = _data(raw)
    if data is None or not valid_inventory(raw) or not valid_inventory(data):
        return None
    entries = data.get("inventory")
    selected = data.get("selected_slot")
    if (
        not isinstance(entries, list)
        or any(not isinstance(entry, Mapping) for entry in entries)
        or isinstance(selected, bool)
        or not isinstance(selected, int)
        or not 0 <= selected <= 8
        or any(
            isinstance(entry.get("slot"), bool)
            or not isinstance(entry.get("slot"), int)
            or not 0 <= entry["slot"] <= 40
            for entry in entries
        )
        or len({entry["slot"] for entry in entries}) != len(entries)
    ):
        return None
    return data


def _safe_tool_slot(client):
    """Return (current, chosen) slots without swapping or consuming items."""
    try:
        raw = client.transport.dispatch("get_inventory", {})
    except Exception:
        return None
    data = _inventory_snapshot(raw)
    entries = data.get("inventory") if data else None
    current = data.get("selected_slot") if data else None
    if data is None or not 0 <= current <= 8:
        return None

    def safe(entry):
        item = entry.get("id", "")
        try:
            count = int(entry.get("count", 0))
            slot = int(entry.get("slot", -1))
        except (TypeError, ValueError):
            return False
        return (
            isinstance(item, str)
            and item.startswith("minecraft:")
            and item.endswith(("_sword", "_pickaxe", "_axe", "_shovel", "_hoe"))
            and count > 0
            and 0 <= slot <= 8
        )

    selected = next((entry for entry in entries if entry.get("slot") == current), None)
    if selected is not None and safe(selected):
        return current, current
    candidate = next((entry for entry in entries if safe(entry)), None)
    if candidate is None:
        return None
    chosen = int(candidate["slot"])
    try:
        client.transport.dispatch("select_slot", {"slot": chosen})
        verified = _inventory_snapshot(client.transport.dispatch("get_inventory", {}))
    except Exception:
        _restore_slot(client, current)
        return None
    if verified is None or verified.get("selected_slot") != chosen:
        _restore_slot(client, current)
        return None
    held = next((entry for entry in verified.get("inventory", []) if entry.get("slot") == chosen), None)
    if held is None or not safe(held):
        _restore_slot(client, current)
        return None
    return current, chosen


def _restore_slot(client, slot):
    try:
        client.transport.dispatch("select_slot", {"slot": slot})
        data = _inventory_snapshot(client.transport.dispatch("get_inventory", {}))
        return bool(data and data.get("selected_slot") == slot)
    except Exception:
        return False


def _safe_door_stand(client, position):
    """A floor torch is passable for travel; workstation placement stays strict."""
    from .farming import _block_data
    from .tunnel_planner import GRAVITY, is_support
    from .base import _ALL_PLANKS

    x, y, z = position
    try:
        feet = _block_data(client, x, y, z).get("id")
        head = _block_data(client, x, y + 1, z).get("id")
        floor = _block_data(client, x, y - 1, z).get("id")
        return (feet in {"minecraft:air", "minecraft:torch", "minecraft:wall_torch"}
                and head == "minecraft:air" and floor not in GRAVITY
                and (floor in _ALL_PLANKS or is_support(floor)))
    except Exception:
        return False


def _open_from_side(client, origin, side):
    from .food_workstation import _door_interaction_safe

    ox, oy, oz = origin
    lower, upper = (ox + 3, oy + 1, oz), (ox + 3, oy + 2, oz)
    opened = _door_state(client, lower, upper)
    if opened is True:
        return True
    if opened is not False:
        return False
    stand = (ox + 3, oy + 1, oz + (1 if side == "inside" else -1))
    from .navigation import goto

    if not _safe_door_stand(client, stand):
        return False
    fresh = _state(client)
    if fresh is None:
        return False
    if tuple(math.floor(value) for value in fresh[1]) != stand:
        try:
            if not goto(client, *stand, timeout=20, tolerance=0.5, radius=0):
                return False
        except Exception:
            return False
    fresh = _state(client)
    if (
        fresh is None
        or tuple(math.floor(value) for value in fresh[1]) != stand
        or math.floor(fresh[1][1]) != oy + 1
        or _door_state(client, lower, upper) is not False
    ):
        return False
    live, _ = fresh
    try:
        from .survival_farm import _hostile_close

        if _hostile_close(client, live):
            return False
    except Exception:
        return False
    if not _door_interaction_safe(client, lambda _client: True):
        return False

    slots = _safe_tool_slot(client)
    if slots is None:
        return False
    previous, chosen = slots
    try:
        # Aim at the known north-facing panel plane, not the unreachable block
        # center. A single pulse is followed by observed state; never retoggle.
        client.transport.dispatch(
            "look_at", {"x": ox + 3.5, "y": oy + 2.5, "z": oz + 0.05}
        )
        time.sleep(0.2)
        fresh = _state(client)
        if (
            fresh is None
            or tuple(math.floor(value) for value in fresh[1]) != stand
            or _door_state(client, lower, upper) is not False
            or not _door_interaction_safe(client, lambda _client: True)
        ):
            return False
        client.transport.dispatch("use_item", {"duration_ms": 0})
    except Exception:
        pass
    finally:
        restored = chosen == previous or _restore_slot(client, previous)

    if not restored:
        return False
    # Reconcile even an uncertain dispatch, but do not send another toggle.
    for _ in range(8):
        observed = _door_state(client, lower, upper)
        if observed is True:
            return True
        if observed is None:
            return False
        time.sleep(0.1)
    return False


def prepare_house_door_for_departure(client: Any, state: Any, anchor: Optional[Sequence[int]]) -> bool:
    """Open only the saved-house door when the bot is inside and leaving."""
    from .food_workstation import _starter_house_origin

    if anchor is None:
        return True
    origin = _starter_house_origin(state, anchor)
    if origin is None:
        return True
    position = _position(client)
    if position is None:
        return False
    if not _in_house(position, origin):
        return True
    fresh = _state(client)
    if fresh is None:
        return False
    return _open_from_side(client, origin, "inside")


def prepare_house_door_for_entry(client: Any, state: Any, anchor: Sequence[int]) -> bool:
    """Open the saved-house door from its known exterior before interior travel."""
    from .food_workstation import _near_home, _starter_house_origin

    origin = _starter_house_origin(state, anchor)
    if origin is None:
        return False
    fresh = _state(client)
    if fresh is None or not _near_home(fresh[1], tuple(float(value) for value in anchor)):
        return False
    if _in_house(fresh[1], origin):
        return True
    return _open_from_side(client, origin, "outside")


def saved_house_arrival(position, state, anchor):
    """Nearby underground cells are not a return to the recorded house."""
    from .food_workstation import _starter_house_origin

    origin = _starter_house_origin(state, anchor)
    return (position is not None and origin is not None
            and _in_house(position, origin) and math.dist(position, anchor) <= 4)


def return_to_saved_house(client, state, anchor):
    """Enter through the observed doorway, verifying each exact surface stage."""
    from .food_workstation import _starter_house_origin
    from .navigation import goto
    from .tasks import PlayerDeathDetected, SurvivalRecoveryRequired

    try:
        if (len(anchor) != 3 or any(isinstance(v, bool) or not isinstance(v, (int, float))
                                  or not math.isfinite(v) for v in anchor)):
            return False
        origin = _starter_house_origin(state, anchor)
        if origin is None:
            return False
        ox, oy, oz = origin
        target = (math.floor(anchor[0]), oy + 1, math.floor(anchor[2]))
        if not _in_house(target, origin) or not _safe_door_stand(client, target):
            return False
        fresh = _state(client)
        if fresh is None:
            return False

        def travel(cell, timeout):
            goto(client, *cell, timeout=timeout, tolerance=0.5, radius=0)
            observed = _state(client)
            return (observed is not None
                    and tuple(math.floor(v) for v in observed[1]) == cell)

        if not _in_house(fresh[1], origin):
            exterior = (ox + 3, oy + 1, oz - 1)
            if not _safe_door_stand(client, exterior) or not travel(exterior, 150):
                return False
            if not _open_from_side(client, origin, "outside"):
                return False
            if not travel((ox + 3, oy + 1, oz), 20):
                return False
            if _door_state(client, (ox + 3, oy + 1, oz), (ox + 3, oy + 2, oz)) is not True:
                return False
        if not _safe_door_stand(client, target) or not travel(target, 20):
            return False
        arrived = _state(client)
        return arrived is not None and saved_house_arrival(arrived[1], state, anchor)
    except (PlayerDeathDetected, SurvivalRecoveryRequired):
        raise
    except Exception:
        return False
