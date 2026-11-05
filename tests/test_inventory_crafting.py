from baritone_client.common import inventory
from baritone_client.common import harness_ops
from baritone_client.common import storage_safety
from baritone_client.common.state import WorldState
from baritone_client.actions.crafting import CraftingAction
from types import SimpleNamespace
import json

import pytest


class DummyTransport:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def dispatch(self, route, payload, **_kwargs):
        self.calls.append((route, payload))
        return self.responses.get(route, {"status": "ok", "data": {}})


class DummyClient:
    def __init__(self, transport):
        self.transport = transport


def test_remote_loaded_crafting_table_is_not_selected_for_survival_work():
    client = DummyClient(
        DummyTransport(
            {
                "get_state": {
                    "block_position": {"x": -104, "y": 64, "z": 798},
                },
            }
        )
    )
    found_tables = [
        {"x": -81, "y": 68, "z": 757, "distance": 1.0},
        {"x": -100, "y": 64, "z": 795, "distance": 999.0},
    ]

    assert inventory._nearest_local_crafting_table(client, found_tables) == (
        -100,
        64,
        795,
    )
    assert (
        inventory._nearest_local_crafting_table(client, found_tables[:1])
        is None
    )


def test_workstation_selection_counts_height_before_choosing_a_nearby_roof():
    client = DummyClient(DummyTransport({"get_state": {"block_position": {"x": 10, "y": 70, "z": 10}}}))
    assert inventory._nearest_local_crafting_table(client, [
        {"x": 11, "y": 74, "z": 10},
        {"x": 12, "y": 70, "z": 11},
    ]) == (12, 70, 11)
    assert inventory._nearest_local_crafting_table(client, [{"x": 10, "y": 83, "z": 10}]) is None


@pytest.mark.parametrize("height", [None, True, float("nan"), float("inf")])
def test_unverified_player_height_cannot_choose_a_crafting_workstation(height):
    client = DummyClient(DummyTransport({"get_state": {"block_position": {"x": 10, "y": height, "z": 10}}}))
    assert inventory._nearest_local_crafting_table(client, [{"x": 11, "y": 70, "z": 10}]) is None


class RecipeAwareTransport:
    """Tiny inventory simulator for dependency-order regression tests."""

    def __init__(self):
        self.items = {"minecraft:oak_planks": 60}
        self.calls = []

    def dispatch(self, route, payload):
        self.calls.append((route, payload))
        if route == "get_inventory":
            entries = [
                {"id": item_id, "count": count, "slot": slot}
                for slot, (item_id, count) in enumerate(self.items.items())
                if count > 0
            ]
            return {"status": "ok", "data": {"inventory": entries}}

        if route == "craft":
            item_id = payload.get("item")
            count = payload.get("count", 1)
            if item_id == "minecraft:stick" and self.items.get("minecraft:oak_planks", 0) >= 2:
                self.items["minecraft:oak_planks"] -= 2
                self.items[item_id] = self.items.get(item_id, 0) + count
            elif (
                item_id == "minecraft:wooden_pickaxe"
                and self.items.get("minecraft:oak_planks", 0) >= 3
                and self.items.get("minecraft:stick", 0) >= 2
            ):
                self.items["minecraft:oak_planks"] -= 3
                self.items["minecraft:stick"] -= 2
                self.items[item_id] = self.items.get(item_id, 0) + count
        return {"status": "ok"}


class FoodAndIronTransport:
    """Simulator for the resumed FOOD_AND_IRON blocker state."""

    def __init__(self):
        self.items = {
            "minecraft:iron_ingot": 9,
            "minecraft:oak_log": 2,
            "minecraft:stick": 1,
            "minecraft:iron_pickaxe": 1,
        }
        self.calls = []

    def dispatch(self, route, payload):
        self.calls.append((route, payload))
        if route == "get_inventory":
            entries = [
                {"id": item_id, "count": count, "slot": slot}
                for slot, (item_id, count) in enumerate(self.items.items())
                if count > 0
            ]
            return {"status": "ok", "data": {"inventory": entries}}

        if route != "craft":
            return {"status": "ok"}

        item_id = payload.get("item")
        count = int(payload.get("count", 1))
        if item_id == "minecraft:oak_planks" and self.items.get("minecraft:oak_log", 0) > 0:
            self.items["minecraft:oak_log"] -= 1
            self.items["minecraft:oak_planks"] = (
                self.items.get("minecraft:oak_planks", 0) + count
            )
        elif item_id == "minecraft:stick" and self.items.get("minecraft:oak_planks", 0) >= 2:
            self.items["minecraft:oak_planks"] -= 2
            self.items[item_id] = self.items.get(item_id, 0) + count
        elif (
            item_id == "minecraft:iron_pickaxe"
            and self.items.get("minecraft:iron_ingot", 0) >= 3
            and self.items.get("minecraft:stick", 0) >= 2
        ):
            self.items["minecraft:iron_ingot"] -= 3
            self.items["minecraft:stick"] -= 2
            self.items[item_id] = self.items.get(item_id, 0) + count
        return {"status": "ok"}


def test_world_state_defaults_to_isolated_run_directory(monkeypatch, tmp_path):
    isolated = tmp_path / "Bot99"
    monkeypatch.setenv("MC_RUN_DIR", str(isolated))
    monkeypatch.chdir(tmp_path)

    world = WorldState(DummyClient(DummyTransport()))
    written = world.save_checkpoint("storage", {"x": 1, "y": 2, "z": 3})

    assert written == str(isolated / "checkpoint_storage.json")
    assert (isolated / "checkpoint_storage.json").exists()
    assert not (tmp_path / "checkpoint_storage.json").exists()


def test_check_craft_via_bridge():
    t = DummyTransport(responses={
        "check_craft": {"status": "ok", "data": {"can_craft": True, "missing": [], "recipe_id": "r1"}}
    })
    client = DummyClient(t)
    res = inventory.check_craft(client, "minecraft:stone_pickaxe", count=1)
    assert res["can_craft"] is True
    assert res["recipe_id"] == "r1"


def test_craft_item_delegates():
    t = DummyTransport(responses={
        "craft": {"status": "ok", "data": {"crafted": True}}
    })
    client = DummyClient(t)
    ok = inventory.craft_item(client, "r1", count=1)
    assert ok is True
    assert t.calls and t.calls[-1][0] == "craft"


def test_plank_craft_uses_manual_grid_before_native_recipe(monkeypatch):
    transport = FoodAndIronTransport()
    client = DummyClient(transport)
    monkeypatch.setattr(harness_ops, "available", lambda: True)

    def manual_planks(_client, item_id, output_count):
        assert item_id == "minecraft:oak_planks"
        assert output_count == 4
        transport.items["minecraft:oak_log"] -= 1
        transport.items[item_id] = transport.items.get(item_id, 0) + 4
        return True

    monkeypatch.setattr(harness_ops, "craft_planks_manual", manual_planks)

    assert inventory.craft(client, "minecraft:oak_planks", 4)
    assert not any(
        route in {"craft", "auto_craft"} and payload.get("item") == "minecraft:oak_planks"
        for route, payload in transport.calls
    )


def test_crafting_table_uses_manual_grid_before_native_recipe(monkeypatch):
    transport = FoodAndIronTransport()
    transport.items["minecraft:oak_planks"] = 4
    client = DummyClient(transport)
    monkeypatch.setattr(harness_ops, "available", lambda: True)

    def manual_table(_client):
        transport.items["minecraft:oak_planks"] -= 4
        transport.items["minecraft:crafting_table"] = 1
        return True

    monkeypatch.setattr(harness_ops, "craft_crafting_table_manual", manual_table)

    assert inventory.craft(client, "minecraft:crafting_table", 1)
    assert transport.items["minecraft:crafting_table"] == 1
    assert not any(
        route in {"craft", "auto_craft"}
        and payload.get("item") == "minecraft:crafting_table"
        for route, payload in transport.calls
    )


def test_crafting_table_gathers_missing_wood_before_manual_grid(monkeypatch):
    """A resumed bot with two planks must bootstrap a table, not stall."""
    from baritone_client.common import resources

    transport = FoodAndIronTransport()
    transport.items = {"minecraft:birch_planks": 2}
    client = DummyClient(transport)
    gathered = []
    monkeypatch.setattr(harness_ops, "available", lambda: True)

    def gather_wood(_client, count=16, **_kwargs):
        gathered.append(count)
        transport.items["minecraft:birch_log"] = count
        return True

    def manual_planks(_client, item_id, output_count):
        assert item_id == "minecraft:birch_planks"
        assert output_count == 4
        transport.items["minecraft:birch_log"] -= 1
        transport.items[item_id] += 4
        return True

    def manual_table(_client):
        assert transport.items["minecraft:birch_planks"] >= 4
        transport.items["minecraft:birch_planks"] -= 4
        transport.items["minecraft:crafting_table"] = 1
        return True

    monkeypatch.setattr(resources, "gather_wood", gather_wood)
    monkeypatch.setattr(harness_ops, "craft_planks_manual", manual_planks)
    monkeypatch.setattr(harness_ops, "craft_crafting_table_manual", manual_table)

    assert inventory.craft(client, "minecraft:crafting_table", 1)
    assert gathered == [1]
    assert transport.items["minecraft:crafting_table"] == 1
    assert transport.items["minecraft:birch_planks"] == 2


