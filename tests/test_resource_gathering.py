from types import SimpleNamespace

import pytest

from baritone_client.common import (
    descent_recovery,
    inventory,
    resources,
    shelf_escape,
    stone_descent,
)


class RecordingTransport:
    def __init__(self, break_response=None):
        self.calls = []
        self.break_response = break_response or {"started": True}

    def dispatch(self, route, payload):
        self.calls.append((route, payload))
        if route == "break_block":
            return self.break_response
        if route == "get_inventory":
            # An explicit empty snapshot is a valid physical read.  Returning
            # {} would model an unavailable bridge response and now correctly
            # abort inventory-dependent helpers.
            return {"inventory": [], "armor": [], "offhand": []}
        return {}


def test_approach_discovered_log_stops_beside_it_then_breaks(monkeypatch):
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport)
    approaches = []
    monkeypatch.setattr(
        resources,
        "goto",
        lambda _client, x, y, z, **kwargs: approaches.append(
            (x, y, z, kwargs["tolerance"])
        )
        or True,
    )
    monkeypatch.setattr(resources, "select_item", lambda *_args, **_kwargs: True)

    assert resources._approach_and_break_log(client, (-71, 63, -114))
    assert approaches == [(-71, 63, -114, 3.0)]
    assert (
        "break_block",
        {"x": -71, "y": 63, "z": -114},
    ) in transport.calls


def test_discovered_log_is_not_broken_when_approach_fails(monkeypatch):
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(resources, "goto", lambda *_args, **_kwargs: False)

    assert not resources._approach_and_break_log(client, (-71, 63, -114))
    assert not any(route == "break_block" for route, _payload in transport.calls)


