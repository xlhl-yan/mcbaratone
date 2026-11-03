from types import SimpleNamespace

import pytest

from baritone_client.automator import mining_storage as storage


@pytest.fixture
def home(monkeypatch):
    anchor = (3, 71, 3)
    blocks = {(5, 70, 1): "minecraft:cobblestone", (6, 71, 1): "minecraft:cobblestone"}
    stock, actions = {}, []
    state = SimpleNamespace(custom_data={"base_location": list(anchor), "structures": {"starter_house": {"origin": [0, 70, 0]}}})
    client = SimpleNamespace(transport=SimpleNamespace(dispatch=lambda route, payload: {"id": blocks.get(tuple(payload[a] for a in ("x", "y", "z")), "minecraft:air")}))
    observed = SimpleNamespace(free=2, safe=True, place=True, bank=True, original_at_home=storage._at_home)
    monkeypatch.setattr(storage, "_at_home", lambda *_a: observed.safe)
    monkeypatch.setattr("baritone_client.common.inventory.count_item", lambda _c, item: stock.get(item, 0))
    def craft(_c, item, count):
        actions.append(("craft", item))
        if item == "minecraft:oak_planks" and stock.get("minecraft:oak_log", 0) >= 2:
            stock["minecraft:oak_planks"], stock["minecraft:oak_log"] = 8, 0
        if item == "minecraft:chest" and stock.get("minecraft:oak_planks", 0) >= 8:
            stock["minecraft:chest"], stock["minecraft:oak_planks"] = 1, 0
        return True
    monkeypatch.setattr("baritone_client.common.inventory.craft", craft)
    def gather(*_a):
        stock["minecraft:oak_log"] = 2
        actions.append(("gather",))
    monkeypatch.setattr("baritone_client.automator.charcoal_wood.gather_charcoal_logs", gather)
    def place(_c, x, y, z, item, **kwargs):
        assert kwargs == {"allow_break": False}
        actions.append(("place", (x, y, z)))
        if observed.place:
            blocks[(x, y, z)] = item
        return True
    monkeypatch.setattr("baritone_client.common.harness_ops.place_block_exact", place)
    monkeypatch.setattr("baritone_client.common.storage_catalog.catalog_for", lambda *_a: SimpleNamespace(register_container=lambda *_a, **_k: actions.append(("catalog",))))
    def deposit(*_a, **kwargs):
        actions.append(("deposit", kwargs))
        if observed.bank:
            observed.free = 10
        return 4
    monkeypatch.setattr("baritone_client.common.inventory.deposit_excess_to_chest", deposit)
    monkeypatch.setattr("baritone_client.common.tunnel_miner.free_slots", lambda _c: observed.free)
    return client, state, anchor, blocks, stock, actions, observed


def bank(home):
    c, s, anchor, *_ = home
    return storage._bank.__wrapped__(c, s, *anchor, 10, {"minecraft:carrot"}, {"minecraft:carrot": 64})


def test_capacity_chain_verifies_chest_and_space_and_preserves_reserves(home):
    assert bank(home)
    assert home[1].custom_data["mining_storage"]["chest"] == [5, 71, 1]
    actions = home[5]
    assert [a[0] for a in actions] == ["gather", "craft", "craft", "place", "catalog", "deposit"]
    assert actions[-1][1]["retain_counts"] == {"minecraft:carrot": 64}


@pytest.mark.parametrize("failure", ["floor", "wall", "obstruction", "headroom", "landmark", "far_home"])
def test_unsafe_spot_never_gathers_or_places(home, failure):
    blocks, state = home[3], home[1]
    if failure == "floor": blocks[(5, 70, 1)] = "minecraft:water"
    elif failure == "wall": blocks[(6, 71, 1)] = "minecraft:void_air"
    elif failure == "obstruction": blocks[(5, 71, 1)] = "minecraft:oak_door"
    elif failure == "headroom": blocks[(5, 72, 1)] = "minecraft:stone"
    elif failure == "landmark": state.custom_data["structures"]["starter_house"]["bed"] = [5, 71, 1]
    else: state.custom_data["structures"]["starter_house"]["origin"] = [100, 70, 100]
    assert not bank(home)
    assert home[5] == []


