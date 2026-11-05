from types import SimpleNamespace

import pytest

from baritone_client.common import combat, combat_melee, inventory
from baritone_client.common.tasks import PlayerDeathDetected


class CombatTransport:
    def __init__(self, health=20.0):
        self.health = health
        self.calls = []

    def dispatch(self, route, payload):
        self.calls.append((route, payload))
        if route == "get_state":
            return {"health": self.health, "block_position": {"x": 0, "y": 64, "z": 0}}
        if route == "get_inventory":
            # This fixture represents a known empty inventory.  An omitted
            # inventory payload is malformed evidence under the strict reader.
            return {"inventory": [], "armor": [], "offhand": []}
        return {}


def test_low_health_defense_never_reengages_combat(monkeypatch):
    transport = CombatTransport(health=5.0)
    client = SimpleNamespace(transport=transport)
    threat = {
        "id": 9,
        "type": "minecraft:skeleton",
        "distance": 5.0,
        "position": {"x": 5, "y": 64, "z": 0},
    }
    attacked = []
    fled = []
    monkeypatch.setattr(combat, "scan_for_threats", lambda _client: [threat])
    monkeypatch.setattr(combat, "heal_if_needed", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(combat, "safe_combat", lambda *_args, **_kwargs: attacked.append(True))
    monkeypatch.setattr(combat, "run_away", lambda _client, target: fled.append(target))
    monkeypatch.setattr(combat.time, "sleep", lambda _seconds: None)

    assert combat.defend_or_flee(client)
    assert not attacked
    assert fled == [threat]
    assert ("chat", {"message": "#stop"}) in transport.calls


def test_clear_area_recovery_navigation_is_not_cancelled(monkeypatch):
    transport = CombatTransport(health=5.0)
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(
        combat,
        "_get_combat_snapshot",
        lambda *_args, **_kwargs: {
            "player": {
                "health": 5.0,
                "food_level": 10,
                "block_position": {"x": 0, "y": 64, "z": 0},
            },
            "entities": [],
        },
    )
    monkeypatch.setattr(
        combat,
        "eat_until_hunger",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("clear recovery travel must remain in control")
        ),
    )

    assert not combat.defend_or_flee(
        client,
        allow_safe_recovery_movement=True,
    )
    assert client._last_defense_intervention == "recovery_movement"
    assert not any(route in {"cancel", "chat"} for route, _ in transport.calls)


def test_distant_hostile_does_not_cancel_recovery_navigation(monkeypatch):
    transport = CombatTransport(health=5.0)
    client = SimpleNamespace(transport=transport)
    threat = {
        "id": 9,
        "type": "minecraft:skeleton",
        "distance": 13.5,
        "is_aggressive": False,
        "can_see_player": False,
        "position": {"x": 13.5, "y": 64, "z": 0},
    }
    monkeypatch.setattr(
        combat,
        "_get_combat_snapshot",
        lambda *_args, **_kwargs: {
            "player": {
                "health": 5.0,
                "food_level": 10,
                "block_position": {"x": 0, "y": 64, "z": 0},
            },
            "entities": [threat],
        },
    )
    monkeypatch.setattr(
        combat,
        "eat_until_hunger",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a non-actionable hostile must not stop recovery")
        ),
    )

    assert not combat.defend_or_flee(
        client,
        allow_safe_recovery_movement=True,
    )
    assert client._last_defense_intervention == "recovery_movement"
    assert not any(route in {"cancel", "chat"} for route, _ in transport.calls)


def test_recovery_navigation_still_intervenes_for_hostile(monkeypatch):
    transport = CombatTransport(health=5.0)
    client = SimpleNamespace(transport=transport)
    threat = {
        "id": 9,
        "type": "minecraft:skeleton",
        "distance": 5.0,
        "position": {"x": 5, "y": 64, "z": 0},
    }
    monkeypatch.setattr(
        combat,
        "_get_combat_snapshot",
        lambda *_args, **_kwargs: {
            "player": {
                "health": 5.0,
                "food_level": 10,
                "block_position": {"x": 0, "y": 64, "z": 0},
            },
            "entities": [threat],
        },
    )
    monkeypatch.setattr(combat, "run_away", lambda *_args, **_kwargs: False)

    assert combat.defend_or_flee(
        client,
        allow_safe_recovery_movement=True,
    )
    assert ("cancel", {}) in transport.calls


def test_skeleton_is_fled_even_at_full_health(monkeypatch):
    transport = CombatTransport(health=20.0)
    client = SimpleNamespace(transport=transport)
    threat = {
        "id": 10,
        "type": "minecraft:skeleton",
        "distance": 9.0,
        "position": {"x": 9, "y": 64, "z": 0},
    }
    attacked = []
    fled = []
    monkeypatch.setattr(combat, "scan_for_threats", lambda _client: [threat])
    monkeypatch.setattr(combat, "safe_combat", lambda *_args, **_kwargs: attacked.append(True))
    monkeypatch.setattr(combat, "run_away", lambda _client, target: fled.append(target))
    monkeypatch.setattr(combat.time, "sleep", lambda _seconds: None)

    assert combat.defend_or_flee(client)
    assert not attacked
    assert fled == [threat]
    assert ("cancel", {}) in transport.calls


def test_unarmored_player_flees_zombie_instead_of_starting_melee(monkeypatch):
    transport = CombatTransport(health=20.0)
    client = SimpleNamespace(transport=transport)
    threat = {
        "id": 11,
        "type": "minecraft:zombie",
        "distance": 6.0,
        "position": {"x": 6, "y": 64, "z": 0},
    }
    attacked = []
    fled = []
    monkeypatch.setattr(combat, "scan_for_threats", lambda _client: [threat])
    monkeypatch.setattr(combat, "get_equipped_armor", lambda _client: {})
    monkeypatch.setattr(combat, "safe_combat", lambda *_args, **_kwargs: attacked.append(True))
    monkeypatch.setattr(combat, "run_away", lambda _client, target: fled.append(target))
    monkeypatch.setattr(combat.time, "sleep", lambda _seconds: None)

    assert combat.defend_or_flee(client)
    assert not attacked
    assert fled == [threat]


