"""Prepared-food work converts carried ingredients before gathering more."""

from types import SimpleNamespace

import pytest

from baritone_client.automator import food_opportunity
from baritone_client.common import food_supply, livestock_food
from baritone_client.common.tasks import PlayerDeathDetected


def _no_departure(*_args, **_kwargs):
    pytest.fail("carried grain must be prepared before gathering more food")


@pytest.mark.parametrize("bread_delta", [0, 3])
def test_balanced_carried_grain_uses_only_verified_prepared_delta(monkeypatch, bread_delta):
    counts = {food_supply.WHEAT: 12, food_supply.BREAD: 1}
    monkeypatch.setattr(food_opportunity, "get_inventory", lambda _client: dict(counts))
    monkeypatch.setattr(livestock_food, "run_livestock_food_cycle", _no_departure)

    def production(_client, _state, **kwargs):
        assert kwargs == {"personal_food_reserve": 32, "carried_wheat_only": True}
        counts[food_supply.BREAD] += bread_delta
        return SimpleNamespace(success=True, detail="bounded preparation")

    monkeypatch.setattr(food_supply, "run_food_cycle", production)
    result = food_opportunity.run_balanced_food_production(object(), object())
    assert result == (bool(bread_delta), "bounded preparation", 1, 1 + bread_delta)


@pytest.mark.parametrize("failure_at", [1, 2])
def test_balanced_unknown_inventory_fails_closed(monkeypatch, failure_at):
    reads = []
    cycles = []

    def inventory(_client):
        reads.append(True)
        if len(reads) == failure_at:
            raise RuntimeError("inventory unavailable")
        return {food_supply.WHEAT: 9, food_supply.BREAD: 2}

    monkeypatch.setattr(food_opportunity, "get_inventory", inventory)
    monkeypatch.setattr(livestock_food, "run_livestock_food_cycle", _no_departure)
    monkeypatch.setattr(
        food_supply, "run_food_cycle",
        lambda *_args, **_kwargs: cycles.append(True) or SimpleNamespace(success=True, detail="accepted"),
    )
    success, detail, before, after = food_opportunity.run_balanced_food_production(object(), object())
    assert not success and before == after
    assert cycles == ([] if failure_at == 1 else [True])
    assert "inventory unavailable" in detail or "could not verify" in detail


def test_balanced_inventory_loss_inside_craft_is_not_production(monkeypatch):
    monkeypatch.setattr(food_opportunity, "get_inventory", lambda _client: {food_supply.WHEAT: 9})
    monkeypatch.setattr(livestock_food, "run_livestock_food_cycle", _no_departure)

    def lost_inventory(*_args, **_kwargs):
        raise RuntimeError("current inventory unavailable")

    monkeypatch.setattr(food_supply, "run_food_cycle", lost_inventory)
    result = food_opportunity.run_balanced_food_production(object(), object())
    assert result[0] is False and result[2:] == (0, 0)


def test_balanced_death_still_yields_to_survival_recovery(monkeypatch):
    def died(_client):
        raise PlayerDeathDetected("death during observation")

    monkeypatch.setattr(food_opportunity, "get_inventory", died)
    with pytest.raises(PlayerDeathDetected):
        food_opportunity.run_balanced_food_production(object(), object())


@pytest.mark.parametrize(
    "capacity,expected",
    [(0, 0), (2, 2), (64, 64), (768, 64)],
)
def test_bread_batch_respects_output_space_and_cycle_bound(monkeypatch, capacity, expected):
    counts = {food_supply.WHEAT: 300, food_supply.BREAD: 0}
    monkeypatch.setattr(food_supply, "get_inventory", lambda _client: dict(counts))
    monkeypatch.setattr(food_supply, "_bread_output_capacity", lambda _client: capacity)
    monkeypatch.setattr(food_supply, "resolve_storage_location", lambda *_a, **_k: None)
    attempted = []

    def craft_home(_client, _state, _anchor, amount, **_kwargs):
        attempted.append(amount)
        counts[food_supply.BREAD] += amount
        counts[food_supply.WHEAT] -= 3 * amount
        return True

    monkeypatch.setattr(food_supply, "craft_bread_at_saved_home", craft_home)
    assert food_supply._prepare_bread(object(), SimpleNamespace(custom_data={}), 32, 8) == (expected, 0)
    assert attempted == ([expected] if expected else [])


def test_failed_carried_craft_does_not_harvest_more_ingredients(monkeypatch):
    counts = {food_supply.WHEAT: 12}
    monkeypatch.setattr(food_supply, "get_inventory", lambda _client: dict(counts))
    monkeypatch.setattr(food_supply, "_bread_output_capacity", lambda _client: 64)
    monkeypatch.setattr(food_supply, "_survival_ready", lambda _client: True)
    monkeypatch.setattr(food_supply, "eat_until_hunger", lambda *_a, **_k: True)
    monkeypatch.setattr(food_supply, "craft_bread_at_saved_home", lambda *_a, **_k: True)
    monkeypatch.setattr(food_supply, "resolve_storage_location", lambda *_a, **_k: None)
    monkeypatch.setattr(food_supply, "prepare_house_door_for_departure", _no_departure)
    state = SimpleNamespace(custom_data={"base_location": [0, 70, 0]})
    result = food_supply.run_food_cycle(object(), state, carried_wheat_only=True)
    assert not result.success and result.bread_crafted == 0
    assert "no verified bread" in result.detail
    assert counts == {food_supply.WHEAT: 12}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"success": False, "inventory": []},
        {
            "inventory": [
                {"slot": slot, "id": "minecraft:stone", "count": 64}
                for slot in range(36)
            ],
            "offhand": [{"slot": 0, "id": food_supply.BREAD, "count": 1}],
        },
    ],
)
def test_unknown_or_offhand_capacity_cannot_admit_a_bread_craft(monkeypatch, payload):
    # The aggregated food total can include offhand bread; only compatible
    # partial stacks in the carried inventory provide output space.
    counts = {food_supply.WHEAT: 6, food_supply.BREAD: 1}
    monkeypatch.setattr(food_supply, "get_inventory", lambda _client: dict(counts))
    monkeypatch.setattr(food_supply, "craft_bread_at_saved_home", _no_departure)
    monkeypatch.setattr(food_supply, "resolve_storage_location", lambda *_a, **_k: None)
    client = SimpleNamespace(transport=SimpleNamespace(dispatch=lambda *_a: payload))
    result = food_supply._prepare_bread(client, SimpleNamespace(custom_data={}), 32, 8)
    assert result == (0, 0)
