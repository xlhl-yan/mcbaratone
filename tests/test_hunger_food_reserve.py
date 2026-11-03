from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from baritone_client.automator.systems import HungerSystem


def eat(snapshot, *, health=20, food=16):
    client = MagicMock()
    client.transport.dispatch.return_value = snapshot
    hunger = HungerSystem(client, MagicMock())
    eaten = []
    with patch(
        "baritone_client.automator.actions.EatAction",
        lambda item: SimpleNamespace(
            execute=lambda _client: eaten.append(item) or SimpleNamespace(success=True)
        ),
    ):
        HungerSystem.try_eat.__wrapped__(hunger, food, health=health)
    return eaten


def stock(bread=3, carrots=48, **flags):
    return {"inventory": [
        {"id": "minecraft:bread", "count": bread, "slot": 0},
        {"id": "minecraft:carrot", "count": carrots, "slot": 1},
    ], "snapshot_valid": True, **flags}


def test_healthy_routine_meal_preserves_small_expedition_reserve():
    assert eat(stock()) == ["minecraft:carrot"]


@pytest.mark.parametrize("kwargs", [
    {"health": 17}, {"health": None}, {"health": True},
    {"health": float("nan")}, {"health": "20"}, {"food": 6},
])
def test_wounded_urgent_or_uncertain_health_keeps_normal_food_priority(kwargs):
    assert eat(stock(), **kwargs) == ["minecraft:bread"]


@pytest.mark.parametrize("bread,carrots", [(32,48), (3,15), (3,0)])
def test_sufficient_prepared_stock_or_scarce_carrots_keeps_normal_priority(bread, carrots):
    assert eat(stock(bread, carrots)) == ["minecraft:bread"]


@pytest.mark.parametrize("change", [
    {"snapshot_valid": False}, {"success": False}, {"error": "unavailable"},
])
def test_rejected_inventory_never_drives_an_eating_action(change):
    assert eat(stock(**change)) == []


@pytest.mark.parametrize("count", [True, "48", -1, 1.5])
def test_uncertain_crop_count_never_drives_an_eating_action(count):
    assert eat(stock(carrots=count)) == []


def test_medicinal_food_keeps_its_existing_priority():
    snapshot = stock()
    snapshot["inventory"].append({"id": "minecraft:golden_apple", "count": 1, "slot": 2})
    assert eat(snapshot) == ["minecraft:golden_apple"]


def test_carrot_stock_summed_across_valid_stacks():
    snapshot = stock(carrots=8)
    snapshot["inventory"].append({"id": "minecraft:carrot", "count": 8, "slot": 2})
    assert eat(snapshot) == ["minecraft:carrot"]


def test_background_tick_passes_fresh_health_to_routine_meal_choice():
    client = MagicMock()
    def dispatch(route, *_args, **_kwargs):
        if route == "get_state":
            return {"food_level": 16, "health": 20}
        if route == "get_screen":
            return {"type": "InventoryMenu"}
        if route == "get_inventory":
            return stock()
        raise AssertionError(route)
    client.transport.dispatch.side_effect = dispatch
    eaten = []
    with patch("baritone_client.automator.actions.EatAction", lambda item: SimpleNamespace(
        execute=lambda _client: eaten.append(item) or SimpleNamespace(success=True)
    )):
        HungerSystem(client, MagicMock()).tick()
    assert eaten == ["minecraft:carrot"]


def test_reserve_counts_all_prepared_food_types():
    snapshot = stock()
    snapshot["inventory"].append({"id": "minecraft:cooked_beef", "count": 29, "slot": 2})
    assert eat(snapshot) == ["minecraft:cooked_beef"]


def test_nested_inventory_uses_same_reserve_policy_and_rejects_inner_errors():
    assert eat({"data": stock()}) == ["minecraft:carrot"]
    assert eat({"data": stock(success=False)}) == []


@pytest.mark.parametrize("food", [True, "16", float("nan"), -1, 21])
def test_uncertain_hunger_never_drives_a_meal(food):
    assert eat(stock(), food=food) == []