def test_recover_health_refuses_work_when_no_food(monkeypatch):
    transport = CombatTransport(health=5.0)
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(combat, "heal_if_needed", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(combat, "_emergency_food_count", lambda _client: 0)

    assert not combat.recover_health(client, minimum_health=12.0, timeout=1.0)
    assert ("cancel", {}) in transport.calls


def test_dead_player_yields_without_local_respawn():
    transport = CombatTransport(health=0.0)
    client = SimpleNamespace(transport=transport)

    with pytest.raises(PlayerDeathDetected):
        combat.defend_or_flee(client)

    assert not any(route == "respawn" for route, _payload in transport.calls)


def test_eat_until_hunger_uses_carried_emergency_food(monkeypatch):
    class HungerTransport(CombatTransport):
        def __init__(self):
            super().__init__(health=20.0)
            self.food_level = 6

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {"health": 20.0, "food_level": self.food_level}
            if route == "use_item":
                self.food_level = min(20, self.food_level + 4)
            return {}

    transport = HungerTransport()
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(
        combat,
        "count_item",
        lambda _client, item_id: 7 if item_id == "minecraft:rotten_flesh" else 0,
    )
    monkeypatch.setattr(combat, "select_item", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(combat.time, "sleep", lambda _seconds: None)

    assert combat.eat_until_hunger(client, minimum_food=14)
    uses = [call for call in transport.calls if call[0] == "use_item"]
    assert len(uses) == 2


def test_eat_until_hunger_yields_death_to_top_level_recovery():
    client = SimpleNamespace(transport=CombatTransport(health=0.0))

    with pytest.raises(PlayerDeathDetected):
        combat.eat_until_hunger(client, minimum_food=14)


def test_emergency_food_list_uses_current_raw_meat_item_ids():
    assert "minecraft:beef" in combat.EMERGENCY_FOOD_ITEMS
    assert "minecraft:porkchop" in combat.EMERGENCY_FOOD_ITEMS
    # Edible fish belong; tropical_fish does NOT -- it has no food value in
    # Minecraft, and listing it made bots believe they had food they could
    # never eat (live: food stayed 0 while they hunted tropical fish in lush
    # caves for days). Pufferfish is edible but poisons the eater.
    assert "minecraft:cod" in combat.EMERGENCY_FOOD_ITEMS
    assert "minecraft:salmon" in combat.EMERGENCY_FOOD_ITEMS
    assert "minecraft:tropical_fish" not in combat.EMERGENCY_FOOD_ITEMS
    assert "minecraft:pufferfish" not in combat.EMERGENCY_FOOD_ITEMS
    assert "minecraft:raw_beef" not in combat.EMERGENCY_FOOD_ITEMS
    assert "minecraft:raw_porkchop" not in combat.EMERGENCY_FOOD_ITEMS


def test_emergency_food_crafts_carried_wheat_before_hunting(monkeypatch):
    from baritone_client.common import emergency_food, resources

    counts = {"minecraft:wheat": 18, "minecraft:bread": 0}
    crafted = []
    monkeypatch.setattr(
        "baritone_client.common.inventory.count_item",
        lambda _client, item_id: counts.get(item_id, 0),
    )
    monkeypatch.setattr(
        emergency_food,
        "emergency_food_count",
        lambda _client: counts["minecraft:bread"],
    )

    def craft(_client, item_id, target):
        crafted.append((item_id, target))
        counts[item_id] = target
        return True

    monkeypatch.setattr(resources, "_craft_with_table", craft)

    assert emergency_food.craft_emergency_bread_from_carried_wheat(object())
    assert crafted == [("minecraft:bread", 6)]


def test_emergency_food_crafts_one_bread_from_partial_wheat(monkeypatch):
    from baritone_client.common import emergency_food, resources

    counts = {"minecraft:wheat": 3, "minecraft:bread": 0}
    monkeypatch.setattr(
        "baritone_client.common.inventory.count_item",
        lambda _client, item_id: counts.get(item_id, 0),
    )
    monkeypatch.setattr(
        emergency_food,
        "emergency_food_count",
        lambda _client: counts["minecraft:bread"],
    )

    def craft(_client, item_id, target):
        counts[item_id] = target
        return True

    monkeypatch.setattr(resources, "_craft_with_table", craft)

    assert emergency_food.craft_emergency_bread_from_carried_wheat(object())
    assert counts["minecraft:bread"] == 1


def test_emergency_food_preserves_wheat_when_food_is_already_carried(monkeypatch):
    from baritone_client.common import emergency_food, resources

    monkeypatch.setattr(emergency_food, "emergency_food_count", lambda _client: 1)
    monkeypatch.setattr(
        resources,
        "_craft_with_table",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not craft")),
    )

    assert emergency_food.craft_emergency_bread_from_carried_wheat(object())


def test_acquire_emergency_food_uses_carried_wheat_recovery(monkeypatch):
    state = {"health": 20.0, "food_level": 10, "world_time": 1000}
    client = SimpleNamespace(
        transport=SimpleNamespace(dispatch=lambda route, _payload: dict(state))
    )
    crafted = []
    monkeypatch.setattr(combat, "recover_health", lambda *_a, **_k: False)
    monkeypatch.setattr(
        combat,
        "prepare_carried_wheat_recovery",
        lambda _client, _state, minimum_food, *_args: (
            crafted.append(True),
            state.__setitem__("food_level", minimum_food),
            True,
        )[-1],
    )

    assert combat.acquire_emergency_food(client, minimum_food=14)
    assert crafted == [True]


def test_full_hunger_still_acquires_requested_expedition_reserve(monkeypatch):
    state = {"health": 20.0, "food_level": 20, "world_time": 1000}
    reserve = {"count": 0}
    client = SimpleNamespace(
        transport=SimpleNamespace(dispatch=lambda route, _payload: dict(state))
    )
    prepared = []
    monkeypatch.setattr(combat, "recover_health", lambda *_a, **_k: True)
    monkeypatch.setattr(
        combat,
        "_emergency_food_count",
        lambda _client: reserve["count"],
    )

    def prepare(_client, _state, _minimum_food, *_args):
        prepared.append(True)
        reserve["count"] = 6
        return True

    monkeypatch.setattr(combat, "prepare_carried_wheat_recovery", prepare)

    assert combat.acquire_emergency_food(
        client,
        minimum_food=18,
        minimum_reserve=6,
    )
    assert prepared == [True]


def test_emergency_recovery_ignores_hostile_sealed_far_below_player():
    state = {"block_position": {"x": -7, "y": 72, "z": 33}}
    cave_creeper = {
        "type": "minecraft:creeper",
        "distance": 10.4,
        "position": {"x": -4, "y": 62, "z": 31},
    }
    surface_creeper = {
        "type": "minecraft:creeper",
        "distance": 10.4,
        "position": {"x": -4, "y": 70, "z": 31},
    }

    assert not combat._threat_can_reach_player(cave_creeper, state)
    assert combat._threat_can_reach_player(surface_creeper, state)


def test_aquatic_food_uses_dynamic_follow_until_melee_range(monkeypatch):
    transport = CombatTransport(health=20.0)
    client = SimpleNamespace(transport=transport)
    scans = iter(
        (
            [{"id": 22, "type": "minecraft:cod", "distance": 8.0}],
            [{"id": 22, "type": "minecraft:cod", "distance": 3.5}],
        )
    )
    monkeypatch.setattr(combat, "get_nearby_entities", lambda *_args, **_kwargs: next(scans))
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(combat.time, "sleep", lambda _seconds: None)

    assert combat._approach_aquatic_food(
        client,
        22,
        "minecraft:cod",
    )
    assert ("chat", {"message": "#follow entity cod"}) in transport.calls
    assert transport.calls[-2:] == [
        ("chat", {"message": "#stop"}),
        ("cancel", {}),
    ]


def test_aquatic_follow_surfaces_when_health_drops_below_margin(monkeypatch):
    class FallingHealthTransport(CombatTransport):
        def __init__(self):
            super().__init__(health=20.0)
            self.health_reads = iter((20.0, 8.0))

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {"health": next(self.health_reads)}
            return {}

    transport = FallingHealthTransport()
    client = SimpleNamespace(transport=transport)
    scans = iter(
        (
            [{"id": 22, "type": "minecraft:cod", "distance": 8.0}],
        )
    )
    surfaced = []
    monkeypatch.setattr(
        combat,
        "get_nearby_entities",
        lambda *_args, **_kwargs: next(scans),
    )
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        combat,
        "_surface_after_aquatic_hunt",
        lambda *_args, **_kwargs: surfaced.append(True) or True,
    )
    monkeypatch.setattr(combat.time, "sleep", lambda _seconds: None)

    assert not combat._approach_aquatic_food(
        client,
        22,
        "minecraft:cod",
    )
    assert surfaced == [True]


def test_aquatic_hunt_surfaces_until_head_reaches_air(monkeypatch):
    class SurfaceTransport(CombatTransport):
        def __init__(self):
            super().__init__(health=18.0)
            self.block_reads = 0

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {
                    "health": self.health,
                    "block_position": {"x": 4, "y": 61, "z": 8},
                }
            if route == "get_block":
                self.block_reads += 1
                return {
                    "id": (
                        "minecraft:water"
                        if self.block_reads <= 33
                        else "minecraft:air"
                    )
                }
            return {}

    transport = SurfaceTransport()
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(combat.time, "sleep", lambda _seconds: None)

    assert combat._surface_after_aquatic_hunt(client)
    assert ("chat", {"message": "#surface"}) in transport.calls
    messages = [
        payload["message"]
        for route, payload in transport.calls
        if route == "chat"
    ]
    assert messages.index("#set assumeWalkOnWater false") < messages.index(
        "#surface"
    )
    assert transport.calls[-2:] == [
        ("chat", {"message": "#stop"}),
        ("cancel", {}),
    ]
    assert client._aquatic_surface_failed is False


def test_aquatic_hunt_uses_loaded_water_column_for_vertical_ascent(monkeypatch):
    class WaterColumnTransport(CombatTransport):
        def __init__(self):
            super().__init__(health=18.0)
            self.player_levels = iter((59, 59, 62))

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {
                    "health": self.health,
                    "block_position": {
                        "x": 4,
                        "y": next(self.player_levels),
                        "z": 8,
                    },
                }
            if route == "get_block":
                return {
                    "id": (
                        "minecraft:water"
                        if int(payload["y"]) <= 62
                        else "minecraft:air"
                    )
                }
            return {}

    transport = WaterColumnTransport()
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(combat.time, "sleep", lambda _seconds: None)

    assert combat._surface_after_aquatic_hunt(client)
    assert (
        "goal",
        {"type": "yLevel", "value": 62},
    ) in transport.calls
    assert ("chat", {"message": "#surface"}) not in transport.calls


def test_aquatic_surface_aborts_downward_route(monkeypatch):
    class DownwardTransport(CombatTransport):
        def __init__(self):
            super().__init__(health=18.0)
            self.states = iter((61, 61, 58))

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {
                    "health": self.health,
                    "block_position": {"x": 4, "y": next(self.states), "z": 8},
                }
            if route == "get_block":
                return {"id": "minecraft:water"}
            return {}

    client = SimpleNamespace(transport=DownwardTransport())
    ticks = iter((0.0, 0.0, 1.0, 2.0))
    monkeypatch.setattr(combat.time, "time", lambda: next(ticks, 2.0))
    monkeypatch.setattr(combat.time, "sleep", lambda _seconds: None)

    assert combat._surface_after_aquatic_hunt(client, timeout=10.0) is False
    assert client._aquatic_surface_failed is True


class SubmergedTransport(CombatTransport):
    def __init__(self, head_id="minecraft:water"):
        super().__init__(health=20.0)
        self.head_id = head_id

    def dispatch(self, route, payload):
        self.calls.append((route, payload))
        if route == "get_state":
            return {"health": self.health, "block_position": {"x": 0, "y": 62, "z": 0}}
        if route == "get_block":
            return {"id": self.head_id}
        return {}


def test_submersion_reflex_ignores_first_tick_then_surfaces(monkeypatch):
    client = SimpleNamespace(transport=SubmergedTransport("minecraft:water"))
    surfaced = []
    monkeypatch.setattr(
        combat,
        "_surface_after_aquatic_hunt",
        lambda _c, **_k: surfaced.append(True) or True,
    )
    state = {"block_position": {"x": 0, "y": 62, "z": 0}}

    # First fully-submerged tick is below threshold -> no surfacing yet.
    assert combat.escape_water_if_submerged(client, state) is False
    assert client._submersion_ticks == 1
    assert surfaced == []

    # Second consecutive tick reaches the threshold -> surface and reset.
    assert combat.escape_water_if_submerged(client, state) is True
    assert surfaced == [True]
    assert client._submersion_ticks == 0


def test_submersion_reflex_resets_when_head_clears(monkeypatch):
    client = SimpleNamespace(
        transport=SubmergedTransport("minecraft:air"), _submersion_ticks=5
    )
    surfaced = []
    monkeypatch.setattr(
        combat,
        "_surface_after_aquatic_hunt",
        lambda _c, **_k: surfaced.append(True) or True,
    )
    state = {"block_position": {"x": 0, "y": 70, "z": 0}}

    # Head is in air (e.g. swimming on the surface) -> reflex is a no-op.
    assert combat.escape_water_if_submerged(client, state) is False
    assert client._submersion_ticks == 0
    assert surfaced == []


def test_water_damage_surfaces_when_swimming_pose_makes_head_probe_dry(monkeypatch):
    class SwimmingTransport(CombatTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_block":
                return {
                    "id": (
                        "minecraft:water"
                        if payload["y"] == 62
                        else "minecraft:air"
                    )
                }
            return {}

    client = SimpleNamespace(transport=SwimmingTransport(health=20.0))
    surfaced = []
    monkeypatch.setattr(
        combat,
        "_surface_after_aquatic_hunt",
        lambda _client, **_kwargs: surfaced.append(True) or True,
    )
    state = {
        "health": 20.0,
        "block_position": {"x": 0, "y": 62, "z": 0},
    }
    assert combat.escape_water_if_submerged(client, state) is False

    state["health"] = 16.0
    assert combat.escape_water_if_submerged(client, state) is True
    assert surfaced == [True]


def test_defend_or_flee_surfaces_before_assessing_threats(monkeypatch):
    # Pre-seed one submersion tick so this call reaches the surface threshold.
    client = SimpleNamespace(
        transport=SubmergedTransport("minecraft:water"), _submersion_ticks=1
    )
    surfaced = []
    monkeypatch.setattr(combat, "_get_combat_snapshot", lambda *_a, **_k: None)
    monkeypatch.setattr(
        combat,
        "_surface_after_aquatic_hunt",
        lambda _c, **_k: surfaced.append(True) or True,
    )
    # Threat assessment must never run while the bot is drowning.
    monkeypatch.setattr(
        combat,
        "scan_for_threats",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("must surface before assessing threats")
        ),
    )

    assert combat.defend_or_flee(client) is True
    assert surfaced == [True]
    assert client._last_defense_intervention == "aquatic"


def test_near_death_recovery_eventually_explores_instead_of_holding_forever(
    monkeypatch,
):
    """Regression for the live Bot09 stall: health=3.5, food=10, no threats,
    no loaded food target. must_hold_for_critical_food(critical_health_floor
    default) is True the whole time, so a caller that returned False on the
    first hold would loop this forever. The bounded hold_cycles grace period
    must fall through to the same bounded exploration a merely-hurt bot uses.
    """

    class NearDeathTransport(CombatTransport):
        def __init__(self):
            super().__init__(health=3.5)

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {
                    "health": self.health,
                    "food_level": 10,
                    "world_time": 1000,
                    "block_position": {"x": 0, "y": 64, "z": 0},
                }
            if route == "get_inventory":
                return {"inventory": [], "armor": [], "offhand": []}
            return {}

    client = SimpleNamespace(transport=NearDeathTransport())
    monkeypatch.setattr(combat, "recover_health", lambda *_a, **_k: False)
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_a, **_k: [])
    monkeypatch.setattr(combat, "get_nearby_entities", lambda *_a, **_k: [])
    monkeypatch.setattr(combat.time, "sleep", lambda _s: None)

    class ReachedExploration(Exception):
        pass

    def explored(*_a, **_k):
        if _a and _a[0] == "explore":
            raise ReachedExploration()
        return {}

    original_dispatch = client.transport.dispatch

    def wrapped_dispatch(route, payload):
        if route == "explore":
            raise ReachedExploration()
        return original_dispatch(route, payload)

    client.transport.dispatch = wrapped_dispatch

    with pytest.raises(ReachedExploration):
        combat.acquire_emergency_food(client, minimum_health=12.0, timeout=60)


def test_emergency_food_search_widens_after_repeated_failed_rotations(monkeypatch):
    """A saturated waypoint rotation must widen, not repeat itself forever.

    Live A1 2026-09-09: health sat pinned at 5/12 for 25+ minutes, retrying
    the same four 96-block waypoints every cycle because the search bound
    never grew past max_exploration_distance. client._emergency_food_waypoint_
    cursor already persists across calls (see EmergencyExploration.resume_
    waypoint_rotation); this asserts a high cursor produces a wider target
    than the same cursor would under the old fixed-radius behavior.
    """
    class NearDeathTransport(CombatTransport):
        def __init__(self):
            super().__init__(health=5.0)

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {
                    "health": self.health,
                    "food_level": 10,
                    "world_time": 1000,
                    "block_position": {"x": 0, "y": 64, "z": 0},
                }
            if route == "get_inventory":
                return {"inventory": [], "armor": [], "offhand": []}
            return {}

    client = SimpleNamespace(transport=NearDeathTransport())
    client._emergency_food_waypoint_cursor = 32
    monkeypatch.setattr(combat, "recover_health", lambda *_a, **_k: False)
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_a, **_k: [])
    monkeypatch.setattr(combat, "get_nearby_entities", lambda *_a, **_k: [])
    monkeypatch.setattr(combat.time, "sleep", lambda _s: None)

    explored = []

    class StopAfterExplore(Exception):
        pass

    original_dispatch = client.transport.dispatch

    def wrapped_dispatch(route, payload):
        if route == "explore":
            explored.append(payload)
            raise StopAfterExplore()
        return original_dispatch(route, payload)

    client.transport.dispatch = wrapped_dispatch

    with pytest.raises(StopAfterExplore):
        combat.acquire_emergency_food(client, minimum_health=12.0, timeout=60)

    # Ring 5 (index 32) at the unescalated 96-block cap would sit at exactly
    # x=96 (min(24*5, 96)); two full rotations must have widened the cap so
    # this ring reaches its uncapped 120-block distance instead.
    assert explored == [{"x": 120, "z": 0}]


