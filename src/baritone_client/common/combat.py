"""
Combat utilities - Mob engagement, retreat logic, and healing.
"""

import logging
import time
from typing import Any, Callable, Dict, List, Optional
from . import combat_telemetry
from .food_recovery import (
    bounded_exploration_origin,
    collect_edible_drop,
    must_hold_for_critical_food,
)
from .combat_targeting import matches_requested_mob
from .combat_intent import exclude_authorized_threats
from .combat_action import dispatch_held_item_use, exclusive_client_function
from .stationary_defense import fight_without_pursuit
from .emergency_food import (
    EMERGENCY_FOOD_ITEMS,
    EmergencyExploration,
    emergency_food_count as _emergency_food_count,
    enforce_dry_food_search_state,
    hunt_target,
    prepare_carried_wheat_recovery,
    prepare_food_search_state,
    return_to_food_search_anchor,
    select_target,
    wait_for_safe_regeneration,
)
from .health_recovery import recover_health
from .water_hazards import restore_water_routes, turn_back_at_fence
from .movement_recovery import block_position
from .passive_hunt_navigation import (
    PassiveHuntNavigation,
    find_unrequested_hostile,
)
from .airborne_threat_confirmation import handle_transient_airborne_creeper

from ..core.exceptions import TransportError

from .defense import (
    AttackStyle,
    DefenseDecision,
    DefenseMode,
    DefenseRuntime,
    assess_threats,
    choose_defense_action,
)

from .inventory import (
    count_item,
    equip_best_weapon,
    get_equipped_armor,
    has_durable_full_armor,
    select_item,
)
from .tasks import PlayerDeathDetected, TaskResult
from .navigation import allow_recovery_navigation, goto, goto_xz

logger = logging.getLogger(__name__)

MELEE_ATTACK_COOLDOWN_THRESHOLD = 0.9
MULTI_THREAT_ABORT_RADIUS = 12.0


class EntityQueryError(RuntimeError):
    """Distinguish a bridge failure from a successful empty entity query."""


def get_nearby_entities(
    client, radius: int = 30, *, raise_on_error: bool = False
) -> List[Dict]:
    """
    Get list of nearby entities.

    Args:
        raise_on_error: when True, a failed bridge query raises
            EntityQueryError instead of returning ``[]``. Callers that must
            not confuse "the bridge is unreachable" with "no entities here"
            (e.g. hunt_mobs deciding whether to wander off exploring) should
            set this. Default stays False so existing best-effort callers are
            unchanged.

    Returns:
        List of entity dictionaries (empty if the area is genuinely empty, or
        on error when raise_on_error is False).
    """
    try:
        data = client.transport.dispatch("get_entities", {"radius": radius})
    except Exception as exc:
        logger.warning("get_entities bridge query failed: %s", exc)
        if raise_on_error:
            raise EntityQueryError(str(exc)) from exc
        return []
    if isinstance(data, dict) and data.get("error"):
        logger.warning("get_entities returned error: %s", data.get("error"))
        if raise_on_error:
            raise EntityQueryError(str(data.get("error")))
        return []
    return data.get("entities", [])


def entity_position(entity: Dict) -> Optional[tuple]:
    """
    Extract (x, y, z) from an entity dict.

    The bridge returns entity coordinates nested under "position"; older code
    assumed top-level x/y/z keys (which silently produced (0, 0, 0)).
    """
    pos = entity.get("position")
    if isinstance(pos, dict) and "x" in pos:
        return (pos.get("x", 0), pos.get("y", 0), pos.get("z", 0))
    if "x" in entity:
        return (entity.get("x", 0), entity.get("y", 0), entity.get("z", 0))
    return None


def look_at_entity(client, entity: Dict) -> bool:
    """
    Look at an entity using its coordinates.

    The bridge's look_at handler only accepts x/y/z (sending entity_id causes
    a NullPointerException server-side). Aim slightly above the feet so we
    face the body rather than the ground. Never raises - a failed look is not
    worth aborting a task over.
    """
    pos = entity_position(entity)
    if pos is None:
        return False
    x, y, z = pos
    try:
        client.transport.dispatch("look_at", {"x": x, "y": y + 1.0, "z": z})
        return True
    except Exception as e:
        print(f"  look_at_entity failed (non-fatal): {e}")
        return False


def find_entity_by_type(
    client,
    entity_types: List[str],
    radius: int = 30,
    *,
    raise_on_error: bool = False,
) -> Optional[Dict]:
    """
    Find nearest entity of specified types.

    Args:
        entity_types: List of entity type substrings (e.g., ["zombie", "skeleton"])
        radius: Search radius
        raise_on_error: propagate a bridge-query failure as EntityQueryError
            instead of returning None (which is otherwise indistinguishable
            from "no matching entity in range").

    Returns:
        Entity dict or None
    """
    entities = get_nearby_entities(client, radius, raise_on_error=raise_on_error)

    for entity in sorted(entities, key=lambda e: e.get("distance", 999)):
        if matches_requested_mob(entity.get("type", ""), entity_types):
            return entity

    return None


def attack_nearest(
    client,
    entity_types: List[str],
    max_range: int = 10,
) -> bool:
    """Fight the nearest exact requested type through verified safe combat."""
    entity = find_entity_by_type(client, entity_types, radius=max_range)
    if entity is None or entity.get("id") is None:
        return False
    return safe_combat(
        client,
        int(entity["id"]),
        purpose="attack_nearest",
        source="attack_nearest",
        target_metadata=entity,
    )