def test_safe_nearby_stone_avoids_block_directly_below_player():
    class StoneTransport(RecordingTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {"block_position": {"x": 10, "y": 62, "z": 20}}
            if route == "find_blocks":
                return {
                    "found": [
                        {"x": 10, "y": 61, "z": 20, "distance": 1.0},
                        {"x": 11, "y": 62, "z": 20, "distance": 1.0},
                    ]
                }
            return {}

    client = SimpleNamespace(transport=StoneTransport())

    assert resources._find_safe_nearby_stone(client) == (11, 62, 20)


def test_safe_nearby_ore_prefers_exposed_side_face():
    class OreTransport(RecordingTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {"block_position": {"x": -23, "y": 60, "z": -120}}
            if route == "find_blocks":
                return {
                    "found": [
                        {"x": -23, "y": 59, "z": -120, "distance": 1.0},
                        {"x": -24, "y": 60, "z": -121, "distance": 1.4},
                    ]
                }
            return {}

    client = SimpleNamespace(transport=OreTransport())

    assert resources._find_safe_nearby_ore(
        client, ["minecraft:coal_ore"]
    ) == (-24, 60, -121)


def test_safe_nearby_ore_skips_excluded_positions():
    class OreTransport(RecordingTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {"block_position": {"x": -23, "y": 60, "z": -120}}
            if route == "find_blocks":
                return {
                    "found": [
                        {"x": -24, "y": 60, "z": -121, "distance": 1.4},
                        {"x": -24, "y": 59, "z": -120, "distance": 1.0},
                    ]
                }
            return {}

    client = SimpleNamespace(transport=OreTransport())

    assert resources._find_safe_nearby_ore(
        client,
        ["minecraft:coal_ore"],
        excluded_positions={(-24, 60, -121)},
    ) == (-24, 59, -120)


def test_exact_stone_break_selects_pickaxe_and_verifies_air(monkeypatch):
    class StoneTransport(RecordingTransport):
        def __init__(self):
            super().__init__()
            self.block_reads = 0

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_block":
                self.block_reads += 1
                return {
                    "id": "minecraft:stone"
                    if self.block_reads == 1
                    else "minecraft:air"
                }
            return {"started": True}

    transport = StoneTransport()
    client = SimpleNamespace(transport=transport)
    approached = []
    selected = []
    monkeypatch.setattr(
        resources,
        "goto",
        lambda _client, x, y, z, **_kwargs: approached.append((x, y, z)) or True,
    )
    monkeypatch.setattr(
        resources,
        "select_item",
        lambda _client, item_id, **_kwargs: selected.append(item_id)
        or item_id == "minecraft:wooden_pickaxe",
    )
    monkeypatch.setattr(resources.time, "sleep", lambda _seconds: None)

    assert resources._approach_and_break_stone(client, (11, 62, 20))
    assert approached == [(11, 62, 20)]
    assert selected[-1] == "minecraft:wooden_pickaxe"
    assert (
        "break_block",
        {"x": 11, "y": 62, "z": 20},
    ) in transport.calls


def test_path_wait_observes_start_and_completion(monkeypatch):
    class PathTransport(RecordingTransport):
        def __init__(self):
            super().__init__()
            self.states = iter((False, True, True, False))

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {"is_pathing": next(self.states)}
            return {}

    client = SimpleNamespace(transport=PathTransport())
    clock = iter(value * 0.5 for value in range(20))
    monkeypatch.setattr(resources.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(resources.time, "sleep", lambda _seconds: None)

    assert resources._wait_for_path_completion(client, timeout=10.0)


def test_craft_with_table_repairs_locally_without_following_saved_waypoint(monkeypatch):
    from baritone_client.common import base

    client = SimpleNamespace(transport=RecordingTransport())
    opens = iter((False, True))
    counts = {
        "minecraft:crafting_table": 1,
        # A stone axe cannot be crafted from thin air; carry its recipe stone
        # so the stone-material gate (a later fix) passes as it would live.
        "minecraft:cobblestone": 3,
    }

    def fake_craft(_client, item_id, count):
        counts[item_id] = counts.get(item_id, 0) + count
        return True

    monkeypatch.setattr(
        resources,
        "count_item",
        lambda _client, item_id: counts.get(item_id, 0),
    )
    monkeypatch.setattr(resources, "ensure_tool_sticks", lambda *_args: True)
    monkeypatch.setattr(resources, "_wait_for_path_completion", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(resources, "craft", fake_craft)
    monkeypatch.setattr(base, "open_crafting_table", lambda *_args, **_kwargs: next(opens))
    placements = []
    monkeypatch.setattr(
        base,
        "place_crafting_table",
        lambda _client, x, y, z: placements.append((x, y, z)) or True,
    )
    monkeypatch.setattr(resources.time, "sleep", lambda _seconds: None)

    assert resources._craft_with_table(client, "minecraft:stone_axe", 1)
    assert placements == [(2, 0, 0)]
    assert (
        "chat",
        {"message": "#goto crafting_table"},
    ) not in client.transport.calls


def test_prepare_safe_furnace_fuel_consolidates_wood_via_raw_plank_helper(monkeypatch):
    client = SimpleNamespace(transport=RecordingTransport())
    counts = {"minecraft:oak_planks": 0}

    def fake_count(_client, item_id):
        return counts.get(item_id, 0)

    def fake_gather_wood(_client, count, timeout):
        # Existing behavior keeps the gather frontier bounded and daylight-safe.
        assert count == 2
        assert timeout == 300
        return True

    def fake_ensure_raw_planks(_client, required_planks):
        assert required_planks == 8
        counts["minecraft:oak_planks"] = 8
        return True

    monkeypatch.setattr(resources, "count_item", fake_count)
    monkeypatch.setattr(resources, "manage_inventory", lambda _client: None)
    monkeypatch.setattr(resources, "gather_wood", fake_gather_wood)
    monkeypatch.setattr(
        inventory,
        "_ensure_raw_planks",
        fake_ensure_raw_planks,
    )

    assert resources._prepare_safe_furnace_fuel(client, 11) == "minecraft:oak_planks"


def test_prepare_safe_furnace_fuel_fails_when_raw_plank_conversion_fails(monkeypatch):
    client = SimpleNamespace(transport=RecordingTransport())
    monkeypatch.setattr(resources, "count_item", lambda _client, _item: 0)
    monkeypatch.setattr(resources, "manage_inventory", lambda _client: None)
    monkeypatch.setattr(resources, "gather_wood", lambda *_a, **_k: True)
    monkeypatch.setattr(
        inventory,
        "_ensure_raw_planks",
        lambda *_a, **_k: False,
    )

    assert resources._prepare_safe_furnace_fuel(client, 11) is None


def test_craft_with_table_uses_raw_plank_helper_for_table_planks(monkeypatch):
    from baritone_client.common import base

    client = SimpleNamespace(transport=RecordingTransport())
    counts = {"minecraft:crafting_table": 0, "minecraft:oak_planks": 0}
    ensure_calls = []

    def fake_count(_client, item_id):
        return counts.get(item_id, 0)

    def fake_ensure_raw_planks(_client, required_planks):
        ensure_calls.append(required_planks)
        counts["minecraft:oak_planks"] = 8
        return True

    def fake_craft(_client, item_id, qty):
        counts[item_id] = counts.get(item_id, 0) + qty
        return True

    placements = []
    monkeypatch.setattr(resources, "count_item", fake_count)
    monkeypatch.setattr(resources, "ensure_tool_sticks", lambda *_a, **_k: True)
    monkeypatch.setattr(resources, "ensure_stone_material", lambda *_a, **_k: True)
    monkeypatch.setattr(base, "open_crafting_table", lambda *_a, **_k: False)
    monkeypatch.setattr(resources, "craft", fake_craft)
    monkeypatch.setattr(
        inventory,
        "_ensure_raw_planks",
        fake_ensure_raw_planks,
    )
    monkeypatch.setattr(resources.time, "sleep", lambda _s: None)
    monkeypatch.setattr(resources, "get_player_pos", lambda _c: (2, 0, 0))
    monkeypatch.setattr(resources, "_wait_for_path_completion", lambda *_a, **_k: True)
    monkeypatch.setattr(
        base,
        "place_crafting_table",
        lambda _client, x, y, z: placements.append((x, y, z)) or True,
    )

    assert resources._craft_with_table(client, "minecraft:flint_and_steel", 1)
    assert ensure_calls == [4]
    assert counts["minecraft:crafting_table"] == 1


def test_ore_gather_recounts_drops_when_pathing_stops(monkeypatch):
    class OreTransport(RecordingTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_inventory":
                return {"inventory": [], "armor": [], "offhand": []}
            if route == "get_state":
                return {"is_pathing": False}
            return {}

    client = SimpleNamespace(transport=OreTransport())
    recounts = iter((0, 0, 15))
    monkeypatch.setattr(
        resources,
        "count_item",
        lambda _client, item_id: (
            1
            if item_id == "minecraft:stone_pickaxe"
            else next(recounts)
            if item_id == "minecraft:raw_iron"
            else 0
        ),
    )
    monkeypatch.setattr(
        "baritone_client.common.combat.defend_or_flee", lambda _client: False
    )
    monkeypatch.setattr(
        resources,
        "remaining_pickaxe_durability",
        lambda *_args, **_kwargs: 100,
    )
    monkeypatch.setattr(resources.time, "sleep", lambda _seconds: None)

    assert resources.gather_ores(client, "iron", count=15, timeout=30)


def test_single_ore_shortfall_keeps_debris_buffer_and_exits_when_done(
    monkeypatch,
):
    class OreTransport(RecordingTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {"is_pathing": True, "food_level": 20}
            return {}

    client = SimpleNamespace(transport=OreTransport())
    mined = {"started": False}
    reservations = []

    monkeypatch.setattr(
        resources,
        "count_item",
        lambda _client, item_id: (
            1
            if item_id == "minecraft:raw_iron" and mined["started"]
            else 0
        ),
    )
    monkeypatch.setattr(
        resources,
        "remaining_pickaxe_durability",
        lambda *_args, **_kwargs: 100,
    )
    monkeypatch.setattr(
        resources,
        "_reserve_gathering_inventory",
        lambda _client, minimum_free_slots=3: reservations.append(
            minimum_free_slots
        )
        or True,
    )
    monkeypatch.setattr(
        resources,
        "_start_mine_process",
        lambda *_args, **_kwargs: mined.__setitem__("started", True),
    )
    monkeypatch.setattr(resources, "free_inventory_slots", lambda _client: 0)

    assert resources.gather_ores(client, "iron", count=1, timeout=30)
    assert reservations == [3]


def test_pickaxe_durability_uses_damage_from_raw_inventory():
    class InventoryTransport(RecordingTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_inventory":
                return {
                    "inventory": [
                        {
                            "id": "minecraft:iron_pickaxe",
                            "count": 1,
                            "damage": 188,
                        },
                        {
                            "id": "minecraft:stone_pickaxe",
                            "count": 1,
                            "damage": 31,
                        },
                    ],
                    "offhand": [],
                }
            return {}

    client = SimpleNamespace(transport=InventoryTransport())

    assert resources.remaining_pickaxe_durability(
        client, ["minecraft:iron_pickaxe"]
    ) == 62
    assert resources.remaining_pickaxe_durability(client) == 162


def test_gather_ores_reuses_the_wooden_fallback_instead_of_hardcoding_stone(
    monkeypatch,
):
    """A1 deadlocked at Y=-45 carrying 8 cobbled_deepslate and 0 real
    cobblestone: the old branch always asked for stone_pickaxe, the recipe
    rejected the deepslate substitute, and nothing here could mine more
    stone without the pickaxe it was trying to craft. The replacement must
    go through _ensure_mining_pickaxe, which already falls back to wooden.
    """
    client = SimpleNamespace(transport=RecordingTransport())
    calls = []
    monkeypatch.setattr(resources, "remaining_pickaxe_durability", lambda *_a, **_k: 0)
    monkeypatch.setattr(
        resources,
        "_ensure_mining_pickaxe",
        lambda _client: calls.append("wooden-fallback") or False,
    )
    monkeypatch.setattr(
        resources,
        "ensure_supplies",
        lambda *_a, **_k: pytest.fail("iron/diamond path must not run for a non-diamond ore"),
    )

    assert resources.gather_ores(client, "iron", count=1, timeout=30) is False
    assert calls == ["wooden-fallback"]


def test_gather_ores_asks_for_the_iron_pickaxe_head_material_too(monkeypatch):
    """Requesting the tool alone dead-ends at its own missing ingredient.

    Live A1 2026-08-29, descending the obsidian -> diamond_pickaxe ->
    diamond -> iron_pickaxe chain, bottomed out here repeating
    "place_recipe failed for minecraft:iron_pickaxe: Missing ingredient
    'minecraft:iron_ingot'" -- _craft_with_table resolves stick and
    cobblestone dependencies but never gathers ore, so the ingot has to be
    named in the requirement set for its own smelting strategy to run.
    """
    client = SimpleNamespace(transport=RecordingTransport())
    requested = []
    monkeypatch.setattr(resources, "remaining_pickaxe_durability", lambda *_a, **_k: 0)
    monkeypatch.setattr(
        resources,
        "ensure_supplies",
        lambda _client, kit, **_k: requested.append(kit)
        or SimpleNamespace(success=False),
    )

    assert resources.gather_ores(client, "diamond", count=1, timeout=30) is False
    assert requested == [
        {"minecraft:iron_pickaxe": 1, "minecraft:iron_ingot": 3}
    ], "the ingot must be requested alongside the tool that consumes it"


def test_gather_ores_still_demands_an_iron_pickaxe_for_diamond(monkeypatch):
    """Diamond genuinely needs iron tier or better; the wooden fallback must
    not be substituted in for that ore type."""
    client = SimpleNamespace(transport=RecordingTransport())
    requested = []
    monkeypatch.setattr(resources, "remaining_pickaxe_durability", lambda *_a, **_k: 0)
    monkeypatch.setattr(
        resources,
        "_ensure_mining_pickaxe",
        lambda _client: pytest.fail("wooden fallback must not run for diamond"),
    )
    monkeypatch.setattr(
        resources,
        "ensure_supplies",
        lambda _client, kit, **_k: requested.append(kit)
        or SimpleNamespace(success=False),
    )

    assert resources.gather_ores(client, "diamond", count=1, timeout=30) is False
    # Asserts the tier, not the exact requirement set: the head material it
    # is requested with is pinned by
    # test_gather_ores_asks_for_the_iron_pickaxe_head_material_too.
    assert len(requested) == 1
    assert requested[0].get("minecraft:iron_pickaxe") == 1


def test_replacing_mine_process_cancels_before_starting(monkeypatch):
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport)
    sleeps = []
    monkeypatch.setattr(resources.time, "sleep", sleeps.append)

    resources._start_mine_process(client, ["minecraft:iron_ore"], 12)

    assert transport.calls == [
        ("cancel", {}),
        ("mine", {"blocks": ["minecraft:iron_ore"], "quantity": 12}),
    ]
    assert sleeps == [0.75, 0.25]


def test_ore_gather_retry_bounded_to_three_safe_exact_targets(monkeypatch):
    class OreTransport(RecordingTransport):
        def __init__(self):
            super().__init__()
            self.states = iter([{"is_pathing": False, "food_level": 20, "block_position": {"x": 0, "y": 64, "z": 0}}] * 20)

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_inventory":
                return {"inventory": [], "armor": [], "offhand": []}
            if route == "get_state":
                return next(self.states)
            return {}

    transport = OreTransport()
    client = SimpleNamespace(transport=transport)
    sleeps = []

    attempts = []
    monkeypatch.setattr(
        resources,
        "count_item",
        lambda _client, item_id: 0,
    )
    monkeypatch.setattr(
        resources,
        "ensure_supplies",
        lambda *_args, **_kwargs: type("Result", (), {"success": True})(),
    )

    def staged_fallback_target(_client, block_types, radius, excluded_positions=None):
        attempts.append((radius, tuple(sorted(excluded_positions or ()))))
        if (5, 64, 2) in (excluded_positions or ()):
            return None
        return (5, 64, 2)

    monkeypatch.setattr(resources, "_find_safe_nearby_ore", staged_fallback_target)
    monkeypatch.setattr(
        resources,
        "_approach_and_break_stone",
        lambda _client, _target: False,
    )
    descent_attempts = []
    monkeypatch.setattr(
        resources,
        "_descend_to_stone_layer",
        lambda _client: descent_attempts.append(True) or False,
    )
    relocation_attempts = []
    monkeypatch.setattr(
        resources,
        "_relocate_to_dry_stone_terrain",
        lambda _client: relocation_attempts.append(True) or False,
    )
    mine_attempts = []
    monkeypatch.setattr(
        resources,
        "_relocate_to_checkpointed_mine",
        lambda _client: mine_attempts.append(True) or False,
    )
    monkeypatch.setattr(resources.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        "baritone_client.common.combat.defend_or_flee",
        lambda _client: False,
    )

    assert not resources.gather_ores(client, "iron", count=1, timeout=30)

    assert attempts == [
        (8, ()),
        (10, ((5, 64, 2),)),
        (12, ((5, 64, 2),)),
    ]
    assert [
        route
        for route, _ in transport.calls
        if route in {"cancel", "mine", "break_block"}
    ] == [
        "cancel",
        "mine",
        "cancel",
        "mine",
        "cancel",
        "mine",
        "cancel",
    ]
    assert 0.75 in sleeps
    assert 0.25 in sleeps
    assert descent_attempts == [True]
    assert relocation_attempts == [True]
    assert mine_attempts == [True]


def test_ore_gather_restarts_after_one_safe_descent(monkeypatch):
    class OreTransport(RecordingTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_inventory":
                return {"inventory": [], "armor": [], "offhand": []}
            if route == "get_state":
                return {"is_pathing": False, "food_level": 20}
            return {}

    transport = OreTransport()
    client = SimpleNamespace(transport=transport)
    descent_attempts = []

    def count(_client, item_id):
        if item_id == "minecraft:stone_pickaxe":
            return 1
        if item_id == "minecraft:raw_iron" and descent_attempts:
            return 1
        return 0

    monkeypatch.setattr(resources, "count_item", count)
    monkeypatch.setattr(
        resources, "remaining_pickaxe_durability", lambda *_args, **_kwargs: 100
    )
    monkeypatch.setattr(
        resources, "_find_safe_nearby_ore", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        resources,
        "_descend_to_stone_layer",
        lambda _client: descent_attempts.append(True) or True,
    )
    monkeypatch.setattr(resources.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "baritone_client.common.combat.defend_or_flee", lambda _client: False
    )

    assert resources.gather_ores(client, "iron", count=1, timeout=30)
    assert descent_attempts == [True]
    assert [route for route, _payload in transport.calls].count("mine") == 4


def test_ore_gather_relocates_from_wet_column_before_final_descent(monkeypatch):
    class OreTransport(RecordingTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_inventory":
                return {"inventory": [], "armor": [], "offhand": []}
            if route == "get_state":
                return {"is_pathing": False, "food_level": 20}
            return {}

    transport = OreTransport()
    client = SimpleNamespace(transport=transport)
    relocation_attempts = []

    def count(_client, item_id):
        if item_id == "minecraft:stone_pickaxe":
            return 1
        if item_id == "minecraft:raw_iron" and relocation_attempts:
            return 1
        return 0

    monkeypatch.setattr(resources, "count_item", count)
    monkeypatch.setattr(
        resources, "remaining_pickaxe_durability", lambda *_args, **_kwargs: 100
    )
    monkeypatch.setattr(
        resources, "_find_safe_nearby_ore", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(resources, "_descend_to_stone_layer", lambda _client: False)
    monkeypatch.setattr(
        resources,
        "_relocate_to_dry_stone_terrain",
        lambda _client: relocation_attempts.append(True) or True,
    )
    monkeypatch.setattr(resources.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "baritone_client.common.combat.defend_or_flee", lambda _client: False
    )

    assert resources.gather_ores(client, "iron", count=1, timeout=30)
    assert relocation_attempts == [True]
    assert [route for route, _payload in transport.calls].count("mine") == 4


def test_checkpointed_mine_relocation_uses_horizontal_then_exact_goal(monkeypatch):
    client = SimpleNamespace(
        transport=RecordingTransport(),
        _automation_state=SimpleNamespace(
            custom_data={
                "shared_mining_staircase": {"entrance": [-159, 104, -375]}
            }
        ),
    )
    horizontal = []
    exact = []
    monkeypatch.setattr(
        resources,
        "goto_xz",
        lambda _client, x, z, **kwargs: horizontal.append((x, z, kwargs)) or True,
    )
    monkeypatch.setattr(
        resources,
        "goto",
        lambda _client, *target, **kwargs: exact.append((target, kwargs)) or True,
    )

    assert resources._relocate_to_checkpointed_mine(client)
    assert horizontal == [(-159, -375, {"timeout": 240, "tolerance": 6.0})]
    assert exact == [
        (
            (-159, 104, -375),
            {"timeout": 90, "check_interval": 1.0, "tolerance": 3.0},
        )
    ]


def test_smelting_strategy_translates_shortfall_to_absolute_target(monkeypatch):
    client = SimpleNamespace()
    calls = []
    monkeypatch.setattr(
        resources,
        "count_item",
        lambda _client, item_id: 5 if item_id == "minecraft:iron_ingot" else 0,
    )
    monkeypatch.setattr(
        resources,
        "_smelt_with_furnace",
        lambda _client, item_id, target: calls.append((item_id, target)) or True,
    )

    assert resources._smelt_requirement_shortfall(
        client, "minecraft:iron_ingot", 7
    )
    assert calls == [("minecraft:iron_ingot", 12)]


def test_smelting_fuel_prefers_carried_planks_without_coal(monkeypatch):
    counts = {"minecraft:birch_planks": 22}
    monkeypatch.setattr(
        resources,
        "count_item",
        lambda _client, item_id: counts.get(item_id, 0),
    )

    assert resources._select_furnace_fuel(object(), 32) == "minecraft:birch_planks"


def test_safe_smelting_fuel_gathers_wood_and_crafts_planks(monkeypatch):
    counts = {
        "minecraft:birch_log": 3,
        "minecraft:birch_planks": 0,
    }
    gathered = []
    crafted = []
    client = SimpleNamespace(
        transport=SimpleNamespace(dispatch=lambda *_args, **_kwargs: {})
    )
    monkeypatch.setattr(
        resources,
        "count_item",
        lambda _client, item_id: counts.get(item_id, 0),
    )
    monkeypatch.setattr(resources, "manage_inventory", lambda _client: None)

    def gather(_client, count, timeout=0):
        gathered.append((count, timeout))
        counts["minecraft:birch_log"] = count
        return True

    def craft(_client, item_id, count):
        crafted.append((item_id, count))
        counts["minecraft:birch_planks"] += count
        return True

    monkeypatch.setattr(resources, "gather_wood", gather)
    monkeypatch.setattr(resources, "craft", craft)

    assert resources._prepare_safe_furnace_fuel(client, 32) == "minecraft:birch_planks"
    assert gathered == [(6, 300)]
    assert crafted == [("minecraft:birch_planks", 22)]


def test_safe_smelting_consolidates_split_plank_families(monkeypatch):
    counts = {
        "minecraft:birch_planks": 6,
        "minecraft:jungle_planks": 8,
        "minecraft:jungle_log": 2,
    }
    client = SimpleNamespace(
        transport=SimpleNamespace(dispatch=lambda *_args, **_kwargs: {})
    )
    monkeypatch.setattr(
        resources, "count_item", lambda _client, item: counts.get(item, 0)
    )
    monkeypatch.setattr(resources, "manage_inventory", lambda _client: True)
    monkeypatch.setattr(resources, "gather_wood", lambda *_args, **_kwargs: True)

    def craft(_client, item_id, count):
        counts[item_id] = counts.get(item_id, 0) + count
        return True

    monkeypatch.setattr(resources, "craft", craft)

    assert resources._prepare_safe_furnace_fuel(client, 15) == (
        "minecraft:jungle_planks"
    )


def test_smelter_prepares_fuel_before_locating_furnace(monkeypatch):
    from baritone_client.common import base, harness_ops

    events = []
    smelted = {"done": False}

    class SmeltTransport:
        def dispatch(self, route, _payload):
            if route == "get_state":
                return {"block_position": {"x": 0, "y": 0, "z": 0}}
            if route == "get_block":
                return {"id": "minecraft:furnace"}
            return {}

    client = SimpleNamespace(transport=SmeltTransport())

    def item_count(_client, item_id):
        if item_id == "minecraft:iron_ingot":
            return 37 if smelted["done"] else 5
        if item_id == "minecraft:raw_iron":
            return 32
        if item_id == "minecraft:furnace":
            return 1
        return 0

    monkeypatch.setattr(resources, "count_item", item_count)
    monkeypatch.setattr(
        resources, "collect_finished_furnace_output", lambda *_a, **_k: False
    )
    monkeypatch.setattr(
        resources,
        "_prepare_safe_furnace_fuel",
        lambda *_args: events.append("fuel") or "minecraft:birch_planks",
    )
    monkeypatch.setattr(
        resources,
        "find_nearby_block",
        lambda *_args, **_kwargs: events.append("find") or (1, 0, 0),
    )
    monkeypatch.setattr(
        resources,
        "goto",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not goto the furnace block")
        ),
    )
    monkeypatch.setattr(
        base,
        "place_furnace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("existing furnace should be reused")
        ),
    )
    monkeypatch.setattr(harness_ops, "available", lambda: True)

    def smelt(*_args, **_kwargs):
        events.append("smelt")
        smelted["done"] = True
        return True

    monkeypatch.setattr(harness_ops, "smelt_in_furnace", smelt)

    assert resources._smelt_with_furnace(client, "minecraft:iron_ingot", 37)
    assert events == ["fuel", "find", "smelt"]


def test_smelter_builds_missing_furnace_through_table_aware_craft(monkeypatch):
    client = SimpleNamespace(
        transport=SimpleNamespace(dispatch=lambda *_args, **_kwargs: {})
    )
    crafted = []
    monkeypatch.setattr(
        resources,
        "count_item",
        lambda _client, item_id: {
            "minecraft:raw_iron": 4,
            "minecraft:cobblestone": 8,
        }.get(item_id, 0),
    )
    monkeypatch.setattr(
        resources, "collect_finished_furnace_output", lambda *_a, **_k: False
    )
    monkeypatch.setattr(
        resources, "_prepare_safe_furnace_fuel", lambda *_a: "minecraft:oak_planks"
    )
    monkeypatch.setattr(resources, "find_nearby_block", lambda *_a, **_k: None)
    monkeypatch.setattr(
        resources,
        "craft",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("missing furnace must use the table-aware craft route")
        ),
    )
    monkeypatch.setattr(
        resources,
        "_craft_with_table",
        lambda _client, item_id, qty: crafted.append((item_id, qty)) or False,
    )

    assert not resources._smelt_with_furnace(
        client, "minecraft:iron_ingot", 4
    )
    assert crafted == [("minecraft:furnace", 1)]


def test_smelter_fallback_collects_completed_output_before_requiring_input(monkeypatch):
    from baritone_client.common import base, harness_ops

    counts = {
        "minecraft:iron_ingot": 1,
        "minecraft:raw_iron": 7,
    }

    class SmeltTransport:
        def dispatch(self, route, payload):
            if route == "get_state":
                return {"block_position": {"x": 0, "y": 64, "z": 0}}
            if route == "get_block":
                return {"id": "minecraft:furnace"}
            if route == "get_screen":
                return {
                    "slots": [
                        {"slot": 1, "id": "minecraft:oak_planks", "count": 19},
                        {"slot": 2, "id": "minecraft:iron_ingot", "count": 7},
                    ]
                }
            if route == "inventory_click":
                assert payload == {"slot": 2, "type": "QUICK_MOVE", "button": 0}
                counts["minecraft:iron_ingot"] += 7
                return {}
            return {}

    client = SimpleNamespace(transport=SmeltTransport())
    monkeypatch.setattr(
        resources, "count_item", lambda _client, item_id: counts.get(item_id, 0)
    )
    recovery_calls = []

    def recover_finished(_client, item_id, qty, furnace_pos):
        recovery_calls.append((item_id, qty, furnace_pos))
        if len(recovery_calls) == 1:
            return False
        counts["minecraft:iron_ingot"] += 7
        return True

    monkeypatch.setattr(
        resources, "collect_finished_furnace_output", recover_finished
    )
    monkeypatch.setattr(
        resources, "_prepare_safe_furnace_fuel", lambda *_a: "minecraft:oak_planks"
    )
    monkeypatch.setattr(resources, "find_nearby_block", lambda *_a, **_k: (1, 64, 0))
    monkeypatch.setattr(harness_ops, "available", lambda: True)
    monkeypatch.setattr(harness_ops, "smelt_in_furnace", lambda *_a, **_k: False)
    monkeypatch.setattr(base, "open_furnace", lambda _client: True)

    assert resources._smelt_with_furnace(client, "minecraft:iron_ingot", 4)
    assert counts["minecraft:iron_ingot"] == 8
    assert recovery_calls == [
        ("minecraft:iron_ingot", 4, None),
        ("minecraft:iron_ingot", 4, (1, 64, 0)),
    ]


def test_smelter_collects_finished_output_before_new_gathering_or_fuel(monkeypatch):
    counts = {
        "minecraft:iron_ingot": 1,
        "minecraft:raw_iron": 20,
    }
    client = SimpleNamespace()
    monkeypatch.setattr(
        resources, "count_item", lambda _client, item_id: counts.get(item_id, 0)
    )

    def collect(_client, item_id, qty, furnace_pos):
        assert (item_id, qty, furnace_pos) == (
            "minecraft:iron_ingot",
            4,
            None,
        )
        counts["minecraft:iron_ingot"] += 7
        return True

    monkeypatch.setattr(resources, "collect_finished_furnace_output", collect)
    monkeypatch.setattr(
        resources,
        "gather_ores",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("finished output must be collected before new ore")
        ),
    )
    monkeypatch.setattr(
        resources,
        "_prepare_safe_furnace_fuel",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("finished output must be collected before new fuel")
        ),
    )

    assert resources._smelt_with_furnace(client, "minecraft:iron_ingot", 4)
    assert counts["minecraft:iron_ingot"] == 8


def test_smelter_recounts_output_collected_at_recovery_timeout(monkeypatch):
    counts = {
        "minecraft:iron_ingot": 1,
        "minecraft:raw_iron": 20,
    }
    client = SimpleNamespace()
    monkeypatch.setattr(
        resources, "count_item", lambda _client, item_id: counts.get(item_id, 0)
    )

    def collect_then_time_out(*_args, **_kwargs):
        counts["minecraft:iron_ingot"] = 6
        return False

    monkeypatch.setattr(
        resources, "collect_finished_furnace_output", collect_then_time_out
    )
    monkeypatch.setattr(
        resources,
        "gather_ores",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("recovered output already satisfies the target")
        ),
    )
    monkeypatch.setattr(
        resources,
        "_prepare_safe_furnace_fuel",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("recovered output already satisfies the target")
        ),
    )

    assert resources._smelt_with_furnace(client, "minecraft:iron_ingot", 4)


def test_finished_furnace_recovery_skips_empty_nearest_candidate(monkeypatch):
    from baritone_client.common import furnace_recovery, harness_ops

    class RecoveryTransport:
        def dispatch(self, route, _payload):
            if route == "find_blocks":
                return {
                    "found": [
                        {"x": 1, "y": 64, "z": 0, "distance": 1.0},
                        {"x": 2, "y": 64, "z": 0, "distance": 2.0},
                    ]
                }
            if route == "get_state":
                return {"block_position": {"x": 0, "y": 64, "z": 0}}
            return {}

    attempted = []
    monkeypatch.setattr(furnace_recovery, "count_item", lambda *_a: 0)
    monkeypatch.setattr(harness_ops, "available", lambda: True)
    monkeypatch.setattr(
        furnace_recovery,
        "resume_active_furnace",
        lambda _client, pos, *_a, **_k: attempted.append(tuple(pos))
        or tuple(pos) == (2, 64, 0),
    )

    assert furnace_recovery.collect_finished_furnace_output(
        SimpleNamespace(transport=RecoveryTransport()),
        "minecraft:iron_ingot",
        4,
    )
    assert attempted == [(1, 64, 0), (2, 64, 0)]


def test_outdoor_gatherer_waits_at_night_boundary(monkeypatch):
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport)
    waited = []
    monkeypatch.setattr(
        "baritone_client.common.base.wait_for_safe_daylight",
        lambda _client: waited.append(True) or True,
    )

    assert resources._ensure_outdoor_daylight(client, {"world_time": 13000})
    assert waited == [True]
    assert ("cancel", {}) in transport.calls


def test_outdoor_gatherer_does_not_pause_during_day(monkeypatch):
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(
        "baritone_client.common.base.wait_for_safe_daylight",
        lambda _client: (_ for _ in ()).throw(AssertionError("daylight must not wait")),
    )

    assert resources._ensure_outdoor_daylight(client, {"world_time": 9000})
    assert not transport.calls


def test_wood_gather_checks_night_before_starting_mine_process(monkeypatch):
    class NightTransport(RecordingTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {"world_time": 18000}
            return {}

    transport = NightTransport()
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(resources, "count_item", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(resources, "_ensure_outdoor_daylight", lambda *_args: False)

    assert not resources.gather_wood(client, count=6)
    assert not any(route == "mine" for route, _payload in transport.calls)


def test_bounded_wood_gather_stops_at_expedition_radius(monkeypatch):
    positions = iter((0, 200))

    class RadiusTransport(RecordingTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_inventory":
                return {"inventory": [], "armor": [], "offhand": []}
            if route == "get_state":
                return {
                    "world_time": 1000,
                    "health": 20,
                    "food_level": 20,
                    "block_position": {
                        "x": next(positions, 200),
                        "y": 64,
                        "z": 0,
                    },
                }
            return {}

    transport = RadiusTransport()
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(resources, "count_item", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(resources, "_ensure_outdoor_daylight", lambda *_args: True)
    monkeypatch.setattr(resources, "_start_mine_process", lambda *_args: None)

    assert not resources.gather_wood(
        client,
        count=3,
        max_distance_from_origin=160.0,
    )
    assert ("cancel", {}) in transport.calls


def test_wood_gather_aborts_after_aquatic_safety_intervention(monkeypatch):
    from baritone_client.common import combat

    class SwampTransport(RecordingTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_inventory":
                return {"inventory": [], "armor": [], "offhand": []}
            if route == "get_state":
                return {
                    "world_time": 1000,
                    "health": 13.0,
                    "food_level": 15,
                    "is_pathing": True,
                    "block_position": {"x": 338, "y": 60, "z": -389},
                }
            return {}

    transport = SwampTransport()
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(resources, "count_item", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(resources, "_ensure_outdoor_daylight", lambda *_args: True)
    monkeypatch.setattr(resources, "_start_mine_process", lambda *_args: None)
    interventions = []
    monkeypatch.setattr(
        combat,
        "survival_tick",
        lambda _client, _state: interventions.append(True) or True,
    )
    monkeypatch.setattr(
        resources, "_relocate_to_dry_stone_terrain", lambda _client: False
    )

    assert not resources.gather_wood(client, count=3, timeout=1)
    assert interventions == [True]
    assert ("cancel", {}) in transport.calls


def test_wood_gather_does_not_resume_mining_after_defense_surfaces_bot(monkeypatch):
    from baritone_client.common import combat

    class DefenseTransport(RecordingTransport):
        submerged = False

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {
                    "world_time": 1000,
                    "health": 20,
                    "food_level": 20,
                    "is_pathing": True,
                    "submerged": self.submerged,
                    "block_position": {"x": 127, "y": 62, "z": -318},
                }
            return {}

    transport = DefenseTransport()
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(resources, "count_item", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(resources, "_ensure_outdoor_daylight", lambda *_args: True)
    monkeypatch.setattr(resources, "_start_mine_process", lambda *_args: None)
    monkeypatch.setattr(resources, "_reserve_gathering_inventory", lambda *_args: True)
    monkeypatch.setattr(resources, "free_inventory_slots", lambda *_args: 32)
    monkeypatch.setattr(resources.time, "sleep", lambda _seconds: None)

    def fake_defend(_client):
        client._last_defense_intervention = "aquatic"
        return True

    monkeypatch.setattr(combat, "defend_or_flee", fake_defend)
    monkeypatch.setattr(
        combat,
        "survival_tick",
        lambda _client, state: bool(state.get("submerged")),
    )
    monkeypatch.setattr(
        resources, "_relocate_to_dry_stone_terrain", lambda _client: False
    )

    assert not resources.gather_wood(client, count=3, timeout=1)
    mine_calls = [call for call in transport.calls if call[0] == "mine"]
    assert mine_calls == []
    assert ("cancel", {}) in transport.calls


def test_wood_gather_relocates_before_resuming_after_aquatic_stop(monkeypatch):
    from baritone_client.common import combat

    class SwampTransport(RecordingTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {
                    "world_time": 1000,
                    "health": 20.0,
                    "food_level": 20,
                    "is_pathing": True,
                    "block_position": {"x": -110, "y": 57, "z": -327},
                }
            return {}

    transport = SwampTransport()
    client = SimpleNamespace(transport=transport)
    inventory = {"minecraft:oak_log": 0}
    starts = []
    relocations = []
    interventions = []

    monkeypatch.setattr(
        resources,
        "count_item",
        lambda _client, item_id: inventory.get(item_id, 0),
    )
    monkeypatch.setattr(resources, "_reserve_gathering_inventory", lambda _c: True)
    monkeypatch.setattr(resources, "free_inventory_slots", lambda _c: 32)
    monkeypatch.setattr(resources, "_ensure_outdoor_daylight", lambda *_args: True)
    monkeypatch.setattr(
        resources,
        "_start_mine_process",
        lambda *_args: starts.append(True),
    )

    def aquatic_stop(_client, _state):
        interventions.append(True)
        return len(interventions) == 1

    def relocate(_client):
        relocations.append(True)
        inventory["minecraft:oak_log"] = 3
        return True

    monkeypatch.setattr(combat, "survival_tick", aquatic_stop)
    monkeypatch.setattr(resources, "_relocate_to_dry_stone_terrain", relocate)

    assert resources.gather_wood(client, count=3, timeout=30)
    assert relocations == [True]
    assert len(starts) == 2


def test_wood_gather_resumes_from_verified_dry_shore_without_relocation(monkeypatch):
    from baritone_client.common import combat

    class SurfacedTransport(RecordingTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {
                    "world_time": 1000,
                    "health": 20.0,
                    "food_level": 20,
                    "is_pathing": True,
                    "block_position": {"x": -437, "y": 9, "z": 14},
                }
            if route == "get_block":
                block_y = int(payload["y"])
                return {
                    "id": "minecraft:stone" if block_y == 8 else "minecraft:air"
                }
            return {}

    transport = SurfacedTransport()
    client = SimpleNamespace(transport=transport)
    inventory = {"minecraft:oak_log": 0}
    starts = []
    interventions = []

    monkeypatch.setattr(
        resources,
        "count_item",
        lambda _client, item_id: inventory.get(item_id, 0),
    )
    monkeypatch.setattr(resources, "_reserve_gathering_inventory", lambda _c: True)
    monkeypatch.setattr(resources, "free_inventory_slots", lambda _c: 32)
    monkeypatch.setattr(resources, "_ensure_outdoor_daylight", lambda *_args: True)

    def start_mining(*_args):
        starts.append(True)
        if len(starts) == 2:
            inventory["minecraft:oak_log"] = 3

    def aquatic_stop(_client, _state):
        interventions.append(True)
        return len(interventions) == 1

    monkeypatch.setattr(resources, "_start_mine_process", start_mining)
    monkeypatch.setattr(combat, "survival_tick", aquatic_stop)
    monkeypatch.setattr(combat, "defend_or_flee", lambda _client: False)
    monkeypatch.setattr(
        resources,
        "_relocate_to_dry_stone_terrain",
        lambda _client: (_ for _ in ()).throw(
            AssertionError("already-dry shore must not force horizontal relocation")
        ),
    )

    assert resources.gather_wood(client, count=3, timeout=30)
    assert len(starts) == 2


def test_aquatic_wood_recovery_completes_surface_escape_before_relocation(
    monkeypatch,
):
    from baritone_client.common import surface_recovery, wood_gathering

    class FloodedTransport(RecordingTransport):
        surfaced = False

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_block":
                block_y = int(payload["y"])
                if block_y == 8:
                    return {"id": "minecraft:stone"}
                return {
                    "id": "minecraft:air"
                    if self.surfaced
                    else "minecraft:water"
                }
            return {}

    transport = FloodedTransport()
    client = SimpleNamespace(transport=transport)
    state = {"block_position": {"x": -437, "y": 9, "z": 14}, "health": 20}
    surface_calls = []
    starts = []
    resets = []

    def complete_surface(
        _client, *, timeout, ensure_alive, require_stable_support
    ):
        surface_calls.append((timeout, ensure_alive, require_stable_support))
        transport.surfaced = True
        return True

    monkeypatch.setattr(
        resources,
        "_read_state_optional",
        lambda *_args, **_kwargs: state,
    )
    monkeypatch.setattr(
        resources,
        "_start_mine_process",
        lambda *_args: starts.append(True),
    )
    monkeypatch.setattr(
        resources,
        "_relocate_to_dry_stone_terrain",
        lambda _client: (_ for _ in ()).throw(
            AssertionError("surface completion should precede relocation")
        ),
    )
    monkeypatch.setattr(surface_recovery, "reach_breathing_air", complete_surface)

    watchdog = SimpleNamespace(reset=lambda value: resets.append(value))
    assert wood_gathering.recover_after_aquatic_stop(
        client,
        wood_gathering.AQUATIC_STOP,
        quantity=5,
        movement_watchdogs=(watchdog,),
    )
    assert surface_calls and surface_calls[0][0] == 45.0
    assert surface_calls[0][2] is True
    assert starts == [True]
    assert resets == [state]


def test_missing_mining_pickaxe_is_replaced(monkeypatch):
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(resources, "count_item", lambda *_args, **_kwargs: 0)
    replacements = []
    monkeypatch.setattr(
        resources,
        "ensure_supplies",
        lambda _client, requirements: replacements.append(requirements)
        or SimpleNamespace(success=True),
    )

    assert resources._ensure_mining_pickaxe(client)
    assert replacements == [{"minecraft:wooden_pickaxe": 1}]


def test_missing_mining_pickaxe_waits_for_daylight_before_replacement(monkeypatch):
    class NightTransport(RecordingTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {"world_time": 13000}
            return {}

    transport = NightTransport()
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(resources, "count_item", lambda *_args, **_kwargs: 0)
    waited = []
    monkeypatch.setattr(
        "baritone_client.common.base.wait_for_safe_daylight",
        lambda _client: waited.append(True) or True,
    )
    replacements = []
    monkeypatch.setattr(
        resources,
        "ensure_supplies",
        lambda _client, requirements: replacements.append(requirements)
        or SimpleNamespace(success=True),
    )

    assert resources._ensure_mining_pickaxe(client)
    assert waited == [True]
    assert replacements == [{"minecraft:wooden_pickaxe": 1}]
    assert ("cancel", {}) in transport.calls


def test_missing_mining_pickaxe_does_not_craft_when_night_wait_fails(monkeypatch):
    class NightTransport(RecordingTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {"world_time": 18000}
            return {}

    client = SimpleNamespace(transport=NightTransport())
    monkeypatch.setattr(resources, "count_item", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(
        "baritone_client.common.base.wait_for_safe_daylight",
        lambda _client: False,
    )
    monkeypatch.setattr(
        resources,
        "ensure_supplies",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("replacement must not start without daylight")
        ),
    )

    assert not resources._ensure_mining_pickaxe(client)


def test_missing_mining_pickaxe_prefers_stone_when_cobble_is_available(monkeypatch):
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport)

    def inventory_count(_client, item_id):
        return 3 if item_id == "minecraft:cobblestone" else 0

    monkeypatch.setattr(resources, "count_item", inventory_count)
    replacements = []
    monkeypatch.setattr(
        resources,
        "ensure_supplies",
        lambda _client, requirements: replacements.append(requirements)
        or SimpleNamespace(success=True),
    )

    assert resources._ensure_mining_pickaxe(client)
    assert replacements == [{"minecraft:stone_pickaxe": 1}]


def test_resume_active_furnace_collects_interrupted_batch(monkeypatch):
    class FurnaceTransport:
        def __init__(self):
            self.screen_reads = 0
            self.clicks = []
            self.collected = False

        def dispatch(self, route, payload):
            if route == "get_screen":
                self.screen_reads += 1
                if self.screen_reads == 1:
                    contents = (
                        {"slot": 0, "id": "minecraft:raw_iron", "count": 1},
                        {"slot": 1, "id": "minecraft:birch_planks", "count": 1},
                        {"slot": 2, "id": "minecraft:air", "count": 0},
                    )
                elif self.screen_reads == 2:
                    contents = (
                        {"slot": 0, "id": "minecraft:air", "count": 0},
                        {"slot": 1, "id": "minecraft:air", "count": 0},
                        {"slot": 2, "id": "minecraft:iron_ingot", "count": 1},
                    )
                else:
                    contents = tuple(
                        {"slot": slot, "id": "minecraft:air", "count": 0}
                        for slot in range(3)
                    )
                return {
                    "sync_id": 9,
                    "type": "FurnaceMenu",
                    "total_slots": 39,
                    "slots": list(contents),
                }
            if route == "inventory_click":
                self.clicks.append(payload)
                if payload.get("slot") == 2:
                    self.collected = True
            if route == "get_block":
                return {"id": "minecraft:furnace", "state": {"lit": "true"}}
            if route == "get_inventory":
                items = (
                    [{"id": "minecraft:iron_ingot", "count": 1}]
                    if self.collected else []
                )
                return {"inventory": items, "armor": [], "offhand": []}
            return {}

    transport = FurnaceTransport()
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr("baritone_client.common.harness_ops.available", lambda: True)
    monkeypatch.setattr(
        "baritone_client.common.harness_ops.open_container",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(resources.time, "sleep", lambda _seconds: None)

    assert resources.resume_active_furnace(
        client,
        (-7, 79, -121),
        "minecraft:raw_iron",
        "minecraft:iron_ingot",
    )
    assert transport.clicks == [
        {
            "slot": 2,
            "type": "QUICK_MOVE",
            "button": 0,
            "sync_id": 9,
        }
    ]


def test_resume_active_furnace_loads_carried_input_into_existing_fuel(monkeypatch):
    from baritone_client.common import furnace_recovery, harness_ops

    class FurnaceTransport:
        def __init__(self):
            self.loaded = False
            self.collected = False
            self.clicks = []

        def dispatch(self, route, payload):
            if route == "get_screen":
                if not self.loaded:
                    slots = [
                        {"slot": 0, "id": "minecraft:air", "count": 0},
                        {"slot": 1, "id": "minecraft:oak_planks", "count": 19},
                        {"slot": 2, "id": "minecraft:air", "count": 0},
                        {"slot": 3, "id": "minecraft:raw_iron", "count": 20},
                    ]
                else:
                    slots = [
                        {"slot": 0, "id": "minecraft:raw_iron", "count": 20},
                        {"slot": 1, "id": "minecraft:oak_planks", "count": 19},
                        {"slot": 2, "id": "minecraft:iron_ingot", "count": 1},
                    ]
                return {"sync_id": 12, "type": "FurnaceMenu", "slots": slots}
            if route == "inventory_click":
                self.clicks.append(payload)
                if payload["slot"] == 3:
                    self.loaded = True
                elif payload["slot"] == 2:
                    self.collected = True
            if route == "get_block":
                return {"id": "minecraft:furnace", "state": {"lit": "true"}}
            return {}

    transport = FurnaceTransport()
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(harness_ops, "available", lambda: True)
    monkeypatch.setattr(harness_ops, "open_container", lambda *_a, **_k: True)
    monkeypatch.setattr(
        furnace_recovery,
        "count_item",
        lambda _client, item_id: (
            1
            if item_id == "minecraft:iron_ingot" and transport.collected
            else 0
        ),
    )
    monkeypatch.setattr(furnace_recovery.time, "sleep", lambda _seconds: None)

    assert furnace_recovery.resume_active_furnace(
        client,
        (-377, 67, -204),
        "minecraft:raw_iron",
        "minecraft:iron_ingot",
        minimum_output=1,
    )
    assert transport.clicks == [
        {"slot": 3, "type": "QUICK_MOVE", "button": 0, "sync_id": 12},
        {"slot": 2, "type": "QUICK_MOVE", "button": 0, "sync_id": 12},
    ]


def test_resume_active_furnace_accepts_delayed_carried_output(monkeypatch):
    """Do not wait for a whole loaded batch after the target reaches inventory."""
    from baritone_client.common import furnace_recovery, harness_ops

    class FurnaceTransport:
        def __init__(self):
            self.closed = False

        def dispatch(self, route, payload):
            if route == "get_screen":
                return {
                    "sync_id": 14,
                    "type": "FurnaceMenu",
                    "slots": [
                        {"slot": 0, "id": "minecraft:raw_iron", "count": 64},
                        {"slot": 1, "id": "minecraft:oak_planks", "count": 19},
                        {"slot": 2, "id": "minecraft:air", "count": 0},
                    ],
                }
            if route == "close_screen":
                self.closed = True
            if route == "get_block":
                return {"id": "minecraft:furnace", "state": {"lit": "true"}}
            return {}

    transport = FurnaceTransport()
    client = SimpleNamespace(transport=transport)
    # One read is the pre-open readiness check, one establishes the starting
    # carried count, and the next observes the delayed output.
    inventory_reads = iter((0, 0, 3))
    monkeypatch.setattr(harness_ops, "available", lambda: True)
    monkeypatch.setattr(harness_ops, "open_container", lambda *_a, **_k: True)
    monkeypatch.setattr(
        furnace_recovery,
        "count_item",
        lambda _client, _item_id: next(inventory_reads),
    )

    assert furnace_recovery.resume_active_furnace(
        client,
        (-411, 79, -14),
        "minecraft:raw_iron",
        "minecraft:iron_ingot",
        minimum_output=3,
    )
    assert transport.closed


def test_resume_active_furnace_clears_incompatible_output(monkeypatch):
    """Old recipe output must not block the loaded target recipe forever."""
    from baritone_client.common import furnace_recovery, harness_ops

    class FurnaceTransport:
        def __init__(self):
            self.obstruction_cleared = False
            self.target_collected = False
            self.clicks = []

        def dispatch(self, route, payload):
            if route == "get_screen":
                output = (
                    {"slot": 2, "id": "minecraft:iron_ingot", "count": 3}
                    if self.obstruction_cleared
                    else {"slot": 2, "id": "minecraft:charcoal", "count": 4}
                )
                return {
                    "sync_id": 51,
                    "type": "FurnaceMenu",
                    "slots": [
                        {"slot": 0, "id": "minecraft:raw_iron", "count": 64},
                        {"slot": 1, "id": "minecraft:oak_planks", "count": 47},
                        output,
                    ],
                }
            if route == "inventory_click":
                self.clicks.append(payload)
                if not self.obstruction_cleared:
                    self.obstruction_cleared = True
                else:
                    self.target_collected = True
            if route == "get_block":
                return {"id": "minecraft:furnace", "state": {"lit": "true"}}
            return {}

    transport = FurnaceTransport()
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(harness_ops, "available", lambda: True)
    monkeypatch.setattr(harness_ops, "open_container", lambda *_a, **_k: True)
    monkeypatch.setattr(
        furnace_recovery,
        "count_item",
        lambda _client, _item_id: 3 if transport.target_collected else 0,
    )
    monkeypatch.setattr(furnace_recovery.time, "sleep", lambda _seconds: None)

    assert furnace_recovery.resume_active_furnace(
        client,
        (-411, 79, -14),
        "minecraft:raw_iron",
        "minecraft:iron_ingot",
        minimum_output=3,
    )
    assert transport.clicks == [
        {"slot": 2, "type": "QUICK_MOVE", "button": 0, "sync_id": 51},
        {"slot": 2, "type": "QUICK_MOVE", "button": 0, "sync_id": 51},
    ]


def test_stone_pickaxe_prep_gathers_missing_cobblestone(monkeypatch):
    client = SimpleNamespace(transport=RecordingTransport())
    counts = {"minecraft:cobblestone": 0, "minecraft:cobbled_deepslate": 0}
    gathered = []

    def fake_gather_stone(_client, count=16, **_kwargs):
        gathered.append(count)
        counts["minecraft:cobblestone"] = count
        return True

    monkeypatch.setattr(resources, "count_item", lambda _c, item: counts.get(item, 0))
    monkeypatch.setattr(resources, "gather_stone", fake_gather_stone)

    assert resources.ensure_stone_material(client, "minecraft:stone_pickaxe", 1)
    assert gathered == [3]


def test_stone_material_prep_is_noop_for_non_stone_recipes(monkeypatch):
    client = SimpleNamespace(transport=RecordingTransport())
    gathered = []
    monkeypatch.setattr(resources, "count_item", lambda _c, _item: 0)
    monkeypatch.setattr(
        resources, "gather_stone", lambda *_a, **_k: gathered.append(True) or True
    )

    assert resources.ensure_stone_material(client, "minecraft:iron_pickaxe", 1)
    assert gathered == []


def test_stone_material_prep_rejects_cobbled_deepslate_as_a_substitute(monkeypatch):
    """No recipe in _STONE_RECIPE_COBBLESTONE_NEEDS accepts cobbled_deepslate
    in place of cobblestone -- confirmed live by the server itself rejecting
    both a stone_pickaxe and a furnace craft with "Missing ingredient
    'minecraft:cobblestone'" while deepslate sat in the inventory. Sitting on
    3 deepslate must not report success for a recipe needing 3 cobblestone,
    and gather_stone must be asked for more than what is already held (held +
    shortfall, not just the shortfall) so it is forced to actually mine
    instead of instantly declaring victory on the existing deepslate stock.
    """
    client = SimpleNamespace(transport=RecordingTransport())
    counts = {"minecraft:cobblestone": 0, "minecraft:cobbled_deepslate": 3}
    gathered = []
    monkeypatch.setattr(resources, "count_item", lambda _c, item: counts.get(item, 0))
    monkeypatch.setattr(
        resources,
        "gather_stone",
        lambda _c, count, **_k: gathered.append(count) or True,
    )

    assert not resources.ensure_stone_material(client, "minecraft:stone_pickaxe", 1)
    assert gathered == [6]


def test_stone_material_prep_fails_closed_when_mining_fails(monkeypatch):
    client = SimpleNamespace(transport=RecordingTransport())
    monkeypatch.setattr(resources, "count_item", lambda _c, _item: 0)
    monkeypatch.setattr(resources, "gather_stone", lambda *_a, **_k: False)

    assert not resources.ensure_stone_material(client, "minecraft:stone_pickaxe", 1)


def test_craft_with_table_stops_when_stone_material_unavailable(monkeypatch):
    # The live FOOD_AND_IRON blocker: stick prep succeeds, cobblestone is zero,
    # and the craft used to be attempted anyway (failing forever). The stone
    # preparation gate must fail closed BEFORE any table interaction.
    client = SimpleNamespace(transport=RecordingTransport())
    monkeypatch.setattr(resources, "count_item", lambda _c, _item: 0)
    monkeypatch.setattr(resources, "ensure_tool_sticks", lambda *_a, **_k: True)
    monkeypatch.setattr(resources, "ensure_stone_material", lambda *_a, **_k: False)
    crafted = []
    monkeypatch.setattr(
        resources, "craft", lambda *_a, **_k: crafted.append(True) or True
    )

    assert not resources._craft_with_table(client, "minecraft:stone_pickaxe", 1)
    assert crafted == []
    assert not any(route == "interact_block" for route, _p in client.transport.calls)


def test_every_manual_grid_recipe_has_an_ensure_supplies_strategy():
    # ensure_supplies skips items without a handler and burns its whole
    # timeout; every manually craftable progression recipe must be reachable
    # (end_game requires {"minecraft:ender_eye": 12} through this exact path).
    from baritone_client.common.inventory import _MANUAL_GRID_RECIPES

    missing = [
        item_id
        for item_id in _MANUAL_GRID_RECIPES
        if item_id not in resources.DEFAULT_REQUIREMENT_STRATEGIES
    ]
    assert missing == []


def test_craft_strategies_treat_their_quantity_as_a_shortfall(monkeypatch):
    """ensure_supplies hands handlers a shortfall, not an absolute target.

    _craft_with_table documents the opposite contract ("craft until the
    absolute carried target qty is satisfied") and early-returns True when
    ``count_item(item) >= qty``. Routing the shortfall straight into it makes
    every already-well-stocked item a silent no-op that still reports success,
    so ensure_supplies re-checks, finds the same shortfall, and spins until it
    times out.

    Confirmed live on A1 2026-08-21: carrying 14 bread and needing 15, the
    shortfall of 1 satisfied ``14 >= 1`` instantly. FOOD_AND_IRON logged
    "Ensuring supplies: {'minecraft:bread': 1}" every ~5.5s for the full 600s
    window, failed "Bake durable prepared food", retried the phase, and
    repeated on an ~11.5 minute cycle for days -- blocking prepared_food_32,
    one of the seven uncleared End-readiness items.
    """
    requested = []

    def fake_craft_with_table(_client, item_id, qty):
        requested.append((item_id, qty))
        return True

    monkeypatch.setattr(resources, "_craft_with_table", fake_craft_with_table)
    monkeypatch.setattr(resources, "count_item", lambda _client, _item: 14)

    strategy = resources.DEFAULT_REQUIREMENT_STRATEGIES["minecraft:bread"]
    strategy(object(), 1)

    assert requested, "the bread strategy must reach the crafting path"
    _item_id, absolute_target = requested[-1]
    assert absolute_target == 15, (
        "a shortfall of 1 on top of 14 carried must become an absolute target "
        f"of 15, not {absolute_target}"
    )


def test_gather_strategies_treat_their_quantity_as_a_shortfall(monkeypatch):
    """The gatherers share _craft_with_table's absolute-target contract.

    gather_wood/gather_stone/gather_ores/gather_gravel each early-return True
    on ``carried >= count``, but ensure_supplies hands them a shortfall. Their
    ``max(qty, N)`` floors do not mask this -- they set a low bar a stocked
    inventory clears trivially, so the gatherer reports success without
    gathering and ensure_supplies spins until it times out.

    Needing 30 cobblestone while carrying 20 gives a shortfall of 10;
    ``max(10, 16)`` is 16, and ``20 >= 16`` returns True having mined nothing.
    """
    carried = {
        "minecraft:cobblestone": 20,
        "minecraft:cobbled_deepslate": 0,
        "minecraft:oak_log": 18,
        "minecraft:diamond": 4,
        "minecraft:flint": 4,
    }
    monkeypatch.setattr(
        resources, "count_item", lambda _client, item: carried.get(item, 0)
    )

    seen = {}
    monkeypatch.setattr(
        resources,
        "gather_stone",
        lambda _client, count=16, **_kw: seen.__setitem__("stone", count) or True,
    )
    monkeypatch.setattr(
        resources,
        "gather_wood",
        lambda _client, count=16, **_kw: seen.__setitem__("wood", count) or True,
    )
    monkeypatch.setattr(
        resources,
        "gather_ores",
        lambda _client, _ore, count=4, **_kw: seen.__setitem__("ore", count) or True,
    )
    monkeypatch.setattr(
        resources,
        "gather_gravel",
        lambda _client, count, **_kw: seen.__setitem__("gravel", count) or True,
    )

    strategies = resources.DEFAULT_REQUIREMENT_STRATEGIES
    strategies["minecraft:cobblestone"](object(), 10)
    strategies["minecraft:oak_log"](object(), 2)
    strategies["minecraft:diamond"](object(), 2)
    strategies["minecraft:flint"](object(), 1)

    assert seen["stone"] >= 30, (
        f"20 carried + shortfall 10 must request at least 30, got {seen['stone']}"
    )
    assert seen["wood"] >= 20, (
        f"18 carried + shortfall 2 must request at least 20, got {seen['wood']}"
    )
    assert seen["ore"] >= 6, (
        f"4 carried + shortfall 2 must request at least 6, got {seen['ore']}"
    )
    # gather_gravel has no max(qty, N) floor at all, so an unconverted
    # shortfall deadlocks it the moment any flint is carried.
    assert seen["gravel"] >= 5, (
        f"4 carried + shortfall 1 must request at least 5, got {seen['gravel']}"
    )


def test_gathering_capacity_guard_requests_three_free_slots(monkeypatch):
    requested = []
    monkeypatch.setattr(resources, "free_inventory_slots", lambda _client: 0)
    monkeypatch.setattr(
        resources,
        "manage_inventory",
        lambda _client, minimum_free_slots: requested.append(minimum_free_slots) or True,
    )

    assert resources._reserve_gathering_inventory(object())
    assert requested == [3]


def test_gathering_capacity_prefers_checkpointed_storage(monkeypatch):
    slots = {"free": 0}
    state = SimpleNamespace(custom_data={})
    client = SimpleNamespace(transport=RecordingTransport(), _automation_state=state)
    monkeypatch.setattr(
        resources, "free_inventory_slots", lambda _client: slots["free"]
    )
    monkeypatch.setattr(
        inventory,
        "resolve_storage_location",
        lambda _client, state=None, verify=True: (1, 65, 1),
    )

    def deposit(_client, _pos, state=None):
        slots["free"] = 5
        return 4

    monkeypatch.setattr(inventory, "deposit_excess_to_chest", deposit)
    monkeypatch.setattr(
        resources,
        "manage_inventory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("durable storage should run before ground disposal")
        ),
    )

    assert resources._reserve_gathering_inventory(client)


def test_gathering_capacity_skips_distant_home_storage(monkeypatch):
    state = SimpleNamespace(custom_data={})
    client = SimpleNamespace(
        transport=RecordingTransport(),
        _automation_state=state,
    )
    monkeypatch.setattr(resources, "free_inventory_slots", lambda _client: 0)
    monkeypatch.setattr(
        inventory,
        "resolve_storage_location",
        lambda *_args, **_kwargs: (500, 65, 500),
    )
    monkeypatch.setattr(
        inventory,
        "deposit_excess_to_chest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("inventory cleanup must not start a distant chest tour")
        ),
    )
    monkeypatch.setattr(resources, "manage_inventory", lambda *_a, **_k: True)

    assert resources._reserve_gathering_inventory(client)


def test_satisfied_stone_target_does_not_discard_inventory(monkeypatch):
    client = SimpleNamespace(transport=RecordingTransport())
    monkeypatch.setattr(
        resources,
        "count_item",
        lambda _client, item_id: 32 if item_id == "minecraft:cobblestone" else 0,
    )
    monkeypatch.setattr(
        resources,
        "_reserve_gathering_inventory",
        lambda _client: (_ for _ in ()).throw(
            AssertionError("already-satisfied gathering must not clean inventory")
        ),
    )

    assert resources.gather_stone(client, count=32)


def test_stone_gathering_does_not_resume_underwater_mine_after_surfacing(
    monkeypatch,
):
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport, _submersion_ticks=1)
    monkeypatch.setattr(resources, "count_item", lambda _c, _i: 0)
    monkeypatch.setattr(resources, "_reserve_gathering_inventory", lambda _c: True)
    monkeypatch.setattr(resources, "_ensure_mining_pickaxe", lambda _c: True)
    monkeypatch.setattr(resources, "free_inventory_slots", lambda _c: 10)

    def surface(_client):
        _client._submersion_ticks = 0
        return True

    monkeypatch.setattr(
        "baritone_client.common.combat.defend_or_flee",
        surface,
    )
    monkeypatch.setattr(
        resources, "_relocate_to_dry_stone_terrain", lambda _client: False
    )

    assert not resources.gather_stone(client, count=9, timeout=30)
    mining = [call for call in transport.calls if call[0] == "mine"]
    assert len(mining) == 1
    assert transport.calls[-1] == ("cancel", {})


def test_stone_gathering_relocates_before_resuming_after_submersion(
    monkeypatch,
):
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport, _submersion_ticks=1)
    inventory = {"minecraft:cobblestone": 31}
    relocations = []

    monkeypatch.setattr(
        resources,
        "count_item",
        lambda _client, item_id: inventory.get(item_id, 0),
    )
    monkeypatch.setattr(resources, "_reserve_gathering_inventory", lambda _c: True)
    monkeypatch.setattr(resources, "_ensure_mining_pickaxe", lambda _c: True)
    monkeypatch.setattr(resources, "free_inventory_slots", lambda _c: 10)

    def surface_once(_client):
        if not relocations:
            _client._submersion_ticks = 0
            return True
        return False

    def relocate(_client):
        relocations.append(True)
        inventory["minecraft:cobblestone"] = 32
        return True

    monkeypatch.setattr(
        "baritone_client.common.combat.defend_or_flee",
        surface_once,
    )
    monkeypatch.setattr(resources, "_relocate_to_dry_stone_terrain", relocate)

    assert resources.gather_stone(client, count=32, timeout=30)
    assert relocations == [True]
    mining = [call for call in transport.calls if call[0] == "mine"]
    assert len(mining) == 2


def test_descend_to_stone_layer_tunnels_down_from_surface(monkeypatch):
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport)
    # Pre-descent read (Y=101), then progress reads that show the tunnel
    # staircasing down until it reaches the stone-mining depth.
    states = iter(
        [
            {"block_position": {"x": 3, "y": 101, "z": -11}},  # initial
            {"block_position": {"x": 3, "y": 92, "z": -11}},   # progress
            {"block_position": {"x": 2, "y": 78, "z": -10}},   # reached depth
        ]
    )
    monkeypatch.setattr(
        resources, "_read_state_optional", lambda *_a, **_k: next(states)
    )
    monkeypatch.setattr(resources, "_ensure_mining_pickaxe", lambda _c: True)
    monkeypatch.setattr(resources.time, "sleep", lambda _s: None)

    assert resources._descend_to_stone_layer(client)
    # A tunnel is issued toward target_y = max(50, 101-25) = 76 at the same X/Z.
    tunnels = [p for r, p in transport.calls if r == "tunnel"]
    assert tunnels == [{"x": 3, "y": 76, "z": -11, "radius": 2}]
    # The broad mine is cancelled before the descent tunnel is issued.
    assert ("cancel", {}) in transport.calls


def test_descend_to_stone_layer_is_noop_when_already_deep(monkeypatch):
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(
        resources,
        "_read_state_optional",
        lambda *_a, **_k: {"block_position": {"x": 0, "y": 52, "z": 0}},
    )

    # Already within a couple blocks of the floor -- descending exposes nothing new.
    assert resources._descend_to_stone_layer(client) is False
    assert [r for r, _ in transport.calls if r == "tunnel"] == []


def test_descend_to_stone_layer_lowers_egress_altitude_after_proven_stall(
    monkeypatch,
):
    client = SimpleNamespace(transport=RecordingTransport())
    monkeypatch.setattr(
        resources,
        "_read_state_optional",
        lambda *_args, **_kwargs: {
            "block_position": {"x": 8, "y": 52, "z": -192}
        },
    )
    egress_calls = []
    monkeypatch.setattr(
        stone_descent,
        "try_lower_surface_egress",
        lambda _client, _state, **kwargs: egress_calls.append(kwargs) or None,
    )

    assert not resources._descend_to_stone_layer(client)
    assert egress_calls == [{"minimum_altitude": 0}]


def test_descend_to_stone_layer_fails_when_no_downward_progress(monkeypatch):
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport)
    # Stays at Y=100 forever -> tunnel never makes progress -> failure.
    monkeypatch.setattr(
        resources,
        "_read_state_optional",
        lambda *_a, **_k: {"block_position": {"x": 0, "y": 100, "z": 0}},
    )
    monkeypatch.setattr(resources, "_ensure_mining_pickaxe", lambda _c: True)
    monkeypatch.setattr(resources.time, "sleep", lambda _s: None)
    # Force the timeout to expire immediately after the first poll.
    clock = iter([0, 0, 999])
    monkeypatch.setattr(resources.time, "time", lambda: next(clock))

    assert resources._descend_to_stone_layer(client) is False


def test_stranded_gatherer_descends_before_relocating(monkeypatch):
    """The recovery ladder must try tunnelling down to stone before giving up
    or relocating to the (also-surface) base."""
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport)

    # Never accumulates cobble, so the loop keeps hitting the stall recovery.
    monkeypatch.setattr(resources, "count_item", lambda _c, _i: 0)
    monkeypatch.setattr(resources, "_reserve_gathering_inventory", lambda _c: True)
    monkeypatch.setattr(resources, "_ensure_mining_pickaxe", lambda _c: True)
    monkeypatch.setattr(resources, "free_inventory_slots", lambda _c: 10)
    monkeypatch.setattr(
        resources, "_read_state_optional", lambda *_a, **_k: {"is_pathing": False}
    )
    monkeypatch.setattr(resources, "_find_safe_nearby_stone", lambda _c, **_k: None)
    monkeypatch.setattr(
        "baritone_client.common.combat.defend_or_flee", lambda _c: False
    )
    monkeypatch.setattr(resources.time, "sleep", lambda _s: None)

    order = []
    monkeypatch.setattr(
        resources,
        "_descend_to_stone_layer",
        lambda _c, **_k: order.append("descend") or False,
    )
    monkeypatch.setattr(
        resources,
        "_relocate_to_checkpointed_stone_source",
        lambda _c, **_k: order.append("relocate") or False,
    )
    monkeypatch.setattr(
        resources,
        "_relocate_to_dry_stone_terrain",
        lambda _c, **_k: order.append("dry") or False,
    )

    assert not resources.gather_stone(client, count=9, timeout=5)
    # Descent is attempted, and strictly before relocation.
    assert order[0] == "descend"
    assert order.index("descend") < order.index("relocate")
    assert order.index("relocate") < order.index("dry")


def test_stranded_gatherer_retries_descent_after_dry_relocation(monkeypatch):
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport)
    attempts = []

    monkeypatch.setattr(
        resources,
        "count_item",
        lambda _c, _i: 9 if len(attempts) > 1 else 0,
    )
    monkeypatch.setattr(resources, "_reserve_gathering_inventory", lambda _c: True)
    monkeypatch.setattr(resources, "_ensure_mining_pickaxe", lambda _c: True)
    monkeypatch.setattr(resources, "free_inventory_slots", lambda _c: 10)
    monkeypatch.setattr(
        resources, "_read_state_optional", lambda *_a, **_k: {"is_pathing": False}
    )
    monkeypatch.setattr(resources, "_find_safe_nearby_stone", lambda _c, **_k: None)
    monkeypatch.setattr(
        "baritone_client.common.combat.defend_or_flee", lambda _c: False
    )
    monkeypatch.setattr(resources.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        resources,
        "_descend_to_stone_layer",
        lambda _c, **_k: attempts.append("descend") or len(attempts) > 1,
    )
    monkeypatch.setattr(
        resources, "_relocate_to_checkpointed_stone_source", lambda _c: False
    )
    monkeypatch.setattr(
        resources, "_relocate_to_dry_stone_terrain", lambda _c: True
    )

    assert resources.gather_stone(client, count=9, timeout=30)
    assert attempts == ["descend", "descend"]


def test_dry_stone_relocation_requires_verified_displacement(monkeypatch):
    class Transport:
        def __init__(self):
            self.state_reads = 0

        def dispatch(self, route, payload):
            if route == "get_state":
                self.state_reads += 1
                x = 0 if self.state_reads == 1 else 16
                return {"block_position": {"x": x, "y": 64, "z": 0}}
            if route == "find_blocks":
                return {"found": [{"x": 16, "y": 63, "z": 0}]}
            return {}

    destinations = []
    monkeypatch.setattr(
        "baritone_client.common.escape_recovery.destination_safe",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        resources,
        "goto",
        lambda _client, *position, **_kwargs: destinations.append(position) or True,
    )

    assert stone_descent.relocate_to_dry_stone_terrain(
        SimpleNamespace(transport=Transport())
    )
    assert destinations == [(16, 64, 0)]


def test_dry_stone_relocation_bounds_expensive_safety_probes(monkeypatch):
    class Transport:
        def dispatch(self, route, payload):
            if route == "get_state":
                return {"block_position": {"x": 0, "y": 64, "z": 0}}
            if route == "find_blocks":
                return {
                    "found": [
                        {"x": x, "y": 63, "z": 0}
                        for x in range(12, 200)
                    ]
                }
            return {}

    safety_probes = []
    monkeypatch.setattr(
        "baritone_client.common.escape_recovery.destination_safe",
        lambda _client, *position: safety_probes.append(position) or False,
    )

    assert not stone_descent.relocate_to_dry_stone_terrain(
        SimpleNamespace(transport=Transport()), attempt_limit=3
    )
    assert len(safety_probes) == 12


def test_high_altitude_stone_fallback_returns_to_checkpointed_storage(monkeypatch):
    class Transport:
        def __init__(self):
            self.calls = []

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {"block_position": {"x": -16, "y": 113, "z": -19}}
            return {}

    transport = Transport()
    automation_state = SimpleNamespace(custom_data={})
    client = SimpleNamespace(
        transport=transport,
        _automation_state=automation_state,
    )
    destinations = []
    monkeypatch.setattr(
        inventory,
        "resolve_storage_location",
        lambda *_args, **_kwargs: (-78, 64, 35),
    )
    monkeypatch.setattr(
        resources,
        "goto",
        lambda _client, x, y, z, **kwargs: destinations.append((x, y, z, kwargs))
        or True,
    )

    assert resources._relocate_to_checkpointed_stone_source(client)
    assert destinations == [
        (-78, 64, 35, {"timeout": 240, "tolerance": 3.0})
    ]


def test_choose_descent_offset_picks_first_open_cardinal_direction():
    class ProbeTransport(RecordingTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_block":
                # (+1,0) is solid; (-1,0) is open -> must pick (-1, 0).
                if payload == {"x": 3 + 3, "y": 101, "z": -11}:
                    return {"id": "minecraft:stone"}
                if payload == {"x": 3 - 3, "y": 101, "z": -11}:
                    return {"id": "minecraft:air"}
                return {"id": "minecraft:stone"}
            return {}

    client = SimpleNamespace(transport=ProbeTransport())

    assert resources._choose_descent_offset(client, 3, 101, -11) == (-1, 0)


def test_choose_descent_offset_none_when_fully_enclosed(monkeypatch, advancing_clock):
    from baritone_client.common import stone_descent

    monkeypatch.setattr(stone_descent, "time", advancing_clock())
    client = SimpleNamespace(transport=RecordingTransport())  # get_block -> {}

    assert resources._choose_descent_offset(client, 3, 101, -11) is None


def test_descend_to_stone_layer_aims_laterally_off_a_narrow_pillar(monkeypatch):
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport)
    states = iter(
        [
            {"block_position": {"x": 3, "y": 101, "z": -11}},
            {"block_position": {"x": 3, "y": 92, "z": -11}},
            {"block_position": {"x": -1, "y": 76, "z": -11}},
        ]
    )
    monkeypatch.setattr(
        resources, "_read_state_optional", lambda *_a, **_k: next(states)
    )
    monkeypatch.setattr(resources, "_ensure_mining_pickaxe", lambda _c: True)
    monkeypatch.setattr(resources.time, "sleep", lambda _s: None)
    # Open to the west (-1, 0); everything else solid.
    monkeypatch.setattr(
        resources,
        "_choose_descent_offset",
        lambda _c, _px, _py, _pz, **_k: (-1, 0),
    )

    assert resources._descend_to_stone_layer(client)
    tunnels = [p for r, p in transport.calls if r == "tunnel"]
    # target_x = 3 + (-1)*8 = -5, same target_y=76, same z.
    assert tunnels == [{"x": -5, "y": 76, "z": -11, "radius": 2}]


def test_manual_column_descend_breaks_one_block_at_a_time_when_supported(monkeypatch):
    """A genuinely solid column (support always present) descends step by
    step using the ``dig_block`` command (bridge 1.0.27+), NOT ``break_block``
    (Baritone builder process, same underfoot refusal) nor ``attack_block``
    (single swing, no progress) -- both verified live to break nothing."""
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport)
    positions = iter([101, 100, 99, 98])  # settles one lower after each break

    # The bridge's dig_block breaks the block over the following ticks; model
    # that as "air after the dig_block for that cell has been issued".
    dug = set()

    def dispatch(route, payload):
        transport.calls.append((route, payload))
        if route == "get_block":
            key = (payload["x"], payload["y"], payload["z"])
            if key in dug:
                return {"id": "minecraft:air"}
            return {"id": "minecraft:cobblestone" if payload["y"] <= 100 else "minecraft:air"}
        if route == "get_state":
            return {"block_position": {"x": 3, "y": next(positions), "z": -11}}
        if route == "dig_block":
            dug.add((payload["x"], payload["y"], payload["z"]))
            return {"started": True}
        return {}

    transport.dispatch = dispatch
    monkeypatch.setattr(resources, "select_item", lambda *_a, **_k: True)
    monkeypatch.setattr(resources, "_ensure_mining_pickaxe", lambda _c: True)
    monkeypatch.setattr(resources.time, "sleep", lambda _s: None)

    assert resources._manual_column_descend(client, target_y=98, max_steps=5)
    assert any(route == "dig_block" for route, _ in transport.calls)
    assert not any(route == "break_block" for route, _ in transport.calls)
    assert not any(route == "attack_block" for route, _ in transport.calls)


def test_manual_escape_descent_can_break_safe_floor_without_pickaxe(monkeypatch):
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport)
    positions = iter([64, 63])
    dug = set()

    def dispatch(route, payload):
        transport.calls.append((route, payload))
        if route == "get_block":
            key = (payload["x"], payload["y"], payload["z"])
            if key in dug:
                return {"id": "minecraft:air"}
            return {"id": "minecraft:stone"}
        if route == "get_state":
            return {
                "block_position": {"x": 7, "y": next(positions), "z": -191}
            }
        if route == "dig_block":
            dug.add((payload["x"], payload["y"], payload["z"]))
            return {"started": True}
        return {}

    transport.dispatch = dispatch
    monkeypatch.setattr(
        resources,
        "_ensure_mining_pickaxe",
        lambda _client: (_ for _ in ()).throw(
            AssertionError("escape descent must not require a pickaxe")
        ),
    )
    monkeypatch.setattr(
        resources,
        "select_item",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("escape descent must not select a pickaxe")
        ),
    )
    monkeypatch.setattr(stone_descent.time, "sleep", lambda _seconds: None)

    assert stone_descent.manual_column_descend(
        client,
        target_y=63,
        max_steps=1,
        require_pickaxe=False,
    )
    assert any(route == "dig_block" for route, _payload in transport.calls)


def test_breathable_source_water_pocket_is_sealed_then_bucketed(monkeypatch):
    """A1's shared descent recovery drains its feet without flooding again."""
    blocks = {
        (0, 4, 0): {"id": "minecraft:water", "state": {"level": "0"}},
        (0, 5, 0): {"id": "minecraft:air", "state": {}},
        (0, 4, -1): {"id": "minecraft:water", "state": {"level": "0"}},
        (0, 4, 1): {"id": "minecraft:deepslate", "state": {}},
        (-1, 4, 0): {"id": "minecraft:diorite", "state": {}},
        (1, 4, 0): {"id": "minecraft:deepslate", "state": {}},
    }
    counts = {
        "minecraft:bucket": 1,
        "minecraft:water_bucket": 3,
        "minecraft:cobblestone": 64,
    }

    class Transport:
        def __init__(self):
            self.calls = []

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_block":
                return blocks.get(
                    (payload["x"], payload["y"], payload["z"]),
                    {"id": "minecraft:stone", "state": {}},
                )
            if route == "place_block":
                key = (payload["x"], payload["y"], payload["z"])
                blocks[key] = {"id": payload["block"], "state": {}}
                return {"placed": True}
            if route == "use_item":
                blocks[(0, 4, 0)] = {"id": "minecraft:air", "state": {}}
                counts["minecraft:bucket"] = 0
                counts["minecraft:water_bucket"] = 4
                return {"used": True}
            return {}

    transport = Transport()
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(
        inventory,
        "count_item",
        lambda _client, item_id: counts.get(item_id, 0),
    )
    monkeypatch.setattr(inventory, "select_item", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(resources, "equip_best_pickaxe", lambda _client: True)
    monkeypatch.setattr(stone_descent.time, "sleep", lambda _seconds: None)

    result = stone_descent.drain_breathable_water_pocket(
        client,
        {"block_position": {"x": 0, "y": 4, "z": 0}},
    )

    assert result is True
    assert blocks[(0, 4, -1)]["id"] == "minecraft:cobblestone"
    assert blocks[(0, 4, 0)]["id"] == "minecraft:air"
    assert any(route == "use_item" for route, _payload in transport.calls)


def test_same_level_sidestep_requires_horizontal_position_postcondition():
    target = {"x": 11, "y": 4, "z": -7}

    assert not descent_recovery.cleared_step_reached(
        {"x": 10, "y": 4, "z": -7},
        target_x=11,
        target_y=4,
        target_z=-7,
        require_horizontal=True,
    )
    assert not descent_recovery.cleared_step_reached(
        {"y": 4},
        target_x=11,
        target_y=4,
        target_z=-7,
        require_horizontal=True,
    )
    assert descent_recovery.cleared_step_reached(
        target,
        target_x=11,
        target_y=4,
        target_z=-7,
        require_horizontal=True,
    )


def test_water_pocket_sidestep_clears_head_first_and_requires_lateral_move():
    blocks = {
        (1, 3, 0): "minecraft:stone",
        (1, 4, 0): "minecraft:deepslate",
        (1, 5, 0): "minecraft:deepslate",
    }
    breaks = []
    walks = []

    def read_block(x, y, z):
        return blocks.get((x, y, z), "minecraft:water")

    def break_for_step(x, y, z):
        breaks.append((x, y, z))
        blocks[(x, y, z)] = "minecraft:air"
        return True

    def walk_to_step(*args, **kwargs):
        walks.append((args, kwargs))
        return "moved"

    result = descent_recovery.attempt_water_pocket_sidestep(
        state={"block_position": {"x": 0, "y": 4, "z": 0}},
        directions=[(1, 0)],
        air_blocks={"minecraft:air"},
        unsafe_blocks={"minecraft:water", "minecraft:lava"},
        read_block=read_block,
        break_for_step=break_for_step,
        walk_to_step=walk_to_step,
    )

    assert result == "moved"
    assert breaks == [(1, 5, 0), (1, 4, 0)]
    assert walks[0][1]["require_horizontal"] is True


def test_water_pocket_sidestep_refuses_an_unsafe_floor():
    walked = []

    result = descent_recovery.attempt_water_pocket_sidestep(
        state={"block_position": {"x": 0, "y": 4, "z": 0}},
        directions=[(1, 0)],
        air_blocks={"minecraft:air"},
        unsafe_blocks={"minecraft:water", "minecraft:lava"},
        read_block=lambda _x, y, _z: (
            "minecraft:water" if y == 3 else "minecraft:air"
        ),
        break_for_step=lambda *_args: (_ for _ in ()).throw(
            AssertionError("unsafe sidestep must not break blocks")
        ),
        walk_to_step=lambda *_args, **_kwargs: walked.append(True),
    )

    assert result == "blocked"
    assert walked == []


def test_low_altitude_column_recovery_is_bounded(monkeypatch):
    calls = []
    monkeypatch.setattr(
        stone_descent,
        "manual_column_descend",
        lambda _client, **kwargs: calls.append(kwargs) or True,
    )

    assert descent_recovery.recover_low_altitude_column(
        object(), current_y=8, target_y=-58
    )
    assert calls == [{"target_y": -4, "max_steps": 12}]

    calls.clear()
    assert not descent_recovery.recover_low_altitude_column(
        object(), current_y=9, target_y=-58
    )
    assert calls == []


def test_supported_descent_breaks_inset_mud_at_player_y(monkeypatch):
    class MudTransport(RecordingTransport):
        def __init__(self):
            super().__init__()
            self.player_y = 64
            self.dug = set()

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {
                    "position": {
                        "x": 8.5,
                        "y": float(self.player_y),
                        "z": -191.5,
                    },
                    "block_position": {
                        "x": 8,
                        "y": self.player_y,
                        "z": -192,
                    },
                    "health": 20,
                }
            if route == "get_block":
                key = (payload["x"], payload["y"], payload["z"])
                if key in self.dug:
                    return {"id": "minecraft:air"}
                if payload["y"] in {62, 64}:
                    return {"id": "minecraft:mud"}
                return {"id": "minecraft:air"}
            if route == "dig_block":
                key = (payload["x"], payload["y"], payload["z"])
                self.dug.add(key)
                self.player_y = 63
            return {}

    transport = MudTransport()
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(shelf_escape.time, "sleep", lambda _seconds: None)

    assert shelf_escape.supported_column_descent(
        client,
        transport.dispatch("get_state", {}),
        target_y=63,
        max_steps=1,
        minimum_altitude=0,
    ) == (8, 63, -192)
    broken = [
        payload for route, payload in transport.calls if route == "dig_block"
    ]
    assert broken == [{"x": 8, "y": 64, "z": -192}]


def test_manual_escape_descent_navigates_to_landing_when_water_prevents_drop(
    monkeypatch,
):
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport)
    position = {"x": 7, "y": 64, "z": -191}
    dug = set()

    def dispatch(route, payload):
        transport.calls.append((route, payload))
        if route == "get_block":
            key = (payload["x"], payload["y"], payload["z"])
            if key in dug:
                return {"id": "minecraft:air"}
            if payload["y"] <= 63:
                return {"id": "minecraft:stone"}
            return {"id": "minecraft:air"}
        if route == "get_state":
            return {"block_position": dict(position)}
        if route == "dig_block":
            dug.add((payload["x"], payload["y"], payload["z"]))
            return {"started": True}
        return {}

    transport.dispatch = dispatch
    clock = {"now": 0.0}

    def tick():
        clock["now"] += 1.0
        return clock["now"]

    monkeypatch.setattr(stone_descent.time, "time", tick)
    monkeypatch.setattr(stone_descent.time, "sleep", lambda _seconds: None)
    destinations = []

    def goto_landing(_client, x, y, z, **_kwargs):
        destinations.append((x, y, z))
        position["y"] = y
        return True

    monkeypatch.setattr(stone_descent, "goto", goto_landing, raising=False)

    assert stone_descent.manual_column_descend(
        client,
        target_y=58,
        max_steps=1,
        require_pickaxe=False,
    )
    assert destinations == [(7, 63, -191)]


def test_manual_escape_descent_uses_preopened_safe_shaft(monkeypatch):
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport)
    position = {"x": 7, "y": 64, "z": -191}

    def dispatch(route, payload):
        transport.calls.append((route, payload))
        if route == "get_block":
            if payload["y"] == 63:
                return {"id": "minecraft:air"}
            if payload["y"] == 62:
                return {"id": "minecraft:stone"}
            return {"id": "minecraft:air"}
        if route == "get_state":
            return {"block_position": dict(position)}
        return {}

    transport.dispatch = dispatch
    destinations = []

    def goto_landing(_client, x, y, z, **_kwargs):
        destinations.append((x, y, z))
        position["y"] = y
        return True

    monkeypatch.setattr(stone_descent, "goto", goto_landing, raising=False)

    assert stone_descent.manual_column_descend(
        client,
        target_y=58,
        max_steps=1,
        require_pickaxe=False,
    )
    assert destinations == [(7, 63, -191)]
    assert not any(route == "dig_block" for route, _payload in transport.calls)


def test_manual_escape_descent_retries_transient_block_reads(monkeypatch):
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport)
    position = {"x": 7, "y": 64, "z": -191}
    reads = {}

    def dispatch(route, payload):
        transport.calls.append((route, payload))
        if route == "get_block":
            y = payload["y"]
            reads[y] = reads.get(y, 0) + 1
            if reads[y] <= 3:
                raise TimeoutError("transient block read")
            return {
                "id": "minecraft:mud" if y == 62 else "minecraft:air"
            }
        if route == "get_state":
            return {"block_position": dict(position)}
        return {}

    transport.dispatch = dispatch
    monkeypatch.setattr(stone_descent.time, "sleep", lambda _seconds: None)

    def goto_landing(_client, _x, y, _z, **_kwargs):
        position["y"] = y
        return True

    monkeypatch.setattr(stone_descent, "goto", goto_landing, raising=False)

    assert stone_descent.manual_column_descend(
        client,
        target_y=58,
        max_steps=1,
        require_pickaxe=False,
    )
    assert reads[63] >= 2
    assert reads[62] >= 2


