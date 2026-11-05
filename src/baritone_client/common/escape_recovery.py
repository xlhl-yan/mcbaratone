"""Terrain-screened, displacement-verified combat escape."""

from __future__ import annotations

import math
import time
from typing import Any, Dict, Optional

from . import combat_telemetry
from .defense import (
    AttackStyle,
    EscapeCandidate,
    ThreatAssessment,
    assess_threats,
    plan_escape_candidates,
)

_HAZARDS = (
    "lava",
    "fire",
    "cactus",
    "magma_block",
    "campfire",
    "pointed_dripstone",
    "sweet_berry_bush",
)
# powder_snow looks solid to a floor probe but a player falls straight into it
# and starts freezing.  Leaving it out made every flee endpoint "standing on"
# powder snow read as supported: dragon-a fled into a grove that is ~24% powder
# snow and froze to death 17 times out of 67.  Only leather boots let a player
# walk on it, so treat it as no floor at all until that is worn.
_NON_GROUND = ("air", "water", "lava", "cave_air", "void_air", "powder_snow")
_PASSABLE_BLOCKS = {
    "air",
    "cave_air",
    "void_air",
    "short_grass",
    "tall_grass",
    "fern",
    "large_fern",
    "snow",
    "vine",
    "glow_lichen",
    "dead_bush",
    "dandelion",
    "poppy",
    "blue_orchid",
    "allium",
    "azure_bluet",
    "oxeye_daisy",
    "cornflower",
    "lily_of_the_valley",
    "torchflower",
    "wither_rose",
}


def _passable(block_id: str) -> bool:
    """Classify replaceable plants without accepting similarly named solids."""
    path = str(block_id or "").split(":", 1)[-1]
    return path in _PASSABLE_BLOCKS or path.endswith("_tulip")


def _block_id(client: Any, x: int, y: int, z: int) -> Optional[str]:
    try:
        result = client.transport.dispatch(
            "get_block", {"x": x, "y": y, "z": z}
        )
    except Exception:
        return None
    if not isinstance(result, dict):
        return None
    block = result.get("id", result.get("block"))
    return str(block).lower() if block else None


def destination_safe(client: Any, x: int, y: int, z: int) -> bool:
    """Reject hazards, blocked headroom, and unsupported endpoints."""
    feet = _block_id(client, x, y, z)
    head = _block_id(client, x, y + 1, z)
    below = _block_id(client, x, y - 1, z)
    # Terrain uncertainty during combat is not evidence of a safe landing.
    # The prior fail-open behavior sent bots toward unreadable endpoints and
    # only discovered the bad route while incoming damage continued.
    if feet is None or head is None or below is None:
        return False
    known = tuple(value for value in (feet, head, below) if value)
    if any(token in block for block in known for token in _HAZARDS):
        return False
    if feet and not _passable(feet):
        return False
    if head and not _passable(head):
        return False
    if below and any(token in below for token in _NON_GROUND):
        return False
    return True


def surface_adjusted_candidates(
    client: Any,
    planned: list[EscapeCandidate],
    *,
    radius: int = 32,
) -> list[EscapeCandidate]:
    """Project flee directions onto nearby exposed natural terrain."""
    try:
        response = client.transport.dispatch(
            "find_blocks",
            {
                "blocks": [
                    "minecraft:grass_block",
                    "minecraft:dirt",
                    "minecraft:coarse_dirt",
                    "minecraft:podzol",
                ],
                "radius": min(16, max(4, int(radius))),
                "limit": 512,
            },
            timeout=2.0,
        )
    except Exception:
        return []
    ranked = []
    for block in response.get("found", []):
        position = (
            int(block["x"]),
            int(block["y"]) + 1,
            int(block["z"]),
        )
        distance = min(
            (position[0] - candidate.x) ** 2
            + (position[2] - candidate.z) ** 2
            + (position[1] - candidate.y) ** 2
            for candidate in planned
        )
        nearest_direction = max(
            planned,
            key=lambda candidate: candidate.direction_score
            - 0.01
            * (
                (position[0] - candidate.x) ** 2
                + (position[2] - candidate.z) ** 2
            ),
        )
        ranked.append(
            (
                distance,
                EscapeCandidate(
                    *position,
                    direction_score=nearest_direction.direction_score,
                ),
            )
        )
    ranked.sort(key=lambda item: item[0] - item[1].direction_score * 10)
    safe = []
    seen = set()
    for _, candidate in ranked[:64]:
        position = (candidate.x, candidate.y, candidate.z)
        if position in seen:
            continue
        seen.add(position)
        if destination_safe(client, *position):
            safe.append(candidate)
            if len(safe) >= 8:
                break
    return safe