def test_place_ack_without_chest_does_not_bank_or_record(home):
    home[6].place = False
    assert not bank(home)
    assert "chest" not in home[1].custom_data["mining_storage"]
    assert not any(a[0] == "deposit" for a in home[5])


def test_deposit_ack_without_room_is_failure(home):
    home[6].bank = False
    assert not bank(home)


@pytest.mark.parametrize("arrived", [True, False])
def test_crafting_on_another_house_level_returns_before_storage_placement(home, monkeypatch, arrived):
    home[4]["minecraft:oak_planks"] = 8
    original = __import__("baritone_client.common.inventory", fromlist=["craft"]).craft
    def upstairs(client, item, count):
        result = original(client, item, count)
        home[6].safe = False
        return result
    monkeypatch.setattr("baritone_client.common.inventory.craft", upstairs)
    monkeypatch.setattr("baritone_client.common.harness_ops.close_container", lambda *_a: home[5].append(("close",)))
    monkeypatch.setattr("baritone_client.common.food_workstation._fresh_safe_position", lambda *_a, **_k: (3, 75, 3))
    def return_home(client, x, y, z, state):
        # The real signature: a fake taking *args hid a missing ``state``
        # that crashed every live overflow attempt (A1 2026-10-15).
        assert state is home[1]
        home[5].append(("return",))
        home[6].safe = arrived
        return True
    monkeypatch.setattr("baritone_client.common.food_return._return", return_home)
    assert bank(home) is arrived
    assert [a[0] for a in home[5]] == (["craft", "close", "return", "place", "catalog", "deposit"] if arrived else ["craft", "close", "return"])


@pytest.mark.parametrize("position", [None, (3, 80, 3), (100, 75, 100)])
def test_post_crafting_unknown_or_remote_position_never_starts_capacity_return(home, monkeypatch, position):
    home[4]["minecraft:chest"] = 1
    home[6].safe = False
    monkeypatch.setattr("baritone_client.common.harness_ops.close_container", lambda *_a: None)
    monkeypatch.setattr("baritone_client.common.food_workstation._fresh_safe_position", lambda *_a, **_k: position)
    monkeypatch.setattr("baritone_client.common.food_return._return", lambda *_a: pytest.fail("unsafe capacity return"))
    assert not bank(home)
    assert home[5] == []


def test_existing_overflow_is_reused_without_new_gather_or_build(home):
    home[1].custom_data["mining_storage"] = {"chest": [5, 71, 1]}
    home[3][(5, 71, 1)] = "minecraft:chest"
    assert bank(home)
    assert [a[0] for a in home[5]] == ["deposit"]


def test_occupied_corners_can_use_an_observed_wall_cell(home):
    blocks = home[3]
    for x, z in ((5, 1), (5, 5), (1, 5), (1, 1)):
        blocks[(x, 71, z)] = "minecraft:crafting_table"
    blocks.update({(5, 70, 2): "minecraft:cobblestone", (6, 71, 2): "minecraft:oak_planks"})
    assert bank(home)
    assert home[1].custom_data["mining_storage"]["chest"] == [5, 71, 2]
    assert [a for a in home[5] if a[0] == "place"] == [("place", (5, 71, 2))]


def test_floor_corner_home_anchor_does_not_limit_capacity_to_the_workstation(home):
    home[1].custom_data["structures"]["starter_house"]["crafting_table"] = [1, 71, 1]
    home[3].update({(1, 71, 1): "minecraft:crafting_table",
                    (1, 70, 3): "minecraft:cobblestone", (0, 71, 3): "minecraft:oak_planks"})
    assert storage._spot(home[0], home[1], (0, 70, 0)) == (1, 71, 3)


