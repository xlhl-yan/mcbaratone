"""Unavailable inventory must never admit crafting or collection as free space."""

from types import SimpleNamespace

import pytest

from baritone_client.common.inventory import free_inventory_slots
from baritone_client.common.tunnel_miner import free_slots
from baritone_client.inventory_evidence import carried_output_capacity


def capacity(payload):
    client = SimpleNamespace(transport=SimpleNamespace(dispatch=lambda *_a: payload))
    result = free_inventory_slots(client)
    assert free_slots(client) == result
    return result


def item(slot=0, count=1):
    return {"slot": slot, "id": "minecraft:stone", "count": count}


@pytest.mark.parametrize("payload", [
    None, {}, {"data": {}}, {"status": "error", "inventory": []},
    {"success": False, "inventory": []}, {"data": {"success": False, "inventory": []}},
    {"snapshot_valid": False, "inventory": []},
    {"data": {"snapshot_valid": False, "inventory": []}},
    {"data": {"error": "unavailable", "inventory": []}},
    {"inventory": {}}, {"inventory": {"minecraft:stone": 1}},
    {"inventory": [item(count=True)]}, {"inventory": [item(count=-1)]},
    {"inventory": [item(slot=True)]}, {"inventory": [item(slot="0")]},
    {"inventory": [item(slot=36)]}, {"inventory": [item(), item()]},
    {"inventory": [{"slot": 0, "count": 1}]},
])
def test_unknown_or_malformed_capacity_is_zero(payload):
    assert capacity(payload) == 0


def test_current_sparse_and_empty_inventories_have_known_capacity():
    assert capacity({"inventory": []}) == 36
    assert capacity({"data": {"inventory": [item(0), item(35)]}}) == 34
    assert capacity({"inventory": [item(i) for i in range(36)]}) == 0


def test_armour_and_offhand_do_not_use_carried_slots():
    assert capacity({"inventory": [item()], "armor": [item(36)], "offhand": [item(40)]}) == 35


def test_transport_failure_does_not_imply_empty_inventory():
    def fail(*_a):
        raise RuntimeError("read unavailable")
    client = SimpleNamespace(transport=SimpleNamespace(dispatch=fail))
    assert free_inventory_slots(client) == 0


def test_output_capacity_uses_carried_plain_stacks_only():
    slots = [item(i, 64) for i in range(36)]
    bread = "minecraft:bread"
    assert carried_output_capacity({"inventory": slots, "offhand": [dict(item(40, 7), id=bread)]}, bread) == 0
    slots[5] = dict(item(5, 7), id=bread)
    assert carried_output_capacity({"inventory": slots}, bread) == 57
    slots[5]["components"] = {"minecraft:custom_name": "custom"}
    assert carried_output_capacity({"inventory": slots}, bread) == 0
    assert carried_output_capacity({"success": False, "inventory": slots}, bread) == 0
    assert carried_output_capacity({}, bread) == 0
