"""Bounded fresh observation for a transient airborne false-positive."""

import math
import time
from typing import Callable, Dict, Optional, Tuple

from .defense import DefenseMode, assess_threats
from . import combat_telemetry


_RECHECKS = 4
_RECHECK_INTERVAL = 0.1


def _number(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _vector(value) -> Optional[Tuple[float, float, float]]:
    if not isinstance(value, dict):
        return None
    axes = tuple(_number(value.get(axis)) for axis in ("x", "y", "z"))
    return (
        axes
        if all(axis is not None and abs(axis) <= 30_000_000 for axis in axes)
        else None
    )


def _complete(snapshot) -> bool:
    radius = _number(snapshot.get("radius")) if isinstance(snapshot, dict) else None
    return (
        isinstance(snapshot, dict)
        and isinstance(snapshot.get("snapshot_version"), int)
        and snapshot.get("snapshot_version") == 1
        and not isinstance(snapshot.get("snapshot_version"), bool)
        and isinstance(snapshot.get("tick"), int)
        and not isinstance(snapshot.get("tick"), bool)
        and snapshot.get("tick") >= 0
        and isinstance(snapshot.get("player"), dict)
        and isinstance(snapshot.get("entities"), list)
        and isinstance(snapshot.get("count"), int)
        and not isinstance(snapshot.get("count"), bool)
        and snapshot.get("count") == len(snapshot["entities"])
        and isinstance(snapshot.get("skipped_count"), int)
        and not isinstance(snapshot.get("skipped_count"), bool)
        and snapshot.get("skipped_count") == 0
        and radius is not None
        and radius > 0
        and all(
            isinstance(item, dict)
            and isinstance(item.get("id"), int)
            and not isinstance(item.get("id"), bool)
            and isinstance(item.get("type"), str)
            and bool(item.get("type"))
            and _number(item.get("distance")) is not None
            and _vector(item.get("position")) is not None
            and _vector(item.get("velocity")) is not None
            for item in snapshot["entities"]
        )
    )


def _eligible(snapshot, primary, assessments) -> bool:
    if not _complete(snapshot) or len(assessments) != 1:
        return False
    entity = primary.entity
    player = snapshot["player"]
    mob_velocity = _vector(entity.get("velocity"))
    player_velocity = _vector(player.get("velocity"))
    pos = _vector(entity.get("position"))
    player_pos = _vector(player.get("position"))
    block_pos = _vector(player.get("block_position"))
    distance = _number(entity.get("distance"))
    fall_distance = _number(player.get("fall_distance"))
    health = _number(player.get("health"))
    return (
        entity.get("type") == "minecraft:creeper"
        and isinstance(entity.get("id"), int)
        and not isinstance(entity.get("id"), bool)
        and distance is not None
        and 10.0 < distance <= float(snapshot["radius"])
        and entity.get("can_see_player") is False
        and entity.get("is_aggressive") is False
        and entity.get("target_id") is None
        and entity.get("is_attacking", False) is False
        and entity.get("angry_at_player", False) is False
        and mob_velocity is not None
        and sum(axis * axis for axis in mob_velocity) <= 0.05**2
        and pos is not None
        and player_pos is not None
        and block_pos is not None
        and all(math.floor(axis) == block for axis, block in zip(player_pos, block_pos))
        and abs(
            math.dist(pos, player_pos) - distance
        ) <= 1.0
        and player_velocity is not None
        and player.get("is_on_ground") is False
        and player.get("is_dead") is False
        and player.get("dimension") == "minecraft:overworld"
        and health is not None
        and health > 0
        and fall_distance is not None
        and 0 <= fall_distance <= 1.25
        and -0.8 <= player_velocity[1] <= 0.8
        and math.hypot(player_velocity[0], player_velocity[2]) <= 0.65
    )


def _same_transient_case(snapshot, initial_id, previous_tick) -> Optional[bool]:
    """True only while the identical hidden calm creeper remains the sole alert."""
    if not _complete(snapshot) or snapshot["tick"] <= previous_tick:
        return False
    entities = snapshot["entities"]
    matching = [
        item for item in entities
        if isinstance(item, dict) and item.get("id") == initial_id
    ]
    if len(matching) != 1:
        return False
    player = snapshot["player"]
    if player.get("is_on_ground") is True:
        return False
    try:
        threats = assess_threats(entities, player)
    except Exception:
        return None
    return (
        len(threats) == 1
        and threats[0].entity.get("id") == initial_id
        and _eligible(snapshot, threats[0], threats)
    )


def reobserve_transient_airborne_creeper(
    client, snapshot: Dict, primary, assessments
) -> Tuple[str, Optional[Dict]]:
    """Return ('fresh', snapshot) to reassess canonically or ('hold', None).

    The caller must already have stopped navigation. This never changes combat
    telemetry or player observations; unknown state therefore fails closed.
    """
    if not _eligible(snapshot, primary, assessments):
        return "none", None
    initial_id = primary.entity["id"]
    tick = snapshot["tick"]
    for _ in range(_RECHECKS):
        time.sleep(_RECHECK_INTERVAL)
        fresh = None
        try:
            fresh = client.transport.dispatch(
                "get_combat_snapshot", {"radius": snapshot["radius"]}
            )
        except Exception:
            pass
        if not _complete(fresh) or fresh["tick"] <= tick:
            return "hold", None
        previous_tick = tick
        tick = fresh["tick"]
        same_case = _same_transient_case(fresh, initial_id, previous_tick)
        if same_case is None:
            return "hold", None
        if not same_case:
            return "fresh", fresh
    return "hold", None


def handle_transient_airborne_creeper(
    client,
    snapshot: Dict,
    primary,
    assessments,
    runtime,
    reassess: Callable[..., bool],
    allow_safe_recovery_movement: bool,
) -> bool:
    """Resolve the bounded observation after the caller stops navigation."""
    observation, fresh = reobserve_transient_airborne_creeper(
        client, snapshot, primary, assessments
    )
    if observation == "none":
        return False
    if observation == "hold":
        client._last_defense_intervention = "airborne_threat_recheck"
        runtime.transition(
            DefenseMode.ALERT,
            "airborne threat remains uncertain after bounded recheck",
        )
        combat_telemetry.record_combat_action(
            client,
            "defense_intervention",
            outcome="airborne_threat_recheck",
        )
        return True

    resolved = reassess(
        client,
        allow_safe_recovery_movement=allow_safe_recovery_movement,
        observed_snapshot=fresh,
        _skip_airborne_recheck=True,
    )
    if not resolved:
        client._last_defense_intervention = (
            "airborne_threat_recheck_clear"
            if runtime.mode == DefenseMode.CLEAR
            else "airborne_threat_recheck_intervened"
        )
    return True