def test_emergency_food_hunts_passive_target_then_recovers(monkeypatch):
    transport = CombatTransport(health=5.0)
    client = SimpleNamespace(transport=transport)
    sheep = {
        "id": 12,
        "type": "minecraft:sheep",
        "distance": 8.0,
        "position": {"x": 8, "y": 64, "z": 0},
    }
    recovery_results = iter((False, True))
    hunted = []
    renewable_sources = []

    def recover(*_args, **_kwargs):
        recovered = next(recovery_results)
        if recovered:
            transport.health = 12.0
        return recovered

    monkeypatch.setattr(combat, "recover_health", recover)
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_args, **_kwargs: [])
    herd = [
        {**sheep, "id": 12 + index, "distance": 8.0 + index}
        for index in range(3)
    ]
    # health=5.0 is below _CRITICAL_HUNT_HEALTH, so the target search uses
    # the shrunk 16-block radius rather than the normal 64.
    monkeypatch.setattr(
        combat,
        "get_nearby_entities",
        lambda _client, radius: herd if radius == 16 else [],
    )
    monkeypatch.setattr(combat, "safe_combat", lambda *_args, **_kwargs: hunted.append(True) or True)
    monkeypatch.setattr(combat, "heal_if_needed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(combat.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr("baritone_client.common.navigation.goto", lambda *_args, **_kwargs: True)

    assert combat.acquire_emergency_food(
        client,
        minimum_health=12.0,
        renewable_source_callback=lambda animal_type, location: renewable_sources.append(
            (animal_type, location)
        ),
    )
    assert hunted == [True]
    assert renewable_sources == [("sheep", (8, 64, 0))]
    assert ("chat", {"message": "#set allowSprint false"}) in transport.calls
    assert ("chat", {"message": "#set allowSprint true"}) in transport.calls


def test_emergency_food_explores_until_passive_target_loads(monkeypatch):
    transport = CombatTransport(health=5.0)
    client = SimpleNamespace(transport=transport)
    cow = {
        "id": 13,
        "type": "minecraft:cow",
        "distance": 10.0,
        "position": {"x": 10, "y": 64, "z": 0},
    }
    recovery_results = iter((False, True))
    land_scans = {"count": 0}
    hunted = []
    def recover(*_args, **_kwargs):
        recovered = next(recovery_results)
        if recovered:
            transport.health = 12.0
        return recovered

    monkeypatch.setattr(combat, "recover_health", recover)
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_args, **_kwargs: [])
    herd = [
        {**cow, "id": 13 + index, "distance": 10.0 + index}
        for index in range(3)
    ]

    # health=5.0 is below _CRITICAL_HUNT_HEALTH, so the target search uses
    # the shrunk 16-block radius rather than the normal 64.
    def nearby(_client, radius):
        if radius != 16:
            return []
        land_scans["count"] += 1
        return herd if land_scans["count"] >= 3 else []

    monkeypatch.setattr(combat, "get_nearby_entities", nearby)
    monkeypatch.setattr(combat, "safe_combat", lambda *_args, **_kwargs: hunted.append(True) or True)
    monkeypatch.setattr(combat, "heal_if_needed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(combat.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr("baritone_client.common.navigation.goto", lambda *_args, **_kwargs: True)

    assert combat.acquire_emergency_food(client, minimum_health=12.0)
    assert hunted == [True]
    exploration_calls = [
        payload for route, payload in transport.calls if route == "explore"
    ]
    assert exploration_calls
    assert all(payload != {"x": 0, "z": 0} for payload in exploration_calls)
    assert ("chat", {"message": "#stop"}) in transport.calls


def test_emergency_food_does_not_treat_full_health_low_hunger_as_recovered(
    monkeypatch, advancing_clock
):
    class HungryTransport(CombatTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {
                    "health": 20.0,
                    "food_level": 12,
                    "world_time": 1000,
                    "block_position": {"x": 0, "y": 64, "z": 0},
                }
            if route == "get_inventory":
                return {"inventory": [], "armor": [], "offhand": []}
            return {}

    client = SimpleNamespace(transport=HungryTransport(health=20.0))
    clock = advancing_clock()
    monkeypatch.setattr(combat, "time", clock)
    monkeypatch.setattr(inventory, "time", clock)
    monkeypatch.setattr(combat, "recover_health", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_args, **_kwargs: [])

    class ReachedFoodSearch(Exception):
        pass

    def reach_food_search(*_args, **_kwargs):
        raise ReachedFoodSearch()

    monkeypatch.setattr(
        combat,
        "get_nearby_entities",
        lambda _client, radius: reach_food_search() if radius == 64 else [],
    )

    with pytest.raises(ReachedFoodSearch):
        combat.acquire_emergency_food(
            client,
            minimum_health=12.0,
            minimum_food=14,
        )


def test_emergency_food_refuses_blind_underground_exploration(
    monkeypatch, advancing_clock
):
    class UndergroundTransport(CombatTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {
                    "health": 20.0,
                    "food_level": 10,
                    "world_time": 1000,
                    "dimension": "minecraft:overworld",
                    "block_position": {"x": 100, "y": 12, "z": 172},
                }
            if route == "get_inventory":
                return {"inventory": [], "armor": [], "offhand": []}
            return {}

    client = SimpleNamespace(transport=UndergroundTransport())
    clock = advancing_clock()
    monkeypatch.setattr(combat, "time", clock)
    monkeypatch.setattr(inventory, "time", clock)
    surface_checks = []
    monkeypatch.setattr(combat, "recover_health", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        combat,
        "prepare_food_search_state",
        lambda _client, state: surface_checks.append(
            state["block_position"]["y"]
        ),
    )
    monkeypatch.setattr(
        combat,
        "get_nearby_entities",
        lambda *_args, **_kwargs: pytest.fail(
            "underground recovery must not begin blind entity exploration"
        ),
    )

    assert not combat.acquire_emergency_food(client, minimum_food=14)
    assert surface_checks == [12]
    assert not any(
        route == "explore" for route, _payload in client.transport.calls
    )


def test_emergency_food_rechecks_dry_surface_after_exploration_enters_water(
    monkeypatch, advancing_clock
):
    dry = {
        "health": 20.0,
        "food_level": 10,
        "world_time": 1000,
        "dimension": "minecraft:overworld",
        "block_position": {"x": 0, "y": 64, "z": 0},
        "wet": False,
    }
    wet = {
        **dry,
        "block_position": {"x": 4, "y": 63, "z": 0},
        "wet": True,
    }

    class WetAfterStartTransport(CombatTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return wet
            if route == "get_inventory":
                return {"inventory": [], "armor": [], "offhand": []}
            return {}

    client = SimpleNamespace(transport=WetAfterStartTransport())
    clock = advancing_clock()
    monkeypatch.setattr(combat, "time", clock)
    monkeypatch.setattr(inventory, "time", clock)
    surface_checks = []

    class RecheckedSurface(Exception):
        pass

    class MissedSurfaceRecheck(Exception):
        pass

    def prepare(_client, state):
        surface_checks.append(state["wet"])
        if len(surface_checks) > 1:
            raise RecheckedSurface()
        return dry

    monkeypatch.setattr(combat, "recover_health", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(combat, "prepare_food_search_state", prepare)
    monkeypatch.setattr(
        "baritone_client.common.emergency_food.prepare_food_search_state",
        prepare,
    )
    monkeypatch.setattr(
        "baritone_client.common.emergency_food.player_is_in_water",
        lambda _client, state: state["wet"],
    )
    monkeypatch.setattr(
        "baritone_client.common.emergency_food.head_block_is_water",
        lambda *_args: False,
    )
    monkeypatch.setattr(
        combat,
        "get_nearby_entities",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            MissedSurfaceRecheck()
        ),
    )

    with pytest.raises(RecheckedSurface):
        combat.acquire_emergency_food(client, minimum_food=14)

    assert surface_checks == [True, True]


def test_secure_recovery_area_requires_sustained_daylight_clearance(monkeypatch):
    transport = CombatTransport(health=20.0)
    client = SimpleNamespace(transport=transport)
    clock = iter((0.0, 0.0, 2.0, 6.0, 8.0))
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(combat.time, "time", lambda: next(clock))
    monkeypatch.setattr(combat.time, "sleep", lambda _seconds: None)

    assert combat.secure_recovery_area(
        client, timeout=90.0, clear_seconds=5.0, radius=16
    )


def test_secure_recovery_area_keeps_defense_attached_to_threat(monkeypatch):
    transport = CombatTransport(health=20.0)
    client = SimpleNamespace(transport=transport)
    threat = {"id": 7, "type": "minecraft:zombie", "distance": 4.0}
    scans = iter(([threat], [], []))
    defended = []
    clock = iter((0.0, 0.0, 1.0, 3.0, 5.0, 6.0))
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_args, **_kwargs: next(scans))
    monkeypatch.setattr(combat, "defend_or_flee", lambda _client: defended.append(True) or True)
    monkeypatch.setattr(combat.time, "time", lambda: next(clock))
    monkeypatch.setattr(combat.time, "sleep", lambda _seconds: None)

    assert combat.secure_recovery_area(
        client, timeout=90.0, clear_seconds=2.0, radius=16
    )
    assert defended == [True]


def test_combat_does_not_count_unobserved_target_as_kill(monkeypatch):
    target = {
        "id": 42,
        "type": "minecraft:zombie",
        "distance": 3.0,
        "position": {"x": 3, "y": 64, "z": 0},
    }

    class VanishingTransport(CombatTransport):
        def dispatch(self, route, payload):
            if route == "attack_entity":
                raise RuntimeError("Entity not found: 42")
            return super().dispatch(route, payload)

    client = SimpleNamespace(transport=VanishingTransport())
    monkeypatch.setattr(combat, "get_nearby_entities", lambda *_args, **_kwargs: [target])
    monkeypatch.setattr(combat, "equip_best_weapon", lambda _client: True)
    monkeypatch.setattr(combat, "look_at_entity", lambda *_args: True)

    assert not combat.safe_combat(client, 42, max_duration=1)


def test_melee_waits_for_attack_cooldown_before_dispatch(monkeypatch):
    target = {
        "id": 42,
        "type": "minecraft:zombie",
        "distance": 3.0,
        "position": {"x": 3, "y": 64, "z": 0},
    }
    snapshots = iter(
        (
            {
                "player": {"health": 20, "attack_cooldown": 0.4},
                "entities": [target],
            },
            {
                "player": {"health": 20, "attack_cooldown": 0.95},
                "entities": [target],
            },
            {
                "player": {"health": 20, "attack_cooldown": 0.1},
                "entities": [{**target, "health": 0}],
            },
        )
    )
    transport = CombatTransport()
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(
        combat,
        "_get_combat_snapshot",
        lambda *_args, **_kwargs: next(snapshots),
    )
    monkeypatch.setattr(combat, "equip_best_weapon", lambda _client: True)
    monkeypatch.setattr(combat, "look_at_entity", lambda *_args: True)
    monkeypatch.setattr(combat.time, "sleep", lambda _seconds: None)

    assert combat.safe_combat(client, target["id"], max_duration=1)
    attacks = [payload for route, payload in transport.calls if route == "attack_entity"]
    assert attacks == [
        {
            "entity_id": target["id"],
            "min_cooldown": combat.MELEE_ATTACK_COOLDOWN_THRESHOLD,
        }
    ]


def test_combat_aborts_for_secondary_hostile_from_canonical_policy(monkeypatch):
    target = {
        "id": 42,
        "type": "minecraft:zombie",
        "distance": 3.0,
        "position": {"x": 3, "y": 64, "z": 0},
    }
    drowned = {
        "id": 7,
        "type": "minecraft:drowned",
        "distance": 8.0,
        "position": {"x": 0, "y": 64, "z": 8},
    }
    snapshot = {
        "player": {
            "health": 20,
            "attack_cooldown": 1.0,
            "block_position": {"x": 0, "y": 64, "z": 0},
        },
        "entities": [target, drowned],
    }
    transport = CombatTransport()
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(combat, "_get_combat_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr(combat, "equip_best_weapon", lambda _client: True)

    assert not combat.safe_combat(
        client,
        target["id"],
        max_duration=1,
        abort_on_other_hostiles=True,
    )
    assert not any(route == "attack_entity" for route, _payload in transport.calls)
    assert ("cancel", {}) in transport.calls


def test_passive_hunt_aborts_if_another_hostile_enters_radius(monkeypatch):
    pig = {
        "id": 42,
        "type": "minecraft:pig",
        "distance": 6.0,
        "position": {"x": 6, "y": 64, "z": 0},
    }
    creeper = {
        "id": 7,
        "type": "minecraft:creeper",
        "distance": 8.0,
        "position": {"x": 0, "y": 64, "z": 8},
    }
    transport = CombatTransport(health=20.0)
    client = SimpleNamespace(transport=transport)
    attacked = []
    monkeypatch.setattr(combat, "get_nearby_entities", lambda *_args, **_kwargs: [pig, creeper])
    monkeypatch.setattr(combat, "equip_best_weapon", lambda _client: True)
    monkeypatch.setattr(
        combat,
        "look_at_entity",
        lambda *_args: attacked.append(True) or True,
    )

    assert not combat.safe_combat(
        client,
        pig["id"],
        max_duration=1,
        abort_on_other_hostiles=True,
    )
    assert not attacked
    assert ("cancel", {}) in transport.calls


def test_far_combat_target_gets_one_bounded_navigation_goal(monkeypatch):
    target = {
        "id": 42,
        "type": "minecraft:cow",
        "distance": 49.0,
        "position": {"x": 49, "y": 64, "z": 0},
    }
    client = SimpleNamespace(transport=CombatTransport())
    navigation_calls = []
    scans = iter(([target], [{**target, "health": 0}]))
    scan_radii = []

    def scan(_client, radius):
        scan_radii.append(radius)
        return next(scans)

    monkeypatch.setattr(combat, "get_nearby_entities", scan)
    monkeypatch.setattr(combat, "equip_best_weapon", lambda _client: True)
    monkeypatch.setattr(
        combat,
        "goto",
        lambda _client, x, y, z, **kwargs: navigation_calls.append(
            (x, y, z, kwargs)
        )
        or True,
    )
    monkeypatch.setattr(combat.time, "sleep", lambda _seconds: None)

    assert combat.safe_combat(client, 42, max_duration=40, tracking_radius=64)
    assert len(navigation_calls) == 1
    assert navigation_calls[0][:3] == (49, 64, 0)
    assert navigation_calls[0][3]["tolerance"] == 3.0
    assert scan_radii == [64, 64]


def test_forced_ranged_approach_equips_and_raises_carried_shield(monkeypatch):
    skeleton = {
        "id": 42,
        "type": "minecraft:skeleton",
        "distance": 8.0,
        "position": {"x": 8, "y": 64, "z": 0},
    }

    class ShieldTransport(CombatTransport):
        def __init__(self):
            super().__init__(health=5.0)
            self.equipped = False

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {
                    "health": self.health,
                    "block_position": {"x": 0, "y": 64, "z": 0},
                }
            if route == "get_inventory":
                return {
                    "inventory": [] if self.equipped else [
                        {"id": "minecraft:shield", "count": 1}
                    ],
                    "offhand": [
                        {"id": "minecraft:shield", "count": 1}
                    ] if self.equipped else [],
                }
            if route == "equip":
                self.equipped = True
                return {"equipped": True}
            if route == "use_item":
                return {
                    "holding": True,
                    "hand": "OFF_HAND",
                    "active_hand": "OFF_HAND",
                    "held_item": "minecraft:shield",
                    "is_using_item": True,
                }
            return {}

    transport = ShieldTransport()
    client = SimpleNamespace(transport=transport)
    snapshots = iter(
        (
            {"player": {"health": 5.0}, "entities": [skeleton]},
            {
                "player": {"health": 5.0},
                "entities": [{**skeleton, "health": 0}],
            },
        )
    )
    monkeypatch.setattr(
        combat, "_get_combat_snapshot", lambda *_args, **_kwargs: next(snapshots)
    )
    monkeypatch.setattr(combat, "equip_best_weapon", lambda _client: True)
    monkeypatch.setattr(
        combat,
        "goto",
        lambda _client, _x, _y, _z, **kwargs: (
            kwargs["on_defense"]() is False
        ),
    )
    monkeypatch.setattr(combat.time, "sleep", lambda _seconds: None)

    assert combat.safe_combat(
        client,
        skeleton["id"],
        no_retreat=True,
        max_duration=2,
    )
    assert (
        "equip",
        {"slot": "offhand", "item": "minecraft:shield"},
    ) in transport.calls
    assert (
        "use_item",
        {"hand": "OFF_HAND", "duration_ms": combat_melee.SHIELD_HOLD_MS},
    ) in transport.calls


def test_close_ranged_target_raises_shield_before_melee(monkeypatch):
    """A blaze spawning in melee range must not bypass the carried shield."""
    blaze = {
        "id": 42,
        "type": "minecraft:blaze",
        "distance": 1.2,
        "position": {"x": 1, "y": 64, "z": 0},
    }

    class ShieldTransport(CombatTransport):
        def __init__(self):
            super().__init__(health=20.0)
            self.equipped = False

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_inventory":
                return {
                    "inventory": [] if self.equipped else [
                        {"id": "minecraft:shield", "count": 1}
                    ],
                    "offhand": [
                        {"id": "minecraft:shield", "count": 1}
                    ] if self.equipped else [],
                }
            if route == "equip":
                self.equipped = True
                return {"equipped": True}
            if route == "use_item":
                return {
                    "holding": True,
                    "hand": "OFF_HAND",
                    "active_hand": "OFF_HAND",
                    "held_item": "minecraft:shield",
                    "is_using_item": True,
                }
            if route == "attack_entity":
                self.attacked = True
                return {"attacked": True}
            return super().dispatch(route, payload)

    transport = ShieldTransport()
    transport.attacked = False
    client = SimpleNamespace(transport=transport)
    clock = {"now": 0.0}

    def monotonic():
        clock["now"] += 0.1
        return clock["now"]

    def snapshot(*_args, **_kwargs):
        target = {**blaze, "health": 0} if transport.attacked else blaze
        return {"player": {"health": 20.0}, "entities": [target]}

    monkeypatch.setattr(combat, "_get_combat_snapshot", snapshot)
    monkeypatch.setattr(combat, "equip_best_weapon", lambda _client: True)
    monkeypatch.setattr(combat, "look_at_entity", lambda *_args: True)
    monkeypatch.setattr(combat, "_attack_cooldown", lambda _state: 1.0)
    monkeypatch.setattr(combat_melee.time, "monotonic", monotonic)
    monkeypatch.setattr(combat_melee.time, "sleep", lambda _seconds: None)

    assert combat.safe_combat(
        client,
        blaze["id"],
        no_retreat=True,
        max_duration=2,
    )
    shield_call = transport.calls.index(
        (
            "use_item",
            {"hand": "OFF_HAND", "duration_ms": combat_melee.SHIELD_HOLD_MS},
        )
    )
    attack_call = transport.calls.index(
        (
            "attack_entity",
            {
                "entity_id": blaze["id"],
                "min_cooldown": combat.MELEE_ATTACK_COOLDOWN_THRESHOLD,
            },
        )
    )
    assert shield_call < attack_call


def test_close_ranged_target_aborts_when_offhand_hold_is_unverified(monkeypatch):
    """Never attack as if protected when the bridge cannot prove shield use."""
    blaze = {
        "id": 42,
        "type": "minecraft:blaze",
        "distance": 1.2,
        "position": {"x": 1, "y": 64, "z": 0},
    }

    class UnverifiedShieldTransport(CombatTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_inventory":
                return {
                    "inventory": [],
                    "offhand": [{"id": "minecraft:shield", "count": 1}],
                }
            if route == "use_item":
                return {
                    "holding": True,
                    "held_item": "minecraft:shield",
                }
            if route == "attack_entity":
                raise AssertionError("unverified shielding must abort before attack")
            return super().dispatch(route, payload)

    transport = UnverifiedShieldTransport(health=20.0)
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(
        combat,
        "_get_combat_snapshot",
        lambda *_args, **_kwargs: {
            "player": {"health": 20.0},
            "entities": [blaze],
        },
    )
    monkeypatch.setattr(combat_melee.time, "sleep", lambda _seconds: None)

    assert not combat.safe_combat(
        client, blaze["id"], no_retreat=True, max_duration=2
    )
    assert ("cancel", {}) in transport.calls
    assert not any(route == "attack_entity" for route, _ in transport.calls)


def test_melee_target_also_raises_a_carried_shield(monkeypatch):
    """A shield blocks melee damage as well as projectiles, so an ordinary
    zombie must not be denied one just because it isn't a ranged attacker.
    Live A1 2026-09-06 death telemetry showed is_blocking=false in every
    one of 15 deaths, 6 of them to zombies, because shield-raising was
    gated entirely behind _is_ranged_target -- this mirrors the proven
    close-range-blaze scenario above with a zombie instead, to confirm
    the same mechanics now apply regardless of attack style.
    """
    zombie = {
        "id": 42,
        "type": "minecraft:zombie",
        "distance": 1.2,
        "position": {"x": 1, "y": 64, "z": 0},
    }

    class ShieldTransport(CombatTransport):
        def __init__(self):
            super().__init__(health=20.0)
            self.equipped = False

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_inventory":
                return {
                    "inventory": [] if self.equipped else [
                        {"id": "minecraft:shield", "count": 1}
                    ],
                    "offhand": [
                        {"id": "minecraft:shield", "count": 1}
                    ] if self.equipped else [],
                }
            if route == "equip":
                self.equipped = True
                return {"equipped": True}
            if route == "use_item":
                return {
                    "holding": True,
                    "hand": "OFF_HAND",
                    "active_hand": "OFF_HAND",
                    "held_item": "minecraft:shield",
                    "is_using_item": True,
                }
            if route == "attack_entity":
                self.attacked = True
                return {"attacked": True}
            return super().dispatch(route, payload)

    transport = ShieldTransport()
    transport.attacked = False
    client = SimpleNamespace(transport=transport)
    clock = {"now": 0.0}

    def monotonic():
        clock["now"] += 0.1
        return clock["now"]

    def snapshot(*_args, **_kwargs):
        target = {**zombie, "health": 0} if transport.attacked else zombie
        return {"player": {"health": 20.0}, "entities": [target]}

    monkeypatch.setattr(combat, "_get_combat_snapshot", snapshot)
    monkeypatch.setattr(combat, "equip_best_weapon", lambda _client: True)
    monkeypatch.setattr(combat, "look_at_entity", lambda *_args: True)
    monkeypatch.setattr(combat, "_attack_cooldown", lambda _state: 1.0)
    monkeypatch.setattr(combat_melee.time, "monotonic", monotonic)
    monkeypatch.setattr(combat_melee.time, "sleep", lambda _seconds: None)

    assert combat.safe_combat(
        client,
        zombie["id"],
        no_retreat=True,
        max_duration=2,
    )
    shield_call = transport.calls.index(
        (
            "use_item",
            {"hand": "OFF_HAND", "duration_ms": combat_melee.SHIELD_HOLD_MS},
        )
    )
    attack_call = transport.calls.index(
        (
            "attack_entity",
            {
                "entity_id": zombie["id"],
                "min_cooldown": combat.MELEE_ATTACK_COOLDOWN_THRESHOLD,
            },
        )
    )
    assert shield_call < attack_call


def test_hunt_steps_onto_kill_position_before_rechecking_loot(monkeypatch):
    target = {
        "id": 42,
        "type": "minecraft:cow",
        "distance": 3.0,
        "position": {"x": 8.8, "y": 64.0, "z": -2.2},
    }
    client = SimpleNamespace(transport=CombatTransport())
    leather = {"count": 0}
    loot_goals = []
    monkeypatch.setattr(
        combat,
        "count_item",
        lambda _client, item_id: leather["count"]
        if item_id == "minecraft:leather"
        else 0,
    )
    monkeypatch.setattr(combat, "heal_if_needed", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(combat, "find_entity_by_type", lambda *_args, **_kwargs: target)
    monkeypatch.setattr(combat, "safe_combat", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(combat.time, "sleep", lambda _seconds: None)

    def collect(_client, x, y, z, **kwargs):
        loot_goals.append((x, y, z, kwargs))
        leather["count"] = 1
        return True

    monkeypatch.setattr(combat, "goto", collect)

    result = combat.hunt_mobs(
        client,
        ["cow"],
        {"minecraft:leather": 1},
        timeout=30,
    )
    assert result.success
    assert loot_goals == [
        (
            8,
            64,
            -2,
            {"timeout": 10, "check_interval": 0.25, "tolerance": 0.75},
        )
    ]


def test_hostile_hunt_can_commit_then_recover_before_loot_movement(monkeypatch):
    target = {
        "id": 43,
        "type": "minecraft:blaze",
        "distance": 3.0,
        "position": {"x": 3.0, "y": 64.0, "z": 0.0},
    }
    transport = CombatTransport()
    client = SimpleNamespace(transport=transport)
    rods = {"count": 0}
    combat_calls = []
    recovered = []
    sequence = []
    monkeypatch.setattr(
        combat,
        "count_item",
        lambda _client, item_id: rods["count"]
        if item_id == "minecraft:blaze_rod"
        else 0,
    )
    monkeypatch.setattr(combat, "heal_if_needed", lambda *_a, **_k: False)
    monkeypatch.setattr(combat, "find_entity_by_type", lambda *_a, **_k: target)

    def fight(_client, _target_id, **kwargs):
        combat_calls.append(kwargs)
        sequence.append("fight")
        rods["count"] = 1
        transport.health = 9.0
        return True

    def recover(_client, **kwargs):
        recovered.append(kwargs)
        sequence.append("recover")
        transport.health = 16.0
        return True

    def move(_client, *_position, **_kwargs):
        sequence.append("move")
        return True

    monkeypatch.setattr(combat, "safe_combat", fight)
    monkeypatch.setattr(combat, "recover_health", recover)
    monkeypatch.setattr(combat, "goto", move)
    monkeypatch.setattr(combat.time, "sleep", lambda _seconds: None)

    result = combat.hunt_mobs(
        client,
        ["blaze"],
        {"minecraft:blaze_rod": 1},
        heal_threshold=16.0,
        no_retreat=True,
        recover_after_combat=True,
        recovery_anchor=(24, 64, 0),
    )

    assert result.success
    assert combat_calls[0]["no_retreat"] is True
    assert recovered == [{"minimum_health": 16.0, "timeout": 30.0}]
    assert sequence == ["fight", "move", "recover", "move"]


def test_passive_hunt_stops_at_daylight_return_boundary(monkeypatch):
    transport = CombatTransport()
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(combat, "count_item", lambda *_args: 0)
    monkeypatch.setattr(
        combat,
        "find_entity_by_type",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("late expedition must not select another target")
        ),
    )
    original_dispatch = transport.dispatch

    def dispatch(route, payload):
        if route == "get_state":
            return {
                "health": 20,
                "world_time": 9000,
                "block_position": {"x": 0, "y": 64, "z": 0},
            }
        return original_dispatch(route, payload)

    transport.dispatch = dispatch

    result = combat.hunt_mobs(
        client,
        ["cow"],
        {"minecraft:leather": 1},
        latest_world_time=9000,
    )
    assert not result.success
    assert ("cancel", {}) in transport.calls


def test_hunt_uses_explicit_exploration_center_when_no_targets(monkeypatch):
    transport = CombatTransport()
    client = SimpleNamespace(transport=transport)
    clock = iter((0.0, 0.0, 0.0, 31.0, 31.0))
    monkeypatch.setattr(combat.time, "time", lambda: next(clock, 31.0))
    monkeypatch.setattr(combat.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(combat, "count_item", lambda *_args: 0)
    monkeypatch.setattr(combat, "heal_if_needed", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(combat, "find_entity_by_type", lambda *_args, **_kwargs: None)

    result = combat.hunt_mobs(
        client,
        ["cow"],
        {"minecraft:leather": 1},
        timeout=30,
        exploration_center=(-6, -250),
    )
    assert not result.success
    assert (
        "explore",
        {"x": -6, "z": -250},
    ) in transport.calls
    assert (
        "chat",
        {"message": "#explore"},
    ) not in transport.calls


def test_hunt_stages_toward_center_when_explore_goal_stays_idle(monkeypatch):
    class IdleExploreTransport(CombatTransport):
        def dispatch(self, route, payload):
            if route == "get_state":
                self.calls.append((route, payload))
                return {
                    "health": 20,
                    "food_level": 20,
                    "world_time": 1000,
                    "is_pathing": False,
                    "block_position": {"x": 0, "y": 64, "z": 0},
                }
            return super().dispatch(route, payload)

    class ReachedSectorFallback(Exception):
        pass

    transport = IdleExploreTransport()
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(combat.time, "time", lambda: 0.0)
    monkeypatch.setattr(combat.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(combat, "count_item", lambda *_args: 0)
    monkeypatch.setattr(combat, "heal_if_needed", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(combat, "find_entity_by_type", lambda *_args, **_kwargs: None)

    def stage(_client, x, z, **kwargs):
        assert (x, z) == (-6, -250)
        assert kwargs == {"timeout": 45, "tolerance": 24.0}
        raise ReachedSectorFallback()

    monkeypatch.setattr(combat, "goto_xz", stage)

    with pytest.raises(ReachedSectorFallback):
        combat.hunt_mobs(
            client,
            ["cow"],
            {"minecraft:leather": 1},
            timeout=30,
            exploration_center=(-6, -250),
        )
    assert ("explore", {"x": -6, "z": -250}) in transport.calls
    assert ("chat", {"message": "#stop"}) in transport.calls


def test_hunt_returns_after_three_rejected_sector_routes(monkeypatch):
    class IdleExploreTransport(CombatTransport):
        def dispatch(self, route, payload):
            if route == "get_state":
                self.calls.append((route, payload))
                return {
                    "health": 20,
                    "food_level": 20,
                    "world_time": 1000,
                    "is_pathing": False,
                    "block_position": {"x": 0, "y": 64, "z": 0},
                }
            return super().dispatch(route, payload)

    transport = IdleExploreTransport()
    client = SimpleNamespace(transport=transport)
    route_attempts = []
    monkeypatch.setattr(combat.time, "time", lambda: 0.0)
    monkeypatch.setattr(combat.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(combat, "count_item", lambda *_args: 0)
    monkeypatch.setattr(combat, "heal_if_needed", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(combat, "find_entity_by_type", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        combat,
        "goto_xz",
        lambda *_args, **_kwargs: route_attempts.append(True) or False,
    )

    result = combat.hunt_mobs(
        client,
        ["cow"],
        {"minecraft:leather": 1},
        timeout=30,
        exploration_center=(-6, -250),
    )
    assert not result.success
    assert result.reason == "Passive-hunt exploration routes were rejected"
    assert route_attempts == [True, True, True]


def test_hunt_eats_before_selecting_another_target(monkeypatch):
    """Hunting a non-food mob (e.g. blaze/enderman) with no carried food and
    no way to eat must stop rather than keep fighting hungry -- unlike a
    food-yielding hunt, killing a blaze can't resolve the hunger itself."""
    class HungryTransport(CombatTransport):
        def dispatch(self, route, payload):
            if route == "get_state":
                return {
                    "health": 20,
                    "food_level": 8,
                    "world_time": 2000,
                    "block_position": {"x": 0, "y": 64, "z": 0},
                }
            return super().dispatch(route, payload)

    client = SimpleNamespace(transport=HungryTransport())
    fed = []
    monkeypatch.setattr(combat, "count_item", lambda *_args: 0)
    monkeypatch.setattr(
        combat,
        "eat_until_hunger",
        lambda _client, minimum_food: fed.append(minimum_food) or False,
    )
    monkeypatch.setattr(
        combat,
        "find_entity_by_type",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("hunger must be handled before target selection")
        ),
    )

    result = combat.hunt_mobs(
        client,
        ["blaze"],
        {"minecraft:blaze_rod": 1},
        timeout=30,
    )
    assert not result.success
    assert fed == [14]


def test_hunt_continues_hunting_food_animal_without_carried_food(monkeypatch):
    """Hunting a food-yielding mob (cow/mooshroom/sheep/pig/chicken/rabbit)
    with no carried food must keep hunting instead of aborting: killing the
    target itself restocks food. Aborting here deadlocked forever live --
    Bot07 got stuck retrying "Gather 46 leather" with an empty supply chest
    and no food anywhere, since the leather hunt IS the only path to food."""
    class HungryTransport(CombatTransport):
        def dispatch(self, route, payload):
            if route == "get_state":
                return {
                    "health": 20,
                    "food_level": 8,
                    "world_time": 2000,
                    "block_position": {"x": 0, "y": 64, "z": 0},
                }
            return super().dispatch(route, payload)

    client = SimpleNamespace(transport=HungryTransport())
    fed = []
    monkeypatch.setattr(combat, "count_item", lambda *_args: 0)
    monkeypatch.setattr(
        combat,
        "eat_until_hunger",
        lambda _client, minimum_food: fed.append(minimum_food) or False,
    )

    class ReachedTargetSelection(Exception):
        pass

    def _reached_target_selection(*_args, **_kwargs):
        raise ReachedTargetSelection()

    monkeypatch.setattr(combat, "find_entity_by_type", _reached_target_selection)

    with pytest.raises(ReachedTargetSelection):
        combat.hunt_mobs(
            client,
            ["cow"],
            {"minecraft:leather": 1},
            timeout=30,
        )
    assert fed == [14]


def test_hunt_does_not_classify_requested_hostile_as_other_hostile(monkeypatch):
    client = SimpleNamespace(transport=CombatTransport())
    zombie = {
        "id": 42,
        "type": "minecraft:zombie",
        "distance": 5.0,
        "position": {"x": 5, "y": 64, "z": 0},
    }
    monkeypatch.setattr(combat, "count_item", lambda *_args: 0)
    monkeypatch.setattr(combat, "heal_if_needed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        combat, "get_nearby_entities", lambda *_args, **_kwargs: [zombie]
    )

    class ReachedTargetSelection(Exception):
        pass

    monkeypatch.setattr(
        combat,
        "find_entity_by_type",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ReachedTargetSelection()
        ),
    )

    with pytest.raises(ReachedTargetSelection):
        combat.hunt_mobs(
            client,
            ["zombie"],
            {"minecraft:rotten_flesh": 1},
            timeout=30,
            abort_on_other_hostiles=True,
        )


def test_passive_hunt_defends_against_the_intruder_before_giving_up(monkeypatch):
    """Aborting the hunt must not leave the bot standing next to the threat.

    Live: a zombie repeatedly killed the bot outright during "Gather 46
    leather" -- this check correctly detected the zombie and aborted the cow
    hunt every time, but nothing ever fought or fled it, so the bot just
    stood there and absorbed hits until one cycle lost the healing race.
    """
    client = SimpleNamespace(transport=CombatTransport())
    zombie = {
        "id": 42,
        "type": "minecraft:zombie",
        "distance": 5.0,
        "position": {"x": 5, "y": 64, "z": 0},
    }
    monkeypatch.setattr(combat, "count_item", lambda *_args: 0)
    monkeypatch.setattr(combat, "heal_if_needed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        combat, "get_nearby_entities", lambda *_args, **_kwargs: [zombie]
    )
    defended = []
    monkeypatch.setattr(
        combat, "defend_or_flee", lambda _client: defended.append(True) or False
    )

    result = combat.hunt_mobs(
        client,
        ["cow"],
        {"minecraft:leather": 1},
        timeout=30,
        abort_on_other_hostiles=True,
    )
    assert not result.success
    assert "zombie" in result.reason
    assert defended == [True]


def test_get_nearby_entities_raises_on_bridge_failure_when_requested():
    """A failed bridge query must be distinguishable from a genuinely empty
    area. Silently returning [] on a transport error made a route-timeout
    flood look like "no animals nearby" -- Bot07 hunted leather for minutes
    with a cow three blocks away because every get_entities call was timing
    out and being swallowed into an empty list."""
    class FailingTransport:
        def dispatch(self, route, payload):
            raise TimeoutError("Read route get_entities timed out")

    client = SimpleNamespace(transport=FailingTransport())

    # Default: best-effort, returns [] (preserves existing callers).
    assert combat.get_nearby_entities(client, 64) == []

    # Opt-in: surfaces the failure instead of masking it as empty.
    with pytest.raises(combat.EntityQueryError):
        combat.get_nearby_entities(client, 64, raise_on_error=True)


def test_hunt_pauses_scan_on_bridge_failure_instead_of_exploring(monkeypatch):
    """When the entity query fails mid-hunt, hunt_mobs must NOT conclude the
    area is empty and wander off exploring -- the mobs it wants may be right
    here; the bridge simply couldn't answer. It should pause and let the
    route-timeout-flood recovery restore the bridge. Confirmed live: Bot07
    explored for leather with a cow three blocks away during a bridge
    degradation."""
    class DegradedTransport(CombatTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {
                    "health": 20,
                    "food_level": 20,
                    "world_time": 2000,
                    "block_position": {"x": 0, "y": 64, "z": 0},
                }
            return {}

    transport = DegradedTransport()
    client = SimpleNamespace(transport=transport)
    clock = iter((0.0, 0.0, 0.0, 31.0, 31.0))
    monkeypatch.setattr(combat.time, "time", lambda: next(clock, 31.0))
    monkeypatch.setattr(combat.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(combat, "count_item", lambda *_args: 0)
    monkeypatch.setattr(combat, "heal_if_needed", lambda *_args, **_kwargs: False)

    def _bridge_down(*_args, **_kwargs):
        raise combat.EntityQueryError("Read route get_entities timed out")

    monkeypatch.setattr(combat, "find_entity_by_type", _bridge_down)

    result = combat.hunt_mobs(
        client,
        ["cow"],
        {"minecraft:leather": 1},
        timeout=30,
        exploration_center=(-6, -250),
    )
    assert not result.success
    # Must not have wandered off exploring on a bridge failure.
    assert ("explore", {"x": -6, "z": -250}) not in transport.calls
    assert ("chat", {"message": "#explore"}) not in transport.calls


def test_survival_tick_surfaces_submerged_player(monkeypatch):
    client = SimpleNamespace(
        transport=SubmergedTransport("minecraft:water"), _submersion_ticks=1
    )
    surfaced = []
    monkeypatch.setattr(
        combat,
        "_surface_after_aquatic_hunt",
        lambda _c, **_k: surfaced.append(True) or True,
    )
    state = {"block_position": {"x": 0, "y": 62, "z": 0}}

    assert combat.survival_tick(client, state) is True
    assert surfaced == [True]


def test_survival_tick_noop_when_dry():
    client = SimpleNamespace(transport=SubmergedTransport("minecraft:air"))
    assert combat.survival_tick(
        client, {"block_position": {"x": 0, "y": 70, "z": 0}}
    ) is False


def test_survival_tick_equips_leather_boots_in_freezing_biome(monkeypatch):
    """THE A1Bot 2026-09-07 bug: armor_upkeep's own opportunity check only
    runs at objective-selection boundaries, so a bot that walks into a
    freezing biome mid-task -- even one already carrying leather boots --
    froze to death (last_damage_source: freeze) with no "freezing biome" log
    line anywhere in the run-up. survival_tick is the shared reflex every
    long-running wait loop already polls each tick for drowning; the same
    swap must fire from there too.
    """
    from baritone_client.automator import armor_upkeep

    client = SimpleNamespace(transport=SubmergedTransport("minecraft:air"))
    state = {"block_position": {"x": 0, "y": 70, "z": 0}, "biome": "minecraft:grove"}
    monkeypatch.setattr(armor_upkeep, "wearing_freeze_boots", lambda _c: False)
    equipped = []
    monkeypatch.setattr(
        armor_upkeep, "equip_freeze_boots", lambda _c: equipped.append(True) or True
    )

    assert combat.survival_tick(client, state) is True
    assert equipped == [True]


def test_survival_tick_freeze_guard_noop_when_already_wearing_leather_boots(
    monkeypatch,
):
    from baritone_client.automator import armor_upkeep

    client = SimpleNamespace(transport=SubmergedTransport("minecraft:air"))
    state = {"block_position": {"x": 0, "y": 70, "z": 0}, "biome": "minecraft:grove"}
    monkeypatch.setattr(armor_upkeep, "wearing_freeze_boots", lambda _c: True)
    monkeypatch.setattr(
        armor_upkeep,
        "equip_freeze_boots",
        lambda *_a: (_ for _ in ()).throw(
            AssertionError("must not re-equip boots already worn")
        ),
    )

    assert combat.survival_tick(client, state) is False


def test_survival_tick_freeze_guard_noop_outside_freezing_biomes(monkeypatch):
    from baritone_client.automator import armor_upkeep

    client = SimpleNamespace(transport=SubmergedTransport("minecraft:air"))
    state = {"block_position": {"x": 0, "y": 70, "z": 0}, "biome": "minecraft:plains"}
    monkeypatch.setattr(
        armor_upkeep,
        "equip_freeze_boots",
        lambda *_a: (_ for _ in ()).throw(
            AssertionError("must not touch boots outside a freezing biome")
        ),
    )

    assert combat.survival_tick(client, state) is False


def test_submerged_too_long_times_out_then_fires(monkeypatch):
    client = SimpleNamespace(transport=SubmergedTransport("minecraft:water"))
    clock = iter([100.0, 109.0])
    monkeypatch.setattr(combat.time, "time", lambda: next(clock))
    state = {"block_position": {"x": 0, "y": 62, "z": 0}}

    # First call starts the submersion timer, not yet over the limit.
    assert combat._submerged_too_long(client, state, max_seconds=8.0) is False
    assert client._submerged_since == 100.0
    # 9s later -> over the 8s limit.
    assert combat._submerged_too_long(client, state, max_seconds=8.0) is True


def test_submerged_too_long_resets_when_head_clears():
    client = SimpleNamespace(
        transport=SubmergedTransport("minecraft:air"), _submerged_since=100.0
    )
    assert (
        combat._submerged_too_long(
            client, {"block_position": {"x": 0, "y": 70, "z": 0}}, max_seconds=8.0
        )
        is False
    )
    assert client._submerged_since is None


def test_safe_combat_surfaces_and_aborts_when_submerged_too_long(monkeypatch):
    client = SimpleNamespace(transport=CombatTransport(health=20.0))
    monkeypatch.setattr(combat, "_get_combat_snapshot", lambda *_a, **_k: None)
    monkeypatch.setattr(combat, "get_nearby_entities", lambda *_a, **_k: [])
    monkeypatch.setattr(combat, "equip_best_weapon", lambda _c: True)
    monkeypatch.setattr(combat, "_submerged_too_long", lambda *_a, **_k: True)
    monkeypatch.setattr(combat.time, "sleep", lambda _s: None)
    surfaced = []
    monkeypatch.setattr(
        combat,
        "_surface_after_aquatic_hunt",
        lambda _c, **_k: surfaced.append(True) or True,
    )

    assert combat.safe_combat(client, target_id=5) is False
    assert surfaced == [True]


def test_approach_aquatic_food_surfaces_when_submerged_too_long(monkeypatch):
    client = SimpleNamespace(transport=SubmergedTransport("minecraft:water"))
    monkeypatch.setattr(combat, "_submerged_too_long", lambda *_a, **_k: True)
    monkeypatch.setattr(combat.time, "sleep", lambda _s: None)
    surfaced = []
    monkeypatch.setattr(
        combat,
        "_surface_after_aquatic_hunt",
        lambda _client, **_kwargs: surfaced.append(True) or True,
    )

    assert (
        combat._approach_aquatic_food(client, target_id=7, entity_type="minecraft:cod")
        is False
    )
    assert surfaced == [True]


def test_repeated_failed_evasion_escalates_to_fighting_back(monkeypatch):
    """Regression for a live cornering: an unarmored bot in mountainous
    terrain got a zombie stuck in melee range for dozens of consecutive
    cycles -- armor_count<3 forces EVADE unconditionally, run_away kept
    returning False ("no terrain-safe escape endpoint found" -- every
    candidate route was rejected for having open air/ledges nearby), and the
    bot slowly died doing nothing but failing to flee. Pure evasion with a
    demonstrated 0% success rate against a threat must eventually escalate
    to fighting, since continuing it is strictly worse than even an
    unfavorable fight."""
    transport = CombatTransport(health=20.0)
    client = SimpleNamespace(transport=transport)
    threat = {
        "id": 42,
        "type": "minecraft:zombie",
        "distance": 8.0,
        "position": {"x": 8, "y": 64, "z": 0},
    }
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_a, **_k: [threat])
    monkeypatch.setattr(combat, "get_equipped_armor", lambda _c: {})  # 0/4 armor
    monkeypatch.setattr(combat, "run_away", lambda *_a, **_k: False)
    monkeypatch.setattr(combat.time, "sleep", lambda _s: None)
    fought = []
    monkeypatch.setattr(
        combat,
        "safe_combat",
        lambda *_a, **_k: fought.append(True) or True,
    )

    # Same runtime persists across calls (stashed on the client). One failed
    # escape gets another chance; the second must act before incoming damage
    # makes any recovery impossible.
    assert combat.defend_or_flee(client)
    assert not fought
    assert combat.defend_or_flee(client)
    assert fought == [True]


def test_critical_recovery_waits_for_completed_food_use(monkeypatch):
    class CriticalTransport(CombatTransport):
        def dispatch(self, route, payload):
            if route == "get_state":
                self.calls.append((route, payload))
                return {
                    "health": 4.0,
                    "food_level": 15,
                    "block_position": {"x": 0, "y": 64, "z": 0},
                }
            return super().dispatch(route, payload)

    client = SimpleNamespace(transport=CriticalTransport(health=4.0))
    completed_food = []
    async_heals = []
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        combat,
        "eat_until_hunger",
        lambda _client, minimum_food: completed_food.append(minimum_food) or True,
    )
    monkeypatch.setattr(
        combat,
        "heal_if_needed",
        lambda *_args, **_kwargs: async_heals.append(True) or True,
    )

    assert combat.defend_or_flee(client)
    assert completed_food == [18]
    assert async_heals == []


def test_evasion_failure_count_survives_primary_threat_switch(monkeypatch):
    """A crowded cave must not reset escape failure whenever scoring chooses
    a different attacker on the next tick."""
    transport = CombatTransport(health=20.0)
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(combat, "get_equipped_armor", lambda _c: {})
    monkeypatch.setattr(combat, "run_away", lambda *_a, **_k: False)
    monkeypatch.setattr(combat.time, "sleep", lambda _s: None)
    fought = []
    monkeypatch.setattr(
        combat, "safe_combat", lambda *_a, **_k: fought.append(True) or True
    )

    threat_a = {
        "id": 1, "type": "minecraft:zombie", "distance": 8.0,
        "position": {"x": 8, "y": 64, "z": 0},
    }
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_a, **_k: [threat_a])
    combat.defend_or_flee(client)

    threat_b = {
        "id": 2, "type": "minecraft:zombie", "distance": 8.0,
        "position": {"x": -8, "y": 64, "z": 0},
    }
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_a, **_k: [threat_b])
    assert combat.defend_or_flee(client)
    assert fought == [True]


def test_no_escape_endpoint_fights_non_explosive_threat_immediately(monkeypatch):
    """A cave with no valid flee tile must not wait for a second outer tick.

    Live Bot16 entered this branch at full health with two creepers, a
    skeleton, and a zombie nearby. Escape planning failed while damage kept
    arriving, and the caller moved on to recovery instead of invoking defense
    again. Commit to the close zombie on the first proven terrain failure.
    """
    transport = CombatTransport(health=20.0)
    client = SimpleNamespace(transport=transport)
    threats = [
        {
            "id": 10,
            "type": "minecraft:zombie",
            "distance": 6.5,
            "position": {"x": 6, "y": 64, "z": 0},
        },
        {
            "id": 11,
            "type": "minecraft:creeper",
            "distance": 10.5,
            "position": {"x": -10, "y": 64, "z": 0},
        },
    ]
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_a, **_k: threats)
    monkeypatch.setattr(
        combat,
        "get_equipped_armor",
        lambda _client: {slot: {} for slot in ("head", "chest", "legs", "feet")},
    )
    monkeypatch.setattr(combat, "equip_best_weapon", lambda _client: True)

    def no_endpoint(value, _target):
        value._last_escape_failure_reason = "no_safe_endpoint"
        return False

    monkeypatch.setattr(combat, "run_away", no_endpoint)
    fought = []
    monkeypatch.setattr(
        combat,
        "safe_combat",
        lambda _client, entity_id, **kwargs: fought.append((entity_id, kwargs)) or True,
    )

    assert combat.defend_or_flee(client)
    assert fought == [
        (
            10,
            {
                "purpose": "hostile_defense",
                "source": "defend_or_flee",
                "target_metadata": threats[0],
                "no_retreat": True,
                "abort_on_other_hostiles": False,
                "max_duration": 12,
            },
        )
    ]


def test_no_escape_endpoint_never_melees_primary_creeper(monkeypatch):
    client, _transport = _evade_client(monkeypatch, "creeper")

    def no_endpoint(value, _target):
        value._last_escape_failure_reason = "no_safe_endpoint"
        return False

    monkeypatch.setattr(combat, "run_away", no_endpoint)
    monkeypatch.setattr(combat, "equip_best_weapon", lambda _client: True)
    fought = []
    monkeypatch.setattr(
        combat, "safe_combat", lambda *_a, **_k: fought.append(True) or True
    )

    assert combat.defend_or_flee(client)
    assert not fought


def test_a_successful_evade_never_escalates(monkeypatch):
    transport = CombatTransport(health=20.0)
    client = SimpleNamespace(transport=transport)
    threat = {
        "id": 7, "type": "minecraft:zombie", "distance": 8.0,
        "position": {"x": 8, "y": 64, "z": 0},
    }
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_a, **_k: [threat])
    monkeypatch.setattr(combat, "get_equipped_armor", lambda _c: {})
    monkeypatch.setattr(combat, "run_away", lambda *_a, **_k: True)
    monkeypatch.setattr(combat.time, "sleep", lambda _s: None)
    fought = []
    monkeypatch.setattr(
        combat, "safe_combat", lambda *_a, **_k: fought.append(True) or True
    )

    for _ in range(6):
        combat.defend_or_flee(client)

    assert not fought


def test_safe_combat_no_retreat_attacks_despite_low_health(monkeypatch):
    """Regression: the escalation path in defend_or_flee passes
    no_retreat=True specifically because a fixed retreat_health floor kept
    getting undercut -- health crashes unpredictably during the failed
    flee attempts that precede escalation, so by the time safe_combat is
    called it is often already below whatever floor was configured, and it
    was retreating on the very first health check without ever attacking.
    Live: this happened at 4.8hp (floor=6.0) and again at 1.999hp
    (floor=2.0). no_retreat=True must skip that gate and actually attack."""
    transport = CombatTransport(health=1.5)
    client = SimpleNamespace(transport=transport)
    target = {"id": 5, "type": "minecraft:zombie", "distance": 3.0}
    monkeypatch.setattr(combat, "equip_best_weapon", lambda *_a, **_k: True)
    monkeypatch.setattr(combat, "_get_combat_snapshot", lambda *_a, **_k: None)
    monkeypatch.setattr(
        combat, "get_nearby_entities", lambda *_a, **_k: [target]
    )
    monkeypatch.setattr(combat, "_submerged_too_long", lambda *_a, **_k: False)
    monkeypatch.setattr(combat, "look_at_entity", lambda *_a, **_k: True)
    monkeypatch.setattr(combat, "_attack_cooldown", lambda *_a, **_k: 999.0)
    monkeypatch.setattr(combat.time, "sleep", lambda _s: None)

    def dispatch(route, payload):
        transport.calls.append((route, payload))
        if route == "get_state":
            return {"health": transport.health, "block_position": {"x": 0, "y": 64, "z": 0}}
        if route == "attack_entity":
            return {"attacked": True}
        return {}

    transport.dispatch = dispatch

    combat.safe_combat(client, 5, retreat_health=6.0, no_retreat=True, max_duration=0.2)

    assert ("attack_entity", {"entity_id": 5, "min_cooldown": combat.MELEE_ATTACK_COOLDOWN_THRESHOLD}) in transport.calls
    assert not any(call[0] == "cancel" for call in transport.calls)


def test_safe_combat_still_retreats_by_default_at_low_health(monkeypatch):
    transport = CombatTransport(health=1.5)
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(combat, "equip_best_weapon", lambda *_a, **_k: None)
    monkeypatch.setattr(combat, "_get_combat_snapshot", lambda *_a, **_k: None)
    monkeypatch.setattr(combat, "get_nearby_entities", lambda *_a, **_k: [])
    monkeypatch.setattr(combat, "_submerged_too_long", lambda *_a, **_k: False)

    assert combat.safe_combat(client, 5, retreat_health=6.0) is False
    assert ("cancel", {}) in transport.calls
    assert not any(call[0] == "attack_entity" for call in transport.calls)


def test_emergency_food_does_not_chase_distant_target_at_critical_health(monkeypatch):
    """Regression: must_hold_for_critical_food only holds below health 6.0, so
    a bot at e.g. 7.3hp is treated as "safe enough" to proceed and would
    otherwise walk toward the nearest singleton target regardless of distance
    -- with no further health check until arrival. Confirmed live: Bot08
    repeatedly walked 48-64m toward a chicken/salmon at ~7hp and died to
    combat or drowning partway there. Below _CRITICAL_HUNT_HEALTH the search
    radius shrinks to 16 (the same perimeter already trusted for the
    immediate-threat scan), so a distant-only target must not be selected."""
    transport = CombatTransport(health=7.3)
    client = SimpleNamespace(transport=transport)
    distant_chicken = {
        "id": 40,
        "type": "minecraft:chicken",
        "distance": 60.0,
        "position": {"x": 60, "y": 64, "z": 0},
    }
    monkeypatch.setattr(combat, "recover_health", lambda *_a, **_k: False)
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_a, **_k: [])
    hunted = []
    monkeypatch.setattr(
        combat,
        "get_nearby_entities",
        lambda _client, radius: [distant_chicken] if radius == 64 else [],
    )
    monkeypatch.setattr(combat, "safe_combat", lambda *_a, **_k: hunted.append(True) or True)
    monkeypatch.setattr(combat.time, "sleep", lambda _s: None)

    combat.acquire_emergency_food(client, minimum_health=12.0, timeout=1.0)

    assert not hunted


def _evade_client(monkeypatch, mob_type, *, escaped=False):
    """Client wired so defend_or_flee always takes the EVADE branch and
    run_away always fails, so repeated ticks accumulate evade failures."""
    transport = CombatTransport(health=20.0)
    client = SimpleNamespace(transport=transport)
    threat = {
        "id": 77,
        "type": f"minecraft:{mob_type}",
        "distance": 5.5,
        "position": {"x": 5, "y": 64, "z": 0},
    }
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_a, **_k: [threat])
    monkeypatch.setattr(combat, "get_equipped_armor", lambda _c: {})
    monkeypatch.setattr(combat, "run_away", lambda *_a, **_k: escaped)
    monkeypatch.setattr(combat.time, "sleep", lambda _s: None)
    return client, transport


def test_repeated_evasion_never_melees_a_creeper(monkeypatch):
    """A meleed creeper detonates -- that is exactly why its ThreatProfile is
    always_evade/EXPLOSIVE. The evade-escalation added for cornered zombies
    originally fired on ANY failed evasion, so it live-fired against creepers
    364x on Bot07 and 367x on Bot08. It must relocate instead of fighting."""
    client, _t = _evade_client(monkeypatch, "creeper")
    fought = []
    relocated = []
    monkeypatch.setattr(combat, "safe_combat", lambda *_a, **_k: fought.append(True) or True)
    monkeypatch.setattr(
        combat, "_relocate_away_from", lambda *_a, **_k: relocated.append(True) or True
    )

    for _ in range(5):
        assert combat.defend_or_flee(client)

    assert not fought, "must never melee an EXPLOSIVE threat"
    assert relocated, "should break the standoff by relocating"


def test_repeated_evasion_still_fights_back_against_a_zombie(monkeypatch):
    """The escalation's original purpose: a zombie is only in EVADE because of
    the armor_count<3 gate, not because it is unfightable by policy. That case
    must still escalate."""
    client, _t = _evade_client(monkeypatch, "zombie")
    fought = []
    relocated = []
    monkeypatch.setattr(combat, "safe_combat", lambda *_a, **_k: fought.append(True) or True)
    monkeypatch.setattr(
        combat, "_relocate_away_from", lambda *_a, **_k: relocated.append(True) or True
    )

    for _ in range(5):
        assert combat.defend_or_flee(client)

    assert fought, "a cornered zombie should still be fought as a last resort"
    assert not relocated


@pytest.mark.parametrize("blaze_count", [1, 2])
def test_full_armor_and_shield_engages_small_close_blaze_pack(
    monkeypatch, blaze_count
):
    transport = CombatTransport(health=20.0)
    client = SimpleNamespace(transport=transport)
    blaze = {
        "id": 79,
        "type": "minecraft:blaze",
        "distance": 1.3,
        "position": {"x": 1, "y": 64, "z": 0},
    }
    threats = [
        {**blaze, "id": blaze["id"] + index, "distance": 1.3 + index * 3}
        for index in range(blaze_count)
    ]
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_a, **_k: threats)
    monkeypatch.setattr(
        combat,
        "get_equipped_armor",
        lambda _client: {slot: {} for slot in ("head", "chest", "legs", "feet")},
    )
    monkeypatch.setattr(
        combat,
        "count_item",
        lambda _client, item: 1 if item == "minecraft:shield" else 0,
    )
    monkeypatch.setattr(combat, "has_durable_full_armor", lambda *_a, **_k: True)
    monkeypatch.setattr(combat, "equip_best_weapon", lambda _client: True)
    escaped = []
    fought = []
    monkeypatch.setattr(
        combat, "run_away", lambda *_a, **_k: escaped.append(True) or True
    )
    monkeypatch.setattr(
        combat,
        "safe_combat",
        lambda _client, entity_id, **kwargs: fought.append((entity_id, kwargs)) or True,
    )

    assert combat.defend_or_flee(client)

    assert not escaped
    assert fought == [
        (
            79,
            {
                "purpose": "hostile_defense",
                "source": "defend_or_flee",
                "target_metadata": blaze,
                "no_retreat": True,
                "abort_on_other_hostiles": False,
            },
        )
    ]


def test_shielded_blaze_exception_rejects_three_urgent_threats(monkeypatch):
    client, _transport = _evade_client(monkeypatch, "blaze", escaped=True)
    threats = [
        {
            "id": 80 + index,
            "type": "minecraft:blaze",
            "distance": 1.0 + index,
            "position": {"x": index + 1, "y": 64, "z": 0},
        }
        for index in range(3)
    ]
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_a, **_k: threats)
    monkeypatch.setattr(
        combat,
        "get_equipped_armor",
        lambda _client: {slot: {} for slot in ("head", "chest", "legs", "feet")},
    )
    monkeypatch.setattr(combat, "count_item", lambda *_a: 1)
    fought = []
    monkeypatch.setattr(
        combat, "safe_combat", lambda *_a, **_k: fought.append(True) or True
    )

    assert combat.defend_or_flee(client)

    assert not fought


def test_switching_attackers_does_not_reset_failed_escape_escalation(monkeypatch):
    transport = CombatTransport(health=20.0)
    client = SimpleNamespace(transport=transport)
    next_id = {"value": 70}

    def rotating_threats(*_args, **_kwargs):
        next_id["value"] += 1
        return [
            {
                "id": next_id["value"],
                "type": "minecraft:bogged",
                "distance": 3.0,
                "position": {"x": 3, "y": 64, "z": 0},
            }
        ]

    monkeypatch.setattr(combat, "scan_for_threats", rotating_threats)
    monkeypatch.setattr(combat, "get_equipped_armor", lambda _c: {})
    monkeypatch.setattr(combat, "run_away", lambda *_a, **_k: False)
    fought = []
    monkeypatch.setattr(
        combat,
        "safe_combat",
        lambda _client, entity_id, **kwargs: fought.append((entity_id, kwargs)) or True,
    )

    assert combat.defend_or_flee(client)
    assert combat.defend_or_flee(client)

    assert fought
    assert fought[0][1]["no_retreat"] is True
    assert fought[0][1]["abort_on_other_hostiles"] is False


def test_failed_creeper_relocation_fights_close_zombie_not_creeper(monkeypatch):
    transport = CombatTransport(health=20.0)
    client = SimpleNamespace(transport=transport)
    threats = [
        {
            "id": 77,
            "type": "minecraft:creeper",
            "distance": 8.0,
            "position": {"x": 8, "y": 64, "z": 0},
        },
        {
            "id": 78,
            "type": "minecraft:zombie",
            "distance": 2.0,
            "position": {"x": 2, "y": 64, "z": 0},
        },
    ]
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_a, **_k: threats)
    monkeypatch.setattr(combat, "get_equipped_armor", lambda _c: {})
    monkeypatch.setattr(combat, "run_away", lambda *_a, **_k: False)
    monkeypatch.setattr(combat, "_relocate_away_from", lambda *_a, **_k: False)
    fought = []
    monkeypatch.setattr(
        combat,
        "safe_combat",
        lambda _client, entity_id, **kwargs: fought.append((entity_id, kwargs)) or True,
    )

    assert combat.defend_or_flee(client)
    assert combat.defend_or_flee(client)

    assert [entity_id for entity_id, _kwargs in fought] == [78]
    assert fought[0][1]["abort_on_other_hostiles"] is False


class _RelocateTransport:
    """Transport whose player walks away from the threat over successive
    get_state polls, so relocation can observe real separation gain."""

    def __init__(self, positions):
        self.calls = []
        self._positions = list(positions)

    def dispatch(self, route, payload, **kwargs):
        self.calls.append((route, payload))
        if route == "get_state":
            x = self._positions[0]
            if len(self._positions) > 1:
                self._positions.pop(0)
            return {"health": 20.0, "block_position": {"x": x, "y": 64, "z": 0},
                    "is_pathing": False}
        if route == "find_blocks":
            return {"found": [{"x": -16, "y": 63, "z": 0}]}
        if route == "get_block":
            return {"id": "minecraft:grass_block" if payload["y"] == 63 else "minecraft:air"}
        if route == "settings":
            return {"value": "false"}
        if route == "get_combat_snapshot":
            player = self.dispatch("get_state", {})
            return {"player": player, "entities": [], "skipped_count": 0}
        return {}


def test_relocate_uses_observed_height_instead_of_blind_horizontal_goal(monkeypatch):
    """Resolve terrain before routing; horizontal goals can select caves."""
    monkeypatch.setattr(combat.time, "sleep", lambda _s: None)
    # Player starts at x=0 and walks to x=-30; threat sits at x=+5.
    transport = _RelocateTransport([0, -12, -30])
    client = SimpleNamespace(transport=transport)
    threat = {"id": 5, "type": "minecraft:creeper", "position": {"x": 5, "y": 64, "z": 0}}

    assert combat._relocate_away_from(client, threat, distance=28)

    assert ("goal", {"x": -16, "y": 64, "z": 0}) in transport.calls
    assert not any(route == "chat" and payload.get("message", "").startswith("#goto")
                   for route, payload in transport.calls)


def test_relocate_reports_failure_when_separation_never_grows(
    monkeypatch, advancing_clock
):
    """A bot pinned in place must report failure so the caller can try
    something else, rather than silently claiming it escaped."""
    from baritone_client.common import escape_recovery

    monkeypatch.setattr(escape_recovery, "time", advancing_clock())
    transport = _RelocateTransport([0])  # never moves
    client = SimpleNamespace(transport=transport)
    threat = {"id": 5, "type": "minecraft:creeper", "position": {"x": 5, "y": 64, "z": 0}}

    assert not combat._relocate_away_from(client, threat, distance=28, timeout=3)


def test_submerged_bot_does_not_target_distant_fish(monkeypatch):
    """Submersion is a reason to surface, not permission for a long pursuit."""
    from baritone_client.common import emergency_food

    looked = []

    def fake_nearby(_client, radius):
        looked.append(radius)
        return [{"id": 7, "type": "minecraft:cod", "distance": 24.0}]

    monkeypatch.setattr(combat, "get_nearby_entities", fake_nearby)

    target = emergency_food.select_target(
        SimpleNamespace(transport=CombatTransport()),
        [],
        current_food=17,          # well above the <=6 emergency gate
        elapsed=0.0,              # and far below the land-search delay
        timeout=240.0,
        renewable_source_callback=None,
        in_water=True,
    )

    assert target is None
    assert looked == [5]


def test_wounded_submerged_bot_limits_aquatic_target_search(monkeypatch):
    from baritone_client.common import emergency_food

    radii = []

    def nearby(_client, radius):
        radii.append(radius)
        if radius == 64:
            return [
                {
                    "id": 7,
                    "type": "minecraft:cod",
                    "distance": 43.0,
                }
            ]
        return []

    monkeypatch.setattr(combat, "get_nearby_entities", nearby)
    target = emergency_food.select_target(
        SimpleNamespace(transport=CombatTransport()),
        [],
        current_food=17,
        elapsed=0.0,
        timeout=240.0,
        renewable_source_callback=None,
        in_water=True,
        aquatic_search_radius=16,
    )

    assert target is None
    assert radii == [5]


def test_dry_bot_still_waits_before_chasing_fish(monkeypatch):
    """The original restriction must survive for a bot on land."""
    from baritone_client.common import emergency_food

    monkeypatch.setattr(
        combat,
        "get_nearby_entities",
        lambda *_a, **_k: [{"id": 1, "type": "minecraft:cod", "distance": 5.0}],
    )

    assert emergency_food.select_target(
        SimpleNamespace(transport=CombatTransport()),
        [],
        current_food=17,
        elapsed=0.0,
        timeout=240.0,
        renewable_source_callback=None,
        in_water=False,
    ) is None


def test_starving_dry_bot_does_not_reenter_water_for_fish(monkeypatch):
    from baritone_client.common import emergency_food

    monkeypatch.setattr(
        combat,
        "get_nearby_entities",
        lambda *_args, **_kwargs: [
            {"id": 1, "type": "minecraft:salmon", "distance": 8.0}
        ],
    )

    assert emergency_food.select_target(
        SimpleNamespace(transport=CombatTransport()),
        [],
        current_food=6,
        elapsed=120.0,
        timeout=240.0,
        renewable_source_callback=None,
        in_water=False,
    ) is None


def test_player_is_in_water_reads_the_feet_block():
    class WaterTransport(CombatTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {"health": 8.0, "block_position": {"x": 0, "y": 62, "z": 0}}
            if route == "get_block":
                return {"id": "minecraft:water"}
            return {}

    client = SimpleNamespace(transport=WaterTransport())
    state = {"block_position": {"x": 0, "y": 62, "z": 0}}
    assert combat._player_is_in_water(client, state)


def test_aquatic_approach_window_scales_with_distance(monkeypatch):
    """A flat 8s follow cannot cover the ~48m separation measured live in a
    lush cave, so every attempt reported "aquatic target could not be reached
    safely" and the bot starved beside its only food source."""
    from baritone_client.common import emergency_food

    seen = {}

    def fake_approach(_c, _id, _type, timeout):
        seen["timeout"] = timeout
        return False  # force the early return; we only care about the window

    monkeypatch.setattr(combat, "_approach_aquatic_food", fake_approach)
    monkeypatch.setattr(emergency_food.time, "sleep", lambda _s: None)

    emergency_food.hunt_target(
        SimpleNamespace(transport=CombatTransport()),
        {"id": 3, "type": "minecraft:cod", "distance": 48.7,
         "position": {"x": 48, "y": 62, "z": 0}},
        minimum_health=12.0,
        recovery_complete=lambda _s=None: False,
    )

    assert seen["timeout"] > 8.0, "window must grow for a distant fish"
    assert seen["timeout"] <= 45.0, "and stay bounded"


def test_aquatic_follow_abandons_a_target_that_never_gets_closer(
    monkeypatch, advancing_clock
):
    """A fish in a sealed flooded chamber is not reachable, and burning the
    whole follow window on it starves the bot. Live: Bot07 reported "safely
    hunting tropical_fish at 46.4m" repeatedly with the distance frozen to the
    decimal, never moving, at 8 health."""
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_a, **_k: [])
    monkeypatch.setattr(combat, "_submerged_too_long", lambda *_a, **_k: False)
    monkeypatch.setattr(combat, "time", advancing_clock())
    # Distance never shrinks.
    monkeypatch.setattr(
        combat,
        "get_nearby_entities",
        lambda *_a, **_k: [
            {"id": 9, "type": "minecraft:cod", "distance": 46.4}
        ],
    )
    client = SimpleNamespace(transport=CombatTransport())

    assert combat._approach_aquatic_food(
        client, 9, "minecraft:cod", timeout=45.0
    ) is False


def test_unreachable_fish_is_not_selected_again(monkeypatch):
    """Without a blacklist the next loop re-selects the same nearest fish and
    retries forever, ignoring other reachable food."""
    from baritone_client.common import emergency_food

    monkeypatch.setattr(
        combat,
        "get_nearby_entities",
        lambda *_a, **_k: [
            {"id": 9, "type": "minecraft:cod", "distance": 3.0},
            {"id": 10, "type": "minecraft:salmon", "distance": 4.0},
        ],
    )
    client = SimpleNamespace(transport=CombatTransport())

    first = emergency_food.select_target(
        client, [], current_food=17, elapsed=0.0, timeout=240.0,
        renewable_source_callback=None, in_water=True,
    )
    assert first["id"] == 9  # nearest wins initially

    second = emergency_food.select_target(
        client, [], current_food=17, elapsed=0.0, timeout=240.0,
        renewable_source_callback=None, in_water=True, unreachable={9},
    )
    assert second["id"] == 10, "must fall through to the next viable fish"


def test_hunt_target_records_the_unreachable_id(monkeypatch):
    from baritone_client.common import emergency_food

    monkeypatch.setattr(combat, "_approach_aquatic_food", lambda *_a, **_k: False)
    monkeypatch.setattr(emergency_food.time, "sleep", lambda _s: None)
    blocked = set()

    emergency_food.hunt_target(
        SimpleNamespace(transport=CombatTransport()),
        {"id": 9, "type": "minecraft:cod", "distance": 46.4,
         "position": {"x": 46, "y": 62, "z": 0}},
        minimum_health=12.0,
        recovery_complete=lambda _s=None: False,
        unreachable=blocked,
    )

    assert blocked == {9}


def test_hunt_target_aborts_after_failed_aquatic_surface(monkeypatch):
    from baritone_client.common import emergency_food

    def fail_surface(client, *_args, **_kwargs):
        client._aquatic_surface_failed = True
        return False

    monkeypatch.setattr(combat, "_approach_aquatic_food", fail_surface)
    client = SimpleNamespace(transport=CombatTransport())

    assert emergency_food.hunt_target(
        client,
        {
            "id": 9,
            "type": "minecraft:salmon",
            "distance": 12.0,
            "position": {"x": 12, "y": 49, "z": 0},
        },
        minimum_health=12.0,
        recovery_complete=lambda _state=None: False,
        unreachable=set(),
    ) is False


def _surface_gate_client(in_water, fish=None, y=62, health=6.0):
    class T(CombatTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {"health": health, "block_position": {"x": 0, "y": y, "z": 0}}
            if route == "get_block":
                return {"id": "minecraft:water" if in_water else "minecraft:air"}
            if route == "get_entities":
                return {"entities": list(fish or [])}
            return {}

    return SimpleNamespace(transport=T())


def test_submerged_bot_may_take_healthy_melee_range_fish():
    """The exception remains bounded to one target already within reach."""
    from baritone_client.common import emergency_food

    client = _surface_gate_client(
        in_water=True,
        fish=[{"id": 1, "type": "minecraft:cod", "distance": 4.0}],
        health=20.0,
    )
    state = {
        "health": 20.0,
        "block_position": {"x": 0, "y": 62, "z": 0},
    }

    assert emergency_food.reach_food_search_surface(client, state) is True
    # It must NOT have attempted an ascent.
    assert not any(r == "goto" for r, _ in client.transport.calls)


def test_submerged_bot_with_no_fish_still_surfaces_first(monkeypatch):
    """Blind exploration while submerged is still gated -- the original
    intent of the check is preserved."""
    from baritone_client.common import emergency_food

    client = _surface_gate_client(in_water=True, fish=[], health=20.0)
    state = {
        "health": 20.0,
        "block_position": {"x": 0, "y": 62, "z": 0},
    }
    ascended = []
    monkeypatch.setattr(
        emergency_food,
        "block_position",
        lambda _s: (0, 62, 0),
    )
    monkeypatch.setattr(
        "baritone_client.common.surface_recovery.reach_dry_surface",
        lambda *_a, **_k: ascended.append(True) or None,
    )
    monkeypatch.setattr(
        "baritone_client.common.build_site_recovery.excavate_surface_egress",
        lambda *_a, **_k: None,
    )

    assert emergency_food.reach_food_search_surface(client, state) is False
    assert ascended, "must still try to surface when there is no visible fish"


def test_critical_submerged_bot_refuses_prolonged_surface_excavation(
    monkeypatch,
):
    """A near-death Bot15 drowned in the excavation fallback."""
    from baritone_client.common import emergency_food

    client = _surface_gate_client(in_water=True, fish=[], health=8.0)
    state = {
        "health": 8.0,
        "block_position": {"x": 0, "y": 62, "z": 0},
    }
    ascended = []
    excavated = []
    monkeypatch.setattr(
        emergency_food,
        "block_position",
        lambda _state: (0, 62, 0),
    )
    monkeypatch.setattr(
        "baritone_client.common.surface_recovery.reach_dry_surface",
        lambda *_args, **_kwargs: ascended.append(True) or None,
    )
    monkeypatch.setattr(
        "baritone_client.common.build_site_recovery.excavate_surface_egress",
        lambda *_args, **_kwargs: excavated.append(True) or None,
    )

    assert not emergency_food.reach_food_search_surface(client, state)
    assert not ascended
    assert not excavated


def test_deep_underground_bot_surfaces_even_with_fish_nearby(monkeypatch):
    """Below MINIMUM_FOOD_SEARCH_Y the depth concern still wins."""
    from baritone_client.common import emergency_food

    client = _surface_gate_client(
        in_water=True,
        fish=[{"id": 1, "type": "minecraft:cod", "distance": 5.0}],
        y=20,
    )
    monkeypatch.setattr(emergency_food, "block_position", lambda _s: (0, 20, 0))
    monkeypatch.setattr(
        "baritone_client.common.surface_recovery.reach_dry_surface",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "baritone_client.common.build_site_recovery.excavate_surface_egress",
        lambda *_a, **_k: None,
    )

    assert emergency_food.reach_food_search_surface(
        client, {"block_position": {"x": 0, "y": 20, "z": 0}}
    ) is False