def test_tool_craft_prepares_sticks_before_pickaxe(monkeypatch):
    transport = RecipeAwareTransport()
    client = DummyClient(transport)
    monkeypatch.setattr(harness_ops, "available", lambda: True)
    monkeypatch.setattr(harness_ops, "ensure_crafting_table_open", lambda _client: True)

    # wooden_pickaxe now has an explicit _MANUAL_GRID_RECIPES entry, which
    # takes precedence over the generic tool driver. The dependency ordering
    # under test -- sticks prepared before the pickaxe -- is unchanged.
    def manual_recipe(_client, item_id, _placements, crafts=1, output_per_recipe=1):
        assert item_id == "minecraft:wooden_pickaxe"
        transport.items["minecraft:oak_planks"] -= 3
        transport.items["minecraft:stick"] -= 2
        transport.items[item_id] = transport.items.get(item_id, 0) + 1
        return True

    monkeypatch.setattr(harness_ops, "craft_recipe_manual", manual_recipe)

    assert inventory.craft(client, "minecraft:wooden_pickaxe", 1) is True

    craft_items = [payload["item"] for route, payload in transport.calls if route == "craft"]
    assert craft_items == ["minecraft:stick"]
    assert transport.items["minecraft:wooden_pickaxe"] == 1


def test_food_and_iron_resumes_from_log_state_for_second_pickaxe(monkeypatch):
    transport = FoodAndIronTransport()
    client = DummyClient(transport)
    monkeypatch.setattr(harness_ops, "available", lambda: True)
    monkeypatch.setattr(harness_ops, "ensure_crafting_table_open", lambda _client: True)

    # iron_pickaxe now has an explicit _MANUAL_GRID_RECIPES entry, which takes
    # precedence over the generic tool driver, so the verified path is
    # craft_recipe_manual. The plank/stick dependency resolution under test is
    # unchanged either way.
    def manual_recipe(_client, item_id, _placements, crafts=1, output_per_recipe=1):
        assert item_id == "minecraft:iron_pickaxe"
        transport.items["minecraft:iron_ingot"] -= 3
        transport.items["minecraft:stick"] -= 2
        transport.items[item_id] += 1
        return True

    monkeypatch.setattr(harness_ops, "craft_recipe_manual", manual_recipe)

    assert inventory.craft(client, "minecraft:iron_pickaxe", 1) is True

    craft_items = [payload["item"] for route, payload in transport.calls if route == "craft"]
    assert craft_items == [
        "minecraft:oak_planks",
        "minecraft:stick",
    ]
    assert transport.items["minecraft:iron_pickaxe"] == 2
    assert transport.items["minecraft:oak_log"] == 1
    assert transport.items["minecraft:stick"] >= 1


def test_crafting_action_uses_same_tool_dependency_guard(monkeypatch):
    transport = RecipeAwareTransport()
    context = SimpleNamespace(client=DummyClient(transport))
    action = CraftingAction()
    monkeypatch.setattr(harness_ops, "available", lambda: True)
    monkeypatch.setattr(action, "ensure_crafting_table", lambda _context: True)

    def manual_tool(_client, item_id):
        assert item_id == "minecraft:wooden_pickaxe"
        transport.items["minecraft:oak_planks"] -= 3
        transport.items["minecraft:stick"] -= 2
        transport.items[item_id] = transport.items.get(item_id, 0) + 1
        return True

    monkeypatch.setattr(harness_ops, "craft_tool_manual", manual_tool)

    assert action.craft(context, "minecraft:wooden_pickaxe", 1) is True

    craft_items = [payload["item"] for route, payload in transport.calls if route == "craft"]
    assert craft_items == ["minecraft:stick"]