def test_manual_escape_descends_from_inset_mud_support(monkeypatch):
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport)
    positions = iter([64, 63])
    dug = set()

    def dispatch(route, payload):
        transport.calls.append((route, payload))
        if route == "get_block":
            key = (payload["x"], payload["y"], payload["z"])
            if key in dug:
                return {"id": "minecraft:air"}
            if payload["y"] in (64, 62):
                return {"id": "minecraft:mud"}
            return {"id": "minecraft:air"}
        if route == "get_state":
            y = next(positions)
            return {
                "position": {"x": 7.3, "y": y + 0.875, "z": -190.7},
                "block_position": {"x": 7, "y": y, "z": -191},
            }
        if route == "dig_block":
            dug.add((payload["x"], payload["y"], payload["z"]))
            return {"started": True}
        return {}

    transport.dispatch = dispatch
    monkeypatch.setattr(stone_descent.time, "sleep", lambda _seconds: None)

    assert stone_descent.manual_column_descend(
        client,
        target_y=58,
        max_steps=1,
        require_pickaxe=False,
    )
    digs = [payload for route, payload in transport.calls if route == "dig_block"]
    assert digs == [
        {"x": 7, "y": 64, "z": -191, "face": "UP", "max_ticks": 160}
    ]


def test_manual_column_descend_stops_before_an_uncontrolled_fall(monkeypatch):
    """If the block two below is open, breaking would exceed a 1-block drop
    -- must stop rather than risk it."""
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport)

    def dispatch(route, payload):
        transport.calls.append((route, payload))
        if route == "get_state":
            return {"block_position": {"x": 3, "y": 101, "z": -11}}
        if route == "get_block":
            # Y=100 (feet-1) is solid; Y=99 (feet-2, the support) is open air.
            return {"id": "minecraft:cobblestone" if payload["y"] == 100 else "minecraft:air"}
        return {}

    transport.dispatch = dispatch
    monkeypatch.setattr(resources, "_ensure_mining_pickaxe", lambda _c: True)

    assert resources._manual_column_descend(client, target_y=70) is False
    assert not any(route == "break_block" for route, _ in transport.calls)


