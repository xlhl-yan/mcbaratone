import copy
from types import SimpleNamespace

import pytest

from baritone_client.automator import mining_compaction as compaction


@pytest.fixture
def bot(monkeypatch):
    rows = [{"slot": n, "id": f"minecraft:item_{n}", "count": 1, "max_count": 1} if n < 27
            else {"slot": n, "id": "minecraft:air", "count": 0} for n in range(36)]
    for slot, count in ((7, 53), (15, 7)):
        rows[slot] = {"slot": slot, "id": "minecraft:carrot", "count": count, "max_count": 64, "components": {}}
    inventory = {"inventory": rows, "armor": [], "offhand": [], "selected_slot": 0, "snapshot_valid": True}
    observed = SimpleNamespace(inventory=inventory, safe=True, actions=[], effect=True, screen_type="InventoryMenu",
                               sync_id=0, corrupt=False, stale=False, screen_error=False)
    monkeypatch.setattr(compaction, "_at_home", lambda *_a: observed.safe)
    def dispatch(route, payload):
        if route == "get_inventory":
            return copy.deepcopy(observed.inventory)
        if route == "get_screen":
            slots = [dict(r, slot=compaction._screen_slot(r["slot"])) for r in observed.inventory["inventory"]]
            if observed.stale:
                slots[15]["count"] = 6
            return {"sync_id": observed.sync_id, "type": observed.screen_type, "total_slots": 46,
                    "slots": slots, "status": "error" if observed.screen_error else "ok"}
        assert route == "inventory_click"
        assert payload == {"slot": 15, "type": "QUICK_MOVE", "button": 0, "sync_id": 0}
        observed.actions.append(payload)
        if observed.effect:
            observed.inventory["inventory"][7]["count"] = 60
            observed.inventory["inventory"][15] = {"slot": 15, "id": "minecraft:air", "count": 0}
        if observed.corrupt:
            observed.inventory["inventory"][0]["count"] = 0
        return {"success": True}
    return SimpleNamespace(transport=SimpleNamespace(dispatch=dispatch)), observed


def test_merge_recovers_the_tenth_slot_without_moving_tools(bot):
    client, observed = bot
    tools = copy.deepcopy(observed.inventory["inventory"][0])
    assert compaction.compact_home_stacks(client, (0, 70, 0), 10) == 1
    assert observed.inventory["inventory"][0] == tools
    assert observed.inventory["inventory"][7]["count"] == 60
    assert len(observed.actions) == 1


@pytest.mark.parametrize("case", ["unsafe", "container", "sync", "stale", "components", "unstackable", "invalid", "capacity", "screen_error", "sync_bool", "inventory_error"])
def test_unproven_or_nonidentical_stacks_never_move(bot, case):
    client, observed = bot
    if case == "unsafe": observed.safe = False
    elif case == "container": observed.screen_type = "GenericContainerScreenHandler"
    elif case == "sync": observed.sync_id = 7
    elif case == "stale": observed.stale = True
    elif case == "components": observed.inventory["inventory"][15]["components"] = {"custom_name": "saved"}
    elif case == "unstackable": observed.inventory["inventory"][15]["max_count"] = 1
    elif case == "invalid": observed.inventory["snapshot_valid"] = False
    elif case == "capacity": observed.inventory["inventory"][7]["count"] = 60
    elif case == "screen_error": observed.screen_error = True
    elif case == "sync_bool": observed.sync_id = False
    elif case == "inventory_error": observed.inventory["success"] = False
    assert compaction.compact_home_stacks(client, (0, 70, 0), 10) == 0
    assert observed.actions == []


def test_acknowledgement_without_merge_does_not_credit_space(bot):
    client, observed = bot
    observed.effect = False
    assert compaction.compact_home_stacks(client, (0, 70, 0), 10) == 0
    assert len(observed.actions) == 1


def test_lost_or_changed_contents_abort_before_mining(bot):
    client, observed = bot
    observed.corrupt = True
    with pytest.raises(RuntimeError, match="postcondition"):
        compaction.compact_home_stacks(client, (0, 70, 0), 10)


def test_no_action_when_space_is_already_sufficient(bot):
    client, observed = bot
    assert compaction.compact_home_stacks(client, (0, 70, 0), 9) == 0
    assert observed.actions == []