def test_capacity_never_occupies_the_doorway_aisle(home):
    home[3].clear()
    home[3].update({(3, 70, 1): "minecraft:cobblestone", (3, 71, 0): "minecraft:oak_planks"})
    assert not bank(home)
    assert home[5] == []


def test_unrecorded_wall_chest_is_reused_before_building(home):
    chest = (5, 71, 2)
    home[3][chest] = "minecraft:chest"
    assert bank(home)
    assert [a[0] for a in home[5]] == ["deposit"]
    assert not home[1].custom_data["mining_storage"]


@pytest.mark.parametrize("rejected", [False, True, "command_error"])
def test_other_observed_capacity_is_tried_before_expansion(home, monkeypatch, rejected):
    from baritone_client.core.exceptions import CommandError

    old, spare = (5, 71, 1), (5, 71, 2)
    home[1].custom_data["mining_storage"] = {"chest": list(old)}
    home[3].update({old: "minecraft:chest", spare: "minecraft:chest"})
    seen = []
    def deposit(_c, chest, **kwargs):
        assert kwargs["retain_counts"] == {"minecraft:carrot": 64}
        seen.append(chest)
        if rejected == "command_error" and chest == old:
            raise CommandError("container interaction rejected")
        if chest == spare:
            home[6].free = 10
        return -1 if rejected and chest == old else 0
    monkeypatch.setattr("baritone_client.common.inventory.deposit_excess_to_chest", deposit)
    monkeypatch.setattr(storage, "_full", lambda _c, chest: chest == old)
    assert bank(home)
    assert seen == [old, spare]
    assert home[5] == []
    assert home[1].custom_data["mining_storage"]["chest"] == list(old)


@pytest.mark.parametrize("response", [
    {"success": False, "id": "minecraft:air"},
    {"status": "error", "data": {"id": "minecraft:air"}},
    {"status": "failed", "data": {"id": "minecraft:air"}},
    {"data": {"status": "failure", "id": "minecraft:air"}},
    {"id": "minecraft:air", "data": None},
    None,
])
def test_failed_block_envelope_cannot_justify_capacity_placement(home, response):
    home[0].transport.dispatch = lambda *_a: response
    assert not storage.bank_mining_overflow(home[0], home[1], home[2], 10, set(), {})
    assert home[5] == []


def test_wrapped_block_observation_is_used(home):
    home[0].transport.dispatch = lambda *_a: {"success": True, "data": {"id": "minecraft:chest"}}
    assert storage._block(home[0], (5, 71, 1)) == "minecraft:chest"


@pytest.mark.parametrize("origin", [[0.5, 70, 0], [False, 70, 0], [0, float("nan"), 0]])
def test_unknown_house_geometry_never_places(home, origin):
    home[1].custom_data["structures"]["starter_house"]["origin"] = origin
    assert not bank(home)
    assert home[5] == []


@pytest.mark.parametrize("full", [True, False, None])
def test_full_existing_overflow_adds_only_one_verified_capacity_batch(home, monkeypatch, full):
    old = (5, 71, 1)
    home[1].custom_data["mining_storage"] = {"chest": list(old)}
    home[3].update({old: "minecraft:chest", (5, 70, 5): "minecraft:cobblestone", (6, 71, 5): "minecraft:cobblestone"})
    monkeypatch.setattr(storage, "_full", lambda *_a: full)
    calls = []
    def deposit(_c, chest, **kwargs):
        calls.append(chest)
        if chest != old:
            home[6].free = 10
        return 0 if chest == old else 3
    monkeypatch.setattr("baritone_client.common.inventory.deposit_excess_to_chest", deposit)
    assert bank(home) is (full is True)
    assert calls == ([old, (5, 71, 5)] if full is True else [old])
    assert home[1].custom_data["mining_storage"]["chest"] == ([5, 71, 5] if full is True else list(old))
    assert sum(a[0] == "place" for a in home[5]) == (1 if full is True else 0)
    assert home[3][old] == "minecraft:chest"


