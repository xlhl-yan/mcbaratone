from types import SimpleNamespace

import pytest

from baritone_client.automator import base_lighting
from baritone_client.automator.torch_recovery import retrieve_torch_supplies


@pytest.fixture
def supplies(monkeypatch):
    items = {"minecraft:birch_planks": 6}
    requests = []
    live = {"dimension": "minecraft:overworld", "health": 20, "food_level": 20,
            "block_position": {"x": 0, "y": 70, "z": 0}}
    client = SimpleNamespace(transport=SimpleNamespace(dispatch=lambda *_a: live))
    state = SimpleNamespace(custom_data={})
    monkeypatch.setattr("baritone_client.common.inventory.count_item", lambda _c, item: items.get(item, 0))
    return items, requests, live, client, state


def test_storage_fuel_is_used_before_wood_expedition(supplies, monkeypatch):
    items, requests, _live, client, state = supplies

    def withdraw(_c, _s, item, target, **kw):
        requests.append((item, target, kw))
        if item == "minecraft:coal":
            items[item] = target

    def craft(_c, item, _count):
        if item == "minecraft:stick":
            items[item] = items.get(item, 0) + 4
        elif item == "minecraft:torch" and items.get("minecraft:coal", 0) and items.get("minecraft:stick", 0):
            items["minecraft:coal"] -= 1
            items["minecraft:stick"] -= 1
            items[item] = items.get(item, 0) + 4
        return True

    monkeypatch.setattr("baritone_client.common.home_respawn.withdraw_from_home_containers", withdraw)
    monkeypatch.setattr("baritone_client.common.inventory.craft", craft)
    monkeypatch.setattr("baritone_client.common.resources.gather_wood", lambda *_a, **_k: pytest.fail("local coal is enough"))
    assert base_lighting.ensure_torches(client, state, 12, (0, 70, 0)) == 12
    assert [r[0] for r in requests] == ["minecraft:torch", "minecraft:coal", "minecraft:stick"]
    assert all(r[2]["radius"] == 32 and r[2]["max_vertical"] == 8 for r in requests)


def test_acknowledged_withdrawal_without_inventory_delta_does_not_supply_fuel(supplies, monkeypatch):
    _items, requests, _live, client, state = supplies
    monkeypatch.setattr("baritone_client.common.home_respawn.withdraw_from_home_containers", lambda _c, _s, item, target, **_kw: requests.append(item) or True)
    retrieve_torch_supplies(client, state, 12, (0, 70, 0))
    assert requests == ["minecraft:torch", "minecraft:coal", "minecraft:charcoal", "minecraft:stick"]


@pytest.mark.parametrize("case", ["far", "hurt", "hungry", "dead", "dimension", "missing_position", "nan"])
def test_no_storage_travel_without_safe_local_observation(supplies, monkeypatch, case):
    _items, _requests, live, client, state = supplies
    if case == "far":
        live["block_position"]["x"] = 100
    elif case == "hurt":
        live["health"] = 10
    elif case == "dead":
        live["is_dead"] = True
    elif case == "hungry":
        live["food_level"] = 5
    elif case == "nan":
        live["block_position"]["x"] = float("nan")
    elif case == "dimension":
        live["dimension"] = "minecraft:the_nether"
    else:
        live.pop("block_position")
    monkeypatch.setattr("baritone_client.common.home_respawn.withdraw_from_home_containers", lambda *_a, **_k: pytest.fail("unsafe withdrawal"))
    retrieve_torch_supplies(client, state, 12, (0, 70, 0))


def test_banked_planks_supply_sticks_when_the_pack_has_no_wood(supplies, monkeypatch):
    """Live A1 2026-10-15: 2 charcoal, no wood carried, 52 planks in the home
    chests. Every iron trip deferred on 'needs torches' for want of a stick."""
    items, requests, _live, client, state = supplies
    items.clear()
    items["minecraft:charcoal"] = 2
    stored = {"minecraft:oak_planks": 36, "minecraft:birch_planks": 16}

    def withdraw(_c, _s, item, target, **kw):
        requests.append(item)
        moved = min(stored.get(item, 0), max(0, target - items.get(item, 0)))
        if moved:
            stored[item] -= moved
            items[item] = items.get(item, 0) + moved

    def craft(_c, item, _count):
        planks = [p for p in ("minecraft:oak_planks", "minecraft:birch_planks") if items.get(p, 0) >= 2]
        if item == "minecraft:stick" and planks:
            items[planks[0]] -= 2
            items[item] = items.get(item, 0) + 4
        elif item == "minecraft:torch" and items.get("minecraft:charcoal", 0) and items.get("minecraft:stick", 0):
            items["minecraft:charcoal"] -= 1
            items["minecraft:stick"] -= 1
            items[item] = items.get(item, 0) + 4
        return True

    monkeypatch.setattr("baritone_client.common.home_respawn.withdraw_from_home_containers", withdraw)
    monkeypatch.setattr("baritone_client.common.inventory.craft", craft)
    monkeypatch.setattr("baritone_client.automator.charcoal_wood.gather_charcoal_logs", lambda *_a: 0)
    monkeypatch.setattr("baritone_client.common.livestock_food._home_furnace", lambda *_a: None)
    assert base_lighting.ensure_torches(client, state, 12, (0, 70, 0)) == 8
    assert "minecraft:oak_planks" in requests
    assert stored["minecraft:birch_planks"] == 16, "only the planks the sticks need"


def test_a_stored_log_is_fetched_when_no_planks_are_banked(supplies, monkeypatch):
    items, requests, _live, client, state = supplies
    items.clear()
    stored = {"minecraft:spruce_log": 3}

    def withdraw(_c, _s, item, target, **_kw):
        requests.append(item)
        if stored.get(item):
            stored[item] -= 1
            items[item] = 1

    monkeypatch.setattr("baritone_client.common.home_respawn.withdraw_from_home_containers", withdraw)
    retrieve_torch_supplies(client, state, 12, (0, 70, 0))
    assert items.get("minecraft:spruce_log") == 1
    assert requests[-1] == "minecraft:spruce_log"


def test_carried_wood_needs_no_storage_trip_for_sticks(supplies, monkeypatch):
    items, requests, _live, client, state = supplies
    monkeypatch.setattr("baritone_client.common.home_respawn.withdraw_from_home_containers",
                        lambda _c, _s, item, _t, **_k: requests.append(item))
    retrieve_torch_supplies(client, state, 12, (0, 70, 0))
    assert not any(item.endswith(("_planks", "_log")) for item in requests)
