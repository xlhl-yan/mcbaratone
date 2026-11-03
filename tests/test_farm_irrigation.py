"""Off-center irrigation must not trigger repeated destructive rebuilds."""

from types import SimpleNamespace

import pytest

from baritone_client.common import farm_irrigation, farming
from baritone_client.common.tasks import PlayerDeathDetected


def client_for(blocks):
    calls = []

    def dispatch(route, payload):
        calls.append((route, payload))
        if route == "get_state":
            return {"position": {"x": .5, "y": 65, "z": .5},
                    "health": 20, "is_dead": False}
        assert route == "get_block", "irrigation reuse must be read-only"
        return blocks.get((payload["x"], payload["y"], payload["z"]),
                          {"id": "minecraft:air", "state": {}})

    return SimpleNamespace(transport=SimpleNamespace(dispatch=dispatch)), calls


def water(level=0):
    return {"id": "minecraft:water", "state": {"level": str(level)}}


@pytest.mark.parametrize("center", [water(1), {"id": "minecraft:farmland"}])
def test_off_center_source_is_reused_without_digging_or_bucket(center):
    client, calls = client_for({(10, 64, 10): center, (10, 64, 11): water()})
    assert farming.ensure_farm_water(client, 10, 64, 10)
    assert all(route == "get_block" for route, _ in calls)


@pytest.mark.parametrize("offset,size,accepted", [
    ((2, 2), 5, True), ((1, 1), 7, True), ((2, 0), 7, False),
    ((3, 0), 5, False), ((1, 0), 9, False),
])
def test_source_must_cover_the_whole_plot(offset, size, accepted):
    dx, dz = offset
    client, _ = client_for({(dx, 64, dz): water()})
    assert bool(farm_irrigation.existing_plot_source(client, 0, 64, 0, size=size)) is accepted


@pytest.mark.parametrize("block", [water(1), water(7),
    {"id": "minecraft:water"}, {"id": "minecraft:water", "state": {"level": None}},
    {"id": "minecraft:water_cauldron", "state": {"level": "0"}},
])
def test_flowing_or_ambiguous_water_is_not_source_proof(block):
    client, _ = client_for({(0, 64, 1): block})
    assert farm_irrigation.existing_plot_source(client, 0, 64, 0) is None


def test_elevated_water_does_not_irrigate_soil_level():
    client, _ = client_for({(0, 65, 1): water()})
    assert farm_irrigation.existing_plot_source(client, 0, 64, 0) is None


@pytest.mark.parametrize("response", [{}, None, {"error": "unavailable"},
    {"id": "minecraft:water", "state": {"level": "0"}, "error": "stale"},
    {"error": "unavailable", "data": water()},
])
def test_unknown_irrigation_fails_closed_without_mutations(response):
    client, calls = client_for({(-2, 64, -2): response})
    assert not farming.ensure_farm_water(client, 0, 64, 0)


def test_source_is_recognized_in_a_wrapped_block_response():
    client, _ = client_for({(0, 64, 1): {"data": water()}})
    assert farming.ensure_farm_water(client, 0, 64, 0)


def test_checkpoint_irrigation_claim_is_not_current_proof(monkeypatch):
    client, calls = client_for({})
    monkeypatch.setattr(farming, "count_item", lambda *_a: 0)
    monkeypatch.setattr(farming, "find_water_source", lambda *_a, **_k: None)
    state = SimpleNamespace(custom_data={"wheat_farm": {
        "water_source": [0, 64, 1], "verified": True,
    }})
    assert not farming.ensure_farm_water(client, 0, 64, 0, state=state)
    assert all(route == "get_block" for route, _ in calls)


def test_irrigation_read_exception_fails_closed():
    def dispatch(_route, payload):
        if payload["x"] != 0 or payload["z"] != 0:
            raise TimeoutError("block observation unavailable")
        return {"id": "minecraft:air"}

    client = SimpleNamespace(transport=SimpleNamespace(dispatch=dispatch))
    assert not farming.ensure_farm_water(client, 0, 64, 0)


def test_player_death_during_irrigation_scan_propagates():
    def dispatch(_route, payload):
        if payload["x"] != 0 or payload["z"] != 0:
            raise PlayerDeathDetected("player died")
        return {"id": "minecraft:air"}

    client = SimpleNamespace(transport=SimpleNamespace(dispatch=dispatch))
    with pytest.raises(PlayerDeathDetected):
        farming.ensure_farm_water(client, 0, 64, 0)


@pytest.mark.parametrize("builder", [farming.establish_wheat_farm, farming.reestablish_wheat_farm])
def test_off_center_irrigation_allows_replanting_to_continue(monkeypatch, builder):
    blocks = {(x, 64, z): {"id": "minecraft:farmland"}
              for x in range(-2, 3) for z in range(-2, 3)}
    blocks[(0, 64, 0)] = water(1)
    blocks[(0, 64, 1)] = water()
    client, calls = client_for(blocks)
    planted = []
    monkeypatch.setattr(farming, "count_item", lambda *_a: 64)
    monkeypatch.setattr(farming, "_gather_seeds", lambda *_a: True)
    monkeypatch.setattr(farming, "goto", lambda *_a, **_k: True)
    monkeypatch.setattr(farming, "_till_and_plant_tile",
                        lambda _c, *tile: planted.append(tile) or True)
    assert builder(client, 0, 64, 0) == (0, 64, 0)
    assert planted
    assert all(route in {"get_block", "get_state"} for route, _ in calls)


@pytest.mark.parametrize("builder", [farming.establish_wheat_farm, farming.reestablish_wheat_farm])
def test_reused_source_replants_real_tiles_and_preserves_water(monkeypatch, builder):
    blocks = {(x, 64, z): {"id": "minecraft:farmland"}
              for x in range(-2, 3) for z in range(-2, 3)}
    blocks[(0, 64, 0)] = water(1)
    blocks[(0, 64, 1)] = water()
    calls = []

    def dispatch(route, payload):
        calls.append((route, payload))
        target = payload["x"], payload["y"], payload["z"]
        if route == "get_block":
            return blocks.get(target, {"id": "minecraft:air"})
        if route == "look_at":
            return {}
        assert route == "interact_block", "existing irrigation must not be rebuilt"
        assert blocks[target]["id"] == "minecraft:farmland"
        blocks[(target[0], target[1] + 1, target[2])] = {"id": "minecraft:wheat"}
        return {}

    client = SimpleNamespace(transport=SimpleNamespace(dispatch=dispatch))
    monkeypatch.setattr(farming, "count_item", lambda *_a: 64)
    monkeypatch.setattr(farming, "_gather_seeds", lambda *_a: True)
    monkeypatch.setattr(farming, "goto", lambda *_a, **_k: True)
    monkeypatch.setattr(farming, "select_item", lambda *_a, **_k: True)
    monkeypatch.setattr(farming.time, "sleep", lambda *_a: None)
    assert builder(client, 0, 64, 0) == (0, 64, 0)
    assert sum(block.get("id") == "minecraft:wheat" for block in blocks.values()) == 23
    assert blocks[(0, 64, 0)] == water(1)
    assert blocks[(0, 64, 1)] == water()