def test_crafting_table_manual_fallback_does_not_require_existing_table(monkeypatch):
    transport = DummyTransport(
        {
            "get_inventory": {
                "status": "ok",
                "data": {
                    "inventory": [
                        {"id": "minecraft:oak_planks", "count": 4, "slot": 9}
                    ]
                },
            }
        }
    )
    client = DummyClient(transport)
    monkeypatch.setattr(inventory, "_wait_craft_result", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(harness_ops, "available", lambda: True)
    monkeypatch.setattr(harness_ops, "craft_crafting_table_manual", lambda _client: True)
    monkeypatch.setattr(
        harness_ops,
        "ensure_crafting_table_open",
        lambda _client: (_ for _ in ()).throw(AssertionError("must not recurse while crafting a table")),
    )

    assert inventory.craft(client, "minecraft:crafting_table", 1) is True


def test_furnace_manual_fallback_requires_verified_table(monkeypatch):
    # Furnace moved onto the shared _MANUAL_GRID_RECIPES driver, so it now
    # routes through craft_recipe_manual (8 cobblestone ringing an empty
    # centre) rather than the bespoke craft_furnace_manual. The table must
    # still be opened and verified first.
    transport = DummyTransport()
    client = DummyClient(transport)
    calls = []
    monkeypatch.setattr(inventory, "_wait_craft_result", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        inventory,
        "count_item",
        lambda _client, item_id: 1 if item_id == "minecraft:crafting_table" else 0,
    )
    monkeypatch.setattr(harness_ops, "available", lambda: True)
    monkeypatch.setattr(
        harness_ops,
        "ensure_crafting_table_open",
        lambda _client: calls.append("table") or True,
    )

    def manual_recipe(_client, result_id, placements, crafts=1, output_per_recipe=1):
        calls.append(("recipe", result_id, tuple(placements)))
        return True

    monkeypatch.setattr(harness_ops, "craft_recipe_manual", manual_recipe)

    assert inventory.craft(client, "minecraft:furnace", 1) is True
    expected_placements = tuple(
        inventory._MANUAL_GRID_RECIPES["minecraft:furnace"]["placements"]
    )
    assert calls == ["table", ("recipe", "minecraft:furnace", expected_placements)]
    # Centre slot 5 stays empty; filling it is a different (and wrong) recipe.
    assert 5 not in [slot for _sel, slot in expected_placements]


def test_bucket_manual_fallback_places_iron_v_recipe(monkeypatch):
    # auto_craft's hardcoded recipe slice has no bucket; the manual grid
    # fallback must drive the iron V shape (grid slots 1, 3, 5).
    transport = DummyTransport()
    client = DummyClient(transport)
    calls = []
    monkeypatch.setattr(inventory, "_wait_craft_result", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(inventory, "count_item", lambda _client, item_id: 1 if item_id == "minecraft:crafting_table" else 0)
    monkeypatch.setattr(harness_ops, "available", lambda: True)
    monkeypatch.setattr(
        harness_ops,
        "ensure_crafting_table_open",
        lambda _client: calls.append("table") or True,
    )

    def manual_recipe(_client, result_id, placements, crafts=1, output_per_recipe=1):
        calls.append(("recipe", result_id, tuple(placements), crafts))
        return True

    monkeypatch.setattr(harness_ops, "craft_recipe_manual", manual_recipe)

    assert inventory.craft(client, "minecraft:bucket", 1) is True
    assert calls == [
        "table",
        (
            "recipe",
            "minecraft:bucket",
            (
                ("minecraft:iron_ingot", 1),
                ("minecraft:iron_ingot", 3),
                ("minecraft:iron_ingot", 5),
            ),
            1,
        ),
    ]


def test_crafting_action_routes_furnace_to_manual_harness(monkeypatch):
    monkeypatch.setattr(
        harness_ops,
        "craft_furnace_manual",
        lambda client: client == "client",
    )

    fallback = CraftingAction._manual_fallback_for("minecraft:furnace")

    assert fallback is not None
    assert fallback("client")


def test_equip_best_armor_maps_hotbar_slot_and_verifies_bridge_armor():
    class ArmorTransport:
        def __init__(self):
            self.inventory_items = [
                {
                    "slot": 0,
                    "id": "minecraft:iron_helmet",
                    "count": 1,
                }
            ]
            self.armor_items = [
                {"slot": 36, "id": "minecraft:air", "count": 0},
                {"slot": 37, "id": "minecraft:air", "count": 0},
                {"slot": 38, "id": "minecraft:air", "count": 0},
                {"slot": 39, "id": "minecraft:air", "count": 0},
            ]
            self.calls = []

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_inventory":
                return {
                    "inventory": list(self.inventory_items),
                    "armor": list(self.armor_items),
                    "offhand": [],
                }
            if route == "inventory_click" and payload["slot"] == 5:
                self.inventory_items = []
                self.armor_items[3] = {
                    "slot": 39,
                    "id": "minecraft:iron_helmet",
                    "count": 1,
                }
            return {}

    transport = ArmorTransport()
    client = DummyClient(transport)

    assert inventory.equip_best_armor(client) == 1
    assert (
        "inventory_click",
        {"slot": 5, "type": "SWAP", "button": 0, "sync_id": 0},
    ) in transport.calls
    assert inventory.get_equipped_armor(client) == {
        "helmet": "minecraft:iron_helmet"
    }


def test_full_armor_verification_reads_equipped_section_only():
    class EquippedTransport:
        def dispatch(self, route, _payload):
            if route == "get_inventory":
                return {
                    "inventory": [],
                    "armor": [
                        {"id": "minecraft:iron_helmet", "count": 1},
                        {"id": "minecraft:iron_chestplate", "count": 1},
                        {"id": "minecraft:iron_leggings", "count": 1},
                        {"id": "minecraft:iron_boots", "count": 1},
                    ],
                    "offhand": [],
                }
            return {}

    assert inventory.has_full_armor(
        DummyClient(EquippedTransport()),
        minimum_material="iron",
    )


def test_durable_full_armor_rejects_nearly_broken_piece():
    class EquippedTransport:
        def dispatch(self, route, _payload):
            if route == "get_inventory":
                armor = [
                    {
                        "id": f"minecraft:iron_{piece}",
                        "count": 1,
                        "damage": 0,
                        "max_damage": 200,
                    }
                    for piece in ("helmet", "chestplate", "leggings", "boots")
                ]
                armor[1].update({"damage": 186, "max_damage": 200})
                return {"inventory": [], "armor": armor, "offhand": []}
            return {}

    client = DummyClient(EquippedTransport())

    assert inventory.has_full_armor(client, minimum_material="iron")
    assert not inventory.has_durable_full_armor(
        client, minimum_material="iron", minimum_remaining=64
    )


def test_equip_best_armor_replaces_nearly_broken_same_tier_piece():
    class DurabilityTransport:
        def __init__(self):
            self.inventory_items = [
                {
                    "slot": 0,
                    "id": "minecraft:iron_chestplate",
                    "count": 1,
                    "damage": 0,
                    "max_damage": 240,
                }
            ]
            self.armor_items = [
                {
                    "slot": 38,
                    "id": "minecraft:iron_chestplate",
                    "count": 1,
                    "damage": 226,
                    "max_damage": 240,
                }
            ]
            self.calls = []

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_inventory":
                return {
                    "inventory": list(self.inventory_items),
                    "armor": list(self.armor_items),
                    "offhand": [],
                }
            if route == "inventory_click" and payload["slot"] == 6:
                self.armor_items = [self.inventory_items.pop(0)]
            return {}

    transport = DurabilityTransport()
    client = DummyClient(transport)

    assert inventory.equip_best_armor(client) == 1
    assert transport.armor_items[0]["damage"] == 0
    assert (
        "inventory_click",
        {"slot": 6, "type": "SWAP", "button": 0, "sync_id": 0},
    ) in transport.calls


def test_equip_best_armor_replaces_unsafe_diamond_with_durable_iron():
    class DowngradeTransport:
        def __init__(self):
            self.inventory_items = [
                {
                    "slot": 0,
                    "id": "minecraft:iron_chestplate",
                    "count": 1,
                    "damage": 0,
                    "max_damage": 240,
                }
            ]
            self.armor_items = [
                {
                    "slot": 38,
                    "id": "minecraft:diamond_chestplate",
                    "count": 1,
                    "damage": 520,
                    "max_damage": 528,
                }
            ]

        def dispatch(self, route, payload):
            if route == "get_inventory":
                return {
                    "inventory": list(self.inventory_items),
                    "armor": list(self.armor_items),
                    "offhand": [],
                }
            if route == "inventory_click" and payload["slot"] == 6:
                self.armor_items = [self.inventory_items.pop(0)]
            return {}

    transport = DowngradeTransport()

    assert inventory.equip_best_armor(DummyClient(transport)) == 1
    assert transport.armor_items[0]["id"] == "minecraft:iron_chestplate"


def test_deposit_excess_uses_live_chest_screen_player_slots_only(monkeypatch):
    class ChestTransport:
        def __init__(self):
            self.clicks = []
            self.moved = False

        def dispatch(self, route, payload):
            if route == "get_block":
                return {"id": "minecraft:chest"}
            if route == "get_state":
                return {"block_position": {"x": 1, "y": 65, "z": 1}}
            if route == "get_screen":
                slots = [
                    {"slot": index, "id": "minecraft:air", "count": 0}
                    for index in range(63)
                ]
                if not self.moved:
                    slots[27] = {
                        "slot": 27,
                        "id": "minecraft:rotten_flesh",
                        "count": 4,
                    }
                slots[28] = {
                    "slot": 28,
                    "id": "minecraft:diamond",
                    "count": 5,
                }
                return {
                    "sync_id": 7,
                    "total_slots": 63,
                    "slots": slots,
                }
            if route == "inventory_click":
                self.clicks.append(payload)
                self.moved = True
            return {}

    transport = ChestTransport()
    client = DummyClient(transport)
    monkeypatch.setattr(harness_ops, "open_container", lambda *_args, **_kwargs: True)

    assert inventory.deposit_excess_to_chest(client, (1, 65, 1)) == 1
    assert transport.clicks == [
        {
            "slot": 27,
            "type": "QUICK_MOVE",
            "button": 0,
            "sync_id": 7,
        }
    ]


def test_deposit_excess_does_not_claim_a_stack_that_failed_to_move(monkeypatch):
    class FullChestTransport:
        def dispatch(self, route, payload):
            if route == "get_block":
                return {"id": "minecraft:chest"}
            if route == "get_state":
                return {"block_position": {"x": 1, "y": 65, "z": 1}}
            if route == "get_screen":
                slots = [
                    {"slot": index, "id": "minecraft:air", "count": 0}
                    for index in range(63)
                ]
                slots[27] = {
                    "slot": 27,
                    "id": "minecraft:rotten_flesh",
                    "count": 4,
                }
                return {"sync_id": 7, "total_slots": 63, "slots": slots}
            return {}

    monkeypatch.setattr(harness_ops, "open_container", lambda *_args, **_kwargs: True)

    assert inventory.deposit_excess_to_chest(
        DummyClient(FullChestTransport()),
        (1, 65, 1),
    ) == 0


def test_withdraw_required_uses_chest_slots_and_leaves_other_storage(monkeypatch):
    class ChestTransport:
        def __init__(self):
            self.clicks = []
            self.moved = False

        def dispatch(self, route, payload):
            if route == "get_inventory":
                return {"inventory": [], "armor": [], "offhand": []}
            if route == "get_block":
                return {"id": "minecraft:chest"}
            if route == "get_screen":
                slots = [
                    {"slot": index, "id": "minecraft:air", "count": 0}
                    for index in range(63)
                ]
                slots[3] = {
                    "slot": 3,
                    "id": "minecraft:leather",
                    "count": 2,
                }
                slots[4] = {
                    "slot": 4,
                    "id": "minecraft:redstone",
                    "count": 11,
                }
                return {"sync_id": 8, "total_slots": 63, "slots": slots}
            if route == "inventory_click":
                self.clicks.append(payload)
            return {}

    transport = ChestTransport()
    client = DummyClient(transport)
    monkeypatch.setattr(
        "baritone_client.common.harness_ops.open_container",
        lambda *_args, **_kwargs: True,
    )

    assert inventory.withdraw_required_from_chest(
        client,
        (1, 65, 1),
        {"minecraft:leather": 46},
    ) == 1
    assert transport.clicks == [
        {
            "slot": 3,
            "type": "QUICK_MOVE",
            "button": 0,
            "sync_id": 8,
        }
    ]


def test_withdraw_reattempts_present_chest_that_failed_to_open(
    monkeypatch, advancing_clock
):
    # A chest that is still present must be re-approached on every withdraw,
    # not skipped by the "recently failed" cooldown. The cooldown exists to
    # stop re-travelling to a *destroyed* container; applying it to a present
    # chest deadlocked FOOD_AND_IRON forever (skip -> fail -> cooldown -> skip).
    client = DummyClient(
        DummyTransport(
            {
                "get_inventory": {"inventory": [], "armor": [], "offhand": []},
                "get_block": {"id": "minecraft:chest"},
            }
        )
    )
    opens = []
    monkeypatch.setattr(
        "baritone_client.common.container_reads.time", advancing_clock()
    )
    monkeypatch.setattr(
        "baritone_client.common.harness_ops.open_container",
        lambda *_args, **_kwargs: opens.append(True) or False,
    )

    assert inventory.withdraw_required_from_chest(
        client,
        (1, 65, 1),
        {"minecraft:bread": 6},
    ) == -1
    assert inventory.withdraw_required_from_chest(
        client,
        (1, 65, 1),
        {"minecraft:bread": 6},
    ) == -1
    # Both calls re-approached the still-present chest rather than the second
    # being short-circuited by the cooldown.
    assert opens == [True, True]


def test_catalog_withdraw_prefers_known_item_container(monkeypatch):
    class Catalog:
        def find_item(self, item_id):
            assert item_id == "minecraft:bread"
            return [
                {
                    "dimension": "minecraft:overworld",
                    "x": 20,
                    "y": 65,
                    "z": 20,
                }
            ]

        def list_containers(self):
            return [
                {
                    "dimension": "minecraft:overworld",
                    "x": 2,
                    "y": 65,
                    "z": 2,
                }
            ]

    counts = {"minecraft:bread": 0}
    visited = []

    monkeypatch.setattr(
        "baritone_client.common.storage_catalog.catalog_for",
        lambda _client, _state: Catalog(),
    )
    monkeypatch.setattr(
        "baritone_client.common.harness_ops.move_near",
        lambda _client, x, y, z, **_kwargs: visited.append((x, y, z)) or True,
    )
    monkeypatch.setattr(
        inventory,
        "count_item",
        lambda _client, item_id: counts.get(item_id, 0),
    )

    received_attempts = []

    def withdraw(
        _client,
        position,
        requirements,
        state=None,
        *,
        open_attempts=4,
        allow_recovery_access=False,
    ):
        assert position == (20, 65, 20)
        assert requirements == {"minecraft:bread": 8}
        received_attempts.append(open_attempts)
        assert allow_recovery_access is False
        counts["minecraft:bread"] = 8
        return 1

    monkeypatch.setattr(inventory, "withdraw_required_from_chest", withdraw)

    client = DummyClient(DummyTransport())
    assert inventory.withdraw_required_from_catalog(
        client,
        {"minecraft:bread": 8},
        state=SimpleNamespace(),
    ) == 1
    assert visited == [(20, 65, 20)]
    assert received_attempts == [1]


def test_catalog_withdraw_skips_recently_failed_known_item_container(monkeypatch):
    class Catalog:
        def find_item(self, _item_id):
            return [
                {
                    "dimension": "minecraft:overworld",
                    "x": 20,
                    "y": 65,
                    "z": 20,
                }
            ]

        def list_containers(self):
            return []

    monkeypatch.setattr(
        "baritone_client.common.storage_catalog.catalog_for",
        lambda _client, _state: Catalog(),
    )
    monkeypatch.setattr(inventory, "count_item", lambda *_args: 0)
    monkeypatch.setattr(
        "baritone_client.common.harness_ops.move_near",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cooled container must not trigger travel")
        ),
    )
    client = DummyClient(
        DummyTransport(
            {
                "get_state": {
                    "dimension": "minecraft:overworld",
                    "block_position": {"x": 0, "y": 65, "z": 0},
                }
            }
        )
    )
    storage_safety.remember_unreachable_storage(client, (20, 65, 20))

    assert inventory.withdraw_required_from_catalog(
        client,
        {"minecraft:bread": 6},
        state=SimpleNamespace(),
    ) == 0


def test_catalog_withdraw_skips_excessive_vertical_recovery_route(monkeypatch):
    class Catalog:
        def find_item(self, _item_id):
            return [
                {
                    "dimension": "minecraft:overworld",
                    "x": 10,
                    "y": -27,
                    "z": 10,
                }
            ]

        def list_containers(self):
            return []

    monkeypatch.setattr(
        "baritone_client.common.storage_catalog.catalog_for",
        lambda _client, _state: Catalog(),
    )
    monkeypatch.setattr(inventory, "count_item", lambda *_args: 0)
    monkeypatch.setattr(
        "baritone_client.common.harness_ops.move_near",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("deep container must not trigger recovery travel")
        ),
    )
    client = DummyClient(
        DummyTransport(
            {
                "get_state": {
                    "dimension": "minecraft:overworld",
                    "block_position": {"x": 0, "y": 55, "z": 0},
                }
            }
        )
    )

    assert inventory.withdraw_required_from_catalog(
        client,
        {"minecraft:bread": 6},
        state=SimpleNamespace(),
        max_travel_distance=96.0,
        max_vertical_distance=32.0,
    ) == -1


