"""Focused helpers for bounded emergency-food recovery."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .aquatic_survival import head_block_is_water, player_is_in_water
from .movement_recovery import (
    ExplorationWaypoints,
    MovementWatchdog,
    block_position,
)
from .site_selection import surface_y_at
from .surface_egress import try_lower_surface_egress
from .tasks import PlayerDeathDetected
from .water_hazards import allow_water, avoid_water, avoiding_water, fenced_hazard


PRIMARY_LAND_FOOD = ("cow", "pig")
SECONDARY_LAND_FOOD = ("sheep", "chicken", "rabbit")
EMERGENCY_FOOD_ITEMS = (
    "minecraft:cooked_beef",
    "minecraft:cooked_porkchop",
    "minecraft:cooked_chicken",
    "minecraft:cooked_mutton",
    "minecraft:cooked_rabbit",
    "minecraft:bread",
    "minecraft:apple",
    "minecraft:cooked_salmon",
    "minecraft:cooked_cod",
    "minecraft:baked_potato",
    "minecraft:potato",
    "minecraft:carrot",
    "minecraft:beetroot",
    "minecraft:golden_carrot",
    "minecraft:golden_apple",
    "minecraft:beef",
    "minecraft:porkchop",
    "minecraft:chicken",
    "minecraft:mutton",
    "minecraft:rabbit",
    "minecraft:salmon",
    "minecraft:cod",
    "minecraft:rotten_flesh",
)


def emergency_food_count(client: Any) -> int:
    """Count all carried items usable by emergency recovery."""
    from .inventory import count_item

    return sum(count_item(client, item_id) for item_id in EMERGENCY_FOOD_ITEMS)


def craft_emergency_bread_from_carried_wheat(
    client: Any,
    *,
    maximum_bread: int = 6,
    minimum_reserve: Optional[int] = None,
) -> bool:
    """Turn carried wheat into enough bread for a verified food reserve."""
    from .inventory import count_item

    maximum_bread = max(1, int(maximum_bread))
    required_reserve = (
        None if minimum_reserve is None else max(1, int(minimum_reserve))
    )
    reserve = emergency_food_count(client)
    if reserve > 0 and required_reserve is None:
        return True
    if required_reserve is not None and reserve >= required_reserve:
        return True
    wheat = count_item(client, "minecraft:wheat")
    bread_to_craft = min(maximum_bread, wheat // 3)
    if required_reserve is not None:
        bread_to_craft = min(bread_to_craft, required_reserve - reserve)
    if bread_to_craft <= 0:
        return False
    bread_target = count_item(client, "minecraft:bread") + bread_to_craft
    try:
        # Lazy import avoids emergency_food <-> resources initialization cycles.
        from .resources import _craft_with_table
        from .enclosed_workstation import sheltered_bread_table

        restricted, table = sheltered_bread_table(client)
        if restricted and table is None:
            return False
        options = {"table_pos": table} if restricted else {}
        if not _craft_with_table(client, "minecraft:bread", bread_target, **options):
            return False
    except PlayerDeathDetected:
        raise
    except Exception as exc:
        print(f"RECOVERY: carried-wheat bread craft failed ({exc})")
        return False
    finally:
        # Crafting leaves a container screen open. A subsequent use_item acts
        # on that GUI instead of eating, so recovery can wait forever beside a
        # full bread stack unless the shared helper closes it explicitly.
        try:
            client.transport.dispatch("close_screen", {})
        except Exception:
            pass
    reserve = emergency_food_count(client)
    if required_reserve is None and reserve <= 0:
        return False
    if required_reserve is not None and reserve < required_reserve:
        return False
    print(
        f"RECOVERY: crafted carried wheat into {bread_to_craft} emergency bread"
    )
    return True


def prepare_carried_wheat_recovery(
    client: Any,
    state: Dict,
    minimum_food: int,
    scan_threats: Callable,
    threat_can_reach: Callable,
    eat_until_hunger: Callable,
    recovery_complete: Callable,
) -> bool:
    """Craft and eat carried wheat when the immediate area is safe."""
    from .inventory import count_item

    if emergency_food_count(client) > 0 or count_item(client, "minecraft:wheat") < 3:
        return False
    threats = scan_threats(client, radius=16, player_state=state)
    if any(
        threat.get("distance", 999) <= 12 and threat_can_reach(threat, state)
        for threat in threats
    ):
        return False
    if not craft_emergency_bread_from_carried_wheat(client):
        return False
    eat_until_hunger(client, minimum_food=minimum_food)
    return bool(recovery_complete())


def wait_for_safe_regeneration(
    client: Any,
    state: Dict,
    minimum_health: float,
    scan_threats: Callable,
    threat_can_reach: Callable,
    stop_exploring: Callable,
) -> bool:
    """Hold a fed, wounded player still when no reachable threat is nearby."""
    health = float(state.get("health", 20) or 0)
    if "food_level" not in state and "food" not in state:
        return False
    food = int(state.get("food_level", state.get("food", 20)) or 0)
    if health >= minimum_health or food < 18:
        return False
    threats = scan_threats(client, radius=16, player_state=state)
    if any(
        threat.get("distance", 999) <= 12 and threat_can_reach(threat, state)
        for threat in threats
    ):
        return False
    stop_exploring()
    print("RECOVERY: food secured; holding for natural regeneration")
    return True
# Tropical fish has no food value. Pufferfish is deliberately excluded
# because it poisons the player.

# Only fish that actually feed the player. tropical_fish was listed here and
# is NOT edible in Minecraft -- hunting one can never raise hunger, so bots in
# lush caves (whose only fish are tropical fish) chased them indefinitely at
# food 0. Pufferfish is excluded as edible-but-poisonous.
WATER_FOOD = ("salmon", "cod")
MINIMUM_FOOD_SEARCH_Y = 55
EXPECTED_SURFACE_Y = 63
MINIMUM_IMMEDIATE_AQUATIC_HUNT_HEALTH = 12.0
MAX_IMMEDIATE_AQUATIC_FOOD_DISTANCE = 4.5
MAX_RECENT_DRY_ANCHOR_DISTANCE = 96.0


def _expected_food_search_y(
    client: Any,
    position: tuple[int, int, int],
) -> int:
    """Estimate the real terrain surface above a cave-bound player."""
    observed = surface_y_at(
        client,
        position[0],
        position[2],
        top=max(120, position[1] + 64),
        bottom=max(-64, position[1] - 32),
    )
    if observed is None:
        return EXPECTED_SURFACE_Y
    return max(EXPECTED_SURFACE_Y, int(observed))


def _is_dry_food_search_surface(
    client: Any,
    position: tuple[int, int, int],
) -> bool:
    """Require dry footing near the top-down terrain surface."""
    from .surface_recovery import position_is_aquatic

    state = {
        "block_position": {
            "x": position[0],
            "y": position[1],
            "z": position[2],
        }
    }
    return (
        position[1] >= _expected_food_search_y(client, position) - 3
        and not position_is_aquatic(client, position)
        and not head_block_is_water(client, state)
    )


def _is_safe_immediate_aquatic_target(
    state: Dict,
    target: Optional[Dict],
) -> bool:
    """Allow only a healthy, already-in-reach fish target while submerged."""
    if target is None:
        return False
    try:
        distance = float(target.get("distance", float("inf")))
        health = float(state.get("health", 0.0))
    except (TypeError, ValueError):
        return False
    return (
        health >= MINIMUM_IMMEDIATE_AQUATIC_HUNT_HEALTH
        and 0.0 <= distance <= MAX_IMMEDIATE_AQUATIC_FOOD_DISTANCE
    )


def _remember_dry_food_anchor(
    client: Any,
    position: tuple[int, int, int],
) -> None:
    """Keep a controller-local escape point for the current food attempt."""
    client._emergency_food_dry_anchor = tuple(int(value) for value in position)


def return_to_food_search_anchor(
    client: Any,
    anchor: tuple[float, float, float],
    *,
    timeout: float = 120.0,
) -> bool:
    """Return a bounded search to its checkpoint-backed dry home anchor."""
    target = tuple(int(round(value)) for value in anchor)
    state = client.transport.dispatch("get_state", {})
    current = block_position(state)
    distance = sum(
        (current[index] - target[index]) ** 2 for index in range(3)
    ) ** 0.5
    if distance <= 3.0 and _is_dry_food_search_surface(client, current):
        _remember_dry_food_anchor(client, current)
        return True

    print(
        "RECOVERY: returning exhausted food search to stable home anchor "
        f"{target}"
    )
    from .navigation import recovery_goto

    reached = recovery_goto(
        client,
        target[0],
        target[1],
        target[2],
        timeout=max(1, int(timeout)),
        check_interval=0.5,
        tolerance=3.0,
    )
    if not reached:
        print("RECOVERY: stable food-search anchor was not safely reachable")
        return False
    refreshed = client.transport.dispatch("get_state", {})
    arrived = block_position(refreshed)
    if not _is_dry_food_search_surface(client, arrived):
        print(
            "RECOVERY: rejected non-dry arrival at stable food-search anchor "
            f"{arrived}"
        )
        return False
    _remember_dry_food_anchor(client, arrived)
    print(f"RECOVERY: returned to stable food-search anchor {arrived}")
    return True


def _return_to_recent_dry_food_anchor(
    client: Any,
    state: Dict,
    origin: tuple[int, int, int],
    *,
    timeout: float = 30.0,
) -> bool:
    """Make one health-monitored return to recently verified dry ground."""
    raw_anchor = getattr(client, "_emergency_food_dry_anchor", None)
    if not isinstance(raw_anchor, (list, tuple)) or len(raw_anchor) != 3:
        return False
    try:
        anchor = tuple(int(value) for value in raw_anchor)
    except (TypeError, ValueError):
        return False
    distance = (
        (anchor[0] - origin[0]) ** 2 + (anchor[2] - origin[2]) ** 2
    ) ** 0.5
    if distance > MAX_RECENT_DRY_ANCHOR_DISTANCE:
        return False
    if not _is_dry_food_search_surface(client, anchor):
        return False

    initial_health = float(state.get("health", 0.0) or 0.0)
    if (
        initial_health < MINIMUM_IMMEDIATE_AQUATIC_HUNT_HEALTH
        and distance > 32.0
    ):
        return False
    print(
        "RECOVERY: returning from water to recent dry food-search anchor "
        f"{anchor}"
    )
    from .surface_recovery import _configure_surface_pathing

    _configure_surface_pathing(client)
    client.transport.dispatch(
        "goto",
        {"x": anchor[0], "y": anchor[1], "z": anchor[2]},
    )
    deadline = time.monotonic() + max(0.0, float(timeout))
    idle_checks = 0
    try:
        while time.monotonic() < deadline:
            current_state = client.transport.dispatch("get_state", {})
            health = float(current_state.get("health", 0.0) or 0.0)
            if (
                current_state.get("is_dead", current_state.get("dead", False))
                or health <= 0.0
                or health < initial_health - 0.5
            ):
                print(
                    "RECOVERY: dry-anchor return lost health; "
                    "aborting before another drowning cycle"
                )
                return False
            current = block_position(current_state)
            current_distance = sum(
                (current[index] - anchor[index]) ** 2
                for index in range(3)
            ) ** 0.5
            if (
                current_distance <= 3.0
                and _is_dry_food_search_surface(client, current)
            ):
                _remember_dry_food_anchor(client, current)
                print(f"RECOVERY: returned to dry food-search anchor {current}")
                return True
            if current_state.get("is_pathing", True):
                idle_checks = 0
            else:
                idle_checks += 1
                if idle_checks >= 2:
                    return False
            time.sleep(0.5)
    finally:
        client.transport.dispatch("cancel", {})
    return False


def reach_food_search_surface(client: Any, state: Dict) -> bool:
    """Reach dry Overworld surface terrain before blind food exploration."""
    position = block_position(state)
    dimension = state.get("dimension", "minecraft:overworld")
    if dimension != "minecraft:overworld":
        return True
    expected_y = _expected_food_search_y(client, position)
    in_water = player_is_in_water(client, state) or head_block_is_water(
        client, state
    )
    if position[1] >= expected_y - 3 and not in_water:
        _remember_dry_food_anchor(client, position)
        return True

    if in_water:
        # Every fallback below (anchor return, dry-surface search, then
        # excavation) is bounded by navigation timeouts from several seconds
        # to 90s+, none of which track the ~15-35s a player actually has
        # before drowning kills them. Live A1 2026-09-06/07: a bot already
        # submerged here spent 67s retrying reach_dry_surface ->
        # excavate_surface_egress across three different target columns and
        # drowned mid-attempt. Surface for air first, on the same fast,
        # bounded budget the rest of the codebase already uses for an active
        # drowning emergency, before spending any more time picking a good
        # spot to search for food from.
        from . import combat as api

        if api._surface_after_aquatic_hunt(client, timeout=12.0):
            state = client.transport.dispatch("get_state", {})
            position = block_position(state)
            expected_y = _expected_food_search_y(client, position)
            in_water = player_is_in_water(client, state) or head_block_is_water(
                client, state
            )
            if position[1] >= expected_y - 3 and not in_water:
                _remember_dry_food_anchor(client, position)
                return True

    if in_water and _return_to_recent_dry_food_anchor(
        client,
        state,
        position,
    ):
        return True

    if (
        in_water
        and float(state.get("health", 20.0) or 0.0)
        < MINIMUM_IMMEDIATE_AQUATIC_HUNT_HEALTH
    ):
        print(
            "RECOVERY: critical health and no reachable dry anchor; "
            "refusing a prolonged surface excavation"
        )
        return False

    # A fish already within melee reach can be a bounded food action. Never
    # turn this exception into an underwater approach: live Bot14 telemetry
    # showed full health collapse between 15-second samples while following
    # cod and salmon 20-28 blocks through a cold ocean.
    if in_water and position[1] >= MINIMUM_FOOD_SEARCH_Y:
        from . import combat as api

        nearby_fish = api.find_entity_by_type(
            client,
            list(WATER_FOOD),
            radius=int(MAX_IMMEDIATE_AQUATIC_FOOD_DISTANCE) + 1,
        )
        if _is_safe_immediate_aquatic_target(state, nearby_fish):
            print(
                "RECOVERY: healthy and submerged with immediate "
                f"{str(nearby_fish.get('type','fish')).split(':')[-1]} is "
                f"{float(nearby_fish.get('distance', 0)):.1f}m away; allowing "
                "one bounded aquatic food action"
            )
            return True

    from .build_site_recovery import excavate_surface_egress
    from .navigation import recovery_goto
    from .surface_recovery import reach_dry_surface

    reason = "submerged" if in_water else f"underground at y={position[1]}"
    print(
        f"RECOVERY: emergency food search is {reason}; "
        "reaching dry surface before exploration"
    )
    recovered = reach_dry_surface(
        client,
        origin=position,
        expected_y=expected_y,
        goto=recovery_goto,
    )
    if recovered is not None and not _is_dry_food_search_surface(
        client, recovered
    ):
        print(
            "RECOVERY: rejected low cave ledge as emergency-food surface "
            f"at {recovered}"
        )
        recovered = None
    if recovered is None:
        recovered = excavate_surface_egress(
            client,
            origin=position,
            expected_y=expected_y,
        )
    if recovered is None or not _is_dry_food_search_surface(client, recovered):
        return False
    _remember_dry_food_anchor(client, recovered)
    return True


def prepare_food_search_state(client: Any, state: Dict) -> Optional[Dict]:
    """Return refreshed surface state, or fail closed when ascent is impossible."""
    if not reach_food_search_surface(client, state):
        print(
            "RECOVERY: could not reach safe surface terrain for emergency "
            "food search"
        )
        return None
    return client.transport.dispatch("get_state", {})


def enforce_dry_food_search_state(
    client: Any,
    state: Dict,
    stop_exploring: Callable[[], None],
) -> bool:
    """Refresh ``state`` on dry ground if exploration entered water."""
    if not (
        player_is_in_water(client, state)
        or head_block_is_water(client, state)
    ):
        return True
    stop_exploring()
    refreshed = prepare_food_search_state(client, state)
    if refreshed is None:
        return False
    state.clear()
    state.update(refreshed)
    return True


def select_target(
    client: Any,
    nearby: List[Dict],
    *,
    current_food: int,
    elapsed: float,
    timeout: float,
    renewable_source_callback: Optional[
        Callable[[str, tuple[int, int, int]], None]
    ],
    in_water: bool = False,
    aquatic_search_radius: int = 64,
    unreachable: Optional[set] = None,
) -> Optional[Dict]:
    """Choose a renewable land target, with immediate fish as fallback.

    ``in_water`` never lifts the aquatic distance bound: submerged bots must
    surface instead of following fish through open water.
    ``unreachable`` holds entity ids already proven unapproachable.
    """
    from . import combat as api

    blocked = unreachable or set()

    def usable(entity: Dict) -> bool:
        return entity.get("id") not in blocked

    for animal_type in PRIMARY_LAND_FOOD + SECONDARY_LAND_FOOD:
        group = [
            entity
            for entity in nearby
            if animal_type in str(entity.get("type", "")).lower()
            and not entity.get("is_baby", False)
            and usable(entity)
        ]
        if len(group) >= 3:
            positions = [api.entity_position(entity) for entity in group]
            positions = [position for position in positions if position is not None]
            if positions and renewable_source_callback is not None:
                centroid = tuple(
                    round(sum(position[axis] for position in positions) / len(positions))
                    for axis in range(3)
                )
                renewable_source_callback(animal_type, centroid)
            return min(group, key=lambda entity: float(entity.get("distance", 999)))
        if len(group) == 1 and current_food <= 8:
            return group[0]
        if len(group) == 2 and current_food <= 2:
            return min(group, key=lambda entity: float(entity.get("distance", 999)))

    def nearest_fish(radius: int) -> Optional[Dict]:
        # find_entity_by_type only ever returns the single nearest match, so a
        # blocked fish would otherwise mask every one behind it. Scan and pick
        # the nearest that is still viable.
        candidates = [
            entity
            for entity in api.get_nearby_entities(client, radius=radius)
            if any(
                water_type in str(entity.get("type", "")).lower()
                for water_type in WATER_FOOD
            )
            and usable(entity)
            and 0.0
            <= float(entity.get("distance", float("inf")))
            <= MAX_IMMEDIATE_AQUATIC_FOOD_DISTANCE
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda e: float(e.get("distance", 999)))

    if in_water:
        target = nearest_fish(
            min(
                int(MAX_IMMEDIATE_AQUATIC_FOOD_DISTANCE) + 1,
                max(1, int(aquatic_search_radius)),
            )
        )
        if target is not None:
            return target

    return None


@dataclass
class EmergencyExploration:
    """Own movement proof, waypoint rotation, and sprint restoration."""

    origin_x: float
    origin_z: float
    maximum_radius: float
    movement: MovementWatchdog = field(default_factory=MovementWatchdog)
    exploring: bool = False
    sprint_suppressed: bool = False
    surface_egress_attempted: bool = False
    waypoint_cursor: int = field(default=0, init=False)
    clear_of_fences: Optional[tuple] = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.waypoints = ExplorationWaypoints(
            self.origin_x, self.origin_z, self.maximum_radius
        )

    def resume_waypoint_rotation(self, client: Any) -> None:
        """Continue with the next direction across bounded recovery attempts."""
        try:
            cursor = int(getattr(client, "_emergency_food_waypoint_cursor", 0))
        except (TypeError, ValueError):
            cursor = 0
        self.waypoint_cursor = max(0, cursor)
        for _ in range(self.waypoint_cursor):
            self.waypoints.next()

    def start(self, client: Any, state: Dict) -> None:
        """Start or rotate toward a non-zero bounded exploration target."""
        if self.exploring:
            client.transport.dispatch("cancel", {})
            client.transport.dispatch("chat", {"message": "#stop"})
        for _ in range(8):  # Skip waypoints inside a drowning fence.
            target_x, target_z = self.waypoints.next()
            self.waypoint_cursor += 1
            if fenced_hazard(client, target_x, target_z) is None:
                break
        client._emergency_food_waypoint_cursor = self.waypoint_cursor
        print(
            "RECOVERY: no passive food source loaded; rotating bounded "
            f"daylight exploration toward ({target_x}, {target_z})"
        )
        # A starving bot must not swim: crossing water is how it drowned.
        avoid_water(client)
        client.transport.dispatch(
            "explore", {"x": target_x, "z": target_z}
        )
        self.exploring = True
        self.movement.reset(state)

    def observe(self, state: Dict) -> None:
        self.movement.observe(state)

    def needs_rotation(self, state: Dict) -> bool:
        return (
            not self.exploring
            or (
                self.movement.stalled(3)
                and not bool(state.get("is_pathing", False))
            )
            or self.movement.stalled(6)
        )

    def try_surface_egress(self, client: Any, state: Dict) -> bool:
        """Attempt a single local egress from a high shelf."""
        if self.surface_egress_attempted or block_position(state)[1] < 96:
            return False
        self.surface_egress_attempted = True
        egress = try_lower_surface_egress(
            client,
            state,
            attempt_limit=6,
            timeout_per_candidate=18.0,
        )
        if egress is None:
            return False
        self.movement.reset(
            {
                "block_position": {
                    "x": egress[0],
                    "y": egress[1],
                    "z": egress[2],
                }
            }
        )
        return True

    def suppress_sprint(self, client: Any) -> None:
        client.transport.dispatch("chat", {"message": "#set allowSprint false"})
        self.sprint_suppressed = True

    def stop(self, client: Any) -> None:
        if self.exploring:
            client.transport.dispatch("cancel", {})
            client.transport.dispatch("chat", {"message": "#stop"})
            self.exploring = False
        allow_water(client)
        if self.sprint_suppressed:
            client.transport.dispatch(
                "chat", {"message": "#set allowSprint true"}
            )
            self.sprint_suppressed = False


@avoiding_water
def hunt_target(
    client: Any,
    target: Dict,
    *,
    minimum_health: float,
    recovery_complete: Callable[[Optional[Dict]], bool],
    unreachable: Optional[set] = None,
) -> Optional[bool]:
    """Hunt one selected target; return True recovered, False failed, None retry.

    ``unreachable`` collects entity ids whose approach provably made no
    progress, so the caller stops re-selecting them.
    """
    from . import combat as api

    target_id = target.get("id")
    target_pos = api.entity_position(target)
    if target_id is None or target_pos is None:
        time.sleep(1)
        return None
    print(
        f"RECOVERY: safely hunting {target.get('type')} at "
        f"{target.get('distance', 999):.1f}m"
    )
    target_type = str(target.get("type", ""))
    aquatic = any(water_type in target_type for water_type in WATER_FOOD)
    if aquatic and float(target.get("distance", 999)) >= 4.5:
        # Scale the follow window to the distance. A flat 8s cannot cover the
        # ~48m separation measured live in a lush cave, so every attempt
        # reported "aquatic target could not be reached safely" and the bot
        # starved beside its only food source. Bounded so a fish that keeps
        # swimming away still ends the attempt. The drowning reflex inside
        # the follow loop remains the safety net for the longer swim.
        # Swimming is roughly 2.2 blocks/s, so a 46m target needs ~21s of pure
        # travel before any pathing overhead -- a 0.5x factor capped at 30s put
        # the deadline right on that edge and it still timed out live
        # ("aquatic target could not be reached safely" on every attempt).
        # Give the swim real margin.
        approach_timeout = min(
            45.0, max(8.0, float(target.get("distance", 8.0)) * 0.75)
        )
        if not api._approach_aquatic_food(
            client, target_id, target_type, timeout=approach_timeout
        ):
            print("RECOVERY: aquatic target could not be reached safely")
            # Without this the next loop re-selects the same nearest fish and
            # retries forever. Live: Bot07 cycled "hunting tropical_fish at
            # 46.4m" -> "could not be reached safely" indefinitely at 8 health
            # while other, reachable food went unconsidered.
            if unreachable is not None and target_id is not None:
                unreachable.add(target_id)
            if getattr(client, "_aquatic_surface_failed", False):
                print("RECOVERY: aborting aquatic hunt after failed surfacing")
                return False
            time.sleep(1)
            return None
    from .inventory import free_inventory_slots
    from .resources import manage_inventory

    if free_inventory_slots(client) < 2:
        manage_inventory(client, minimum_free_slots=2)
        if free_inventory_slots(client) < 2:
            print("RECOVERY: no verified inventory space for food drops")
            return False
    if not api.safe_combat(
        client,
        target_id,
        retreat_health=1.0,
        max_duration=35,
        abort_on_other_hostiles=True,
        tracking_radius=64,
        purpose="emergency_food",
        target_metadata=target,
    ):
        return False
    if aquatic:
        if not api._surface_after_aquatic_hunt(client):
            print("RECOVERY: could not prove breathing air after aquatic hunt")
            return False
    else:
        from .navigation import recovery_goto

        recovery_goto(
            client,
            int(target_pos[0]),
            int(target_pos[1]),
            int(target_pos[2]),
            timeout=45,
            tolerance=1.5,
        )
    time.sleep(2)
    api.heal_if_needed(client, threshold=minimum_health)
    api.recover_health(client, minimum_health=minimum_health, timeout=25.0)
    return True if recovery_complete(None) else None
