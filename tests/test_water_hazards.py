"""A starving search stays out of water and away from known drowning spots.

Live A1 2026-10-04..15: 29 drownings, seven at one riverbed (-411, 61, -142)
below a cliff on the main northbound route. The emergency food search walked
into the river, the dry-search guard cancelled Baritone in the water, and the
bot sank under two blocks of water one block from dry grass.
"""

from types import SimpleNamespace

import pytest

from baritone_client.common import water_hazards as hazards


class _Recorder:
    def __init__(self, positions=None):
        self.calls = []
        self.positions = list(positions or [])

    def dispatch(self, route, payload=None):
        self.calls.append((route, payload))
        if route == "get_state":
            x, y, z = self.positions.pop(0) if len(self.positions) > 1 else self.positions[0]
            return {"health": 20.0, "food_level": 10, "world_time": 1000,
                    "block_position": {"x": x, "y": y, "z": z}}
        if route == "get_inventory":
            return {"inventory": [], "armor": [], "offhand": []}
        return {}

    def chats(self):
        return [payload["message"] for route, payload in self.calls if route == "chat"]


def _state(entries=None):
    return SimpleNamespace(custom_data={} if entries is None else {hazards.HAZARD_KEY: entries})


def test_only_a_drowning_death_is_recorded():
    assert hazards.drowned({"last_damage_source": {"type": "drown"}})
    assert not hazards.drowned({"last_damage_source": {"type": "mob"}})
    assert not hazards.drowned({})


def test_repeat_drownings_at_one_trap_merge_into_one_counted_hazard():
    state = _state()
    for position in ({"x": -411, "y": 61, "z": -142}, (-410, 61, -141), {"x": -383, "y": 61, "z": -141}):
        hazards.record_drowning(state, position)
    assert state.custom_data[hazards.HAZARD_KEY] == [
        {"x": -411, "y": 61, "z": -142, "deaths": 2},
        {"x": -383, "y": 61, "z": -141, "deaths": 1},
    ]


@pytest.mark.parametrize("position, dimension", [
    ({"x": 1, "y": 60, "z": 1}, "minecraft:the_nether"),
    ({"x": "?", "y": 60, "z": 1}, "minecraft:overworld"),
    ({"x": float("nan"), "y": 60, "z": 1}, "minecraft:overworld"),
    ({}, "minecraft:overworld"),
])
def test_unusable_deaths_are_not_fenced(position, dimension):
    state = _state()
    assert hazards.record_drowning(state, position, dimension) is None
    assert state.custom_data == {}


def test_bound_hazards_fence_their_radius_only():
    client = SimpleNamespace()
    hazards.bind_drowning_hazards(client, _state([{"x": -411, "y": 61, "z": -142, "deaths": 7}, {"bad": 1}]))
    assert client._drowning_hazards == ((-411, 61, -142),)
    assert hazards.fenced_hazard(client, -422, -121) is None  # 23.7 blocks away
    assert hazards.fenced_hazard(client, -413, -130) == (-411, 61, -142)


def test_water_avoidance_is_restored_only_when_set_unless_forced():
    transport = _Recorder()
    client = SimpleNamespace(transport=transport)
    hazards.allow_water(client, sleep=lambda _s: None)
    assert transport.chats() == []
    hazards.avoid_water(client, sleep=lambda _s: None)
    hazards.allow_water(client, sleep=lambda _s: None)
    hazards.allow_water(client, sleep=lambda _s: None)
    hazards.allow_water(client, force=True, sleep=lambda _s: None)
    assert transport.chats() == [
        "#set blocksToAvoid minecraft:tripwire,minecraft:water,minecraft:seagrass,minecraft:tall_seagrass,"
        "minecraft:kelp,minecraft:kelp_plant",
        "#set reset blocksToAvoid",
        "#set reset blocksToAvoid",
    ]


def test_a_search_that_dies_still_restores_water_routes(monkeypatch):
    """Baritone saves #set; a leaked ban would block a swimming grave route."""
    monkeypatch.setattr(hazards.time, "sleep", lambda _s: None)
    transport = _Recorder()
    client = SimpleNamespace(transport=transport)

    @hazards.restore_water_routes
    def search(client):
        hazards.avoid_water(client)
        raise RuntimeError("died")

    with pytest.raises(RuntimeError):
        search(client)
    assert transport.chats()[-1] == "#set reset blocksToAvoid"
    assert client._avoiding_water is False


def test_surfacing_always_regains_water_routes(monkeypatch):
    from baritone_client.common import surface_recovery

    transport = _Recorder()
    client = SimpleNamespace(transport=transport, _avoiding_water=True)
    surface_recovery._configure_surface_pathing(client, sleep=lambda _s: None)
    assert transport.chats()[0] == "#set reset blocksToAvoid"


def test_exploration_avoids_water_and_skips_fenced_waypoints(monkeypatch):
    from baritone_client.common.emergency_food import EmergencyExploration

    monkeypatch.setattr(hazards.time, "sleep", lambda _s: None)
    transport = _Recorder([(0, 64, 0)])
    client = SimpleNamespace(transport=transport, _drowning_hazards=((24, 61, 0),))
    exploration = EmergencyExploration(0.0, 0.0, 96.0)
    exploration.start(client, transport.dispatch("get_state", {}))

    routes = [(route, payload) for route, payload in transport.calls if route in ("chat", "explore")]
    assert routes[0][1]["message"].startswith("#set blocksToAvoid minecraft:tripwire,minecraft:water")
    # The first waypoint, (24, 0), sits on the hazard; the next one is used.
    assert routes[1] == ("explore", {"x": 0, "z": 24})
    exploration.stop(client)
    assert transport.chats()[-1] == "#set reset blocksToAvoid"