def test_manual_column_descend_refuses_lava(monkeypatch):
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport)

    def dispatch(route, payload):
        transport.calls.append((route, payload))
        if route == "get_state":
            return {"block_position": {"x": 3, "y": 101, "z": -11}}
        if route == "get_block":
            return {"id": "minecraft:lava"}
        return {}

    transport.dispatch = dispatch
    monkeypatch.setattr(resources, "_ensure_mining_pickaxe", lambda _c: True)

    assert resources._manual_column_descend(client, target_y=70) is False
    assert not any(route == "break_block" for route, _ in transport.calls)


def test_manual_column_descend_allows_a_safe_short_fall_over_a_small_gap(monkeypatch):
    """A pillar sitting over a <=3-block air gap must still descend: the drop
    is damage-free, so refusing it would needlessly strand the bot."""
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport)
    # Feet at 101; Y=100 solid (feet-1), Y=99/98 air gap, Y=97 solid landing
    # (a 3-block fall -> no damage). After the break the player lands at 98.
    positions = iter([101, 98])
    dug = set()

    def dispatch(route, payload):
        transport.calls.append((route, payload))
        if route == "get_block":
            y = payload["y"]
            key = (payload["x"], y, payload["z"])
            if key in dug:
                return {"id": "minecraft:air"}
            if y in (100, 97):
                return {"id": "minecraft:cobblestone"}
            return {"id": "minecraft:air"}  # 99, 98 gap
        if route == "get_state":
            return {"block_position": {"x": 3, "y": next(positions), "z": -11}}
        if route == "dig_block":
            dug.add((payload["x"], payload["y"], payload["z"]))
            return {"started": True}
        return {}

    transport.dispatch = dispatch
    monkeypatch.setattr(resources, "select_item", lambda *_a, **_k: True)
    monkeypatch.setattr(resources, "_ensure_mining_pickaxe", lambda _c: True)
    monkeypatch.setattr(resources.time, "sleep", lambda _s: None)

    assert resources._manual_column_descend(client, target_y=97, max_steps=3)
    assert any(route == "dig_block" for route, _ in transport.calls)