def test_rejected_overflow_never_justifies_capacity_growth(home, monkeypatch):
    home[1].custom_data["mining_storage"] = {"chest": [5, 71, 1]}
    home[3][(5, 71, 1)] = "minecraft:chest"
    monkeypatch.setattr("baritone_client.common.inventory.deposit_excess_to_chest", lambda *_a, **_k: -1)
    monkeypatch.setattr(storage, "_full", lambda *_a: pytest.fail("rejected chest is not a full chest"))
    assert not bank(home)
    assert home[5] == []


@pytest.mark.parametrize("case", ["full", "empty", "partial", "duplicate", "error", "sync", "count", "unknown", "total", "envelope"])
def test_fullness_requires_complete_fresh_container_evidence(home, monkeypatch, case):
    slots = [{"slot": n, "id": "minecraft:stone" if n < 27 else "minecraft:air", "count": 1 if n < 27 else 0} for n in range(63)]
    screen = {"sync_id": 2, "total_slots": 63, "slots": slots}
    if case == "empty": slots[0].update(id="minecraft:air", count=0)
    elif case == "partial": slots.pop()
    elif case == "duplicate": slots[1]["slot"] = 0
    elif case == "error": screen["success"] = False
    elif case == "sync": screen["sync_id"] = False
    elif case == "count": slots[0]["count"] = True
    elif case == "unknown": screen = None
    elif case == "total": screen["total_slots"] = 63.0
    elif case == "envelope": screen = {"status": "error", "data": screen}
    closed = []
    monkeypatch.setattr("baritone_client.common.harness_ops.open_container", lambda *_a, **_k: True)
    monkeypatch.setattr("baritone_client.common.harness_ops.close_container", lambda *_a: closed.append(True))
    home[0].transport.dispatch = lambda *_a: screen
    assert storage._full(home[0], (5, 71, 1)) is (case == "full")
    assert closed == [True]


def test_failed_home_observation_refuses_all_work(home):
    home[6].safe = False
    assert not storage.bank_mining_overflow(home[0], home[1], home[2], 10, set(), {})
    assert home[5] == []


def test_read_failure_is_closed(home, monkeypatch):
    def fail(*_a): raise RuntimeError("unknown block")
    monkeypatch.setattr(storage, "_block", fail)
    assert not storage.bank_mining_overflow(home[0], home[1], home[2], 10, set(), {})


@pytest.mark.parametrize("material", ["minecraft:oak_planks", "minecraft:spruce_planks", "minecraft:cobblestone"])
def test_observed_plank_house_materials_support_capacity(home, material):
    home[3][(5, 70, 1)] = material
    home[3][(6, 71, 1)] = material
    assert bank(home)


def test_local_preparation_first_reaches_verified_home(home, monkeypatch):
    home[6].safe = False
    monkeypatch.setattr("baritone_client.common.food_workstation._fresh_safe_position", lambda *_a, **_k: (3, 71, 8))
    def arrive(client, x, y, z, state):
        assert state is home[1]
        home[6].safe = True
    monkeypatch.setattr("baritone_client.common.food_return._return", arrive)
    monkeypatch.setattr(storage, "_bank", storage._bank.__wrapped__)
    assert storage.bank_mining_overflow(home[0], home[1], home[2], 10, set(), {})


def test_home_navigation_ack_without_arrival_does_not_build(home, monkeypatch):
    home[6].safe = False
    monkeypatch.setattr("baritone_client.common.food_workstation._fresh_safe_position", lambda *_a, **_k: (3, 71, 8))
    monkeypatch.setattr("baritone_client.common.food_return._return", lambda *_a: True)
    assert not storage.bank_mining_overflow(home[0], home[1], home[2], 10, set(), {})
    assert home[5] == []


