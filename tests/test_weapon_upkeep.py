"""A worn-out sword or missing pickaxe is replaced from local materials."""

from types import SimpleNamespace

import pytest

from baritone_client.automator import armor_upkeep, weapon_upkeep


def _client(items):
    def dispatch(route, _payload):
        if route == "get_inventory":
            return {"inventory": [dict(i, slot=n) for n, i in enumerate(items)]}
        return {}

    return SimpleNamespace(transport=SimpleNamespace(dispatch=dispatch))


SWORD_247 = {"id": "minecraft:iron_sword", "count": 1, "damage": 247, "max_damage": 250}
FRESH_PICK = {"id": "minecraft:stone_pickaxe", "count": 1, "damage": 0, "max_damage": 131}
COBBLE = {"id": "minecraft:cobblestone", "count": 73}
PLANKS = {"id": "minecraft:oak_planks", "count": 9}


def test_live_a1_inventory_needs_a_stone_sword_and_pickaxe():
    # 2026-09-28: 3 uses left on the only sword, no pickaxe, 73 cobblestone.
    client = _client([SWORD_247, COBBLE, PLANKS])
    assert weapon_upkeep.kit_gaps(client) == [
        "minecraft:stone_sword",
        "minecraft:stone_pickaxe",
    ]


def test_durable_kit_needs_nothing():
    sword = dict(SWORD_247, damage=100)
    assert weapon_upkeep.kit_gaps(_client([sword, FRESH_PICK, COBBLE])) == []


@pytest.mark.parametrize("remaining", [16, 17, 31, 32, 33])
def test_mining_return_reserve_is_replaced_before_another_trip(monkeypatch, remaining):
    from baritone_client.common.tunnel_miner import PICKAXE_RETURN_RESERVE

    items = [dict(SWORD_247, damage=0), dict(FRESH_PICK, damage=131-remaining), dict(COBBLE)]
    client = _client(items)
    needs_pick = remaining <= PICKAXE_RETURN_RESERVE
    assert weapon_upkeep.has_mining_pickaxe(client) is (not needs_pick)
    assert weapon_upkeep.kit_gaps(client) == (["minecraft:stone_pickaxe"] if needs_pick else [])
    made = []
    def craft(_client, item, count):
        made.append(item)
        items.append(dict(FRESH_PICK))
        return True
    monkeypatch.setattr("baritone_client.common.inventory.craft", craft)
    assert weapon_upkeep.restore_kit(client) == (["stone_pickaxe"] if needs_pick else [])
    assert made == (["minecraft:stone_pickaxe"] if needs_pick else [])
    assert weapon_upkeep.has_mining_pickaxe(client)
    assert weapon_upkeep.kit_gaps(client) == []


def test_pickaxe_reserve_does_not_change_sword_replacement_threshold():
    client = _client([dict(SWORD_247, damage=230), FRESH_PICK, COBBLE])
    assert weapon_upkeep.kit_gaps(client) == []


@pytest.mark.parametrize("tier", ["wooden", "golden"])
def test_iron_mining_requires_an_upgrade_from_wood_or_gold(tier):
    client = _client([dict(SWORD_247, damage=0), dict(FRESH_PICK, id=f"minecraft:{tier}_pickaxe"), COBBLE])
    assert not weapon_upkeep.has_mining_pickaxe(client)
    assert weapon_upkeep.kit_gaps(client) == ["minecraft:stone_pickaxe"]


def test_mining_tool_read_failure_is_not_a_usable_pickaxe():
    client = SimpleNamespace(transport=SimpleNamespace(dispatch=lambda *_a: {}))
    assert not weapon_upkeep.has_mining_pickaxe(client)


def test_wood_is_the_fallback_and_nothing_is_offered_without_materials():
    assert weapon_upkeep.kit_gaps(_client([FRESH_PICK, PLANKS])) == ["minecraft:wooden_sword"]
    assert weapon_upkeep.kit_gaps(_client([FRESH_PICK])) == []
    assert weapon_upkeep.kit_gaps(_client([])) == []


def test_armour_upkeep_offers_and_crafts_the_missing_kit(monkeypatch):
    items = [SWORD_247, COBBLE, PLANKS]
    client = _client(items)
    crafted = []

    def craft(_client, item, _count):
        crafted.append(item)
        items.append({"id": item, "count": 1, "damage": 0, "max_damage": 131})
        return True

    monkeypatch.setattr("baritone_client.common.inventory.craft", craft)
    monkeypatch.setattr(armor_upkeep, "equipped_pieces", lambda _c: 4)
    monkeypatch.setattr(armor_upkeep, "needs_freeze_boots", lambda _c: False)
    monkeypatch.setattr(armor_upkeep, "worn_out_pieces", lambda _c: [])
    monkeypatch.setattr("baritone_client.common.inventory.equip_best_armor", lambda _c: 4)

    offer = armor_upkeep.select_armor_opportunity(
        client, SimpleNamespace(health=20.0, observed=True), True
    )
    assert offer is not None and "stone_sword" in offer.reason

    success, detail, _before, _after = armor_upkeep.run_armor_upkeep(client, None)
    assert success and "stone_sword" in detail and "stone_pickaxe" in detail
    assert crafted == ["minecraft:stone_sword", "minecraft:stone_pickaxe"]
    assert weapon_upkeep.kit_gaps(client) == []