def test_manual_column_descend_refuses_a_long_fall(monkeypatch):
    """A drop with no solid landing within the safe-fall window must stop."""
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport)

    def dispatch(route, payload):
        transport.calls.append((route, payload))
        if route == "get_state":
            return {"block_position": {"x": 3, "y": 101, "z": -11}}
        if route == "get_block":
            # Only feet-1 solid; a deep void below (no landing within 3).
            return {"id": "minecraft:cobblestone" if payload["y"] == 100 else "minecraft:air"}
        return {}

    transport.dispatch = dispatch
    monkeypatch.setattr(resources, "_ensure_mining_pickaxe", lambda _c: True)

    assert resources._manual_column_descend(client, target_y=70) is False
    assert not any(route == "dig_block" for route, _ in transport.calls)


def test_ensure_supplies_raises_on_death_instead_of_looping_forever(monkeypatch):
    """Regression: ensure_supplies had no death/health check at all, so a
    bootstrap loop (e.g. _ensure_mining_pickaxe -> ensure_supplies({"minecraft
    :wooden_pickaxe": 1})) kept retrying a gather_wood/craft chain that
    internally refuses on low health, with nothing in THIS loop ever noticing
    the player was dead. Confirmed live: Bot10 looped "Ensuring supplies" for
    its full 600s while get_state().is_dead was already True."""
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport)

    def dispatch(route, payload):
        transport.calls.append((route, payload))
        if route == "get_state":
            return {"health": 0.0, "food_level": 14, "is_dead": True}
        return {}

    transport.dispatch = dispatch
    monkeypatch.setattr(resources, "count_item", lambda _c, _item: 0)

    with pytest.raises(resources.PlayerDeathDetected):
        resources.ensure_supplies(client, {"minecraft:wooden_pickaxe": 1})
    assert ("cancel", {}) in transport.calls


