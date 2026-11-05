"""Bounded recovery from underground positions with unreachable surface goals."""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from .automation_utils import HAND_BREAKABLE_BLOCKS
from .movement_recovery import block_position
from .surface_recovery_helpers import (
    _loaded_breathing_level_above,
    _loaded_two_block_air_level_above,
    _state_has_breathing_air,
    wait_for_dry_level as _wait_for_dry_level,
)


_SURFACE_BLOCKS = [
    "minecraft:grass_block",
    "minecraft:dirt",
    "minecraft:coarse_dirt",
    "minecraft:podzol",
    "minecraft:sand",
    "minecraft:gravel",
    "minecraft:stone",
]

_NON_BREATHABLE_BLOCK_TOKENS = (
    "water",
    "lava",
    "bubble_column",
    "kelp",
    "seagrass",
)
_NON_SUPPORT_BLOCKS = {
    "air",
    "cave_air",
    "void_air",
    "water",
    "lava",
    "bubble_column",
    "kelp",
    "kelp_plant",
    "seagrass",
    "tall_seagrass",
}
_OPEN_PLAYER_BLOCKS = {"air", "cave_air", "void_air"}
_UNBREAKABLE_EGRESS_BLOCKS = {"bedrock", "barrier", "end_portal_frame"}

_AQUATIC_WALKWAY_MATERIALS = (
    "minecraft:dirt",
    "minecraft:cobblestone",
    "minecraft:cobbled_deepslate",
    "minecraft:oak_planks",
    "minecraft:spruce_planks",
    "minecraft:birch_planks",
    "minecraft:jungle_planks",
    "minecraft:acacia_planks",
    "minecraft:dark_oak_planks",
    "minecraft:mangrove_planks",
    "minecraft:cherry_planks",
    "minecraft:oak_log",
    "minecraft:spruce_log",
    "minecraft:birch_log",
    "minecraft:jungle_log",
    "minecraft:acacia_log",
    "minecraft:dark_oak_log",
    "minecraft:mangrove_log",
    "minecraft:cherry_log",
)