def test_food_search_turns_back_at_a_drowning_fence(monkeypatch):
    from baritone_client.common import combat, navigation

    transport = _Recorder([(0, 64, 0)])
    client = SimpleNamespace(transport=transport, _drowning_hazards=((40, 61, 0),))
    monkeypatch.setattr(combat, "recover_health", lambda *_a, **_k: False)
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_a, **_k: [])
    monkeypatch.setattr(combat, "get_nearby_entities", lambda *_a, **_k: [])
    monkeypatch.setattr(combat.time, "sleep", lambda _s: None)
    monkeypatch.setattr(hazards.time, "sleep", lambda _s: None)
    retreats = []
    monkeypatch.setattr(navigation, "recovery_goto", lambda _c, x, y, z, **_k: retreats.append((x, y, z)) or True)

    explored = []

    class SecondExplore(Exception):
        pass

    original = transport.dispatch

    def dispatch(route, payload=None):
        if route == "explore":
            explored.append(payload)
            if len(explored) == 2:
                raise SecondExplore()
            transport.positions = [(30, 64, 0)]  # walked to 10 blocks from the trap
        return original(route, payload)

    transport.dispatch = dispatch
    with pytest.raises(SecondExplore):
        combat.acquire_emergency_food(client, minimum_health=12.0, timeout=60)

    assert retreats == [(0, 64, 0)], "walk back to the last position outside the fence"
    assert explored[0] != explored[1]
    assert ("cancel", {}) in transport.calls
    assert transport.chats()[-1] == "#set reset blocksToAvoid", "the dying search restored water routes"


def test_drowning_death_is_fenced_once_per_death():
    state = SimpleNamespace(custom_data={})
    client = SimpleNamespace()
    recovery = {}
    death = {"last_damage_source": {"type": "drown"}}
    position = {"x": -411, "y": 61, "z": -142}
    hazards.fence_drowning_death(state, client, death, recovery, position, "minecraft:overworld")
    hazards.fence_drowning_death(state, client, death, recovery, position, "minecraft:overworld")
    assert state.custom_data[hazards.HAZARD_KEY] == [{"x": -411, "y": 61, "z": -142, "deaths": 1}]
    assert client._drowning_hazards == ((-411, 61, -142),)

    mob = {"last_damage_source": {"type": "mob"}}
    hazards.fence_drowning_death(state, client, mob, {}, {"x": 5, "y": 64, "z": 5}, "minecraft:overworld")
    assert len(state.custom_data[hazards.HAZARD_KEY]) == 1


def test_hunting_for_emergency_food_never_routes_through_water(monkeypatch):
    from baritone_client.common import emergency_food

    monkeypatch.setattr(hazards.time, "sleep", lambda _s: None)
    monkeypatch.setattr(emergency_food.time, "sleep", lambda _s: None)
    transport = _Recorder([(0, 64, 0)])
    client = SimpleNamespace(transport=transport)
    emergency_food.hunt_target(client, {"type": "pig"}, minimum_health=12.0,
                               recovery_complete=lambda _s=None: False)
    chats = transport.chats()
    assert chats[0].startswith("#set blocksToAvoid minecraft:tripwire,minecraft:water")
    assert chats[-1] == "#set reset blocksToAvoid"


def test_controller_start_clears_a_saved_water_ban():
    from baritone_client.common.home_surface import bind_home_surface

    transport = _Recorder()
    client = SimpleNamespace(transport=transport)
    bind_home_surface(client, _state(), controller_start=True)
    assert transport.chats()[0] == "#set reset blocksToAvoid"
    bind_home_surface(client, _state())
    assert transport.chats().count("#set reset blocksToAvoid") == 1


def test_home_binding_installs_checkpointed_hazards():
    from baritone_client.common.home_surface import bind_home_surface

    client = SimpleNamespace()
    bind_home_surface(client, _state([{"x": 1, "y": 60, "z": 2, "deaths": 3}]))
    assert client._drowning_hazards == ((1, 60, 2),)



def test_a_search_that_starts_inside_a_fence_may_leave_it(monkeypatch, capsys):
    """A drowning in a home pond must not pin the food search at home."""
    from baritone_client.common import combat, navigation

    transport = _Recorder([(0, 64, 0)])  # four blocks from the pond hazard
    client = SimpleNamespace(transport=transport, _drowning_hazards=((4, 61, 0),))
    monkeypatch.setattr(combat, "recover_health", lambda *_a, **_k: False)
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_a, **_k: [])
    monkeypatch.setattr(combat, "get_nearby_entities", lambda *_a, **_k: [])
    monkeypatch.setattr(combat.time, "sleep", lambda _s: None)
    monkeypatch.setattr(hazards.time, "sleep", lambda _s: None)
    monkeypatch.setattr(navigation, "recovery_goto", lambda *_a, **_k: pytest.fail("no fence was crossed"))
    seen = []

    class Enough(Exception):
        pass

    original = transport.dispatch

    def dispatch(route, payload=None):
        if route == "explore":
            seen.append(route)
        elif route == "get_state" and seen:
            seen.append(route)
            if seen.count("get_state") > 8:
                raise Enough()
        return original(route, payload)

    transport.dispatch = dispatch
    with pytest.raises(Enough):
        combat.acquire_emergency_food(client, minimum_health=12.0, timeout=60)
    assert "turning back" not in capsys.readouterr().out