def test_ensure_supplies_recovers_health_before_gathering(monkeypatch):
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport)

    def dispatch(route, payload):
        transport.calls.append((route, payload))
        if route == "get_state":
            return {"health": 8.0, "food_level": 14, "is_dead": False}
        return {}

    transport.dispatch = dispatch
    # Never satisfied: keeps the loop iterating so we can observe the
    # recovery call without needing to simulate a full successful craft.
    monkeypatch.setattr(resources, "count_item", lambda _c, _item: 0)
    monkeypatch.setattr(resources.time, "sleep", lambda _s: None)
    recovered = []

    def fake_recover_health(*_a, **_k):
        recovered.append(True)
        # False -> ensure_supplies itself raises SurvivalRecoveryRequired,
        # which stops the test loop deterministically via the real code path.
        return False

    monkeypatch.setattr(
        "baritone_client.common.combat.recover_health", fake_recover_health
    )

    with pytest.raises(resources.SurvivalRecoveryRequired):
        resources.ensure_supplies(client, {"minecraft:stick": 1})

    assert recovered == [True]


def test_ensure_supplies_yields_when_health_cannot_recover(monkeypatch):
    transport = RecordingTransport()
    client = SimpleNamespace(transport=transport)

    def dispatch(route, payload):
        transport.calls.append((route, payload))
        if route == "get_state":
            return {"health": 6.0, "food_level": 14, "is_dead": False}
        return {}

    transport.dispatch = dispatch
    monkeypatch.setattr(resources, "count_item", lambda _c, _item: 0)
    monkeypatch.setattr(
        "baritone_client.common.combat.recover_health", lambda *_a, **_k: False
    )

    with pytest.raises(resources.SurvivalRecoveryRequired):
        resources.ensure_supplies(client, {"minecraft:wooden_pickaxe": 1})


