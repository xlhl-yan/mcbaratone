"""A nearby herd becomes cooked food without wiping out the herd."""

from types import SimpleNamespace

import pytest

from baritone_client.automator import food_opportunity
from baritone_client.common import livestock_food


FURNACE = (5, 64, 5)


class _World:
    def __init__(self, *, health=20.0, food=20, herds=None, items=None):
        self.health = health
        self.food = food
        self.herds = dict(herds or {})
        self.items = dict(items or {})
        self.transport = SimpleNamespace(dispatch=self.dispatch)
        self.hunts = []
        self.smelts = []
        self.breeds = []

    def dispatch(self, route, payload):
        if route == "get_state":
            return {"health": self.health, "food_level": self.food, "is_dead": False}
        if route == "get_block":
            if (payload["x"], payload["y"], payload["z"]) == FURNACE:
                return {"id": "minecraft:furnace"}
            return {"id": "minecraft:air"}
        return {}


def _state():
    return SimpleNamespace(
        custom_data={"structures": {"starter_house": {"furnace": list(FURNACE)}}}
    )


@pytest.fixture
def world(monkeypatch):
    live = _World(herds={"cow": 9, "chicken": 10})
    monkeypatch.setattr(
        "baritone_client.common.inventory.count_item",
        lambda _c, item: live.items.get(item, 0),
    )
    monkeypatch.setattr(
        "baritone_client.common.husbandry.count_herd",
        lambda _c, family, radius=16: live.herds.get(family, 0),
    )
    monkeypatch.setattr(
        "baritone_client.common.husbandry.breed_pair",
        lambda _c, family, radius=8: live.breeds.append(family) or False,
    )

    def hunt(_client, *, mob_types, required_loot, max_kills, **kwargs):
        family = mob_types[0]
        (raw, wanted), = required_loot.items()
        live.hunts.append((family, wanted, max_kills, kwargs))
        per_kill = 2 if family == "cow" else 1
        kills = min(max_kills, live.herds[family])
        live.herds[family] -= kills
        live.items[raw] = live.items.get(raw, 0) + kills * per_kill
        return SimpleNamespace(success=True)

    monkeypatch.setattr("baritone_client.common.combat.hunt_mobs", hunt)
    monkeypatch.setattr(
        "baritone_client.common.resources._prepare_safe_furnace_fuel",
        lambda _c, _n: "minecraft:oak_planks",
    )

    def smelt(_client, furnace, raw, fuel, cooked, count, **_k):
        live.smelts.append((furnace, raw, cooked, count))
        live.items[raw] -= count
        live.items[cooked] = live.items.get(cooked, 0) + count
        return True

    monkeypatch.setattr("baritone_client.common.harness_ops.smelt_in_furnace", smelt)
    return live


def test_herd_is_hunted_and_cooked_toward_the_reserve(world):
    result = livestock_food.run_livestock_food_cycle(
        world, _state(), prepared_now=2, target=32
    )

    assert result.success
    # Cows first, capped at six kills per cycle and never below a breeding pair.
    assert world.hunts[0][0] == "cow" and world.hunts[0][2] == 6
    assert world.herds["cow"] == 3 and world.herds["chicken"] >= 2
    assert all(hunt[3]["abort_on_other_hostiles"] for hunt in world.hunts)
    assert {smelt[0] for smelt in world.smelts} == {FURNACE}
    assert world.items["minecraft:cooked_beef"] == 12
    assert result.cooked == world.items["minecraft:cooked_beef"] + world.items.get(
        "minecraft:cooked_chicken", 0
    )
    assert world.breeds[0] == "cow"


def test_breeding_pair_of_every_family_is_kept(world):
    world.herds = {"cow": 2, "chicken": 3}

    livestock_food.run_livestock_food_cycle(world, _state(), prepared_now=0, target=32)

    assert [hunt[0] for hunt in world.hunts] == ["chicken"]
    assert world.hunts[0][2] == 1
    assert world.herds == {"cow": 2, "chicken": 2}


def test_hunt_stops_once_the_reserve_would_be_full(world):
    livestock_food.run_livestock_food_cycle(world, _state(), prepared_now=30, target=32)

    assert len(world.hunts) == 1
    assert world.hunts[0][2] == 2


def test_carried_raw_meat_is_cooked_even_when_hunting_is_unsafe(world):
    world.health = 9.0
    world.items["minecraft:porkchop"] = 3

    result = livestock_food.run_livestock_food_cycle(
        world, _state(), prepared_now=0, target=32
    )

    assert world.hunts == []
    assert world.items["minecraft:cooked_porkchop"] == 3
    assert "too low to hunt" in result.detail


def test_full_reserve_does_not_hunt(world):
    result = livestock_food.run_livestock_food_cycle(
        world, _state(), prepared_now=32, target=32
    )

    assert world.hunts == [] and not result.success


def test_balanced_food_production_runs_livestock_before_wheat(monkeypatch):
    order = []
    inventory = {}
    def livestock_cycle(*_args, **kwargs):
        order.append(("livestock", kwargs["target"]))
        inventory["minecraft:cooked_beef"] = 6
        return livestock_food.LivestockResult(True, "hunted 6 raw meat, cooked 6", 6, 6)
    monkeypatch.setattr(
        "baritone_client.common.livestock_food.run_livestock_food_cycle",
        livestock_cycle,
    )
    monkeypatch.setattr(
        "baritone_client.common.food_supply.run_food_cycle",
        lambda *_a, **_k: order.append(("wheat", None))
        or SimpleNamespace(success=False, detail="harvested 0 wheat"),
    )
    monkeypatch.setattr(food_opportunity, "get_inventory", lambda *_a: dict(inventory))

    success, detail, _before, _after = food_opportunity.run_balanced_food_production(
        object(), SimpleNamespace(custom_data={})
    )

    assert order == [("livestock", 32), ("wheat", None)]
    assert success and "cooked 6" in detail and "harvested 0 wheat" in detail


def test_a_refused_furnace_click_does_not_abort_the_cycle(world, monkeypatch):
    def refuse(*_a, **_k):
        raise RuntimeError("Observed effect deadline exceeded; reconcile before retry")

    monkeypatch.setattr("baritone_client.common.harness_ops.smelt_in_furnace", refuse)

    result = livestock_food.run_livestock_food_cycle(
        world, _state(), prepared_now=2, target=32
    )

    assert world.hunts and result.hunted > 0
    assert result.cooked == 0