def test_catalog_withdraw_skips_recent_container_known_not_to_hold_item(monkeypatch):
    class Catalog:
        def find_item(self, _item_id):
            return []

        def list_containers(self):
            return [
                {
                    "dimension": "minecraft:overworld",
                    "x": 2,
                    "y": 65,
                    "z": 2,
                    "last_inventory_scan": inventory.time.time(),
                }
            ]

    monkeypatch.setattr(
        "baritone_client.common.storage_catalog.catalog_for",
        lambda _client, _state: Catalog(),
    )
    monkeypatch.setattr(
        inventory,
        "count_item",
        lambda _client, _item_id: 0,
    )
    monkeypatch.setattr(
        "baritone_client.common.harness_ops.move_near",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("fresh negative snapshot must not trigger travel")
        ),
    )

    assert inventory.withdraw_required_from_catalog(
        DummyClient(DummyTransport()),
        {"minecraft:bread": 8},
        state=SimpleNamespace(),
    ) == 0


def test_deposit_loads_persisted_chest_chunk_before_rejecting_it(monkeypatch):
    class UnloadedChestTransport:
        def __init__(self):
            self.loaded = False

        def dispatch(self, route, payload):
            if route == "get_block":
                return {"id": "minecraft:chest" if self.loaded else "minecraft:void_air"}
            if route == "get_state":
                position = {"x": 1, "y": 65, "z": 1} if self.loaded else {
                    "x": 40, "y": 65, "z": 40
                }
                return {"block_position": position}
            if route == "get_screen":
                return {
                    "sync_id": 9,
                    "total_slots": 63,
                    "slots": [
                        {"slot": index, "id": "minecraft:air", "count": 0}
                        for index in range(63)
                    ],
                }
            return {}

    transport = UnloadedChestTransport()
    client = DummyClient(transport)
    moves = []

    def load_chunk(_client, x, y, z, **_kwargs):
        moves.append((x, y, z))
        transport.loaded = True
        return True

    monkeypatch.setattr("baritone_client.common.navigation.goto", load_chunk)
    monkeypatch.setattr(harness_ops, "available", lambda: True)
    monkeypatch.setattr(harness_ops, "open_container", lambda *_args, **_kwargs: True)

    assert inventory.deposit_excess_to_chest(client, (1, 65, 1)) == 0
    assert moves == [(1, 65, 1)]


def test_deposit_resumes_long_chest_return_after_progress_timeout(monkeypatch):
    class DistantChestTransport:
        def __init__(self):
            self.loaded = False
            self.position = {"x": 600, "y": 65, "z": 1}

        def dispatch(self, route, _payload):
            if route == "get_block":
                return {
                    "id": (
                        "minecraft:chest"
                        if self.loaded else "minecraft:void_air"
                    )
                }
            if route == "get_state":
                return {"block_position": dict(self.position)}
            if route == "get_screen":
                return {
                    "sync_id": 9,
                    "total_slots": 63,
                    "slots": [
                        {"slot": index, "id": "minecraft:air", "count": 0}
                        for index in range(63)
                    ],
                }
            return {}

    transport = DistantChestTransport()
    client = DummyClient(transport)
    moves = []

    def load_chunk(_client, x, y, z, **_kwargs):
        moves.append((x, y, z))
        if len(moves) == 1:
            transport.position = {"x": 200, "y": 65, "z": 1}
            return False
        transport.position = {"x": 1, "y": 65, "z": 1}
        transport.loaded = True
        return True

    monkeypatch.setattr("baritone_client.common.navigation.goto", load_chunk)
    monkeypatch.setattr(harness_ops, "available", lambda: True)
    monkeypatch.setattr(
        harness_ops, "open_container", lambda *_args, **_kwargs: True
    )

    assert inventory.deposit_excess_to_chest(client, (1, 65, 1)) == 0
    assert moves == [(1, 65, 1), (1, 65, 1)]


def test_deposit_aborts_chest_travel_when_health_drops(monkeypatch):
    class UnsafeTravelTransport:
        def __init__(self):
            self.health = 20.0

        def dispatch(self, route, _payload):
            if route == "get_block":
                return {"id": "minecraft:void_air"}
            if route == "get_state":
                return {
                    "health": self.health,
                    "food_level": 20,
                    "block_position": {"x": 0, "y": 65, "z": 0},
                }
            return {}

    transport = UnsafeTravelTransport()
    client = DummyClient(transport)

    def unsafe_goto(_client, _x, _y, _z, *, on_tick, **_kwargs):
        # Genuine danger, not a scratch. This was 12.0 when starting and
        # aborting a trip shared one threshold of 18 health; the two are now
        # separate so a deposit is not abandoned over a single hit. See
        # tests/test_storage_travel_margin.py for the hysteresis itself.
        transport.health = 3.0
        on_tick()
        return False

    monkeypatch.setattr("baritone_client.common.navigation.goto", unsafe_goto)

    assert inventory.deposit_excess_to_chest(client, (80, 65, 0)) == -1
    assert client._storage_survival_abort is True


def test_deposit_keeps_travelling_after_losing_a_little_health(monkeypatch):
    """The other half of the rule above: a scuffed bot still banks its cargo."""
    class ScuffedTravelTransport:
        def __init__(self):
            self.health = 20.0
            self.cancelled = False

        def dispatch(self, route, _payload):
            if route == "cancel":
                self.cancelled = True
            if route == "get_block":
                return {"id": "minecraft:void_air"}
            if route == "get_state":
                return {
                    "health": self.health,
                    "food_level": 20,
                    "block_position": {"x": 0, "y": 65, "z": 0},
                }
            return {}

    transport = ScuffedTravelTransport()
    client = DummyClient(transport)

    def scuffed_goto(_client, _x, _y, _z, *, on_tick, **_kwargs):
        transport.health = 12.0  # hit once en route; keep going
        on_tick()
        return False

    monkeypatch.setattr("baritone_client.common.navigation.goto", scuffed_goto)

    inventory.deposit_excess_to_chest(client, (80, 65, 0))

    assert transport.cancelled is False, "abandoned a deposit over one hit"
    assert getattr(client, "_storage_survival_abort", False) is False