@pytest.mark.parametrize("position", [(3, 65, 3), (100, 71, 100), None])
def test_preparation_never_leaves_remote_or_underground_work_for_capacity(home, monkeypatch, position):
    home[6].safe = False
    monkeypatch.setattr("baritone_client.common.food_workstation._fresh_safe_position", lambda *_a, **_k: position)
    monkeypatch.setattr("baritone_client.common.food_return._return", lambda *_a: pytest.fail("unsafe capacity travel"))
    assert not storage.bank_mining_overflow(home[0], home[1], home[2], 10, set(), {})


@pytest.mark.parametrize("unsafe", [None, "health", "food", "dead", "moving", "airborne", "threat", "dimension", "mode"])
def test_indoor_capacity_requires_fresh_local_survival_margin(home, monkeypatch, unsafe):
    live = {"is_dead": False, "is_pathing": False, "is_on_ground": True,
            "health": 20, "food_level": 20, "world_time": 13000,
            "dimension": "minecraft:overworld", "game_mode": "survival"}
    if unsafe == "health": live["health"] = 17
    elif unsafe == "food": live["food_level"] = 13
    elif unsafe == "dead": live["is_dead"] = True
    elif unsafe == "moving": live["is_pathing"] = True
    elif unsafe == "airborne": live["is_on_ground"] = False
    elif unsafe == "dimension": live["dimension"] = "minecraft:the_nether"
    elif unsafe == "mode": live["game_mode"] = "creative"
    home[0].transport.dispatch = lambda *_a: live
    monkeypatch.setattr("baritone_client.common.food_workstation._fresh_safe_position", lambda *_a, **_k: home[2])
    def scan(*_a, **kwargs):
        assert kwargs["radius"] == 8 and kwargs["raise_on_error"] is True
        return [{"type": "minecraft:creeper"}] if unsafe == "threat" else []
    monkeypatch.setattr("baritone_client.common.combat.scan_for_threats", scan)
    assert home[6].original_at_home(home[0], home[2]) is (unsafe is None)


@pytest.mark.parametrize("actually_freed", [True, False])
def test_full_pack_reclaims_only_rubble_before_wood_collection(home, monkeypatch, actually_freed):
    home[6].free = 0
    def drop(_c, items, **kwargs):
        assert "minecraft:carrot" not in items and "minecraft:iron_ingot" not in items
        assert kwargs == {"max_stacks": 2, "retain_counts": {"minecraft:cobblestone": 64}}
        home[5].append(("drop",))
        if actually_freed:
            home[6].free = 1
        return 2  # acknowledgement alone must not admit wood collection
    monkeypatch.setattr("baritone_client.common.inventory.drop_items", drop)
    assert bank(home) is actually_freed
    if not actually_freed:
        assert home[5] == [("drop",)]


@pytest.mark.parametrize("unknown_settings", [False, True])
def test_public_entry_executes_real_coordinate_bound_guard(home, unknown_settings):
    client, state, anchor, *_ = home
    original = client.transport.dispatch
    settings = {"allowBreak": "true", "allowParkour": "true", "maxFallHeightNoWater": "3"}
    writes = []
    def dispatch(route, payload):
        if route == "settings":
            if "get" in payload:
                return {} if unknown_settings else {"key": payload["get"], "value": settings[payload["get"]]}
            settings[payload["set"]] = payload["value"]
            writes.append((payload["set"], payload["value"]))
            return {"success": True}
        if route == "get_state":
            return {"is_pathing": False}
        return original(route, payload)
    client.transport.dispatch = dispatch
    result = storage.bank_mining_overflow(client, state, anchor, 10, set(), {})
    assert result is (not unknown_settings)
    if unknown_settings:
        assert home[5] == [] and writes == []
    else:
        assert ("allowBreak", "false") in writes and ("maxFallHeightNoWater", "1") in writes
        assert settings == {"allowBreak": "true", "allowParkour": "true", "maxFallHeightNoWater": "3"}
        assert any(a[0] == "deposit" for a in home[5])
