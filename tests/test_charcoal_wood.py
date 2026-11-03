from types import SimpleNamespace

import pytest

from baritone_client.automator import charcoal_wood as wood


@pytest.fixture
def tree(monkeypatch):
    blocks = {(20, 70 + dy, 0): "minecraft:oak_log" for dy in range(5)}
    blocks[(20, 69, 0)] = "minecraft:grass_block"
    blocks[(21, 69, 0)] = "minecraft:grass_block"
    blocks[(21, 74, 0)] = "minecraft:oak_leaves"
    live = {"health": 20, "food_level": 20, "world_time": 1000,
            "dimension": "minecraft:overworld", "is_dead": False,
            "block_position": {"x": 0, "y": 70, "z": 0}}
    inventory, digs, trips = {}, [], []

    def dispatch(route, payload):
        if route == "get_state":
            return live
        if route == "find_blocks":
            assert payload["radius"] == 48
            return {"found": [{"x": 20, "y": 70, "z": 0}]}
        if route == "get_block":
            return {"id": blocks.get(tuple(payload[a] for a in ("x", "y", "z")), "minecraft:air")}
        if route == "dig_block":
            cell = tuple(payload[a] for a in ("x", "y", "z"))
            digs.append(cell)
            inventory[blocks.pop(cell)] = inventory.get("minecraft:oak_log", 0) + 1
            return {"success": True}
        pytest.fail(f"unexpected route {route}")

    client = SimpleNamespace(transport=SimpleNamespace(dispatch=dispatch), _protected_home_anchor=(0, 70, 0))
    monkeypatch.setattr("baritone_client.common.inventory.count_item", lambda _c, item: inventory.get(item, 0))
    monkeypatch.setattr("baritone_client.common.combat.scan_for_threats", lambda *_a, **_kw: [])
    monkeypatch.setattr(wood, "_travel", lambda _c, *xyz: trips.append(xyz) or True)
    monkeypatch.setattr(wood.time, "sleep", lambda _s: None)
    return client, SimpleNamespace(custom_data={}), blocks, live, inventory, digs, trips


def test_small_verified_trunk_supplies_charcoal_and_returns_home(tree):
    client, state, _blocks, _live, inventory, digs, trips = tree
    assert wood.gather_charcoal_logs(client, state, 5, (0, 70, 0)) == 5
    assert inventory["minecraft:oak_log"] == 5 and len(digs) == 5
    assert trips[-1] == (0, 70, 0)
    assert state.custom_data["charcoal_wood"]["logs_harvested"] == 5


@pytest.mark.parametrize("case", ["night", "threat", "unknown_entities", "unsupported", "no_leaves", "protected_structure"])
def test_uncertain_or_unsafe_tree_work_never_digs(tree, monkeypatch, case):
    client, state, blocks, live, _inventory, digs, _trips = tree
    if case == "night":
        live["world_time"] = 13000
    elif case == "threat":
        monkeypatch.setattr("baritone_client.common.combat.scan_for_threats", lambda *_a, **_kw: [{"type": "minecraft:creeper"}])
    elif case == "unknown_entities":
        def unavailable(*_a, **_kw):
            raise RuntimeError("entity telemetry unavailable")
        monkeypatch.setattr("baritone_client.common.combat.scan_for_threats", unavailable)
    elif case == "unsupported":
        blocks.pop((21, 69, 0))
    elif case == "no_leaves":
        blocks.pop((21, 74, 0))
    else:
        blocks[(20, 69, 0)] = "minecraft:oak_planks"
    assert wood.gather_charcoal_logs(client, state, 5, (0, 70, 0)) == 0
    assert not digs


def test_ray_rejection_cools_down_the_failed_tree_and_preserves_home_return(tree):
    client, state, _blocks, _live, _inventory, digs, trips = tree
    original = client.transport.dispatch

    def rejected(route, payload):
        if route == "dig_block":
            raise RuntimeError("not visible on a real block ray")
        return original(route, payload)

    client.transport.dispatch = rejected
    assert wood.gather_charcoal_logs(client, state, 5, (0, 70, 0)) == 0
    assert trips[-1] == (0, 70, 0) and not digs
    attempts = len(trips)
    assert wood.gather_charcoal_logs(client, state, 5, (0, 70, 0)) == 0
    assert len(trips) == attempts


