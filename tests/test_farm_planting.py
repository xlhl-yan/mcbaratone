"""Planting failures remain bounded and do not strand a whole crop plot."""

import pytest

from baritone_client.common import farming
from baritone_client.common.farm_planting import plant_farm_tiles


def test_reachable_tile_plants_without_walking_on_farmland(monkeypatch):
    from types import SimpleNamespace
    live = {"position": {"x": .5, "y": 66, "z": .5}, "health": 20, "is_dead": False}
    client = SimpleNamespace(transport=SimpleNamespace(dispatch=lambda *_a: live))
    monkeypatch.setattr(farming, "count_item", lambda *_a: 1)
    monkeypatch.setattr(farming, "_till_and_plant_tile", lambda *_a: True)
    monkeypatch.setattr(farming, "goto", lambda *_a, **_k: pytest.fail("must not trample soil"))
    assert plant_farm_tiles(client, [(1,64,0)]) == 1


def test_occluded_near_tile_retains_verified_approach_fallback(monkeypatch):
    from types import SimpleNamespace
    live = {"position": {"x": .5, "y": 65, "z": .5}, "health": 20, "is_dead": False}
    client = SimpleNamespace(transport=SimpleNamespace(dispatch=lambda *_a: live))
    monkeypatch.setattr(farming, "count_item", lambda *_a: 1)
    attempts = iter([False, True])
    monkeypatch.setattr(farming, "_till_and_plant_tile", lambda *_a: next(attempts))
    moved = []
    monkeypatch.setattr(farming, "goto", lambda *_a, **_k: moved.append(True) or True)
    assert plant_farm_tiles(client, [(1,64,0)]) == 1
    assert moved == [True]


def test_observed_carrot_crop_replants_with_carrot_not_wheat_seed(monkeypatch):
    from types import SimpleNamespace

    blocks = {(1, 64, 0): "minecraft:farmland", (1, 65, 0): "minecraft:air"}
    selected = []

    def dispatch(route, payload):
        if route == "get_state":
            return {"position": {"x": .5, "y": 65, "z": .5},
                    "health": 20, "is_dead": False}
        if route == "get_block":
            return {"id": blocks.get((payload["x"], payload["y"], payload["z"]),
                                     "minecraft:air")}
        if route == "interact_block":
            blocks[(payload["x"], payload["y"] + 1, payload["z"])] = "minecraft:carrots"
        return {}

    client = SimpleNamespace(transport=SimpleNamespace(dispatch=dispatch))
    count = lambda _c, item: 1 if item == "minecraft:carrot" else 0
    monkeypatch.setattr(farming, "count_item", count)
    monkeypatch.setattr("baritone_client.common.inventory.count_item", count)
    monkeypatch.setattr(
        "baritone_client.common.inventory.select_item",
        lambda _c, item, **_kwargs: selected.append(item) or True,
    )

    assert plant_farm_tiles(client, [(1, 64, 0)], crop_item="minecraft:carrot") == 1
    assert selected == ["minecraft:carrot"]


def test_unsupported_crop_item_fails_closed(monkeypatch):
    monkeypatch.setattr(
        farming, "count_item",
        lambda *_a: pytest.fail("unsupported crop must be rejected before inventory"),
    )

    assert plant_farm_tiles(object(), [(1, 64, 0)], crop_item="minecraft:melon_seeds") == 0
from baritone_client.common.tasks import PlayerDeathDetected, SurvivalRecoveryRequired


def setup(monkeypatch, traveler, planter):
    monkeypatch.setattr(farming, "count_item", lambda *_a: 12)
    monkeypatch.setattr(farming, "goto", traveler)
    monkeypatch.setattr(farming, "_till_and_plant_tile", planter)


def test_planting_approaches_crop_height_with_a_close_bounded_goal(monkeypatch):
    moves = []
    setup(monkeypatch, lambda _c, *p, **kw: moves.append((p, kw)) or True,
          lambda *_a: True)
    assert plant_farm_tiles(object(), [(1, 64, 2)]) == 1
    assert moves == [((1, 65, 2), {"timeout": 15, "tolerance": 1.5, "radius": 1})]


def test_unreachable_tile_is_not_planted_and_later_tiles_continue(monkeypatch):
    planted = []
    setup(monkeypatch, lambda _c, x, *_a, **_k: x != 1,
          lambda _c, *p: planted.append(p) or True)
    assert plant_farm_tiles(object(), [(1, 64, 0), (2, 64, 0)]) == 1
    assert planted == [(2, 64, 0)]


def test_visibility_failure_does_not_abort_the_remaining_plot(monkeypatch):
    attempts = []

    def plant(_c, x, y, z):
        attempts.append((x, y, z))
        if x == 1:
            raise RuntimeError("Target is not visible on a real block ray")
        return True

    setup(monkeypatch, lambda *_a, **_k: True, plant)
    assert plant_farm_tiles(object(), [(1, 64, 0), (2, 64, 0)]) == 1
    assert len(attempts) == 2


@pytest.mark.parametrize("error", [PlayerDeathDetected, SurvivalRecoveryRequired])
def test_survival_abort_propagates_without_planting_more_tiles(monkeypatch, error):
    attempts = []

    def plant(*_a):
        attempts.append(1)
        raise error("survival abort")

    setup(monkeypatch, lambda *_a, **_k: True, plant)
    with pytest.raises(error):
        plant_farm_tiles(object(), [(1, 64, 0), (2, 64, 0)])
    assert attempts == [1]


def test_seed_exhaustion_stops_without_another_trip(monkeypatch):
    setup(monkeypatch, lambda *_a, **_k: pytest.fail("no seed, no trip"),
          lambda *_a: pytest.fail("no seed, no planting"))
    monkeypatch.setattr(farming, "count_item", lambda *_a: 0)
    assert plant_farm_tiles(object(), [(1, 64, 0)]) == 0


def test_rejected_planting_does_not_credit_output(monkeypatch):
    setup(monkeypatch, lambda *_a, **_k: True, lambda *_a: False)
    assert plant_farm_tiles(object(), [(1, 64, 0)]) == 0