def test_wood_gathering_does_not_charge_defense_time_to_its_budget(monkeypatch):
    """A mob camped near the work area triggers evade -> relocate -> walk
    back, which eats far more of the gathering window than a short flee did.
    Live: Bot07/Bot08 spent every 180s window on 4-5 such cycles, hit
    "gather_wood timeout" with 0 logs on every single attempt for a full day,
    and sat at wood=0/64 while showing 20/20 health the whole time. Defence is
    not gathering, so its cost must not be billed to the gathering budget."""
    from baritone_client.common import combat

    clock = {"t": 1000.0}
    monkeypatch.setattr(resources.time, "time", lambda: clock["t"])
    monkeypatch.setattr(resources.time, "sleep", lambda _s: None)

    class SteadyTransport(RecordingTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {
                    "world_time": 1000,
                    "health": 20,
                    "food_level": 20,
                    "is_pathing": True,
                    "block_position": {"x": 0, "y": 64, "z": 0},
                }
            return {}

    transport = SteadyTransport()
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(resources, "count_item", lambda *_a, **_k: 0)
    monkeypatch.setattr(resources, "_ensure_outdoor_daylight", lambda *_a: True)
    monkeypatch.setattr(resources, "_start_mine_process", lambda *_a: None)
    monkeypatch.setattr(resources, "_reserve_gathering_inventory", lambda *_a: True)
    monkeypatch.setattr(resources, "free_inventory_slots", lambda *_a: 32)

    defense_calls = []

    def fake_defend(_client):
        defense_calls.append(True)
        clock["t"] += 40.0  # each evade/relocate cycle burns 40s
        return True

    monkeypatch.setattr(combat, "defend_or_flee", fake_defend)
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_a, **_k: [])

    resources.gather_wood(client, count=6, timeout=100)

    # Billing defence to the budget allows only ~2 cycles before the 100s
    # window closes. Crediting it back must allow meaningfully more, so the
    # bot still gets real working time in a contested area.
    assert len(defense_calls) > 3, (
        f"defence time still charged to the gathering budget "
        f"(only {len(defense_calls)} cycles fit)"
    )
    # ...but the credit is capped, so a permanently contested area terminates.
    assert len(defense_calls) < 12, "budget credit must remain bounded"


def test_marooned_wood_gatherer_attempts_lower_surface_egress(
    monkeypatch, advancing_clock
):
    """Baritone idle + zero displacement means the bot cannot reach anything
    from where it stands, not that trees are scarce. Live: Bot07 sat motionless
    for hours on a single block at y=85 with air on all four sides, while every
    flee, relocate, explore and log-approach silently no-op'd -- ~4000
    cancelled actions at wood=0/64. Re-issuing mine/explore can never fix that;
    it has to get off the island first."""
    from baritone_client.common import resources as res

    class StuckTransport(RecordingTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {
                    "world_time": 1000,
                    "health": 20,
                    "food_level": 20,
                    # Baritone reports it is "pathing" while working flee
                    # goals, so a pinned bot looks busy while going nowhere.
                    "is_pathing": True,
                    "block_position": {"x": -9, "y": 85, "z": -7},  # never moves
                }
            return {}

    transport = StuckTransport()
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(res, "count_item", lambda *_a, **_k: 0)
    monkeypatch.setattr(res, "_ensure_outdoor_daylight", lambda *_a: True)
    monkeypatch.setattr(res, "_start_mine_process", lambda *_a: None)
    monkeypatch.setattr(res, "_reserve_gathering_inventory", lambda *_a: True)
    monkeypatch.setattr(res, "free_inventory_slots", lambda *_a: 32)
    monkeypatch.setattr(res, "_find_blocks_optional", lambda *_a, **_k: {"found": []})
    monkeypatch.setattr(res, "time", advancing_clock())

    egress_calls = []

    def fake_egress(_client, _state, **kwargs):
        egress_calls.append(kwargs)
        return None  # no way off; gather still ends, but it must have TRIED

    monkeypatch.setattr(res, "try_lower_surface_egress", fake_egress)

    res.gather_wood(client, count=6, timeout=40)

    assert egress_calls, "a marooned gatherer must attempt surface egress"
    # The altitude gate must be lowered: being stranded is a property of local
    # terrain, not height. y=85 would be refused by the default 96 floor.
    assert egress_calls[0].get("minimum_altitude") == 0
    # Bounded: exactly one attempt per gather, not once per idle tick.
    assert len(egress_calls) == 1


def test_surface_egress_altitude_gate_is_configurable():
    """The default keeps the original high-shelf behaviour for existing
    callers; only a caller that has proven the bot is marooned lowers it."""
    from baritone_client.common import surface_egress

    low_state = {"block_position": {"x": 0, "y": 85, "z": 0}}
    client = SimpleNamespace(transport=RecordingTransport())

    # Default gate refuses at y=85 without touching the bridge.
    assert surface_egress.try_lower_surface_egress(client, low_state) is None
    assert not client.transport.calls


def test_marooned_egress_fires_even_while_defence_interrupts_every_tick(
    monkeypatch, advancing_clock
):
    """The live failure: Bot07/Bot08 were marooned on the SAME one-block island
    at (-9,85,-7) with a creeper parked beside them. Defence fired on every
    loop iteration and `continue`d -- and reset idle_checks to 0 -- so an
    egress check placed in the idle path was unreachable exactly when needed.
    Escaping the island is what resolves both the stranding and the creeper,
    so the maroon check must run before defence, not after it."""
    from baritone_client.common import resources as res
    from baritone_client.common import combat

    class StuckTransport(RecordingTransport):
        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {
                    "world_time": 1000,
                    "health": 20,
                    "food_level": 20,
                    "is_pathing": True,
                    "block_position": {"x": -9, "y": 85, "z": -7},
                }
            return {}

    transport = StuckTransport()
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(res, "count_item", lambda *_a, **_k: 0)
    monkeypatch.setattr(res, "_ensure_outdoor_daylight", lambda *_a: True)
    monkeypatch.setattr(res, "_start_mine_process", lambda *_a: None)
    monkeypatch.setattr(res, "_reserve_gathering_inventory", lambda *_a: True)
    monkeypatch.setattr(res, "free_inventory_slots", lambda *_a: 32)
    monkeypatch.setattr(res, "_find_blocks_optional", lambda *_a, **_k: {"found": []})
    monkeypatch.setattr(res, "time", advancing_clock())
    # A creeper is always present: defence handles it and reports "interrupted"
    # on every single iteration, exactly as it did live.
    monkeypatch.setattr(combat, "defend_or_flee", lambda _c: True)
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_a, **_k: [])

    egress_calls = []
    monkeypatch.setattr(
        res,
        "try_lower_surface_egress",
        lambda _c, _s, **kw: egress_calls.append(kw) or None,
    )

    res.gather_wood(client, count=6, timeout=40)

    assert egress_calls, (
        "marooned egress must still fire when defence interrupts every tick"
    )


def _drop_client(column, health=20.0, pos=(-9, 85, -7), own=None):
    """column: {y: block_id}, applied to every column EXCEPT the bot's own
    (stepping off a pillar lands the bot in an adjacent column)."""
    own = own or {}

    class T(RecordingTransport):
        def __init__(self):
            super().__init__()
            self.here = list(pos)

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {
                    "health": health,
                    "block_position": {
                        "x": self.here[0], "y": self.here[1], "z": self.here[2]
                    },
                }
            if route == "get_block":
                if (payload["x"], payload["z"]) == (pos[0], pos[2]):
                    return {"id": own.get(payload["y"], "minecraft:air")}
                return {"id": column.get(payload["y"], "minecraft:air")}
            return {}

    t = T()
    return SimpleNamespace(transport=t), t


def test_survivable_drop_escapes_a_pillar_with_an_empty_inventory(monkeypatch):
    """Live: Bot07/Bot08 stood on a 3-block cobblestone pillar they built
    themselves at (-9,85,-7) with a completely empty inventory -- no blocks to
    bridge with, no pickaxe to mine down. Every other escape needs materials,
    so all of them no-op'd and both burned ~4000 cancelled actions at
    wood=0/64. An 11-block drop costs ~4 hearts at full health."""
    from baritone_client.common import surface_egress

    # Pillar under the bot, 8-block air gap, stone floor from y=73 down.
    pillar = {84: "minecraft:cobblestone", 83: "minecraft:cobblestone",
              82: "minecraft:cobblestone"}
    column = {y: "minecraft:stone" for y in range(73, 61, -1)}
    client, transport = _drop_client(column, own=pillar)

    def fake_goto(_c, x, y, z, **_kw):
        transport.here = [x, y, z]
        return True

    monkeypatch.setattr(surface_egress, "goto", fake_goto)

    reached = surface_egress.try_survivable_drop(client, {
        "health": 20.0, "block_position": {"x": -9, "y": 85, "z": -7},
    })

    # Lands in an ADJACENT column at the stone floor (y=73) -> feet at y=74.
    assert reached is not None
    assert reached[1] == 74
    assert abs(reached[0] - (-9)) + abs(reached[2] - (-7)) == 1
    # The fall limit must be raised for the drop and always restored after.
    msgs = [p["message"] for r, p in transport.calls if r == "chat"]
    assert any(m.startswith("#set maxFallHeightNoWater 1") for m in msgs)
    assert msgs[-1] == "#set maxFallHeightNoWater 3"


def test_survivable_drop_refuses_lava_landing():
    from baritone_client.common import surface_egress

    column = {75: "minecraft:lava"}
    client, _t = _drop_client(column)

    assert surface_egress.try_survivable_drop(client, {
        "health": 20.0, "block_position": {"x": -9, "y": 85, "z": -7},
    }) is None


def test_survivable_drop_refuses_when_the_fall_would_leave_too_little_health():
    from baritone_client.common import surface_egress

    column = {60: "minecraft:stone"}  # a 24-block fall
    client, _t = _drop_client(column, health=20.0)

    assert surface_egress.try_survivable_drop(client, {
        "health": 20.0, "block_position": {"x": -9, "y": 85, "z": -7},
    }) is None


def test_manage_inventory_can_shed_raw_copper(monkeypatch):
    """Copper is mined incidentally toward iron but has no consumer.

    It was absent from every discard tier, so manage_inventory could never
    reclaim those slots. Live 2026-08-02: Bot07 carried 640 raw copper -- 10
    permanently locked stacks -- and all four bots filled up, aborted the
    descent, walked back to a chest and started over, roughly hourly.
    """
    offered = []
    free = {"slots": 0}

    def fake_drop(_client, candidates, max_stacks=1, retain_counts=None):
        offered.append(list(candidates))
        if "minecraft:raw_copper" in candidates:
            free["slots"] = 3          # shedding copper reclaims the space
            return 1
        return 0

    monkeypatch.setattr(
        "baritone_client.common.inventory.free_inventory_slots",
        lambda _client: free["slots"],
    )
    monkeypatch.setattr(
        "baritone_client.common.inventory.drop_items", fake_drop
    )

    assert resources.manage_inventory(SimpleNamespace(), minimum_free_slots=3) is True
    assert any("minecraft:raw_copper" in tier for tier in offered), (
        "copper must be offered to the disposal path"
    )


def test_orphaned_decorative_loot_is_storage_eligible():
    from baritone_client.common.inventory import EARLY_GAME_EXCESS_ITEMS

    assert {
        "minecraft:decorated_pot",
        "minecraft:tuff_bricks",
        "minecraft:waxed_copper_block",
        "minecraft:waxed_exposed_copper_bulb",
        "minecraft:waxed_oxidized_cut_copper_stairs",
    } <= EARLY_GAME_EXCESS_ITEMS


def test_copper_is_shed_after_stone_not_before(monkeypatch):
    """A bot mining for iron should shed unusable ore before usable material."""
    from baritone_client.common import resources as res
    import inspect

    src = inspect.getsource(res.manage_inventory)
    copper = src.index("minecraft:raw_copper")
    cobble = src.index('"minecraft:cobblestone",\n            "minecraft:deepslate"')
    assert copper < cobble, "copper should be listed ahead of building stone"


def test_building_stone_is_still_retained_in_bulk(monkeypatch):
    """Adding copper must not change the cobblestone reserve."""
    from baritone_client.common import resources as res
    import inspect

    src = inspect.getsource(res.manage_inventory)
    assert '"minecraft:cobblestone": 128' in src