def separation_from(entity: Dict, player_position: Dict) -> float:
    """Measure horizontal separation from a threat."""
    from . import combat as api

    position = api.entity_position(entity)
    if position is None:
        return float(entity.get("distance", 0) or 0)
    try:
        return (
            (float(position[0]) - float(player_position.get("x", 0))) ** 2
            + (float(position[2]) - float(player_position.get("z", 0))) ** 2
        ) ** 0.5
    except (TypeError, ValueError):
        return float(entity.get("distance", 0) or 0)


def verify_escape(
    client: Any,
    threat_id: Optional[int],
    initial_distance: float,
    *,
    timeout: float,
    minimum_gain: float,
) -> bool:
    """Confirm that an escape command really increases separation."""
    from . import combat as api

    deadline = time.monotonic() + max(0.5, timeout)
    while time.monotonic() < deadline:
        api.ensure_alive(client)
        try:
            entities = api.get_nearby_entities(
                client, 40, raise_on_error=True
            )
        except api.EntityQueryError:
            time.sleep(0.5)
            continue
        target = next(
            (entity for entity in entities if entity.get("id") == threat_id),
            None,
        )
        if target is None:
            return True
        state = client.transport.dispatch("get_state", {})
        position = state.get("block_position", state.get("position", {})) or {}
        if separation_from(target, position) >= max(
            14.0, initial_distance + minimum_gain
        ):
            return True
        time.sleep(0.5)
    return False