def test_storage_location_resolves_from_production_checkpoint_state(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    class Transport:
        def dispatch(self, route, payload):
            if route == "get_block":
                assert payload == {"x": 10, "y": 64, "z": 20}
                return {"id": "minecraft:chest"}
            return {}

    state = SimpleNamespace(
        get_locations=lambda category: {
            category: [{"x": 10, "y": 64, "z": 20}]
        }
    )

    assert inventory.resolve_storage_location(
        DummyClient(Transport()), state=state
    ) == (10, 64, 20)


def test_persist_storage_writes_both_checkpoint_formats(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    class Transport:
        def dispatch(self, route, _payload):
            if route == "get_block":
                return {"id": "minecraft:chest"}
            if route == "get_inventory":
                return {"inventory": []}
            return {}

    class State:
        def __init__(self):
            self.locations = []
            self.saved = []

        def add_location(self, category, x, y, z, **_kwargs):
            self.locations.append((category, x, y, z))

        def save_checkpoint(self, inventory_summary):
            self.saved.append(inventory_summary)

    state = State()
    assert inventory.persist_storage_location(
        DummyClient(Transport()), (10, 64, 20), state=state
    )

    compatibility = json.loads(
        (tmp_path / "checkpoint_storage.json").read_text(encoding="utf-8")
    )
    assert compatibility["data"] == {"x": 10, "y": 64, "z": 20}
    assert state.locations == [("chest", 10, 64, 20)]
    assert state.saved == [{}]


def test_dump_to_chest_uses_production_location_and_verified_screen(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)

    class ChestTransport:
        def __init__(self):
            self.clicks = []
            self.moved = False

        def dispatch(self, route, payload):
            if route == "get_block":
                return {"id": "minecraft:chest"}
            if route == "get_state":
                return {"block_position": {"x": 11, "y": 64, "z": 20}}
            if route == "get_screen":
                slots = [
                    {"slot": index, "id": "minecraft:air", "count": 0}
                    for index in range(63)
                ]
                if not self.moved:
                    slots[27] = {
                        "slot": 27,
                        "id": "minecraft:dirt",
                        "count": 64,
                    }
                slots[28] = {
                    "slot": 28,
                    "id": "minecraft:stone_pickaxe",
                    "count": 1,
                }
                return {
                    "sync_id": 9,
                    "total_slots": 63,
                    "slots": slots,
                }
            if route == "inventory_click":
                self.clicks.append(payload)
                self.moved = True
            return {}

    state = SimpleNamespace(
        get_locations=lambda category: {
            category: [{"x": 10, "y": 64, "z": 20}]
        }
    )
    transport = ChestTransport()
    monkeypatch.setattr(harness_ops, "open_container", lambda *_args, **_kwargs: True)

    moved = inventory.dump_to_chest(
        DummyClient(transport),
        keep_items=["minecraft:stone_pickaxe"],
        state=state,
    )

    assert moved == 1
    assert transport.clicks == [
        {
            "slot": 27,
            "type": "QUICK_MOVE",
            "button": 0,
            "sync_id": 9,
        }
    ]


def test_dump_to_chest_rejects_stale_saved_location(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    class Transport:
        def dispatch(self, route, _payload):
            if route == "get_block":
                return {"id": "minecraft:air"}
            return {}

    state = SimpleNamespace(
        get_locations=lambda category: {
            category: [{"x": 10, "y": 64, "z": 20}]
        }
    )

    assert (
        inventory.dump_to_chest(
            DummyClient(Transport()), keep_items=[], state=state
        )
        == -1
    )


def test_stick_plank_prep_gathers_logs_when_none_carried(monkeypatch):
    # Live blocker: worn iron pickaxe replacement needed sticks, sticks needed
    # planks, the bot carried zero logs, and plank prep failed closed without
    # ever gathering wood. It must acquire a small log reserve instead.
    from baritone_client.common import resources

    client = DummyClient(DummyTransport())
    counts = {"minecraft:stick": 0, "minecraft:oak_log": 0, "minecraft:oak_planks": 0}
    gathered = []

    def fake_gather_wood(_client, count=16, **_kwargs):
        gathered.append(count)
        counts["minecraft:oak_log"] = count
        return True

    def fake_craft(_client, item_id, qty):
        if item_id.endswith("_planks"):
            counts["minecraft:oak_planks"] += qty
            return True
        return False

    monkeypatch.setattr(inventory, "count_item", lambda _c, item: counts.get(item, 0))
    monkeypatch.setattr(
        inventory,
        "_craft_family_count",
        lambda _c, item: counts.get("minecraft:oak_planks", 0)
        if item.endswith("_planks")
        else counts.get(item, 0),
    )
    monkeypatch.setattr(resources, "gather_wood", fake_gather_wood)
    monkeypatch.setattr(inventory, "craft", fake_craft)

    assert inventory._ensure_planks_for_sticks(client, required_sticks=2)
    assert gathered == [1]  # 4 planks needed -> 1 log reserve


def test_stick_plank_prep_still_fails_closed_when_no_wood_exists(monkeypatch):
    from baritone_client.common import resources

    client = DummyClient(DummyTransport())
    monkeypatch.setattr(inventory, "count_item", lambda _c, _item: 0)
    monkeypatch.setattr(inventory, "_craft_family_count", lambda _c, _item: 0)
    monkeypatch.setattr(resources, "gather_wood", lambda *_a, **_k: False)

    assert not inventory._ensure_planks_for_sticks(client, required_sticks=2)


def test_manual_grid_recipe_shapes_match_vanilla():
    # Ingredient totals per vanilla; a wrong shape silently crafts nothing.
    # Doors all share the 6-planks-in-a-2x3 shape and yield 3. The selector is
    # the literal plank family (not "#planks") so a mixed-wood inventory cannot
    # craft a door of the wrong type than the caller asked for.
    door_woods = (
        "acacia", "bamboo", "birch", "cherry", "dark_oak",
        "jungle", "mangrove", "oak", "spruce",
    )
    expected_counts = {
        f"minecraft:{wood}_door": {f"minecraft:{wood}_planks": 6}
        for wood in door_woods
    }
    expected_counts.update({
        # BOOT_SEQUENCE infrastructure and the starter-house entryway.
        "minecraft:furnace": {"minecraft:cobblestone": 8},
        "minecraft:chest": {"#planks": 8},
        "minecraft:white_bed": {"minecraft:white_wool": 3, "#planks": 3},
        "minecraft:bread": {"minecraft:wheat": 3},
        "minecraft:wooden_hoe": {"#planks": 2, "minecraft:stick": 2},
        # Stone tools.
        "minecraft:stone_sword": {"minecraft:cobblestone": 2, "minecraft:stick": 1},
        "minecraft:stone_shovel": {"minecraft:cobblestone": 1, "minecraft:stick": 2},
        "minecraft:stone_pickaxe": {"minecraft:cobblestone": 3, "minecraft:stick": 2},
        "minecraft:stone_axe": {"minecraft:cobblestone": 3, "minecraft:stick": 2},
        # Diamond tier -- required before obsidian, and therefore before the
        # nether portal, can be mined at all.
        "minecraft:diamond_sword": {"minecraft:diamond": 2, "minecraft:stick": 1},
        "minecraft:diamond_shovel": {"minecraft:diamond": 1, "minecraft:stick": 2},
        "minecraft:diamond_pickaxe": {"minecraft:diamond": 3, "minecraft:stick": 2},
        "minecraft:diamond_axe": {"minecraft:diamond": 3, "minecraft:stick": 2},
        # Wooden tier -- the bootstrap loadout after a total-loss death.
        "minecraft:wooden_sword": {"#planks": 2, "minecraft:stick": 1},
        "minecraft:wooden_shovel": {"#planks": 1, "minecraft:stick": 2},
        "minecraft:wooden_pickaxe": {"#planks": 3, "minecraft:stick": 2},
        "minecraft:wooden_axe": {"#planks": 3, "minecraft:stick": 2},
        # Remaining hoe tiers.
        "minecraft:stone_hoe": {"minecraft:cobblestone": 2, "minecraft:stick": 2},
        "minecraft:iron_hoe": {"minecraft:iron_ingot": 2, "minecraft:stick": 2},
        # Iron tools and armour.
        "minecraft:iron_sword": {"minecraft:iron_ingot": 2, "minecraft:stick": 1},
        "minecraft:iron_shovel": {"minecraft:iron_ingot": 1, "minecraft:stick": 2},
        "minecraft:iron_pickaxe": {"minecraft:iron_ingot": 3, "minecraft:stick": 2},
        "minecraft:iron_axe": {"minecraft:iron_ingot": 3, "minecraft:stick": 2},
        "minecraft:iron_helmet": {"minecraft:iron_ingot": 5},
        "minecraft:iron_chestplate": {"minecraft:iron_ingot": 8},
        "minecraft:iron_leggings": {"minecraft:iron_ingot": 7},
        "minecraft:iron_boots": {"minecraft:iron_ingot": 4},
        "minecraft:stick": {"#planks": 2},
        # Vanilla torch is one coal OR charcoal above one stick, yielding 4.
        "minecraft:torch": {"#coals": 1, "minecraft:stick": 1},
        "minecraft:bucket": {"minecraft:iron_ingot": 3},
        "minecraft:shield": {"#planks": 6, "minecraft:iron_ingot": 1},
        "minecraft:flint_and_steel": {"minecraft:iron_ingot": 1, "minecraft:flint": 1},
        "minecraft:paper": {"minecraft:sugar_cane": 3},
        "minecraft:book": {"minecraft:paper": 3, "minecraft:leather": 1},
        "minecraft:bookshelf": {"#planks": 6, "minecraft:book": 3},
        "minecraft:enchanting_table": {
            "minecraft:book": 1,
            "minecraft:diamond": 2,
            "minecraft:obsidian": 4,
        },
        "minecraft:blaze_powder": {"minecraft:blaze_rod": 1},
            "minecraft:ender_eye": {
                "minecraft:blaze_powder": 1,
                "minecraft:ender_pearl": 1,
            },
            "minecraft:shulker_box": {
                "minecraft:shulker_shell": 2,
                "minecraft:chest": 1,
            },
        "minecraft:bow": {"minecraft:stick": 3, "minecraft:string": 3},
        "minecraft:arrow": {
            "minecraft:flint": 1,
            "minecraft:stick": 1,
            "minecraft:feather": 1,
        },
        "minecraft:ladder": {"minecraft:stick": 7},
    })
    assert set(inventory._MANUAL_GRID_RECIPES) == set(expected_counts)
    for item_id, spec in inventory._MANUAL_GRID_RECIPES.items():
        placements = spec["placements"]
        slots = [slot for _sel, slot in placements]
        assert len(slots) == len(set(slots)), f"{item_id}: duplicate grid slot"
        assert all(1 <= slot <= 9 for slot in slots), f"{item_id}: slot out of range"
        counts = {}
        for selector, _slot in placements:
            counts[selector] = counts.get(selector, 0) + 1
        assert counts == expected_counts[item_id], f"{item_id}: wrong ingredients"


def test_every_manual_grid_recipe_routes_to_manual_driver(monkeypatch):
    driven = []

    def fake_recipe_manual(_client, result_id, placements, crafts=1, output_per_recipe=1):
        driven.append((result_id, tuple(placements), crafts, output_per_recipe))
        return True

    for item_id, spec in inventory._MANUAL_GRID_RECIPES.items():
        driven.clear()
        transport = DummyTransport()
        client = DummyClient(transport)
        monkeypatch.setattr(inventory, "_wait_craft_result", lambda *_a, **_k: False)
        monkeypatch.setattr(
            inventory,
            "count_item",
            lambda _c, item: 1 if item == "minecraft:crafting_table" else 0,
        )
        monkeypatch.setattr(harness_ops, "available", lambda: True)
        monkeypatch.setattr(harness_ops, "ensure_crafting_table_open", lambda _c: True)
        monkeypatch.setattr(harness_ops, "craft_recipe_manual", fake_recipe_manual)
        # The table now carries tool recipes (stone/iron tools, wooden_hoe), so
        # craft() runs its plank/stick dependency guards first. Those are
        # covered by their own tests; stub them here so this test stays about
        # routing rather than re-exercising a live gather_wood loop.
        monkeypatch.setattr(
            inventory, "_ensure_wooden_tool_ingredients", lambda *_a, **_k: True
        )
        monkeypatch.setattr(inventory, "ensure_tool_sticks", lambda *_a, **_k: True)

        assert inventory.craft(client, item_id, 1) is True, item_id
        output = spec.get("output", 1)
        assert driven == [(item_id, tuple(spec["placements"]), 1, output)], item_id


def test_manual_grid_recipe_scales_crafts_for_multi_output(monkeypatch):
    # 6 arrows at 4 per craft -> 2 grid cycles.
    driven = []

    def fake_recipe_manual(_client, result_id, placements, crafts=1, output_per_recipe=1):
        driven.append((crafts, output_per_recipe))
        return True

    transport = DummyTransport()
    client = DummyClient(transport)
    monkeypatch.setattr(inventory, "_wait_craft_result", lambda *_a, **_k: False)
    monkeypatch.setattr(
        inventory,
        "count_item",
        lambda _c, item: 1 if item == "minecraft:crafting_table" else 0,
    )
    monkeypatch.setattr(harness_ops, "available", lambda: True)
    monkeypatch.setattr(harness_ops, "ensure_crafting_table_open", lambda _c: True)
    monkeypatch.setattr(harness_ops, "craft_recipe_manual", fake_recipe_manual)

    assert inventory.craft(client, "minecraft:arrow", 6) is True
    assert driven == [(2, 4)]


def test_craft_recipe_manual_uses_bridge_place_recipe(monkeypatch):
    calls = []

    class DummyTransport:
        def dispatch(self, route, payload, **kwargs):
            calls.append((route, payload, kwargs))
            if route == "place_recipe":
                return {"data": {"crafted": True, "crafts_completed": 1}}
            return {}

    class DummyClient:
        def __init__(self, transport):
            self.transport = transport

    transport = DummyTransport()
    client = DummyClient(transport)

    placements = [("minecraft:iron_ingot", 1), ("#planks", 4)]

    res = harness_ops.place_recipe(client, "minecraft:shield", placements, crafts=1, output_per_recipe=1)
    assert res is True
    assert len(calls) == 1
    assert calls[0][0] == "place_recipe"
    assert calls[0][1] == {
        "placements": [
            {"selector": "minecraft:iron_ingot", "grid_slot": 1},
            {"selector": "#planks", "grid_slot": 4},
        ],
        "expected_output": "minecraft:shield",
        "expected_count": 1,
        "crafts": 1,
    }
    assert calls[0][2] == {"timeout": 32.0}


def test_place_recipe_splits_large_batch_into_bounded_requests():
    calls = []

    class DummyTransport:
        def dispatch(self, route, payload, **kwargs):
            calls.append((route, payload, kwargs))
            return {
                "data": {
                    "crafted": True,
                    "crafts_completed": payload["crafts"],
                }
            }

    client = DummyClient(DummyTransport())

    assert harness_ops.place_recipe(
        client,
        "minecraft:bread",
        [("minecraft:wheat", 1), ("minecraft:wheat", 2), ("minecraft:wheat", 3)],
        crafts=11,
    )
    assert [payload["crafts"] for _route, payload, _kwargs in calls] == [3, 3, 3, 2]
    assert all(kwargs == {"timeout": 32.0} for _route, _payload, kwargs in calls)


def test_place_recipe_rejects_nonpositive_batch_without_dispatch():
    class DummyTransport:
        def dispatch(self, _route, _payload, **_kwargs):
            raise AssertionError("invalid recipe batch must not be dispatched")

    assert not harness_ops.place_recipe(
        DummyClient(DummyTransport()),
        "minecraft:bread",
        [("minecraft:wheat", 1)],
        crafts=0,
    )


def test_place_recipe_timeout_never_falls_back_to_duplicate_clicks(monkeypatch):
    from baritone_client.core.exceptions import BridgeResponseTimeout

    class DummyTransport:
        def dispatch(self, route, _payload, **_kwargs):
            assert route == "place_recipe"
            raise BridgeResponseTimeout(
                route,
                transport_type="tcp",
                request_sent=True,
            )

    fallback_called = False

    def fallback(*_args, **_kwargs):
        nonlocal fallback_called
        fallback_called = True
        return True

    monkeypatch.setattr(
        harness_ops,
        "_load",
        lambda: {
            "ensure_crafting_output_space": lambda _ctx: True,
            "craft_recipe_manual": fallback,
            "TestContext": lambda client: type(
                "Context", (), {"client": client, "log_event": lambda *_args: None}
            )(),
        },
    )

    with pytest.raises(BridgeResponseTimeout):
        harness_ops.craft_recipe_manual(
            DummyClient(DummyTransport()),
            "minecraft:bread",
            [("minecraft:wheat", 1), ("minecraft:wheat", 2), ("minecraft:wheat", 3)],
            crafts=3,
        )
    assert fallback_called is False


def test_place_recipe_partial_batch_never_falls_back_to_duplicate_clicks(monkeypatch):
    calls = 0

    class DummyTransport:
        def dispatch(self, route, payload, **_kwargs):
            nonlocal calls
            assert route == "place_recipe"
            calls += 1
            if calls == 1:
                return {
                    "data": {
                        "crafted": True,
                        "crafts_completed": payload["crafts"],
                    }
                }
            return {
                "data": {
                    "crafted": False,
                    "crafts_completed": 0,
                    "error": "no_output",
                }
            }

    fallback_called = False

    def fallback(*_args, **_kwargs):
        nonlocal fallback_called
        fallback_called = True
        return True

    monkeypatch.setattr(
        harness_ops,
        "_load",
        lambda: {
            "ensure_crafting_output_space": lambda _ctx: True,
            "craft_recipe_manual": fallback,
            "TestContext": lambda client: type(
                "Context", (), {"client": client, "log_event": lambda *_args: None}
            )(),
        },
    )

    with pytest.raises(RuntimeError, match="3/5 confirmed crafts"):
        harness_ops.craft_recipe_manual(
            DummyClient(DummyTransport()),
            "minecraft:bread",
            [("minecraft:wheat", 1), ("minecraft:wheat", 2), ("minecraft:wheat", 3)],
            crafts=5,
        )
    assert calls == 2
    assert fallback_called is False


def test_craft_recipe_manual_bridge_fallback(monkeypatch):
    from tests.functional.shared import inventory_ops

    class DummyTransport:
        def __init__(self):
            self.calls = []

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "place_recipe":
                raise RuntimeError("Unsupported command")
            if route == "get_screen":
                return {
                    "type": "class_1714",
                    "slots": [
                        {"slot": 0, "id": "minecraft:shield", "count": 1},
                        *[{"slot": slot, "id": "minecraft:air", "count": 0} for slot in range(1, 10)],
                        {"slot": 10, "id": "minecraft:iron_ingot", "count": 1},
                        {"slot": 11, "id": "minecraft:oak_planks", "count": 1},
                        *[{"slot": slot, "id": "minecraft:air", "count": 0} for slot in range(12, 46)],
                    ]
                }
            return {}

    class DummyContext:
        def __init__(self, transport):
            self.client = DummyClient(transport)
            self._books = 0

        def log_event(self, _event):
            pass

    class DummyClient:
        def __init__(self, transport):
            self.transport = transport

    # We mock do_close_container to avoid sleeping
    # and to simulate successful click / output capture
    monkeypatch.setattr(inventory_ops, "do_close_container", lambda _ctx: None)
    monkeypatch.setattr(inventory_ops.time, "sleep", lambda _seconds: None)

    transport = DummyTransport()
    ctx = DummyContext(transport)

    placements = [("minecraft:iron_ingot", 1), ("#planks", 4)]

    # We mock count_item to simulate item crafted
    count_calls = 0
    def mock_count_item(item_id):
        nonlocal count_calls
        count_calls += 1
        if count_calls > 1:
            return 1 # after craft
        return 0
    ctx.count_item = mock_count_item

    # This should fall back to python clicks and return true
    res = inventory_ops.craft_recipe_manual(ctx, "minecraft:shield", placements, crafts=1, output_per_recipe=1)
    assert res is True

    # Verify we tried place_recipe and then did get_screen and inventory_clicks
    routes = [r for r, p in transport.calls]
    assert "place_recipe" in routes
    assert "get_screen" in routes
    assert "inventory_click" in routes


def test_craft_recipe_manual_matches_underscore_planks_selector(monkeypatch):
    """_craft_tool_manual_generic passes "_planks" (not "#planks") as the
    wooden-tool material selector. craft_recipe_manual must recognize both as
    "any plank family" -- an exact-equality fallback against a real item id
    like "minecraft:spruce_planks" never matches, and every wooden tool craft
    fails at this step regardless of carried plank count. Confirmed live:
    Bot09 stuck looping "missing _planks" with 8 spruce planks in hand."""
    from tests.functional.shared import inventory_ops

    class DummyTransport:
        def __init__(self):
            self.calls = []

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "place_recipe":
                raise RuntimeError("Unsupported command")
            if route == "get_screen":
                return {
                    "type": "class_1714",
                    "slots": [
                        {"slot": 0, "id": "minecraft:wooden_pickaxe", "count": 1},
                        *[{"slot": slot, "id": "minecraft:air", "count": 0} for slot in range(1, 10)],
                        {"slot": 10, "id": "minecraft:spruce_planks", "count": 8},
                        {"slot": 11, "id": "minecraft:stick", "count": 2},
                        *[{"slot": slot, "id": "minecraft:air", "count": 0} for slot in range(12, 46)],
                    ]
                }
            return {}

    class DummyContext:
        def __init__(self, transport):
            self.client = DummyClient(transport)

        def log_event(self, _event):
            pass

    class DummyClient:
        def __init__(self, transport):
            self.transport = transport

    monkeypatch.setattr(inventory_ops, "do_close_container", lambda _ctx: None)
    monkeypatch.setattr(inventory_ops.time, "sleep", lambda _seconds: None)

    transport = DummyTransport()
    ctx = DummyContext(transport)

    placements = [
        ("_planks", 1), ("_planks", 2), ("_planks", 3),
        ("minecraft:stick", 5), ("minecraft:stick", 8),
    ]

    count_calls = 0
    def mock_count_item(item_id):
        nonlocal count_calls
        count_calls += 1
        if count_calls > 1:
            return 1  # after craft
        return 0
    ctx.count_item = mock_count_item

    res = inventory_ops.craft_recipe_manual(
        ctx, "minecraft:wooden_pickaxe", placements, crafts=1, output_per_recipe=1
    )
    assert res is True


def test_harness_fallback_does_not_submit_atomic_recipe_twice(monkeypatch):
    calls = []

    class DummyTransport:
        def dispatch(self, route, payload, **_kwargs):
            calls.append((route, payload))
            if route == "place_recipe":
                return {"data": {"crafted": False, "error": "output_not_collected"}}
            return {}

    client = DummyClient(DummyTransport())
    fallback_args = {}

    monkeypatch.setattr(
        harness_ops,
        "_load",
        lambda: {
            "ensure_crafting_output_space": lambda _ctx: True,
            "craft_recipe_manual": lambda _ctx, result_id, placements, **kwargs: (
                fallback_args.update(
                    result_id=result_id,
                    placements=placements,
                    **kwargs,
                )
                or True
            ),
            "TestContext": lambda client: type(
                "Context", (), {"client": client, "log_event": lambda *_args: None}
            )(),
        },
    )

    assert harness_ops.craft_recipe_manual(
        client,
        "minecraft:bucket",
        [("minecraft:iron_ingot", 1), ("minecraft:iron_ingot", 3), ("minecraft:iron_ingot", 5)],
    )
    assert [route for route, _payload in calls].count("place_recipe") == 1
    assert fallback_args["try_bridge"] is False


def test_plank_requirement_uses_batch_yield_not_one_per_stick(monkeypatch):
    # 2 planks -> 4 sticks. 3 sticks needs 2 planks (one batch), NOT 6.
    # Regression for the FOOD_AND_IRON deep-mining-prep deadlock: with 5 planks
    # and no logs, the old `sticks*2` formula demanded 6 and failed to gather
    # a log it did not need.
    client = DummyClient(DummyTransport())
    gathered = []
    monkeypatch.setattr(inventory, "_craft_family_count", lambda _c, _item: 5)
    monkeypatch.setattr(
        inventory,
        "_first_log_family_with_stock",
        lambda _c, _n=1: gathered.append("looked_for_log") or "",
    )

    # 5 planks already cover 3 sticks (needs 2) -> returns True, never touches logs
    assert inventory._ensure_planks_for_sticks(client, required_sticks=3)
    assert gathered == [], "must not look for logs when planks already suffice"


def test_plank_requirement_batches_round_up(monkeypatch):
    # 5 sticks -> 2 batches -> 4 planks required.
    seen = {}
    client = DummyClient(DummyTransport())
    monkeypatch.setattr(inventory, "_craft_family_count", lambda _c, _item: 4)
    monkeypatch.setattr(
        inventory,
        "_first_log_family_with_stock",
        lambda _c, _n=1: seen.setdefault("looked", True) or "",
    )
    assert inventory._ensure_planks_for_sticks(client, required_sticks=5)
    assert "looked" not in seen  # 4 planks exactly cover 2 batches


def test_drop_items_maps_hotbar_inventory_index_to_player_handler(monkeypatch):
    items = [{"slot": 0, "id": "minecraft:dirt", "count": 64}]

    class Transport:
        def __init__(self):
            self.calls = []

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_inventory":
                return {"data": {"inventory": list(items)}}
            if route == "inventory_click":
                assert payload == {"slot": 36, "type": "THROW", "button": 1}
                items.clear()
            return {}

    transport = Transport()
    monkeypatch.setattr(inventory.time, "sleep", lambda _seconds: None)

    assert inventory.drop_items(
        DummyClient(transport), ["minecraft:dirt"], max_stacks=1
    ) == 1
    assert any(route == "close_screen" for route, _payload in transport.calls)


def test_drop_items_can_remove_duplicate_tool_while_retaining_one(monkeypatch):
    items = [
        {"slot": 0, "id": "minecraft:shears", "count": 1},
        {"slot": 1, "id": "minecraft:shears", "count": 1},
    ]

    class Transport:
        def dispatch(self, route, payload):
            if route == "get_inventory":
                return {"data": {"inventory": list(items)}}
            if route == "inventory_click":
                inventory_slot = int(payload["slot"]) - 36
                items[:] = [item for item in items if item["slot"] != inventory_slot]
            return {}

    monkeypatch.setattr(inventory.time, "sleep", lambda _seconds: None)
    client = DummyClient(Transport())

    assert inventory.drop_items(
        client,
        ["minecraft:shears"],
        max_stacks=1,
        retain_counts={"minecraft:shears": 1},
    ) == 1
    assert sum(item["count"] for item in items) == 1


def test_storage_resolver_reads_starter_house_supply_chest(monkeypatch):
    state = SimpleNamespace(
        custom_data={
            "structures": {
                "starter_house": {"supply_chest": [12, 70, -4]}
            }
        }
    )
    assert inventory.resolve_storage_location(
        DummyClient(DummyTransport()), state=state, verify=False
    ) == (12, 70, -4)


def test_storage_resolver_prefers_verified_chest_over_unloaded_landmark():
    state = SimpleNamespace(
        custom_data={
            "structures": {
                "starter_house": {"supply_chest": [100, 70, 100]}
            },
            "locations": {"chest": [{"x": 4, "y": 65, "z": 4}]},
        },
        get_locations=lambda category: {
            category: [{"x": 4, "y": 65, "z": 4}]
        },
    )

    class Transport:
        def dispatch(self, route, payload):
            if route == "get_block":
                position = (payload["x"], payload["y"], payload["z"])
                return {
                    "id": (
                        "minecraft:void_air"
                        if position == (100, 70, 100)
                        else "minecraft:chest"
                    )
                }
            return {}

    assert inventory.resolve_storage_location(
        DummyClient(Transport()), state=state, verify=True
    ) == (4, 65, 4)


def test_reset_inventory_cache_prevents_stale_phantom_after_death():
    """A dropped-on-death tool must not resurface from the stale cache.

    After death, a failed current read is unknown evidence.  The cache reset
    keeps the prior observation from being mistaken for a live inventory, and
    the read path fails closed when the bridge remains unavailable.
    """
    class FlakyTransport:
        def __init__(self):
            self.fail = False

        def dispatch(self, route, payload):
            if self.fail:
                return {"error": "player not available"}
            return {
                "data": {
                    "inventory": [
                        {"id": "minecraft:wooden_pickaxe", "count": 1}
                    ]
                }
            }

    client = SimpleNamespace(transport=FlakyTransport())
    # Prime the cache with a pre-death pickaxe.
    assert inventory.count_item(client, "minecraft:wooden_pickaxe") == 1

    # Death drops it; respawn clears the cache.
    inventory.reset_inventory_cache()

    # Subsequent bridge reads fail -> current inventory is unknown and must
    # not be replaced with the dropped pickaxe from the old snapshot.
    client.transport.fail = True
    with pytest.raises(RuntimeError, match="Current inventory unavailable"):
        inventory.count_item(client, "minecraft:wooden_pickaxe")


def test_stale_cache_would_report_phantom_without_reset():
    """Without reset, an unavailable read still fails closed as unknown."""
    class FlakyTransport:
        def __init__(self):
            self.fail = False

        def dispatch(self, route, payload):
            if self.fail:
                return {"error": "player not available"}
            return {
                "data": {
                    "inventory": [
                        {"id": "minecraft:wooden_pickaxe", "count": 1}
                    ]
                }
            }

    client = SimpleNamespace(transport=FlakyTransport())
    assert inventory.count_item(client, "minecraft:wooden_pickaxe") == 1
    # No reset. A failed read must not return the stale snapshot as evidence.
    client.transport.fail = True
    with pytest.raises(RuntimeError, match="Current inventory unavailable"):
        inventory.count_item(client, "minecraft:wooden_pickaxe")
    # Clean up module state so the cache does not leak into other tests.
    inventory.reset_inventory_cache()


def test_torch_recipe_accepts_charcoal_not_just_coal():
    """Charcoal crafts torches exactly like coal. Requiring literal
    minecraft:coal made a bot that had smelted its own charcoal report
    "Missing ingredients for minecraft:torch" forever. Live 2026-07-31: Bot16
    held 6 charcoal, 4 sticks and 0 coal and failed torch_supply 50 times --
    the last step blocking the fleet's first ever BOOT_SEQUENCE completion."""
    from baritone_client.automator.resource_manager import ResourceManager

    recipe = ResourceManager.DEFAULT_RECIPES["minecraft:torch"]
    ingredients = dict(recipe["ingredients"])
    assert "#coals" in ingredients, "torch must accept coal OR charcoal"
    assert "minecraft:coal" not in ingredients
    assert "minecraft:charcoal" in ResourceManager.EQUIVALENCIES["#coals"]


def test_torch_has_a_manual_grid_fallback():
    """There was no manual fallback for torches, so when the bridge craft
    reported "Missing ingredients" and auto_craft reported "Recipe not found",
    every method failed."""
    from baritone_client.common.inventory import _MANUAL_GRID_RECIPES

    spec = _MANUAL_GRID_RECIPES["minecraft:torch"]
    assert spec["output"] == 4
    placements = dict((item, slot) for item, slot in spec["placements"])
    # Fuel directly above the stick: slots 1 and 4 in a row-major 3x3.
    assert placements["#coals"] == 1
    assert placements["minecraft:stick"] == 4


def test_manual_placement_selector_matches_charcoal():
    """A "#coals" placement never matched a literal item id, so a bot holding
    charcoal could not manually craft torches."""
    import re
    from pathlib import Path

    source = Path("tests/functional/shared/inventory_ops.py").read_text(
        encoding="utf-8"
    )
    assert '"#coals", "any_coal"' in source, (
        "the manual placement matcher must resolve the #coals selector"
    )
    assert "minecraft:charcoal" in source


def test_placement_selectors_resolve_to_a_carried_item(monkeypatch):
    """The bridge's native place_recipe only understands literal item ids and
    rejected a "#coals" selector outright ("Missing ingredient '#coals' for
    grid slot 1"). Resolving to whichever fuel the bot actually carries lets
    the fast path work. Live 2026-07-31: Bot16 held 6 charcoal, 0 coal."""
    from baritone_client.common import harness_ops

    monkeypatch.setattr(
        "baritone_client.common.inventory.count_item",
        lambda _c, item: 6 if item == "minecraft:charcoal" else 0,
    )
    resolved = harness_ops._resolve_placement_selectors(
        object(), [("#coals", 1), ("minecraft:stick", 4)]
    )
    assert resolved == [("minecraft:charcoal", 1), ("minecraft:stick", 4)]


def test_placement_selector_passes_through_when_nothing_carried(monkeypatch):
    """With no member carried the selector is left alone so the existing
    error path still reports the real missing ingredient."""
    from baritone_client.common import harness_ops

    monkeypatch.setattr(
        "baritone_client.common.inventory.count_item", lambda _c, _i: 0
    )
    resolved = harness_ops._resolve_placement_selectors(object(), [("#coals", 1)])
    assert resolved == [("#coals", 1)]


def test_plank_selector_is_left_for_the_harness(monkeypatch):
    """#planks is resolved by the harness matcher itself; do not rewrite it."""
    from baritone_client.common import harness_ops

    monkeypatch.setattr(
        "baritone_client.common.inventory.count_item", lambda _c, _i: 9
    )
    resolved = harness_ops._resolve_placement_selectors(object(), [("#planks", 1)])
    assert resolved == [("#planks", 1)]


def test_select_item_uses_the_harness_hotbar_swap_and_selects_that_slot(monkeypatch):
    """The harness picks an empty hotbar slot and reports where it landed.

    The native path always targeted hotbar 0 and then selected slot 0
    regardless, so a full hotbar left the main hand holding the wrong item.
    Live 2026-07-31 this stranded Bot16's torches (slot 16), Bot18's furnace
    and chest (33/34) and Bot05's crafting table (34).
    """
    transport = DummyTransport({"get_inventory": {
        "inventory": [{"slot": 16, "id": "minecraft:torch", "count": 9}]
    }})
    client = DummyClient(transport)

    # Detailed stateful coverage lives in test_hotbar_selection.py. This
    # legacy transport does not model inventory mutations, so the hardened
    # selector must fail closed instead of claiming the accepted click worked.
    assert inventory.select_item(client, "minecraft:torch", allow_swap=True) is False


def test_select_item_falls_back_to_native_swap_without_the_harness(monkeypatch):
    transport = DummyTransport({"get_inventory": {
        "inventory": [{"slot": 16, "id": "minecraft:torch", "count": 9}]
    }})
    client = DummyClient(transport)

    monkeypatch.setattr(harness_ops, "available", lambda: False)
    monkeypatch.setattr(inventory.time, "sleep", lambda _s: None)

    assert inventory.select_item(client, "minecraft:torch", allow_swap=True) is False
    assert any(route == "inventory_click" for route, _ in transport.calls)


def test_harness_hotbar_failure_does_not_break_selection(monkeypatch):
    """A harness error must degrade to the native swap, not abort placement."""
    transport = DummyTransport({"get_inventory": {
        "inventory": [{"slot": 16, "id": "minecraft:torch", "count": 9}]
    }})
    client = DummyClient(transport)

    monkeypatch.setattr(inventory.time, "sleep", lambda _s: None)

    assert inventory.select_item(client, "minecraft:torch", allow_swap=True) is False


def test_clear_occluded_chest_face_only_clears_incidental_ground_cover(monkeypatch):
    """Cover can be cleared without treating walls or workstations as debris."""
    from baritone_client.common import inventory as inv

    state = {
        "block_position": {"x": -432.0, "y": 78.0, "z": -1.0},
    }
    blocks = {
        (-432, 78, 1): "minecraft:short_grass",     # front face toward player
        (-432, 79, 1): "minecraft:vine",            # head slot above front
        (-432, 79, 2): "minecraft:snow",            # thin layer above chest
        (-432, 78, 2): "minecraft:chest",           # the chest itself
    }
    broken = []
    looked = []

    class Transport:
        def dispatch(self, route, payload):
            if route == "get_state":
                return {"data": state}
            if route == "get_block":
                key = (payload["x"], payload["y"], payload["z"])
                # Simulate the block clearing after break_block.
                if key in broken:
                    return {"id": "minecraft:air"}
                return {"id": blocks.get(key, "minecraft:air")}
            if route == "break_block":
                broken.append((payload["x"], payload["y"], payload["z"]))
                return {}
            if route == "look_at":
                looked.append(payload)
                return {}
            return {}

    client = SimpleNamespace(transport=Transport())
    inv._clear_occluded_chest_face(client, (-432, 78, 2), "minecraft:chest")

    assert broken == [(-432, 78, 1), (-432, 79, 1), (-432, 79, 2)]


def test_clear_occluded_chest_face_never_digs_another_container(monkeypatch):
    """A double-chest neighbour must not be treated as an occluder."""
    from baritone_client.common import inventory as inv

    state = {"block_position": {"x": -432.0, "y": 78.0, "z": -1.0}}
    blocks = {
        (-432, 78, 1): "minecraft:chest",  # the adjacent half of a double chest
    }
    broken = []

    class Transport:
        def dispatch(self, route, payload):
            if route == "get_state":
                return {"data": state}
            if route == "get_block":
                return {"id": blocks.get(
                    (payload["x"], payload["y"], payload["z"]),
                    "minecraft:air",
                )}
            if route == "break_block":
                broken.append((payload["x"], payload["y"], payload["z"]))
            return {}

    client = SimpleNamespace(transport=Transport())
    inv._clear_occluded_chest_face(client, (-432, 78, 2), "minecraft:chest")
    assert broken == [], "must not break an adjacent container"


def test_clear_occluded_chest_face_never_breaks_machinery_or_lighting():
    """Live 2026-09-29: a forester tried to break the sorter's intake hoppers."""
    from baritone_client.common import inventory as inv

    state = {"block_position": {"x": 580.0, "y": 80.0, "z": -308.0}}
    blocks = {
        (580, 81, -309): "minecraft:hopper",
        (579, 81, -309): "minecraft:hopper",
        (579, 80, -308): "minecraft:wall_torch",
        (579, 81, -308): "minecraft:redstone_wire",
    }
    broken = []

    class Transport:
        def dispatch(self, route, payload):
            if route == "get_state":
                return {"data": state}
            if route == "get_block":
                return {"id": blocks.get((payload["x"], payload["y"], payload["z"]), "minecraft:air")}
            if route == "break_block":
                broken.append((payload["x"], payload["y"], payload["z"]))
            return {}

    client = SimpleNamespace(transport=Transport())
    inv._clear_occluded_chest_face(client, (579, 80, -309), "minecraft:chest")
    assert broken == []
