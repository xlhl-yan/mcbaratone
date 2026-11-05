"""Fail-closed defense exception for one verified tunnel-corridor hop."""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, Optional, Tuple

from .tunnel_planner import is_support

Cell = Tuple[int, int, int]
_OPEN = frozenset({"minecraft:air", "minecraft:cave_air", "minecraft:torch"})
_CREEPER_CLEARANCE = 6.0
_CORRIDOR_TOLERANCE = 1.0


def _finite_vector(value: Any) -> Optional[Tuple[float, float, float]]:
    if not isinstance(value, dict):
        return None
    components = tuple(value.get(axis) for axis in ("x", "y", "z"))
    if any(
        isinstance(component, bool) or not isinstance(component, (int, float))
        for component in components
    ):
        return None
    vector = tuple(float(component) for component in components)
    if not all(math.isfinite(component) for component in vector):
        return None
    return vector


def _numeric(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _valid_cell(cell: Any) -> bool:
    return (
        isinstance(cell, (tuple, list))
        and len(cell) == 3
        and all(isinstance(value, int) and not isinstance(value, bool) for value in cell)
    )


def _valid_step(origin: Cell, target: Cell) -> bool:
    if not _valid_cell(origin) or not _valid_cell(target):
        return False
    dx, dy, dz = (target[i] - origin[i] for i in range(3))
    return abs(dx) + abs(dz) == 1 and abs(dy) <= 1


def _supported_open_cell(cell: Cell, get_block: Callable[[Cell], str]) -> bool:
    x, y, z = cell
    floor = get_block((x, y - 1, z))
    feet = get_block(cell)
    head = get_block((x, y + 1, z))
    return is_support(floor) and feet in _OPEN and head in _OPEN


#: Preflight's reason when the next saved step lost its floor or headroom.
TARGET_UNSUPPORTED = "tunnel corridor target is no longer supported and open"
_UNREAD = frozenset({"", "minecraft:void_air"})


def broken_step(cell: Cell, get_block: Callable[[Cell], str]) -> bool:
    """A saved step that real blocks prove broken; an unread one is not."""
    x, y, z = cell
    try:
        floor, feet, head = (get_block((x, y + dy, z)) for dy in (-1, 0, 1))
    except Exception:
        return False
    if _UNREAD & {floor, feet, head}:
        return False
    return not (is_support(floor) and feet in _OPEN and head in _OPEN)


def intact_route_start(spine, get_block: Callable[[Cell], str], *, max_skip: int = 16, run: int = 3) -> int:
    """Where a saved route can be re-entered past a lost prefix; 0 keeps it.

    Live A1 2026-10-16..20: a pit near the entrance took the floor from the
    first four steps of a 283-step tunnel. All 63 trips refused that first
    unsupported step and mined no iron, while the tunnel from step 5 down
    was intact. Only a short prefix is skipped, and only onto a run of
    supported, open steps; anything else keeps the route and its abort.
    """
    cells = [tuple(cell) for cell in spine]
    try:
        broken = next((index for index, cell in enumerate(cells[: max_skip + run])
                       if not _supported_open_cell(cell, get_block)), None)
        if broken is None:
            return 0
        for start in range(broken + 1, min(max_skip, len(cells) - run) + 1):
            if all(_supported_open_cell(cell, get_block) for cell in cells[start:start + run]):
                return start
    except Exception:
        return 0  # unknown terrain is not evidence the route moved
    return 0


def _distance_to_segment(point, start, end) -> float:
    direction = tuple(end[i] - start[i] for i in range(3))
    length_squared = sum(component * component for component in direction)
    if length_squared == 0:
        return math.dist(point, start)
    fraction = sum((point[i] - start[i]) * direction[i] for i in range(3)) / length_squared
    fraction = min(1.0, max(0.0, fraction))
    projection = tuple(start[i] + fraction * direction[i] for i in range(3))
    return math.dist(point, projection)


def _valid_player_corridor(player: Dict[str, Any], origin: Cell, target: Cell, beyond: Optional[Cell] = None) -> bool:
    """Prove the player is on this hop, or overshot onto the route's next step.

    Live A1 2026-10-21: every downward hop of a 1-wide staircase carried the
    bot one step past its target before it settled. That put it a block
    lower, about 1.4 blocks off the hop and up to 2 blocks into a fall, so
    each snapshot was rejected and the trip retreated with 0 iron.
    """
    if not isinstance(player, dict):
        return False
    cells = [tuple(origin), tuple(target)]
    if beyond is not None and _valid_step(target, beyond):
        cells.append(tuple(beyond))
    overshoot_drop = max(0, cells[1][1] - cells[-1][1])
    position = _finite_vector(player.get("position"))
    block_position = _finite_vector(player.get("block_position"))
    velocity = _finite_vector(player.get("velocity"))
    health = _numeric(player.get("health"))
    fall_distance = _numeric(player.get("fall_distance"))
    if (
        position is None
        or block_position is None
        or velocity is None
        or health is None
        or health <= 0
        or fall_distance is None
        or not 0 <= fall_distance <= 1.0 + overshoot_drop
        or player.get("is_dead") is not False
        or not isinstance(player.get("is_on_ground"), bool)
        or player.get("dimension") != "minecraft:overworld"
    ):
        return False
    if any(math.floor(position[i]) != block_position[i] for i in range(3)):
        return False
    horizontal_speed = math.hypot(velocity[0], velocity[2])
    if horizontal_speed > 0.65 or not -0.7 <= velocity[1] <= 0.6:
        return False

    points = [(cell[0] + 0.5, float(cell[1]), cell[2] + 0.5) for cell in cells]
    if min(_distance_to_segment(position, points[i], points[i + 1])
           for i in range(len(points) - 1)) > _CORRIDOR_TOLERANCE:
        return False
    low_y, high_y = min(cell[1] for cell in cells), max(cell[1] for cell in cells)
    return low_y - 0.25 <= position[1] <= high_y + 1.25


def _valid_snapshot(snapshot: Any, origin: Cell, target: Cell, beyond: Optional[Cell] = None) -> bool:
    if not isinstance(snapshot, dict):
        return False
    tick = snapshot.get("tick")
    count = snapshot.get("count")
    skipped = snapshot.get("skipped_count")
    entities = snapshot.get("entities")
    if (
        isinstance(snapshot.get("snapshot_version"), bool)
        or snapshot.get("snapshot_version") != 1
        or isinstance(tick, bool)
        or not isinstance(tick, int)
        or tick < 0
        or isinstance(count, bool)
        or not isinstance(count, int)
        or not isinstance(entities, list)
        or count != len(entities)
        or isinstance(skipped, bool)
        or not isinstance(skipped, int)
        or skipped != 0
    ):
        return False
    if not _valid_player_corridor(snapshot.get("player"), origin, target, beyond):
        return False
    for entity in entities:
        if (
            not isinstance(entity, dict)
            or not isinstance(entity.get("type"), str)
            or not entity["type"]
            or isinstance(entity.get("id"), bool)
            or not isinstance(entity.get("id"), int)
            or _numeric(entity.get("distance")) is None
            or _finite_vector(entity.get("position")) is None
            or _finite_vector(entity.get("velocity")) is None
        ):
            return False
    return True


def _stationary_occluded_creeper(entity: Dict[str, Any], player: Dict[str, Any]) -> bool:
    if (
        entity["type"] != "minecraft:creeper"
        or entity.get("can_see_player") is not False
        or entity.get("is_aggressive") is not False
        or entity.get("target_id") is not None
        or any(
            key in entity and entity[key] is not False
            for key in ("is_attacking", "angry_at_player")
        )
    ):
        return False
    velocity = _finite_vector(entity.get("velocity"))
    player_position = _finite_vector(player.get("position"))
    mob_position = _finite_vector(entity.get("position"))
    reported_distance = _numeric(entity.get("distance"))
    if (
        velocity is None
        or player_position is None
        or mob_position is None
        or reported_distance is None
        or math.sqrt(sum(component * component for component in velocity)) > 0.05
    ):
        return False
    observed_distance = math.dist(player_position, mob_position)
    return (
        reported_distance > _CREEPER_CLEARANCE
        and observed_distance > _CREEPER_CLEARANCE
        and abs(reported_distance - observed_distance) <= 0.75
    )


def corridor_snapshot(snapshot: Any, origin: Cell, target: Cell, beyond: Optional[Cell] = None):
    """Return a fresh snapshot with only a proven harmless travel creeper omitted."""
    if not _valid_step(origin, target) or not _valid_snapshot(snapshot, origin, target, beyond):
        return None
    player = snapshot["player"]
    suppressed = [
        entity for entity in snapshot["entities"]
        if _stationary_occluded_creeper(entity, player)
    ]
    if not suppressed:
        return snapshot
    filtered = dict(snapshot)
    filtered["entities"] = [
        entity for entity in snapshot["entities"]
        if not _stationary_occluded_creeper(entity, player)
    ]
    # Keep raw count/tick/skipped metadata: these describe the captured frame.
    return filtered


class CorridorStepDefense:
    """Navigation callback scoped to one freshly verified adjacent hop."""

    def __init__(self, client: Any, origin: Cell, target: Cell, get_block, beyond: Optional[Cell] = None):
        self.client = client
        self.origin = tuple(origin)
        self.target = tuple(target)
        self.beyond = tuple(beyond) if beyond is not None else None
        self.get_block = get_block
        self.abort_reason: Optional[str] = None

    def _environment_is_safe(self) -> bool:
        from .home_surface import _read_break_setting

        if not _valid_step(self.origin, self.target):
            self.abort_reason = "invalid tunnel corridor step"
            return False
        try:
            if _read_break_setting(self.client) != "false":
                self.abort_reason = "tunnel corridor digging is not verified disabled"
                return False
            if not _supported_open_cell(self.origin, self.get_block):
                self.abort_reason = "tunnel corridor origin is no longer supported and open"
                return False
            if not _supported_open_cell(self.target, self.get_block):
                self.abort_reason = TARGET_UNSUPPORTED
                return False
        except Exception:
            self.abort_reason = "tunnel corridor support or setting is unknown"
            return False
        return True

    def preflight(self) -> bool:
        """Prove the exact step before starting its navigation goal."""
        self.abort_reason = None
        return self._environment_is_safe()

    def __call__(self) -> bool:
        from . import combat

        if not self._environment_is_safe():
            return True
        try:
            snapshot = combat._get_combat_snapshot(self.client, radius=16)
            filtered = corridor_snapshot(snapshot, self.origin, self.target, self.beyond)
            if filtered is None:
                self.abort_reason = "tunnel corridor combat observation is incomplete or unsafe"
                return True
            return combat.defend_or_flee(self.client, observed_snapshot=filtered)
        except Exception:
            self.abort_reason = "tunnel corridor defense observation failed"
            return True