def _configure_surface_pathing(
    client: Any,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Apply upward and water-surface constraints before starting movement."""
    from .water_hazards import allow_water

    # Surfacing has to swim; a starving search may have banned water routes.
    allow_water(client, sleep=sleep)
    for command in (
        "#set allowBreak true",
        "#set allowPlace true",
        "#set allowDownward false",
        # This is a Baritone pathfinding assumption, not a movement ability.
        # Enabling it makes routes treat water as solid while Minecraft still
        # lets the player sink, which can strand the bot on a riverbed.
        "#set assumeWalkOnWater false",
    ):
        client.transport.dispatch("chat", {"message": command})
        # Chat settings apply on Minecraft ticks. Starting a path in the same
        # instant can retain the previous unsafe value for its first plan.
        sleep(0.1)


def _block_is_breathable(block_id: object) -> bool:
    """Return whether a player's head can breathe in the reported block."""
    value = str(block_id)
    return not any(token in value for token in _NON_BREATHABLE_BLOCK_TOKENS)


def position_is_aquatic(
    client: Any,
    position: tuple[int, int, int],
) -> bool:
    """Return whether the player's feet occupy a liquid or aquatic block."""
    try:
        block = client.transport.dispatch(
            "get_block",
            {"x": position[0], "y": position[1], "z": position[2]},
        ).get("id", "")
    except Exception:
        return False
    return not _block_is_breathable(block)


def _has_stable_support(
    client: Any,
    position: tuple[int, int, int],
) -> bool:
    """Require a readable non-liquid block directly below the player."""
    try:
        block = client.transport.dispatch(
            "get_block",
            {"x": position[0], "y": position[1] - 1, "z": position[2]},
        ).get("id", "")
    except Exception:
        return False
    value = str(block).split(":")[-1]
    return bool(value) and value not in _NON_SUPPORT_BLOCKS


def _clear_reachable_ascent_obstructions(
    client: Any,
    position: tuple[int, int, int],
    *,
    target_y: int,
    deadline: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Clear solid blocks in the reachable part of a loaded vertical escape."""
    from .inventory import select_item

    _, origin_y, _ = position
    breakable = []
    for block_y in range(origin_y + 1, min(int(target_y), origin_y + 5)):
        block = (position[0], block_y, position[2])
        name = _block_name(client, block)
        if not name:
            return False
        if name in _OPEN_PLAYER_BLOCKS or name in _NON_SUPPORT_BLOCKS:
            continue
        if name in _UNBREAKABLE_EGRESS_BLOCKS:
            return False
        breakable.append((block, name))
    if not breakable:
        return True
    if not any(
        select_item(client, pickaxe, allow_swap=True)
        for pickaxe in (
            "minecraft:netherite_pickaxe",
            "minecraft:diamond_pickaxe",
            "minecraft:iron_pickaxe",
            "minecraft:stone_pickaxe",
            "minecraft:wooden_pickaxe",
        )
    ) and not all(name in HAND_BREAKABLE_BLOCKS for _b, name in breakable):
        return False
    for block, _name in reversed(breakable):
        if deadline is not None and clock() >= deadline:
            return False
        try:
            client.transport.dispatch(
                "break_block",
                {"x": block[0], "y": block[1], "z": block[2]},
            )
        except Exception:
            return False
        wait_timeout = 5.0
        if deadline is not None:
            wait_timeout = max(0.0, min(wait_timeout, deadline - clock()))
        if not _wait_for_open_block(client, block, timeout=wait_timeout):
            return False
    return True


def _start_loaded_column_ascent(
    client: Any,
    position: tuple[int, int, int],
    *,
    require_stable_support: bool = False,
    deadline: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Prefer an upward-only Y goal when the water surface is already loaded."""
    target_y = (
        _loaded_two_block_air_level_above(
            client, position, deadline=deadline, clock=clock
        )
        if require_stable_support
        else _loaded_breathing_level_above(
            client, position, deadline=deadline, clock=clock
        )
    )
    if target_y is None or target_y <= position[1]:
        return False
    # Drowning takes this path with require_stable_support False, which used to
    # skip this entirely and leave the ceiling above a submerged bot unbroken.
    cleared = _clear_reachable_ascent_obstructions(
        client,
        position,
        target_y=target_y,
        deadline=deadline,
        clock=clock,
    )
    if require_stable_support and not cleared:
        return False
    return _start_y_level_ascent(client, target_y)


def _start_y_level_ascent(client: Any, target_y: int) -> bool:
    """Replace the active route with an upward Baritone Y-level goal."""
    try:
        client.transport.dispatch("cancel", {})
        client.transport.dispatch(
            "goal",
            {"type": "yLevel", "value": target_y},
        )
    except Exception:
        return False
    print(f"SURVIVAL: starting upward Y-level ascent to y={target_y}")
    return True


def _aquatic_walkway_plan(
    client: Any,
    origin: tuple[int, int, int],
    *,
    length: int,
) -> Optional[tuple[tuple[int, int], list[tuple[int, int, int]]]]:
    """Plan a short bottom-up walkway through water up to two blocks deep."""
    ox, oy, oz = origin
    for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        viable = True
        placements = []
        for step in range(1, length + 1):
            x, z = ox + dx * step, oz + dz * step
            try:
                head = client.transport.dispatch(
                    "get_block",
                    {"x": x, "y": oy + 1, "z": z},
                ).get("id", "")
            except Exception:
                viable = False
                break
            if not _block_is_breathable(head):
                viable = False
                break
            floor_y = None
            for candidate_y in (oy - 1, oy - 2):
                try:
                    candidate = client.transport.dispatch(
                        "get_block",
                        {"x": x, "y": candidate_y, "z": z},
                    ).get("id", "")
                except Exception:
                    candidate = ""
                candidate_value = str(candidate)
                unsupported = not candidate_value or any(
                    token in candidate_value
                    for token in (
                        "air",
                        "water",
                        "lava",
                        "seagrass",
                        "kelp",
                        "vine",
                    )
                )
                if not unsupported:
                    floor_y = candidate_y
                    break
            if floor_y is None:
                viable = False
                break
            for fill_y in range(floor_y + 1, oy + 1):
                try:
                    block = client.transport.dispatch(
                        "get_block",
                        {"x": x, "y": fill_y, "z": z},
                    ).get("id", "")
                except Exception:
                    block = ""
                if _block_is_breathable(block):
                    viable = False
                    break
                placements.append((x, fill_y, z))
            if not viable:
                break
        if viable:
            return ((dx, dz), placements)
    return None


def _walkway_materials(
    inventory: dict[str, int],
    *,
    required: int,
) -> Optional[list[str]]:
    """Allocate a bounded walkway from mixed carried solid blocks."""
    allocated = []
    for item_id in _AQUATIC_WALKWAY_MATERIALS:
        count = int(inventory.get(item_id, 0) or 0)
        allocated.extend([item_id] * min(count, required - len(allocated)))
        if len(allocated) >= required:
            return allocated
    return None


def _place_support_below_breathing_position(
    client: Any,
    position: tuple[int, int, int],
) -> bool:
    """Install one carried solid block beneath an unsupported air pocket."""
    from . import harness_ops
    from .inventory import get_inventory, select_item

    if _has_stable_support(client, position):
        return True
    try:
        materials = _walkway_materials(get_inventory(client), required=1)
    except Exception:
        # Inventory telemetry is optional for this emergency path.  An
        # unreadable inventory means support cannot be claimed, not that the
        # recovery should crash before its bounded fallback can run.
        return False
    if not materials or not harness_ops.available():
        return False
    material = materials[0]
    if not select_item(client, material, allow_swap=True):
        return False
    x, y, z = position
    if not harness_ops.place_block_exact(
        client,
        x,
        y - 1,
        z,
        material,
        allow_break=False,
    ):
        return False
    return _has_stable_support(client, position)


def _block_name(client: Any, position: tuple[int, int, int]) -> str:
    try:
        response = client.transport.dispatch(
            "get_block",
            {"x": position[0], "y": position[1], "z": position[2]},
        )
    except Exception:
        return ""
    value = response.get("id", response.get("block")) if response else None
    return str(value).split(":")[-1] if value else ""


def _wait_for_open_block(
    client: Any,
    position: tuple[int, int, int],
    *,
    timeout: float = 5.0,
) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if _block_name(client, position) in _OPEN_PLAYER_BLOCKS:
            return True
        time.sleep(0.2)
    return False


def _excavate_supported_breathing_ledge(
    client: Any,
    position: tuple[int, int, int],
) -> Optional[tuple[int, int, int]]:
    """Open one adjacent two-high cell whose floor is already solid."""
    from .inventory import select_item
    from .navigation import goto

    if not any(
        select_item(client, pickaxe, allow_swap=True)
        for pickaxe in (
            "minecraft:netherite_pickaxe",
            "minecraft:diamond_pickaxe",
            "minecraft:iron_pickaxe",
            "minecraft:stone_pickaxe",
            "minecraft:wooden_pickaxe",
        )
    ):
        return None
    px, py, pz = position
    for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        target = (px + dx, py, pz + dz)
        feet_name = _block_name(client, target)
        head_name = _block_name(client, (target[0], target[1] + 1, target[2]))
        if (
            not feet_name
            or not head_name
            or feet_name in _UNBREAKABLE_EGRESS_BLOCKS
            or head_name in _UNBREAKABLE_EGRESS_BLOCKS
            or not _has_stable_support(client, target)
        ):
            continue
        opened = True
        for block in (
            (target[0], target[1] + 1, target[2]),
            target,
        ):
            if _block_name(client, block) not in _OPEN_PLAYER_BLOCKS:
                try:
                    client.transport.dispatch(
                        "break_block",
                        {"x": block[0], "y": block[1], "z": block[2]},
                    )
                except Exception:
                    opened = False
                    break
                if not _wait_for_open_block(client, block):
                    opened = False
                    break
        if not opened:
            continue
        if not goto(client, *target, timeout=15.0, tolerance=1.0):
            continue
        current = block_position(client.transport.dispatch("get_state", {}))
        if (
            _has_stable_support(client, current)
            and not position_is_aquatic(client, current)
            and _head_is_dry(client, current)
        ):
            return current
    return None


def _build_aquatic_walkway(
    client: Any,
    origin: tuple[int, int, int],
    *,
    goto: Callable[..., bool],
    length: int = 4,
) -> Optional[tuple[int, int, int]]:
    """Build a short supported path out of shallow water and walk onto it."""
    from .inventory import get_inventory, select_item

    inventory = get_inventory(client)
    plan = _aquatic_walkway_plan(client, origin, length=length)
    if plan is None:
        return None
    direction, placements = plan
    materials = _walkway_materials(inventory, required=len(placements))
    if materials is None:
        return None

    ox, oy, oz = origin
    dx, dz = direction
    client.transport.dispatch("cancel", {})
    for target, material in zip(placements, materials):
        if not select_item(client, material, allow_swap=True):
            return None
        try:
            client.transport.dispatch(
                "place_block",
                {"x": target[0], "y": target[1], "z": target[2]},
            )
            time.sleep(0.2)
            placed = client.transport.dispatch(
                "get_block",
                {"x": target[0], "y": target[1], "z": target[2]},
            ).get("id", "")
        except Exception:
            return None
        if placed != material:
            return None

    target = (ox + dx * length, oy + 1, oz + dz * length)
    print(f"SURVIVAL: built shallow-water escape walkway toward {target}")
    if not goto(client, *target, timeout=30.0):
        return None
    current = block_position(client.transport.dispatch("get_state", {}))
    moved_sq = (current[0] - ox) ** 2 + (current[2] - oz) ** 2
    if moved_sq < 2**2 or position_is_aquatic(client, current):
        return None
    return current


def _loaded_dry_shore_candidates(
    client: Any,
    origin: tuple[int, int, int],
    *,
    search_radius: int = 64,
) -> list[tuple[int, int, int]]:
    """Return nearby loaded ground columns with verified dry standing space."""
    ox, oy, oz = origin
    try:
        response = client.transport.dispatch(
            "find_blocks",
            {
                "blocks": _SURFACE_BLOCKS,
                "radius": int(search_radius),
                "limit": 4096,
            },
        )
    except Exception:
        return []
    highest_by_column = {}
    for block in response.get("found", []):
        x, y, z = int(block["x"]), int(block["y"]) + 1, int(block["z"])
        highest_by_column[(x, z)] = max(
            y,
            highest_by_column.get((x, z), -64),
        )
    candidates = []
    for (x, z), y in highest_by_column.items():
        distance_sq = (x - ox) ** 2 + (z - oz) ** 2
        if (
            distance_sq < 6**2
            or distance_sq > search_radius**2
            or y < oy - 2
        ):
            continue
        position = (x, y, z)
        if position_is_aquatic(client, position) or not _head_is_dry(
            client,
            position,
        ):
            continue
        candidates.append((distance_sq, position))
    return [
        position
        for _distance, position in sorted(candidates)
    ]


def _swim_to_loaded_dry_shore(
    client: Any,
    origin: tuple[int, int, int],
    *,
    timeout: float = 30.0,
    deadline: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Optional[tuple[int, int, int]]:
    """Use one bounded route from surface water to verified loaded land."""
    candidates = _loaded_dry_shore_candidates(client, origin)[:2]
    if not candidates:
        return None
    deadline = (
        float(deadline)
        if deadline is not None
        else clock() + max(0.0, timeout)
    )
    for target in candidates:
        if clock() >= deadline:
            break
        client.transport.dispatch("cancel", {})
        client.transport.dispatch(
            "goto",
            {"x": target[0], "y": target[1], "z": target[2]},
        )
        checks = 0
        while clock() < deadline:
            state = client.transport.dispatch("get_state", {})
            current = block_position(state)
            if (
                not position_is_aquatic(client, current)
                and _head_is_dry(client, current)
                and _has_stable_support(client, current)
                and _state_has_breathing_air(state)
            ):
                client.transport.dispatch("cancel", {})
                print(f"SURVIVAL: reached loaded dry shore at {current}")
                return current
            if current[1] < origin[1] - 2:
                break
            checks += 1
            if checks >= 2 and not state.get("is_pathing", True):
                break
            sleep(min(0.5, max(0.0, deadline - clock())))
        client.transport.dispatch("cancel", {})
    return None


def _head_is_dry(client: Any, position: tuple[int, int, int]) -> bool:
    try:
        block = client.transport.dispatch(
            "get_block",
            {"x": position[0], "y": position[1] + 1, "z": position[2]},
        ).get("id", "")
    except Exception:
        return False
    return _block_is_breathable(block)


def reach_breathing_air(
    client: Any,
    *,
    timeout: float,
    ensure_alive: Callable[[Any, Optional[dict]], None],
    require_stable_support: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Reach breathing air, optionally continuing until footing is stable."""
    started_at = clock()
    deadline = started_at + max(0.0, float(timeout))
    initial = block_position(client.transport.dispatch("get_state", {}))
    _configure_surface_pathing(client, sleep=sleep)
    shore = _swim_to_loaded_dry_shore(
        client,
        initial,
        timeout=max(0.0, float(timeout)),
        deadline=deadline,
        clock=clock,
        sleep=sleep,
    )
    if shore is not None:
        return True
    loaded_ascent = _start_loaded_column_ascent(
        client,
        initial,
        require_stable_support=require_stable_support,
        deadline=deadline,
        clock=clock,
    )
    if not loaded_ascent:
        client.transport.dispatch("chat", {"message": "#surface"})
    highest_y = initial[1]
    if require_stable_support:
        # Loaded flooded shafts need several seconds to swim a vertical
        # column before the supported-air-pocket logic can run. The emergency
        # breathing path keeps its fast fallback; stable recovery can spend a
        # bounded larger slice of its own timeout on the upward goal.
        progress_window = min(12.0, max(4.0, timeout * 0.35))
    else:
        progress_window = min(3.0, max(1.0, timeout * 0.4))
    progress_deadline = min(deadline, started_at + progress_window)
    try:
        while True:
            now = clock()
            if now >= deadline:
                return False
            state = client.transport.dispatch("get_state", {})
            ensure_alive(client, state)
            current = block_position(state)
            if _head_is_dry(client, current):
                aquatic = position_is_aquatic(client, current)
                if aquatic:
                    shore = _swim_to_loaded_dry_shore(
                        client,
                        current,
                        timeout=max(0.0, float(timeout)),
                        deadline=deadline,
                        clock=clock,
                        sleep=sleep,
                    )
                    if shore is not None:
                        return True
                if _state_has_breathing_air(state) and (
                    not require_stable_support
                    or (
                    not aquatic and _has_stable_support(client, current)
                    )
                ):
                    return True
                if require_stable_support and not aquatic:
                    ledge = _excavate_supported_breathing_ledge(client, current)
                    if ledge is not None:
                        print(
                            "SURVIVAL: excavated supported breathing ledge at "
                            f"{ledge}"
                        )
                        return True
                    if _place_support_below_breathing_position(client, current):
                        print(
                            "SURVIVAL: stabilized breathing air with carried "
                            f"support at {current}"
                        )
                        return True
            if current[1] < initial[1] - 2:
                print("SURVIVAL: surface route moved downward; aborting it")
                return False
            if current[1] > highest_y:
                highest_y = current[1]
                # Vertical progress earns another bounded progress window,
                # while the overall drowning timeout remains unchanged.
                progress_deadline = min(deadline, now + progress_window)
            if loaded_ascent and now >= progress_deadline:
                print(
                    "SURVIVAL: loaded-column ascent made no vertical "
                    "progress; falling back to #surface"
                )
                client.transport.dispatch("chat", {"message": "#stop"})
                client.transport.dispatch("cancel", {})
                client.transport.dispatch("chat", {"message": "#surface"})
                loaded_ascent = False
            sleep(0.5)
    finally:
        client.transport.dispatch("chat", {"message": "#stop"})
        client.transport.dispatch("cancel", {})


def reach_dry_surface(
    client: Any,
    *,
    origin: tuple[int, int, int],
    expected_y: int,
    goto: Callable[..., bool],
    search_radius: int = 48,
    attempt_limit: int = 8,
    command_timeout: float = 90.0,
) -> Optional[tuple[int, int, int]]:
    """Try broader surface columns, then a verified upward-only surface command."""
    ox, oy, oz = origin
    _configure_surface_pathing(client)
    aquatic_origin = position_is_aquatic(client, origin)
    if (
        expected_y > oy
        and aquatic_origin
        and _start_y_level_ascent(client, expected_y)
    ):
        recovered = _wait_for_dry_level(
            client,
            origin_y=oy,
            expected_y=expected_y,
            timeout=min(12.0, command_timeout),
        )
        if recovered is not None:
            return recovered
        walkway_exit = _build_aquatic_walkway(
            client,
            origin,
            goto=goto,
        )
        if walkway_exit is not None:
            return walkway_exit
        shore_exit = _swim_to_loaded_dry_shore(client, origin)
        if shore_exit is not None:
            return shore_exit
        # Coordinate routes cannot make progress from this same aquatic cell
        # if the upward goal itself never moved. Let callers proceed to their
        # bounded excavation fallback instead of spending minutes retrying it.
        return None
    try:
        response = client.transport.dispatch(
            "find_blocks",
            {
                "blocks": _SURFACE_BLOCKS,
                "radius": int(search_radius),
                "limit": 4096,
            },
        )
    except Exception:
        response = {}
    highest_by_column = {}
    for block in response.get("found", []):
        x, y, z = int(block["x"]), int(block["y"]) + 1, int(block["z"])
        highest_by_column[(x, z)] = max(y, highest_by_column.get((x, z), -64))
    candidates = []
    for (x, z), target_y in highest_by_column.items():
        distance_sq = (x - ox) ** 2 + (z - oz) ** 2
        if distance_sq < 8**2 or target_y < expected_y - 8:
            continue
        candidates.append((abs(target_y - expected_y), distance_sq, x, target_y, z))
    for _height_delta, _distance, x, target_y, z in sorted(candidates)[:attempt_limit]:
        if not goto(client, x, target_y, z, timeout=45.0):
            continue
        current = block_position(client.transport.dispatch("get_state", {}))
        if current[1] >= target_y - 3 and _head_is_dry(client, current):
            return current

    fallback_origin = block_position(
        client.transport.dispatch("get_state", {})
    )
    if not _start_loaded_column_ascent(client, fallback_origin):
        client.transport.dispatch("chat", {"message": "#surface"})
    return _wait_for_dry_level(
        client,
        origin_y=oy,
        expected_y=expected_y,
        timeout=command_timeout,
    )