@pytest.mark.parametrize("cover", ["leaf_litter", "short_grass", "fern"])
def test_noncolliding_ground_cover_keeps_supported_tree_stand_usable(tree, cover):
    client, state, blocks, _live, inventory, digs, trips = tree
    blocks[(21, 70, 0)] = "minecraft:" + cover
    assert wood.gather_charcoal_logs(client, state, 5, (0, 70, 0)) == 5
    assert inventory["minecraft:oak_log"] == 5
    assert digs == [(20, 70 + dy, 0) for dy in range(5)]
    assert trips[0] == (21, 70, 0) and trips[-1] == (0, 70, 0)


@pytest.mark.parametrize("obstruction", ["water", "oak_leaves", "stone"])
def test_ground_cover_allowance_does_not_admit_obstructed_stands(tree, obstruction):
    client, state, blocks, _live, _inventory, digs, trips = tree
    blocks[(21, 70, 0)] = "minecraft:" + obstruction
    assert wood.gather_charcoal_logs(client, state, 5, (0, 70, 0)) == 0
    assert not digs and not trips


def test_tree_below_protected_surface_floor_is_not_admitted(tree):
    client, state, _blocks, _live, _inventory, digs, trips = tree
    client._protected_home_anchor = (0, 73, 0)
    assert wood.gather_charcoal_logs(client, state, 5, (0, 73, 0)) == 0
    assert not digs and not trips


def test_progressive_break_waits_for_actual_removal_before_collecting(tree):
    client, state, blocks, _live, inventory, digs, trips = tree
    original = client.transport.dispatch
    pending = {}
    def dispatch(route, payload):
        cell = tuple(payload.get(a) for a in ("x", "y", "z"))
        if route == "dig_block":
            pending[cell] = 5
            return {"started": True}
        if route == "get_block" and cell in pending:
            pending[cell] -= 1
            if pending[cell] == 0:
                original("dig_block", dict(zip(("x", "y", "z"), cell)))
                pending.pop(cell)
        return original(route, payload)
    client.transport.dispatch = dispatch
    assert wood.gather_charcoal_logs(client, state, 1, (0, 70, 0)) == 1
    assert inventory["minecraft:oak_log"] == 1 and digs == [(20, 70, 0)]
    assert trips[-1] == (0, 70, 0)


def test_acknowledged_but_unchanged_log_is_stopped_without_false_credit(tree):
    client, state, _blocks, _live, _inventory, digs, trips = tree
    original = client.transport.dispatch
    stopped = []
    def dispatch(route, payload):
        if route == "dig_block":return {"started": True}
        if route == "stop":stopped.append(True);return {"success": True}
        return original(route, payload)
    client.transport.dispatch = dispatch
    assert wood.gather_charcoal_logs(client, state, 1, (0, 70, 0)) == 0
    assert stopped == [True] and not digs
    assert trips[-1] == (0, 70, 0)


def test_lighting_zone_centre_does_not_replace_the_saved_home_return(tree):
    client, state, _blocks, _live, _inventory, _digs, trips = tree
    assert wood.gather_charcoal_logs(client, state, 1, (5, 70, 5)) == 1
    assert trips[-1] == client._protected_home_anchor


@pytest.mark.parametrize("failure", ["unknown_block", "threat_telemetry"])
def test_progressive_break_stops_on_uncertain_observation(tree, monkeypatch, failure):
    client, _state, _blocks, _live, _inventory, _digs, _trips = tree
    original = client.transport.dispatch
    stopped = []
    def dispatch(route, payload):
        if route == "dig_block":return {"started": True}
        if route == "stop":stopped.append(True);return {}
        if route == "get_block" and failure == "unknown_block":return {"id": "minecraft:void_air"}
        return original(route, payload)
    client.transport.dispatch = dispatch
    if failure == "threat_telemetry":
        def unavailable(*_args):raise RuntimeError("telemetry unavailable")
        monkeypatch.setattr(wood, "_safe", unavailable)
        with pytest.raises(RuntimeError, match="telemetry unavailable"):
            wood._break_log(client, (20, 70, 0), "minecraft:oak_log", (0, 70, 0))
    else:
        assert not wood._break_log(client, (20, 70, 0), "minecraft:oak_log", (0, 70, 0))
    assert stopped == [True]


def test_no_usable_trunk_says_so(tree, capsys):
    """A silent zero hid why A1 never made sticks for its iron-trip torches."""
    client, state, _blocks, _live, _inventory, digs, _trips = tree
    client._protected_home_anchor = (0, 73, 0)
    assert wood.gather_charcoal_logs(client, state, 5, (0, 73, 0)) == 0
    assert "no natural trunk" in capsys.readouterr().out