@combat_telemetry.trace_safe_combat
def safe_combat(
    client,
    target_id: int,
    retreat_health: float = 6.0,
    max_duration: int = 30,
    abort_on_other_hostiles: bool = False,
    tracking_radius: int = 30,
    no_retreat: bool = False,
    *,
    purpose: Optional[str] = None,
    source: str = "safe_combat",
    target_metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """Fight one target through the supervised cooldown-aware melee loop."""
    from .combat_intent import CombatIntent, combat_intent
    from .combat_melee import execute_safe_combat

    metadata = target_metadata if isinstance(target_metadata, dict) else {}
    intent = CombatIntent.for_target(
        target_id,
        purpose=purpose or source,
        target_type=metadata.get("type", ""),
    )
    with combat_intent(client, intent):
        return execute_safe_combat(
            client,
            target_id,
            retreat_health,
            max_duration,
            abort_on_other_hostiles,
            tracking_radius,
            no_retreat,
        )

def _approach_aquatic_food(
    client,
    target_id: int,
    entity_type: str,
    *,
    timeout: float = 15.0,
) -> bool:
    """Use Baritone's dynamic entity goal to reach a swimming food mob.

    A coordinate goal at a fish's water block is not standable, so normal
    ``goto`` retries can never enter melee range. Entity-follow maintains a
    nearby reachable goal as the fish moves, and is always cancelled before
    combat takes ownership of movement.
    """
    command_type = str(entity_type).split(":")[-1]
    # Track the best (smallest) separation seen. If the gap never shrinks the
    # target is unreachable (a fish in a sealed flooded chamber, say) -- live:
    # Bot07 reported the same frozen distance for the whole window at 8 health.
    best_distance = None
    progress_deadline = time.time() + max(6.0, float(timeout) * 0.35)
    try:
        client.transport.dispatch(
            "chat",
            {"message": f"#follow entity {command_type}"},
        )
        deadline = time.time() + max(0.0, float(timeout))
        while time.time() < deadline:
            # #follow has no goto drowning reflex; bail after a bounded dive.
            follow_state = client.transport.dispatch("get_state", {})
            follow_health = float(follow_state.get("health", 20) or 0)
            if follow_health < 12.0:
                print(
                    "RECOVERY: health fell below 12 during aquatic follow; "
                    "surfacing and abandoning the fish target"
                )
                _surface_after_aquatic_hunt(client, timeout=12.0)
                return False
            if _submerged_too_long(client, follow_state, max_seconds=8.0):
                print("SURVIVAL: submerged too long chasing aquatic food; surfacing")
                _surface_after_aquatic_hunt(client, timeout=12.0)
                client._submerged_since = None
                return False
            entities = get_nearby_entities(client, radius=64)
            target = next(
                (entity for entity in entities if entity.get("id") == target_id),
                None,
            )
            if target is None:
                return True
            distance = float(target.get("distance", 999))
            if distance < 4.5:
                return True
            if best_distance is None or distance < best_distance - 1.0:
                best_distance = distance
                progress_deadline = time.time() + max(6.0, float(timeout) * 0.35)
            elif time.time() > progress_deadline:
                print(
                    f"FOLLOW: no closing progress on {command_type} "
                    f"(held at {distance:.1f}m); treating it as unreachable"
                )
                return False
            threats = scan_for_threats(client, radius=12)
            if threats and float(threats[0].get("distance", 999)) <= 12:
                return False
            time.sleep(0.5)
        return False
    finally:
        client.transport.dispatch("chat", {"message": "#stop"})
        client.transport.dispatch("cancel", {})


def _surface_after_aquatic_hunt(client, *, timeout: float = 10.0) -> bool:
    """Reach breathing air after a fish kill without targeting its water block."""
    from .surface_recovery import reach_breathing_air

    reached = reach_breathing_air(
        client,
        timeout=timeout,
        ensure_alive=ensure_alive,
        sleep=time.sleep,
        clock=time.time,
    )
    client._aquatic_surface_failed = not reached
    return reached


# Consecutive supervision ticks with the head underwater before forcing a
# surface.  At ~3s between ticks this reacts within ~6s -- well inside the
# ~15s before drown damage begins -- while ignoring a momentary dip through a
# waterfall or a one-block plunge.
_SUBMERSION_TICKS_BEFORE_SURFACE = 2


def _head_block_is_water(client, state) -> bool:
    from .aquatic_survival import head_block_is_water

    return head_block_is_water(client, state)


def _player_is_in_water(client, state) -> bool:
    from .aquatic_survival import player_is_in_water

    return player_is_in_water(client, state)


def _submerged_too_long(client, state, *, max_seconds: float) -> bool:
    from .aquatic_survival import submerged_too_long

    return submerged_too_long(
        client,
        state,
        max_seconds=max_seconds,
        head_is_water=_head_block_is_water,
        clock=time.time,
    )


def escape_water_if_submerged(client, state) -> bool:
    from .aquatic_survival import escape_water_if_submerged as implementation

    return implementation(
        client,
        state,
        tick_limit=_SUBMERSION_TICKS_BEFORE_SURFACE,
        head_is_water=_head_block_is_water,
        feet_in_water=_player_is_in_water,
        surface=_surface_after_aquatic_hunt,
    )


def survival_tick(client, state=None) -> bool:
    """Central per-poll survival reflex for any long-running wait loop.

    The shared entry point every blocking primitive (goto, nether_travel,
    wood_gathering) already calls each poll: surfaces a submerged player,
    then equips leather boots in a freezing biome. Returns True if it
    intervened (the caller should assume Baritone was cancelled and reissue).
    """
    from .freeze_survival import guard_against_freezing

    try:
        if state is None:
            state = client.transport.dispatch("get_state", {})
        ensure_alive(client, state)
        if escape_water_if_submerged(client, state):
            return True
        return guard_against_freezing(client, state)
    except PlayerDeathDetected:
        raise
    except Exception:
        return False


def _attack_cooldown(state: Dict) -> float:
    """Return normalized melee readiness, defaulting ready for old bridges."""
    try:
        return min(1.0, max(0.0, float(state.get("attack_cooldown", 1.0))))
    except (TypeError, ValueError):
        return 0.0


def _threat_can_reach_player(
    threat: Dict,
    player_state: Dict,
    *,
    max_vertical_separation: float = 6.0,
) -> bool:
    """Reject sealed cave mobs that are close only in three-dimensional range."""
    threat_position = entity_position(threat)
    player_position = player_state.get(
        "block_position", player_state.get("position", {})
    )
    if threat_position is None or "y" not in player_position:
        return True
    try:
        return abs(float(threat_position[1]) - float(player_position["y"])) <= float(
            max_vertical_separation
        )
    except (TypeError, ValueError):
        return True


# Mob-type substrings whose kill drops raw food, i.e. hunting them is itself
# a way back to a stable hunger level.
_FOOD_YIELDING_MOBS = ("cow", "mooshroom", "sheep", "pig", "chicken", "rabbit")

# Below this health, acquire_emergency_food restricts its passive-food target
# search to a short radius rather than committing to a long unprotected trek.
# See the hunt_radius comment at its call site for the live failure this fixes.
_CRITICAL_HUNT_HEALTH = 10.0


@exclusive_client_function
def eat_until_hunger(client, minimum_food: int = 14) -> bool:
    """Consume carried food until the hunger bar is safe for progression."""
    minimum_food = max(1, min(int(minimum_food), 20))
    attempts = 0
    while attempts < 10:
        state = client.transport.dispatch("get_state", {})
        ensure_alive(client, state)
        food_level = int(state.get("food_level", state.get("food", 20)))
        if food_level >= minimum_food:
            return True

        selected = None
        for item_id in EMERGENCY_FOOD_ITEMS:
            if count_item(client, item_id) <= 0:
                continue
            if select_item(client, item_id, allow_swap=True):
                selected = item_id
                break
        if selected is None:
            print(f"HUNGER: no carried food available at level {food_level}")
            return False

        print(f"HUNGER: eating {selected} at level {food_level}")
        before_count = count_item(client, selected)
        # Right-clicking food while the crosshair is on the home chest opens
        # the container instead of eating.  Face harmlessly upward first so
        # the held-use action cannot interact with a nearby block.
        try:
            client.transport.dispatch("look", {"yaw": 0, "pitch": -90})
        except Exception:
            pass
        dispatch_held_item_use(client, 2500)
        # The bridge starts a held-use action asynchronously.  Reissuing it
        # every half-second resets the eating timer, so wait for a real hunger
        # or stack-count delta before touching the selected item again.
        completed = False
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            time.sleep(0.25)
            updated = client.transport.dispatch("get_state", {})
            updated_food = int(updated.get("food_level", updated.get("food", food_level)))
            if updated_food > food_level or count_item(client, selected) < before_count:
                completed = True
                break
        if not completed:
            # The held-use release and the food update can land on the next
            # client tick just after the polling deadline.  Verify once more
            # before reporting failure; the live 1.21 client has exhibited
            # this exact late-but-successful completion.
            time.sleep(0.25)
            final_food = int(
                client.transport.dispatch("get_state", {}).get("food_level", food_level)
            )
            if final_food >= minimum_food:
                return True
            print(f"HUNGER: eating {selected} did not complete; retrying after cooldown")
            attempts += 1
            time.sleep(1.0)
            continue
        attempts += 1

    final_state = client.transport.dispatch("get_state", {})
    return int(final_state.get("food_level", final_state.get("food", 0))) >= minimum_food


@exclusive_client_function
def heal_if_needed(client, threshold: float = 10.0) -> bool:
    """
    Eat food if health below threshold.
    
    Args:
        client: Baritone client
        threshold: Health threshold to trigger healing
        
    Returns:
        True if healing was attempted
    """
    # Check health
    state = client.transport.dispatch("get_state", {})
    health = state.get("health", 20)
    if health >= threshold:
        return False
    
    # Early survival frequently has only raw meat or rotten flesh, and it may
    # be in the main inventory.  Move it to the hotbar instead of silently
    # ignoring every non-hotbar slot.
    for item_id in EMERGENCY_FOOD_ITEMS:
        if count_item(client, item_id) <= 0:
            continue
        if select_item(client, item_id, allow_swap=True):
            time.sleep(0.1)
            try:
                client.transport.dispatch("look", {"yaw": 0, "pitch": -90})
            except Exception:
                pass
            dispatch_held_item_use(client, 2500)
            return True
    
    return False


@allow_recovery_navigation
@restore_water_routes
def acquire_emergency_food(
    client,
    minimum_health: float = 12.0,
    minimum_food: int = 14,
    minimum_reserve: int = 0,
    timeout: float = 240.0,
    max_exploration_distance: float = 96.0,
    exploration_center: Optional[tuple[float, ...]] = None,
    return_to_exploration_center: bool = False,
    renewable_source_callback: Optional[
        Callable[[str, tuple[int, int, int]], None]
    ] = None,
) -> bool:
    """Recover an early-game player by hunting only passive food sources.

    This is intentionally conservative: it waits for daylight, never explores
    blind while critically wounded, and aborts when a hostile approaches.
    """
    minimum_food = max(1, min(int(minimum_food), 20))
    minimum_reserve = max(0, int(minimum_reserve))

    def recovery_complete(state: Optional[Dict] = None) -> bool:
        state = state or client.transport.dispatch("get_state", {})
        health = float(state.get("health", 20) or 0)
        food = int(state.get("food_level", state.get("food", 20)))
        return (
            health >= minimum_health
            and food >= minimum_food
            and _emergency_food_count(client) >= minimum_reserve
        )

    recover_health(client, minimum_health=minimum_health, timeout=10.0)
    if recovery_complete():
        return True

    try:
        state = client.transport.dispatch("get_state", {})
    except Exception:
        print("RECOVERY: could not read initial state for emergency food; retrying briefly.")
        time.sleep(1.0)
        try:
            state = client.transport.dispatch("get_state", {})
        except Exception:
            state = {}
    cached_state = state if isinstance(state, dict) else {}
    if int(state.get("world_time", 0)) % 24000 >= 12000:
        print("RECOVERY: night detected; waiting in shelter before seeking food")
        from .base import wait_for_safe_daylight
        if not wait_for_safe_daylight(client, max_wait=720, poll_interval=5):
            return False

    if prepare_carried_wheat_recovery(
        client,
        state,
        minimum_food,
        scan_for_threats,
        _threat_can_reach_player,
        eat_until_hunger,
        recovery_complete,
    ):
        return True

    start = time.time()
    state = client.transport.dispatch("get_state", {})
    state = prepare_food_search_state(client, state)
    if state is None:
        return False
    position = state.get("block_position", state.get("position", {}))
    # Widen the bound after repeated rotations find nothing -- a genuinely
    # food-barren area otherwise saturates it and repeats forever. Live A1
    # 2026-09-09: health sat pinned at 5/12 for 25+ min on the same waypoints.
    try:
        prior_cursor = int(getattr(client, "_emergency_food_waypoint_cursor", 0))
    except (TypeError, ValueError):
        prior_cursor = 0
    search_radius = max_exploration_distance * (2 ** min(prior_cursor // 4, 2))
    stable_anchor = None
    if (
        return_to_exploration_center
        and isinstance(exploration_center, (list, tuple))
        and len(exploration_center) == 3
    ):
        stable_anchor = tuple(float(value) for value in exploration_center)
        origin_x, origin_z = stable_anchor[0], stable_anchor[2]
    else:
        origin_x, origin_z = bounded_exploration_origin(
            position,
            exploration_center,
            search_radius,
        )
    exploration = EmergencyExploration(
        origin_x,
        origin_z,
        search_radius,
    )
    exploration.resume_waypoint_rotation(client)
    exploration.movement.reset(state)

    def stop_exploring() -> None:
        exploration.stop(client)

    # Suppress sprinting: live Bot10 burned its remaining hunger before a food
    # source loaded. ``stop_exploring`` restores the normal setting.
    exploration.suppress_sprint(client)
    if stable_anchor is not None:
        current = block_position(state)
        distance_from_anchor = (
            (current[0] - stable_anchor[0]) ** 2
            + (current[2] - stable_anchor[2]) ** 2
        ) ** 0.5
        if distance_from_anchor > search_radius:
            if not return_to_food_search_anchor(client, stable_anchor):
                stop_exploring()
                return False
            state = prepare_food_search_state(
                client,
                client.transport.dispatch("get_state", {}),
            )
            if state is None:
                stop_exploring()
                return False
            cached_state = state
            exploration.movement.reset(state)
    # Bound both the critical-health hold and retries of unreachable food.
    unreachable_food: set = set()
    hold_cycles = 0
    max_hold_cycles = 3
    while time.time() - start < timeout:
        try:
            state = client.transport.dispatch("get_state", {})
            if isinstance(state, dict) and state:
                cached_state = state
        except Exception:
            if cached_state:
                print("RECOVERY: state read failed; using cached state this cycle.")
                state = cached_state
            else:
                print("RECOVERY: state unavailable and no cache; waiting before retry.")
                time.sleep(1.0)
                continue
        ensure_alive(client, state)
        if not enforce_dry_food_search_state(client, state, stop_exploring):
            return False
        exploration.observe(state)
        if turn_back_at_fence(exploration, client, state):
            continue
        if recovery_complete(state):
            stop_exploring()
            return True
        if eat_until_hunger(client, minimum_food=minimum_food):
            refreshed = client.transport.dispatch("get_state", {})
            if recovery_complete(refreshed):
                stop_exploring()
                return True
            if wait_for_safe_regeneration(
                client,
                refreshed,
                minimum_health,
                scan_for_threats,
                _threat_can_reach_player,
                stop_exploring,
            ):
                time.sleep(2.0)
                continue
        from .navigation import recovery_goto

        if collect_edible_drop(
            client,
            get_nearby_entities(client, radius=48),
            recovery_goto,
        ):
            continue

        day_time = int(state.get("world_time", 0)) % 24000
        if day_time >= 12000:
            stop_exploring()
            print("RECOVERY: daylight ended before emergency food was secured")
            return False

        position = state.get("block_position", state.get("position", {}))
        current_x = float(position.get("x", state.get("x", origin_x)) or origin_x)
        current_z = float(position.get("z", state.get("z", origin_z)) or origin_z)
        distance = ((current_x - origin_x) ** 2 + (current_z - origin_z) ** 2) ** 0.5
        if distance > search_radius:
            print(
                "RECOVERY: emergency food search reached its "
                f"{search_radius:.0f}-block safety radius"
            )
            if stable_anchor is not None:
                client.transport.dispatch("cancel", {})
                return_to_food_search_anchor(client, stable_anchor)
            stop_exploring()
            return False

        threats = scan_for_threats(client, radius=16, player_state=state)
        immediate_threat = next(
            (
                threat
                for threat in threats
                if threat.get("distance", 999) <= 12
                and _threat_can_reach_player(threat, state)
            ),
            None,
        )
        if immediate_threat is not None:
            stop_exploring()
            client.transport.dispatch("cancel", {})
            from .base import _has_existing_enclosure
            if _has_existing_enclosure(client, state):
                print(
                    f"RECOVERY: {immediate_threat.get('type')} is "
                    f"{immediate_threat.get('distance', 999):.1f}m away; "
                    "holding inside verified shelter"
                )
                time.sleep(5)
                continue
            if run_away(client, immediate_threat):
                print(
                    "RECOVERY: escaped nearby hostile; resuming bounded "
                    "food search"
                )
                exploration.movement.reset(
                    client.transport.dispatch("get_state", {})
                )
                continue
            # Closed loop: work blocked by the health floor, healing by no
            # food, food by this branch, fleeing by run_away, fighting by no
            # weapon. Digging in needs none of those (live dragon-a: 300s here).
            from .night_shelter import shelter_in_place
            if shelter_in_place(client):
                time.sleep(5)
                continue
            print("RECOVERY: hostile nearby and no enclosure; aborting food run")
            return False

        current_food = int(state.get("food_level", state.get("food", 20)))
        current_health = float(state.get("health", 20) or 0)
        # Live Bot08 died on 48-64m food approaches while wounded. At critical
        # health, accept only targets inside the 16-block threat perimeter.
        hunt_radius = 16.0 if current_health < _CRITICAL_HUNT_HEALTH else 64.0
        nearby = get_nearby_entities(client, radius=hunt_radius)

        target = select_target(
            client,
            nearby,
            current_food=current_food,
            elapsed=time.time() - start,
            timeout=timeout,
            renewable_source_callback=renewable_source_callback,
            in_water=_player_is_in_water(client, state),
            aquatic_search_radius=int(hunt_radius),
            unreachable=unreachable_food,
        )
        if target is None:
            if (
                must_hold_for_critical_food(
                    state,
                    minimum_health=minimum_health,
                    current_food=current_food,
                )
                and hold_cycles < max_hold_cycles
            ):
                hold_cycles += 1
                stop_exploring()
                print(
                    "RECOVERY: critical health/hunger with no loaded food "
                    f"target; holding {hold_cycles}/{max_hold_cycles} cycles "
                    "before searching anyway"
                )
                time.sleep(3)
                continue

            if exploration.try_surface_egress(client, state):
                continue
            if exploration.needs_rotation(state):
                exploration.start(client, state)
            time.sleep(3)
            continue

        stop_exploring()
        outcome = hunt_target(
            client, target, minimum_health=minimum_health,
            recovery_complete=recovery_complete, unreachable=unreachable_food,
        )
        if outcome is False:
            return False
        if outcome is True:
            return True

    stop_exploring()
    return False


def hunt_passive_mobs(
    client,
    target_count: int = 10,
    timeout: int = 300,
    type_filter: Optional[List[str]] = None,
) -> int:
    """
    Hunt passive mobs for food.
    
    Args:
        client: Baritone client
        target_count: Number of mobs to kill
        timeout: Maximum time
        
    Returns:
        Number of mobs killed
    """
    result = hunt_mobs(
        client,
        mob_types=type_filter if type_filter else ["pig", "cow", "sheep", "chicken"],
        required_loot={"minecraft:cooked_beef": 0},
        search_radius=50,
        timeout=timeout,
        heal_threshold=5.0,
        target_kills=target_count,
    )
    return int(result.data.get("kills", 0))


def _post_hunt_recovery(
    client, heal_threshold, missing, kills, recovery_anchor
):
    """Leave the spawn radius, then stabilize before moving again."""
    if recovery_anchor is not None:
        print(f"RECOVERY: leaving hostile spawn radius toward {recovery_anchor}")
        goto(
            client,
            *recovery_anchor,
            timeout=20,
            check_interval=0.25,
            tolerance=5.0,
            # The purpose of this route is to break contact. Re-entering the
            # combat policy here cancels the escape and strands the bot at the
            # spawner again.
            on_defense=lambda: False,
        )
    state = client.transport.dispatch("get_state", {})
    health = float(state.get("health", 20) or 0)
    if health >= heal_threshold or recover_health(
        client,
        minimum_health=heal_threshold,
        timeout=30.0,
    ):
        return None
    return TaskResult.fail(
        "Hunt stopped because post-combat health did not recover",
        missing=missing,
        kills=kills,
        health=health,
    )


def hunt_mobs(
    client,
    mob_types: List[str],
    required_loot: Dict[str, int],
    search_radius: int = 48,
    timeout: int = 600,
    heal_threshold: float = 8.0,
    target_kills: Optional[int] = None,
    abort_on_other_hostiles: bool = False,
    latest_world_time: Optional[int] = None,
    max_distance_from_origin: Optional[float] = None,
    exploration_center: Optional[tuple[int, int]] = None,
    max_kills: Optional[int] = None,
    explore_when_empty: bool = True,
    no_retreat: bool = False,
    recover_after_combat: bool = False,
    recovery_anchor: Optional[tuple[int, int, int]] = None,
) -> TaskResult:
    """
    Hunt a set of mobs until loot requirements are satisfied.

    Args:
        mob_types: Substrings of entity types to target
        required_loot: Dict of item_id -> count
        search_radius: Radius to scan for mobs
        timeout: Maximum duration
        heal_threshold: Auto-heal when health drops below this value
        target_kills: Optional kill target to stop early even if loot collected
        abort_on_other_hostiles: Stop passive hunting when another hostile is close
        latest_world_time: Stop early enough to return before sunset
        max_distance_from_origin: Bound expedition radius around its start
        exploration_center: Explicit X/Z center that leads away from owned structures
        max_kills: Hard safety cap, even when requested loot is still missing
        no_retreat: Commit to the selected target instead of disengaging mid-kill
        recover_after_combat: Stabilize health before moving to drops or a new target
        recovery_anchor: Known route back outside a hostile spawn radius
    """
    start = time.time()
    baseline = {item: count_item(client, item) for item in required_loot}
    kills = 0
    start_state = client.transport.dispatch("get_state", {})
    start_pos = start_state.get("block_position", start_state.get("position", {}))
    origin = (
        float(start_pos.get("x", 0)),
        float(start_pos.get("z", 0)),
    )

    def _missing() -> Dict[str, int]:
        missing: Dict[str, int] = {}
        for item_id, count in required_loot.items():
            current = count_item(client, item_id) - baseline.get(item_id, 0)
            if current < count:
                missing[item_id] = count - max(current, 0)
        return missing

    missing = _missing()
    if not missing and not target_kills:
        return TaskResult.ok("Already satisfied", kills=kills, missing={})

    exploration = PassiveHuntNavigation()
    last_time_check = 0
    while (missing or (target_kills and kills < target_kills)) and time.time() - start < timeout:
        if max_kills is not None and kills >= max(0, int(max_kills)):
            break
        live_state = client.transport.dispatch("get_state", {})
        day_time = int(live_state.get("world_time", 0)) % 24000
        if latest_world_time is not None and day_time >= int(latest_world_time):
            exploration.stop(client)
            client.transport.dispatch("cancel", {})
            return TaskResult.fail(
                "Daylight return boundary reached",
                missing=missing,
                kills=kills,
            )
        if max_distance_from_origin is not None:
            live_pos = live_state.get(
                "block_position", live_state.get("position", {})
            )
            distance_from_origin = (
                (float(live_pos.get("x", 0)) - origin[0]) ** 2
                + (float(live_pos.get("z", 0)) - origin[1]) ** 2
            ) ** 0.5
            if distance_from_origin > float(max_distance_from_origin):
                exploration.stop(client)
                client.transport.dispatch("cancel", {})
                return TaskResult.fail(
                    "Passive-hunt expedition radius reached",
                    missing=missing,
                    kills=kills,
                    distance=distance_from_origin,
                )
        food_level = int(live_state.get("food_level", live_state.get("food", 20)))
        if food_level <= 10:
            client.transport.dispatch("cancel", {})
            exploration.reset()
            if eat_until_hunger(client, minimum_food=14):
                continue
            # No carried food and none of the hunted mobs can supply it
            # (e.g. blaze/enderman/spider hunts): keep the original safety
            # net and stop rather than risk continued combat while hungry.
            hunting_food_animal = any(
                food_mob in mob_type.lower()
                for mob_type in mob_types
                for food_mob in _FOOD_YIELDING_MOBS
            )
            if not hunting_food_animal or food_level <= 3:
                return TaskResult.fail(
                    "Hunt stopped because hunger could not be stabilized",
                    missing=missing,
                    kills=kills,
                    food_level=food_level,
                )
            # These mob types drop raw meat on death, so hunting them is
            # itself the path back to food -- aborting here deadlocked Bot07
            # forever, stuck retrying "Gather 46 leather" with nothing to eat.
            print(
                f"  HUNGER: no carried food at {food_level}; continuing hunt "
                "to find some"
            )
        # Check for night every 10 seconds
        if time.time() - last_time_check > 10:
            last_time_check = time.time()
            state = live_state
            if day_time >= 13000:
                 print("  Night detected! Aborting hunt.")
                 exploration.stop(client)
                 return TaskResult.fail("Night detected")

        heal_if_needed(client, threshold=heal_threshold)
        if abort_on_other_hostiles:
            nearby = get_nearby_entities(client, radius=12)
            threat = find_unrequested_hostile(nearby, mob_types)
            if threat is not None:
                client.transport.dispatch("chat", {"message": "#stop"})
                client.transport.dispatch("cancel", {})
                from .hunt_safety import resolve_intruding_hostile
                resolve_intruding_hostile(client)
                return TaskResult.fail(
                    f"Hostile {threat.get('type')} entered passive-hunt radius",
                    kills=kills,
                    missing=missing,
                )
        try:
            entity = find_entity_by_type(
                client, mob_types, radius=search_radius, raise_on_error=True
            )
        except EntityQueryError as exc:
            # A bridge query failure is NOT an empty area -- do not "explore"
            # away from mobs that may be right here; back off instead. Bot07
            # once hunted for minutes with a cow three blocks away this way.
            print(f"  Entity query failed (bridge issue: {exc}); pausing hunt scan.")
            exploration.stop(client)
            time.sleep(3)
            continue

        if entity is None:
            failure = exploration.advance(
                client,
                live_state,
                exploration_center,
                enabled=explore_when_empty,
                navigate=goto_xz,
            )
            if failure:
                return TaskResult.fail(failure, missing=missing, kills=kills)
            time.sleep(3)
            continue

        if exploration.active:
             print("  Target found! Stopping exploration.")
             exploration.stop(client)
             time.sleep(0.5)

        target_id = entity.get("id")
        if target_id is None:
            time.sleep(1)
            continue
            
        target_pos = entity_position(entity)
        target_purpose = (
            "hostile_hunt"
            if assess_threats([entity], live_state)
            else "passive_hunt"
        )
        defeated = safe_combat(
            client,
            target_id,
            retreat_health=heal_threshold - 2,
            max_duration=40,
            abort_on_other_hostiles=abort_on_other_hostiles,
            tracking_radius=search_radius,
            purpose=target_purpose,
            source="hunt_mobs",
            target_metadata=entity,
            no_retreat=no_retreat,
        )
        if recover_after_combat:
            recovery_failure = _post_hunt_recovery(
                client, heal_threshold, missing, kills, recovery_anchor
            )
            if recovery_failure is not None:
                return recovery_failure
        if defeated:
            kills += 1
            # Entity death can be detected while the player is still near the
            # edge of melee range.  Step onto the last known position after
            # the pickup delay so leather/food is not left behind while the
            # next target is selected.
            if target_pos is not None:
                time.sleep(0.6)
                goto(
                    client,
                    int(target_pos[0]),
                    int(target_pos[1]),
                    int(target_pos[2]),
                    timeout=10,
                    check_interval=0.25,
                    tolerance=0.75,
                )
                time.sleep(0.4)
            missing = _missing()
        else:
            heal_if_needed(client, threshold=heal_threshold)
        time.sleep(1)

    gained = {
        item: max(0, count_item(client, item) - baseline.get(item, 0))
        for item in required_loot
    }

    if missing:
        return TaskResult.fail(
            "Failed to acquire required loot",
            missing=missing,
            kills=kills,
            gained=gained,
            duration=time.time() - start,
        )

    return TaskResult.ok(
        "Loot collection complete",
        missing={},
        kills=kills,
        gained=gained,
        duration=time.time() - start,
    )


def scan_for_threats(
    client,
    radius: int = 16,
    *,
    raise_on_error: bool = False,
    player_state: Optional[Dict] = None,
) -> List[Dict]:
    """Return active hostile entities ordered by assessed danger.

    The return type remains a list of bridge entity dictionaries for existing
    callers.  Classification and ordering come from the canonical policy in
    :mod:`baritone_client.common.defense`.
    """
    nearby = get_nearby_entities(
        client,
        radius,
        raise_on_error=raise_on_error,
    )
    return [item.entity for item in assess_threats(nearby, player_state)]


def _get_combat_snapshot(client, radius: int = 16) -> Optional[Dict]:
    """Use the atomic bridge observation when available.

    Bridge 1.0.23 and older do not expose this route.  Treat an absent or
    malformed response as a capability miss and retain the established
    get_state/get_entities path.
    """
    try:
        snapshot = client.transport.dispatch(
            "get_combat_snapshot",
            {"radius": radius},
        )
    except Exception as exc:
        logger.debug("Atomic combat snapshot unavailable: %s", exc)
        return None
    if not isinstance(snapshot, dict):
        return None
    if not isinstance(snapshot.get("player"), dict):
        return None
    if not isinstance(snapshot.get("entities"), list):
        return None
    return snapshot


def _snapshot_skipped_count(snapshot: Optional[Dict]) -> int:
    """Return incomplete atomic-serialization count; legacy snapshots are complete."""
    if not isinstance(snapshot, dict) or "skipped_count" not in snapshot:
        return 0
    raw_count = snapshot.get("skipped_count")
    try:
        skipped = int(raw_count)
    except (TypeError, ValueError):
        return 1
    if isinstance(raw_count, float) and not raw_count.is_integer():
        return 1
    return skipped if skipped >= 0 else 1


def secure_recovery_area(
    client,
    timeout: float = 90.0,
    clear_seconds: float = 10.0,
    radius: int = 16,
) -> bool:
    """Protect a recovered inventory until the surrounding area is stable.

    Death drops are commonly guarded by the mob that killed the player.  Do
    not hand control back to a long-running phase immediately after pickup,
    and do not begin slow block placement while a threat can approach.  Keep
    combat supervision attached until there have been ``clear_seconds`` of
    daylight with no nearby hostiles.
    """
    deadline = time.time() + timeout
    clear_since: Optional[float] = None

    while time.time() < deadline:
        try:
            state = client.transport.dispatch("get_state", {})
        except TransportError:
            # If the bridge times out while securing, keep the area safe and
            # continue polling rather than immediately failing recovery.
            clear_since = None
            time.sleep(0.5)
            continue
        if float(state.get("health", 20) or 0) <= 0:
            print("RECOVERY: player died while securing the pickup area")
            return False

        threats = scan_for_threats(client, radius=radius)
        if threats:
            clear_since = None
            defend_or_flee(client)
            continue

        now = time.time()
        clear_since = clear_since or now
        world_time = int(state.get("world_time", 0)) % 24000
        if world_time < 12000 and now - clear_since >= clear_seconds:
            print("RECOVERY: pickup area is clear and stable")
            return True
        time.sleep(0.5)

    print("RECOVERY: pickup area did not remain safely clear")
    return False


def _defense_runtime(client) -> DefenseRuntime:
    """Return state attached to the client without module-global bot mixing."""
    runtime = getattr(client, "_mcbaratone_defense_runtime", None)
    if runtime is None:
        runtime = getattr(client.transport, "_mcbaratone_defense_runtime", None)
    if isinstance(runtime, DefenseRuntime):
        return runtime
    runtime = DefenseRuntime()
    try:
        setattr(client, "_mcbaratone_defense_runtime", runtime)
    except Exception:
        # A few client doubles use slots; their transport remains session-local.
        setattr(client.transport, "_mcbaratone_defense_runtime", runtime)
    return runtime


def _stop_for_defense(client) -> None:
    """Cancel both Baritone command and transport-level work."""
    client.transport.dispatch("chat", {"message": "#stop"})
    client.transport.dispatch("cancel", {})


def _relocate_away_from(
    client,
    threat: Dict,
    *,
    distance: int = 28,
    timeout: int = 30,
) -> bool:
    from .escape_recovery import relocate_away_from

    return relocate_away_from(
        client,
        threat,
        distance=distance,
        timeout=timeout,
    )


def _escape_destination_safe(client, x: int, y: int, z: int) -> bool:
    from .escape_recovery import destination_safe

    return destination_safe(client, x, y, z)


def _separation_from(entity: Dict, player_position: Dict) -> float:
    from .escape_recovery import separation_from

    return separation_from(entity, player_position)


def _verify_escape(
    client,
    threat_id: Optional[int],
    initial_distance: float,
    *,
    timeout: float,
    minimum_gain: float,
) -> bool:
    from .escape_recovery import verify_escape

    return verify_escape(
        client,
        threat_id,
        initial_distance,
        timeout=timeout,
        minimum_gain=minimum_gain,
    )


def run_away(
    client,
    threat: Dict,
    *,
    timeout: float = 6.0,
    minimum_gain: float = 5.0,
) -> bool:
    from .escape_recovery import run_away as implementation

    return implementation(
        client,
        threat,
        timeout=timeout,
        minimum_gain=minimum_gain,
    )


def ensure_alive(client, state: Optional[Dict] = None) -> bool:
    """Raise when dead so only top-level death recovery may respawn."""
    try:
        if state is None:
            state = client.transport.dispatch("get_state", {})
        health = state.get("health", 20)
        if state.get("is_dead", False) or (
            health is not None and float(health) <= 0
        ):
            raise PlayerDeathDetected(
                "Player died during combat or health recovery"
            )
    except PlayerDeathDetected:
        raise
    except Exception as e:
        print(f"DEATH: ensure_alive check failed: {e}")
    return False


def _fight_defensive_target(client, target: Dict, **kwargs) -> bool:
    """Run melee while consistently labeling a defensive target."""
    return safe_combat(
        client,
        target.get("id"),
        purpose="hostile_defense",
        source="defend_or_flee",
        target_metadata=target,
        **kwargs,
    )


def _handle_defense_recovery(
    client,
    decision: DefenseDecision,
    *,
    allow_safe_recovery_movement: bool,
) -> bool:
    """Pause to recover unless clear-area movement is the recovery action."""
    if allow_safe_recovery_movement and decision.primary is None:
        client._last_defense_intervention = "recovery_movement"
        return False
    print(f"DEFENSE: Recovery mode ({decision.reason})")
    _stop_for_defense(client)
    # Wait for the bite; restarting asynchronous use each tick prevents
    # hunger recovery and therefore natural regeneration.
    if not eat_until_hunger(client, minimum_food=18):
        heal_if_needed(client, threshold=12.0)
    return True


def _choose_supervised_defense(
    client,
    assessments,
    *,
    health: float,
    armor_count: int,
    runtime: DefenseRuntime,
):
    """Choose defense with the narrow shielded-blaze live override."""
    has_weapon = False
    shielded_blaze = False
    if assessments:
        primary = assessments[0]
        urgent_count = sum(item.distance <= 10.0 for item in assessments)
        urgent = [item for item in assessments if item.distance <= 10.0]
        shielded_blaze = (
            1 <= len(urgent) <= 2
            and all(item.entity_type == "blaze" for item in urgent)
            and health >= 12.0
            and armor_count >= 4
            and has_durable_full_armor(client, minimum_remaining=16)
            and count_item(client, "minecraft:shield") > 0
        )
        if (
            (health >= 16.0 or shielded_blaze)
            and armor_count >= 3
            and primary.distance <= 10.0
            and (urgent_count <= 1 or shielded_blaze)
            and (not primary.always_evade or shielded_blaze)
        ):
            try:
                has_weapon = equip_best_weapon(client, primary.entity_type)
            except TypeError:  # compatibility with injected one-argument fakes
                has_weapon = equip_best_weapon(client)
    decision = choose_defense_action(
        assessments,
        health=health,
        armor_count=armor_count,
        has_weapon=has_weapon,
        runtime=runtime,
    )
    if shielded_blaze and has_weapon:
        decision = DefenseDecision(
            DefenseMode.ENGAGE,
            "full armor and shield against one close blaze",
            assessments[0],
        )
    return decision, shielded_blaze


def defend_or_flee(
    client,
    *,
    allow_safe_recovery_movement: bool = False,
    observed_snapshot: Optional[Dict] = None, _skip_airborne_recheck: bool = False,
) -> bool:
    """Advance the canonical defensive state machine by one supervised tick."""
    client._last_defense_intervention = None
    snapshot = (
        observed_snapshot
        if observed_snapshot is not None
        else _get_combat_snapshot(client)
    )
    state = (
        snapshot.get("player", {})
        if snapshot is not None
        else client.transport.dispatch("get_state", {})
    )
    ensure_alive(client, state)
    runtime = _defense_runtime(client)
    skipped = _snapshot_skipped_count(snapshot)
    if skipped:
        client._last_defense_intervention = "incomplete_snapshot"
        runtime.transition(
            DefenseMode.ALERT,
            f"incomplete combat snapshot ({skipped} skipped)",
        )
        combat_telemetry.record_combat_action(
            client,
            "defense_intervention",
            outcome="incomplete_snapshot",
            skipped_count=skipped,
        )
        _stop_for_defense(client)
        return True
    if escape_water_if_submerged(client, state):
        client._last_defense_intervention = "aquatic"
        return True
    health = float(state.get("health", 20) or 0)
    try:
        if snapshot is not None:
            threats = [
                item.entity
                for item in assess_threats(snapshot["entities"], state)
            ]
        else:
            threats = scan_for_threats(
                client,
                raise_on_error=True,
                player_state=state,
            )
    except TypeError:
        # Preserve compatibility with existing callers/tests that replace the
        # old one-argument scanner.
        threats = scan_for_threats(client)
    except EntityQueryError as exc:
        runtime.transition(DefenseMode.ALERT, f"entity query unavailable: {exc}")
        _stop_for_defense(client)
        return True

    assessments = exclude_authorized_threats(
        client, assess_threats(threats, state)
    )
    threats = [item.entity for item in assessments]
    armor_count = (
        int(state.get("armor_count", 0) or 0)
        if assessments and "armor_count" in state
        else len(get_equipped_armor(client)) if assessments else 0
    )
    decision, shielded_blaze = _choose_supervised_defense(
        client,
        assessments,
        health=health,
        armor_count=armor_count,
        runtime=runtime,
    )
    from .sheltered_alert import hold_sheltered_alert
    decision = hold_sheltered_alert(client, decision, state, assessments)
    runtime.transition(decision.mode, decision.reason)
    combat_telemetry.record_defense_decision(client, decision, state, threats)

    if decision.mode == DefenseMode.CLEAR:
        return False
    if decision.mode == DefenseMode.ALERT:
        return False
    if decision.mode == DefenseMode.RECOVER:
        return _handle_defense_recovery(
            client,
            decision,
            allow_safe_recovery_movement=allow_safe_recovery_movement,
        )
    if decision.primary is None:
        return False

    primary = decision.primary
    print(
        f"DEFENSE: {decision.mode.value} {primary.entity.get('type')} "
        f"at {primary.distance:.1f}m ({decision.reason}; score={primary.score:.1f})"
    )
    _stop_for_defense(client)
    if decision.mode == DefenseMode.EVADE:
        if not _skip_airborne_recheck and handle_transient_airborne_creeper(
            client, snapshot, primary, assessments, runtime, defend_or_flee,
            allow_safe_recovery_movement):
            return True
        threat_id = primary.entity.get("id")
        escaped = run_away(client, primary.entity)
        runtime.record_evade_result(threat_id, escaped)
        if escaped:
            runtime.hold_recovery(8.0)
            return True
        from .cornered_defense import fight_if_no_escape
        if fight_if_no_escape(
            client,
            primary,
            runtime,
            equip_weapon=equip_best_weapon, fight=_fight_defensive_target,
        ):
            return True
        if not runtime.should_escalate_to_combat(threat_id):
            # The attacker is still present. Recovery hysteresis after a
            # failed escape made the bot stand still and try to heal while it
            # was being hit; retry defense immediately on the next tick.
            return True
        from .cornered_defense import handle_non_engageable_escape_failure

        if handle_non_engageable_escape_failure(
            client,
            primary,
            assessments,
            runtime,
            relocate=_relocate_away_from,
            fight=_fight_defensive_target,
        ):
            return True
        # Failed evasion permits in-place last-resort defense, not pursuit.
        print(
            f"DEFENSE: evasion failed {runtime.evade_failures}x against "
            f"{primary.entity.get('type')}; defending in place as a last resort"
        )
        # Allow adjacent strikes even below the ordinary retreat floor.
        defeated = fight_without_pursuit(
            client,
            _fight_defensive_target,
            primary.entity,
            no_retreat=True,
            # Reaching this branch proves that escape has already failed.
            # Aborting because another hostile is nearby simply recreated the
            # same no-op loop in crowded caves.
            abort_on_other_hostiles=False,
        )
        if defeated:
            runtime.record_evade_result(threat_id, True)
            runtime.hold_recovery(6.0)
        return True

    defeated = _fight_defensive_target(
        client,
        primary.entity,
        **(
            {"no_retreat": True, "abort_on_other_hostiles": False}
            if shielded_blaze
            else {"retreat_health": 12.0, "abort_on_other_hostiles": True}
        ),
    )
    if not defeated:
        escape_target = primary.entity
        latest = _get_combat_snapshot(client)
        if latest is not None:
            updated = exclude_authorized_threats(
                client,
                assess_threats(latest["entities"], latest["player"]),
            )
            if updated:
                escape_target = updated[0].entity
        run_away(client, escape_target)
    runtime.hold_recovery(6.0 if defeated else 10.0)
    return True