def verify_escape_from_threats(
    client: Any,
    initial_distances: Dict[int, float],
    initial_position: Dict,
    *,
    timeout: float,
    minimum_gain: float,
    minimum_y: Optional[float] = None,
) -> bool:
    """Verify separation from every original and newly urgent threat."""
    from . import combat as api

    deadline = time.monotonic() + max(0.5, timeout)
    clear_observations = 0
    while time.monotonic() < deadline:
        api.ensure_alive(client)
        snapshot = api._get_combat_snapshot(client, radius=40)
        if snapshot is None or int(snapshot.get("skipped_count", 0) or 0) > 0:
            time.sleep(0.5)
            continue
        entities = snapshot["entities"]
        state = snapshot["player"]
        position = state.get("block_position", state.get("position", {})) or {}
        try:
            current_y = float(position["y"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(current_y) or (
            minimum_y is not None and current_y < minimum_y
        ):
            if minimum_y is not None:
                client._last_escape_failure_reason = "unsafe_descent"
            return False
        by_id = {
            int(entity["id"]): entity
            for entity in entities
            if entity.get("id") is not None
        }
        originals_clear = all(
            threat_id not in by_id
            or separation_from(by_id[threat_id], position)
            >= max(14.0, initial + minimum_gain)
            for threat_id, initial in initial_distances.items()
        )
        from .combat_intent import exclude_authorized_threats

        urgent_now = [
            item
            for item in exclude_authorized_threats(
                client, assess_threats(entities, state)
            )
            if item.distance < 12.0
        ]
        displacement = (
            (float(position.get("x", 0)) - float(initial_position.get("x", 0))) ** 2
            + (float(position.get("z", 0)) - float(initial_position.get("z", 0))) ** 2
        ) ** 0.5
        clear_observations = (
            clear_observations + 1
            if originals_clear and not urgent_now and displacement >= 3.0
            else 0
        )
        if clear_observations >= 2:
            return True
        time.sleep(0.5)
    return False


def relocate_away_from(
    client: Any,
    threat: Dict,
    *,
    distance: int = 28,
    timeout: int = 30,
) -> bool:
    """Use an observed landing and guard the route against unsafe descent."""
    from .defense_relocation import relocate

    return relocate(client, threat, distance=distance, timeout=timeout)


@combat_telemetry.trace_escape
def run_away(
    client: Any,
    threat: Dict,
    *,
    timeout: float = 6.0,
    minimum_gain: float = 5.0,
) -> bool:
    """Choose a safe endpoint and prove movement away from a threat."""
    from . import combat as api

    client._last_escape_failure_reason = None
    previous_break = None
    verified = False
    try:
        state = client.transport.dispatch("get_state", {})
        position = state.get("block_position", state.get("position", {})) or {}
        if not all(
            math.isfinite(float(position[axis])) for axis in ("x", "y", "z")
        ):
            client._last_escape_failure_reason = "unknown_player_position"
            return False
        from .defense_relocation import relocation_floor

        minimum_y = relocation_floor(client, position)
        try:
            nearby = api.scan_for_threats(
                client,
                radius=16,
                raise_on_error=True,
                player_state=state,
            )
        except api.EntityQueryError:
            nearby = [threat]
        if not any(item.get("id") == threat.get("id") for item in nearby):
            nearby.append(threat)
        from .combat_intent import exclude_authorized_threats

        assessments = exclude_authorized_threats(
            client, assess_threats(nearby, state)
        ) or [
            ThreatAssessment(
                entity=threat,
                entity_type=str(threat.get("type", "unknown")),
                distance=float(threat.get("distance", 0) or 0),
                closing_speed=0.0,
                score=1.0,
                style=AttackStyle.MELEE,
                always_evade=True,
            )
        ]
        initial = separation_from(threat, position)
        initial_distances = {
            int(item.entity["id"]): separation_from(item.entity, position)
            for item in assessments
            if item.entity.get("id") is not None and item.distance <= 16.0
        }
        if threat.get("id") is not None:
            initial_distances[int(threat["id"])] = initial
        candidates = plan_escape_candidates(position, assessments)
        # Active combat cannot afford a broad surface-block scan. Live Bot16
        # spent ~22 seconds inside the retried find_blocks fallback while four
        # cave mobs kept attacking. Screen the nearest local candidates only;
        # when none are safe, the caller immediately chooses relocation or
        # last-resort combat.
        safe = [
            candidate
            for candidate in candidates[:4]
            if candidate.y >= minimum_y
            and destination_safe(client, candidate.x, candidate.y, candidate.z)
        ]
        combat_telemetry.record_combat_action(
            client,
            "escape_plan",
            outcome="ready" if safe else "no_safe_endpoint",
            candidate_count=len(candidates),
            safe_candidate_count=len(safe),
        )
        if not safe:
            print("FLEE: no terrain-safe escape endpoint found")
            client._last_escape_failure_reason = "no_safe_endpoint"
            return False
        # A combat route must not excavate a drop or tunnel while the player
        # is under pressure.  Refuse if this safety setting cannot be read;
        # restore it only after the route is observed stopped.
        from .home_surface import _read_break_setting, _write_break_setting

        previous_break = _read_break_setting(client)
        if previous_break not in ("true", "false"):
            raise ValueError("allowBreak setting unavailable or malformed")
        if previous_break == "true":
            _write_break_setting(client, "false")
        per_candidate = max(1.5, timeout / len(safe))
        for candidate in safe:
            destination = {
                "x": candidate.x,
                "y": candidate.y,
                "z": candidate.z,
            }
            combat_telemetry.record_combat_action(
                client,
                "escape_route",
                outcome="attempted",
                destination=destination,
            )
            print(
                f"FLEE: pathing to {candidate.x},{candidate.y},{candidate.z}; "
                f"initial separation {initial:.1f}m"
            )
            client.transport.dispatch(
                "goal",
                {"x": candidate.x, "y": candidate.y, "z": candidate.z},
            )
            client.transport.dispatch("chat", {"message": "#path"})
            if verify_escape_from_threats(
                client,
                initial_distances,
                position,
                timeout=per_candidate,
                minimum_gain=minimum_gain,
                minimum_y=minimum_y,
            ):
                combat_telemetry.record_combat_action(
                    client,
                    "escape_route",
                    outcome="verified",
                    destination=destination,
                )
                print("FLEE: separation verified")
                verified = True
                break
            client.transport.dispatch("cancel", {})
            if client._last_escape_failure_reason == "unsafe_descent":
                break
            combat_telemetry.record_combat_action(
                client,
                "escape_route",
                outcome="failed",
                destination=destination,
            )
        if not verified and client._last_escape_failure_reason != "unsafe_descent":
            print("FLEE: candidate routes did not increase separation")
            client._last_escape_failure_reason = "no_separation_gain"
    except api.PlayerDeathDetected:
        raise
    except Exception as exc:
        client._last_escape_failure_reason = "error"
        print(f"Run away failed: {exc}")
    finally:
        if previous_break is not None:
            stopped = False
            try:
                api._stop_for_defense(client)
                stopped = (
                    client.transport.dispatch("get_state", {}).get("is_pathing")
                    is False
                )
            except Exception:
                stopped = False
            if stopped and previous_break == "true":
                try:
                    from .home_surface import _write_break_setting

                    _write_break_setting(client, previous_break)
                except Exception:
                    stopped = False
            if not stopped:
                verified = False
                client._last_escape_failure_reason = "route_stop_unverified"
                if previous_break == "true":
                    # Silently leaving digging off is the leak the home-route
                    # guard had: one unverified flee stop on live A1
                    # 2026-10-21 17:50 kept allowBreak false for 16 hours.
                    # Owe it; the guard's retry restores it once nothing moves.
                    from .home_surface import _owe

                    _owe(client, {"allowBreak": "true"})
                    print("FLEE: route stop unverified; digging stays off until it can be restored")
    return verified