def test_manage_inventory_falls_back_to_the_inverted_keep_list(monkeypatch):
    """When every named tier frees nothing, disposal must invert the question.

    Live A1 2026-08-31 at 35/36 slots, 598m from home so nothing could be
    banked: of 29 carried types the named tiers matched only cobblestone (48,
    floor 128) and cobbled_deepslate (58, floor 64), and whole-stack disposal
    skipped both. It freed zero slots and retried every ~6s indefinitely while
    amethyst_shard, redstone, string and sand sat there unprotected. The
    named-tier loop must hand off to space_reclaim rather than just printing
    a failure.
    """
    from baritone_client.common import resources as res

    delegated = []
    monkeypatch.setattr(res, "_store_surplus_in_chest", lambda *_a, **_k: False)
    monkeypatch.setattr(
        "baritone_client.common.inventory.free_inventory_slots", lambda _c: 1
    )
    monkeypatch.setattr(
        "baritone_client.common.inventory.drop_items",
        lambda *_a, **_k: 0,  # every named tier frees nothing, as on A1
    )
    monkeypatch.setattr(
        "baritone_client.common.space_reclaim.reclaim_drop_tier",
        lambda client, required, retain_counts: delegated.append(
            (required, retain_counts.get("minecraft:cobblestone"))
        )
        or True,
    )

    assert res.manage_inventory(SimpleNamespace(), minimum_free_slots=3)
    assert delegated == [(3, 128)], (
        "must delegate with the caller's stricter building-stone floor intact"
    )


def _slots(chest_slots, occupied):
    """Build a container screen payload: chest slots then the 36 player slots."""
    items = []
    for i in range(chest_slots):
        items.append({"slot": i, "id": "minecraft:cobblestone" if i < occupied else "minecraft:air"})
    for i in range(chest_slots, chest_slots + 36):
        items.append({"slot": i, "id": "minecraft:air"})
    return {"slots": items, "total_slots": chest_slots + 36}


def test_chest_slot_parsing_distinguishes_single_from_double():
    """27 chest slots -> 63 total; 54 -> 90. A 36-slot player screen is not a
    container at all, which is how a blocked chest read as 'open'."""
    from baritone_client.common import harness_ops

    def client_for(payload):
        return SimpleNamespace(transport=SimpleNamespace(dispatch=lambda *_a, **_k: payload))

    assert harness_ops.open_container_slots(client_for(_slots(27, 0)))[1] == 27
    assert harness_ops.open_container_slots(client_for(_slots(54, 0)))[1] == 54
    # Player inventory screen only -- must not be treated as a container.
    assert harness_ops.open_container_slots(
        client_for({"slots": [{"slot": i, "id": "minecraft:air"} for i in range(36)], "total_slots": 36})
    ) is None


def test_chest_support_and_placement_predicates():
    from baritone_client.common import harness_ops

    assert harness_ops._is_solid_support_block("minecraft:grass_block")
    assert harness_ops._is_solid_support_block("minecraft:stone")
    assert not harness_ops._is_solid_support_block("minecraft:short_grass")
    assert not harness_ops._is_solid_support_block("minecraft:air")
    assert not harness_ops._is_solid_support_block("minecraft:water")

    assert harness_ops._is_placeable_target("minecraft:air")
    assert not harness_ops._is_placeable_target("minecraft:stone")
    # Never stack a chest onto an existing chest.
    assert not harness_ops._is_placeable_target("minecraft:chest")


def test_single_chest_spot_accepts_one_isolated_mud_support():
    from baritone_client.common import harness_ops

    def dispatch(route, payload):
        if route == "get_state":
            return {"block_position": {"x": 0, "y": 64, "z": 0}}
        position = (payload["x"], payload["y"], payload["z"])
        if position == (1, 64, 0):
            return {"id": "minecraft:air"}
        if position == (1, 63, 0):
            return {"id": "minecraft:mud"}
        return {"id": "minecraft:water"}

    client = SimpleNamespace(
        transport=SimpleNamespace(dispatch=dispatch)
    )

    assert harness_ops.find_single_chest_spot(client) == (1, 64, 0)


def test_single_chest_spot_checks_air_above_reported_surface_y():
    from baritone_client.common import harness_ops

    def dispatch(route, payload):
        if route == "get_state":
            return {"block_position": {"x": 0, "y": 64, "z": 0}}
        position = (payload["x"], payload["y"], payload["z"])
        if position[1] == 65:
            return {"id": "minecraft:air"}
        if position[1] == 64:
            return {"id": "minecraft:mud"}
        return {"id": "minecraft:water"}

    client = SimpleNamespace(
        transport=SimpleNamespace(dispatch=dispatch)
    )

    assert harness_ops.find_single_chest_spot(client) == (-1, 65, 0)


def test_manage_inventory_banks_surplus_before_discarding(monkeypatch):
    """Storing beats destroying: the discard tiers must not run when a chest
    with room can take the load."""
    from baritone_client.common import resources as res

    dropped = []
    monkeypatch.setattr("baritone_client.common.inventory.drop_items",
                        lambda *a, **k: dropped.append(a) or 0)
    monkeypatch.setattr(res, "_store_surplus_in_chest", lambda _c, _r: True)
    monkeypatch.setattr("baritone_client.common.inventory.free_inventory_slots",
                        lambda _c: 0)

    assert res.manage_inventory(SimpleNamespace(), minimum_free_slots=3) is True
    assert dropped == [], "nothing should be destroyed when storage accepted it"


def test_manage_inventory_still_discards_when_storage_cannot_help(monkeypatch):
    """If storage is unavailable the bounded discard path must still run."""
    from baritone_client.common import resources as res

    free = {"n": 0}
    def fake_drop(_client, candidates, max_stacks=1, retain_counts=None):
        free["n"] = 3
        return 1
    monkeypatch.setattr("baritone_client.common.inventory.drop_items", fake_drop)
    monkeypatch.setattr("baritone_client.common.inventory.free_inventory_slots",
                        lambda _c: free["n"])
    monkeypatch.setattr(res, "_store_surplus_in_chest", lambda _c, _r: False)

    assert res.manage_inventory(SimpleNamespace(), minimum_free_slots=3) is True


def test_surplus_storage_builds_a_double_chest_when_all_are_full(monkeypatch):
    """The overflow rule from suite 1100: full chests mean build another."""
    from baritone_client.common import resources as res
    from baritone_client.common import harness_ops

    built = []
    client = SimpleNamespace(
        transport=SimpleNamespace(
            dispatch=lambda route, _payload: {
                "health": 20,
                "food_level": 20,
                "dimension": "minecraft:overworld",
                "block_position": {"x": 0, "y": 64, "z": 0},
            }
            if route == "get_state"
            else {}
        )
    )
    monkeypatch.setattr(harness_ops, "available", lambda: True)
    monkeypatch.setattr(harness_ops, "chest_is_full", lambda _c, _p: True)
    monkeypatch.setattr(
        "baritone_client.common.inventory.count_item", lambda *_args: 2
    )
    monkeypatch.setattr(harness_ops, "create_double_chest",
                        lambda _c: built.append(True) or ((0, 64, 0), (1, 64, 0)))
    monkeypatch.setattr("baritone_client.common.storage_catalog.catalog_for",
                        lambda _c: SimpleNamespace(
                            list_containers=lambda: [{"x": 5, "y": 64, "z": 5}]))
    monkeypatch.setattr("baritone_client.common.inventory.deposit_excess_to_chest",
                        lambda _c, pos, **k: 0 if pos == (5, 64, 5) else 4)
    monkeypatch.setattr("baritone_client.common.inventory.free_inventory_slots",
                        lambda _c: 6)

    assert res._store_surplus_in_chest(client, 3) is True
    assert built, "a new double chest must be built when every chest is full"


def test_surplus_storage_ignores_remote_fleet_containers(monkeypatch):
    """Emergency cleanup must not tour arbitrary fleet storage for one slot."""
    from baritone_client.common import harness_ops
    from baritone_client.common import resources as res

    client = SimpleNamespace(
        transport=SimpleNamespace(
            dispatch=lambda route, _payload: {
                "health": 20,
                "food_level": 20,
                "dimension": "minecraft:overworld",
                "block_position": {"x": 0, "y": 64, "z": 0},
            }
            if route == "get_state"
            else {}
        )
    )
    monkeypatch.setattr(harness_ops, "available", lambda: True)
    monkeypatch.setattr(
        harness_ops,
        "create_double_chest",
        lambda _c: (_ for _ in ()).throw(
            AssertionError("adjacent single storage should precede a remote double")
        ),
    )
    monkeypatch.setattr(
        "baritone_client.common.inventory.count_item", lambda *_args: 0
    )
    monkeypatch.setattr(
        "baritone_client.common.storage_catalog.catalog_for",
        lambda _c: SimpleNamespace(
            list_containers=lambda: [
                {
                    "dimension": "minecraft:overworld",
                    "x": 600,
                    "y": 64,
                    "z": 600,
                },
                {
                    "dimension": "minecraft:the_nether",
                    "x": 4,
                    "y": 64,
                    "z": 4,
                },
            ]
        ),
    )
    monkeypatch.setattr(
        "baritone_client.common.inventory.deposit_excess_to_chest",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("remote or cross-dimension storage must be ignored")
        ),
    )

    assert res._store_surplus_in_chest(client, 3) is False


def test_surplus_storage_builds_one_carried_chest_when_double_is_unavailable(
    monkeypatch,
):
    from baritone_client.common import harness_ops
    from baritone_client.common import resources as res

    client = SimpleNamespace(
        transport=SimpleNamespace(
            dispatch=lambda route, _payload: {
                "health": 20,
                "food_level": 20,
                "dimension": "minecraft:overworld",
                "block_position": {"x": 0, "y": 64, "z": 0},
            }
            if route == "get_state"
            else {}
        )
    )
    placed = []
    deposits = []
    monkeypatch.setattr(harness_ops, "available", lambda: True)
    monkeypatch.setattr(
        harness_ops,
        "create_double_chest",
        lambda _c: (_ for _ in ()).throw(
            AssertionError("adjacent single storage should precede a remote double")
        ),
    )
    monkeypatch.setattr(
        harness_ops,
        "find_single_chest_spot",
        lambda _c: (1, 64, 0),
    )
    monkeypatch.setattr(harness_ops, "move_near", lambda *_a, **_k: True)
    monkeypatch.setattr(
        harness_ops,
        "place_block_exact",
        lambda _c, x, y, z, block: placed.append((x, y, z, block)) or True,
    )
    monkeypatch.setattr(
        "baritone_client.common.inventory.count_item", lambda *_args: 1
    )
    monkeypatch.setattr(
        "baritone_client.common.storage_catalog.catalog_for",
        lambda _c: SimpleNamespace(list_containers=lambda: []),
    )
    monkeypatch.setattr(
        "baritone_client.common.inventory.deposit_excess_to_chest",
        lambda *_args, **kwargs: deposits.append(kwargs) or 2,
    )
    monkeypatch.setattr(
        "baritone_client.common.inventory.free_inventory_slots", lambda _c: 4
    )

    assert res._store_surplus_in_chest(client, 3)
    assert placed == [(1, 64, 0, "minecraft:chest")]
    assert "minecraft:basalt" in deposits[0]["deposit_items"]
    assert "minecraft:raw_copper" in deposits[0]["deposit_items"]
    assert "minecraft:quartz" in deposits[0]["deposit_items"]
    assert deposits[0]["retain_counts"]["minecraft:cobblestone"] == 64


def test_unreachable_storage_is_cooled_down_before_next_cleanup(monkeypatch):
    from baritone_client.common import storage_safety

    client = SimpleNamespace()
    snapshot = {
        "dimension": "minecraft:overworld",
        "block_position": {"x": 0, "y": 64, "z": 0},
    }
    monkeypatch.setattr(
        "baritone_client.common.storage_catalog.catalog_for",
        lambda _c: SimpleNamespace(
            list_containers=lambda: [
                {
                    "dimension": "minecraft:overworld",
                    "x": 40,
                    "y": 64,
                    "z": 0,
                }
            ]
        ),
    )

    assert list(storage_safety.nearby_storage_positions(client, snapshot)) == [
        (40, 64, 0)
    ]
    storage_safety.remember_unreachable_storage(client, (40, 64, 0))
    assert list(storage_safety.nearby_storage_positions(client, snapshot)) == []


def test_storage_margin_restoration_eats_and_regenerates(monkeypatch):
    from baritone_client.common import storage_safety

    state = {"health": 15.5, "food_level": 17, "is_dead": False}
    client = SimpleNamespace(
        transport=SimpleNamespace(dispatch=lambda *_args: dict(state))
    )

    def eat(_client, minimum_food):
        assert minimum_food == 18
        state["food_level"] = 20
        return True

    def heal(_client, minimum_health, timeout):
        assert minimum_health == 18.0
        assert timeout == 45.0
        state["health"] = 18.0
        return True

    monkeypatch.setattr("baritone_client.common.combat.eat_until_hunger", eat)
    monkeypatch.setattr("baritone_client.common.health_recovery.recover_health", heal)

    assert storage_safety.restore_storage_travel_margin(client)


class _PickTransport:
    def __init__(self, items):
        self.items = items
        self.selected = None
        self.calls = []

    def dispatch(self, route, payload=None):
        self.calls.append((route, payload))
        if route == "get_inventory":
            return {"inventory": self.items}
        if route == "select_slot":
            self.selected = payload["slot"]
        return {}


def test_equip_best_pickaxe_prefers_durability_over_tier(monkeypatch):
    """A nearly-spent diamond pick must lose to a fresh iron one.

    equip_best_weapon walks a fixed tier list and returns on first match, so
    it would pick the diamond regardless of how worn it is.
    """
    from baritone_client.common import resources as res

    chosen = []
    monkeypatch.setattr("baritone_client.common.inventory.select_item",
                        lambda _c, item_id, allow_swap=False: chosen.append(item_id) or True)

    transport = _PickTransport([
        # diamond: 1561 max, 1555 damage -> 6 uses left
        {"slot": 0, "id": "minecraft:diamond_pickaxe", "count": 1, "damage": 1555},
        # iron: 250 max, 10 damage -> 240 uses left
        {"slot": 1, "id": "minecraft:iron_pickaxe", "count": 1, "damage": 10},
    ])
    assert res.equip_best_pickaxe(SimpleNamespace(transport=transport)) is True
    assert chosen == ["minecraft:iron_pickaxe"]


def test_equip_best_pickaxe_ignores_fully_broken_tools(monkeypatch):
    from baritone_client.common import resources as res

    chosen = []
    monkeypatch.setattr("baritone_client.common.inventory.select_item",
                        lambda _c, item_id, allow_swap=False: chosen.append(item_id) or True)

    transport = _PickTransport([
        {"slot": 0, "id": "minecraft:iron_pickaxe", "count": 1, "damage": 250},   # spent
        {"slot": 1, "id": "minecraft:stone_pickaxe", "count": 1, "damage": 0},    # fresh
    ])
    assert res.equip_best_pickaxe(SimpleNamespace(transport=transport)) is True
    assert chosen == ["minecraft:stone_pickaxe"]


def test_equip_best_pickaxe_reports_failure_with_no_usable_pick(monkeypatch):
    from baritone_client.common import resources as res

    monkeypatch.setattr("baritone_client.common.inventory.select_item",
                        lambda *_a, **_k: True)
    transport = _PickTransport([{"slot": 0, "id": "minecraft:dirt", "count": 5}])
    assert res.equip_best_pickaxe(SimpleNamespace(transport=transport)) is False


def test_equip_best_pickaxe_swaps_a_pick_out_of_main_inventory(monkeypatch):
    """The best pick is useless in slot 30; it must go through the hotbar swap."""
    from baritone_client.common import resources as res

    swaps = []
    monkeypatch.setattr("baritone_client.common.inventory.select_item",
                        lambda _c, item_id, allow_swap=False: swaps.append(allow_swap) or True)
    transport = _PickTransport([
        {"slot": 30, "id": "minecraft:diamond_pickaxe", "count": 1, "damage": 0},
    ])
    assert res.equip_best_pickaxe(SimpleNamespace(transport=transport)) is True
    assert swaps == [True], "must allow the hotbar swap"
