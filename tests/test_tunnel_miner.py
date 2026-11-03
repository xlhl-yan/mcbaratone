"""The tunnel miner digs a safe staircase to iron, lights it, and comes home."""

from types import SimpleNamespace

import pytest

from baritone_client.common import tunnel_miner as tm
from baritone_client.common import tunnel_planner as tp

AIR = "minecraft:air"
STONE = "minecraft:stone"
IRON = "minecraft:iron_ore"
ENTRANCE = (0, 70, 0)


class FakeWorld:
    """A stone world under a grass surface at y=69, with a scriptable bot."""

    def __init__(self, extent=20, bottom=30):
        self.blocks = {}
        for x in range(-extent, extent + 1):
            for z in range(-extent, extent + 1):
                for y in range(bottom, 70):
                    self.blocks[(x, y, z)] = "minecraft:grass_block" if y == 69 else STONE
        self.pos = ENTRANCE
        self.health = 20.0
        self.entities = []
        self.entity_response = None
        self.entity_error = False
        self.state_response = None
        self.state_error = False
        self.state_extra = {}
        self.raw_iron = 0
        self.torches = 12
        self.dug = []
        self.placed = []
        self.hostile_after_digs = None
        self.used_slots = 1
        self.pick_damage = 0
        self.allow_break = "true"
        self.pending_allow_break = None
        self.allow_break_apply_after_reads = 0
        self.pending_allow_break_reads = 0
        self.never_apply_allow_break = False
        self.allow_break_writes = []
        self.pathing = False
        self.transport = SimpleNamespace(dispatch=self.dispatch)

    def open(self, cell):
        return self.blocks.get(cell, AIR) in (AIR, "minecraft:torch")

    def dispatch(self, route, payload):
        if route == "get_state":
            if self.state_error:
                raise RuntimeError("state unavailable")
            if self.state_response is not None:
                return self.state_response
            x, y, z = self.pos
            return {
                "block_position": {"x": x, "y": y, "z": z}, "health": self.health,
                "food_level": 20, "dimension": "minecraft:overworld", "is_dead": self.health <= 0,
                "is_pathing": self.pathing,
                **self.state_extra,
            }
        if route == "settings":
            if "get" in payload:
                if self.pending_allow_break is not None and not self.never_apply_allow_break:
                    if self.pending_allow_break_reads <= 0:
                        self.allow_break = self.pending_allow_break
                        self.pending_allow_break = None
                    else:
                        self.pending_allow_break_reads -= 1
                return {"value": self.allow_break}
            value = str(payload.get("value"))
            self.allow_break_writes.append(value)
            self.pending_allow_break = value
            self.pending_allow_break_reads = self.allow_break_apply_after_reads
            if self.pending_allow_break_reads <= 0 and not self.never_apply_allow_break:
                self.allow_break = value
                self.pending_allow_break = None
            return {"status": "requested"}
        if route == "get_block":
            return {"id": self.blocks.get((payload["x"], payload["y"], payload["z"]), AIR)}
        if route == "get_view":
            r, (px, py, pz) = payload["radius"], self.pos
            return {"voxels": [
                {"x": x, "y": y, "z": z, "id": b} for (x, y, z), b in self.blocks.items()
                if abs(x - px) <= r and abs(y - py) <= r and abs(z - pz) <= r
            ]}
        if route == "get_entities":
            if self.entity_error:
                raise RuntimeError("entities unavailable")
            if self.entity_response is not None:
                return self.entity_response
            return {"entities": list(self.entities)}
        if route == "get_inventory":
            rows = [
                {"slot": n, "id": "minecraft:stone_pickaxe" if n == 0 else "minecraft:dirt", "count": 1}
                for n in range(self.used_slots)
            ]
            rows[0].update(max_damage=131, damage=self.pick_damage)
            return {"inventory": rows, "snapshot_valid": True, "selected_slot": 0}
        if route == "find_blocks":
            return {"found": [
                {"x": x, "y": y, "z": z} for (x, y, z), b in self.blocks.items() if b in payload["blocks"]
            ]}
        if route == "dig_block":
            cell = (payload["x"], payload["y"], payload["z"])
            block = self.blocks.pop(cell, AIR)
            self.dug.append((cell, block))
            if block == IRON:
                self.raw_iron += 1
            if self.hostile_after_digs is not None and len(self.dug) >= self.hostile_after_digs:
                self.entities = [{"type": "minecraft:zombie", "distance": 5.0}]
            return {"started": True}
        return {}

    def goto(self, _client, x, y, z, **_kw):
        target = (x, y, z)
        if self.open(target) and self.open((x, y + 1, z)):
            self.pos = target
            return True
        return False

    def place(self, _client, x, y, z, block, allow_break=True):
        self.blocks[(x, y, z)] = block
        self.placed.append((x, y, z))
        self.torches -= 1
        return True


@pytest.fixture
def world(monkeypatch):
    w = FakeWorld()
    monkeypatch.setattr("baritone_client.common.navigation.goto", w.goto)
    monkeypatch.setattr("baritone_client.common.harness_ops.place_block_exact", w.place)
    monkeypatch.setattr("baritone_client.common.resources.equip_best_pickaxe", lambda _c, **_k: True)
    monkeypatch.setattr(tm.time, "sleep", lambda _s: None)
    return w


def make_miner(world, seconds=10_000.0, **kwargs):
    deadline = tm.time.monotonic() + seconds
    return tm.TunnelMiner(
        world, surface_y=70, deadline=deadline, entrance=ENTRANCE, min_y=12,
        count_fn=lambda item: world.torches if item == "minecraft:torch" else 0, **kwargs,
    )


def test_digs_a_staircase_to_iron_lights_it_and_retreats(world):
    for cell in [(6, 55, 3), (7, 55, 3), (6, 54, 3)]:
        world.blocks[cell] = IRON
    miner = make_miner(world)

    reason = miner.mine(3, lambda: world.raw_iron)

    assert reason == "quota" and world.raw_iron == 3
    assert miner.stats.moves > 15
    # Every dug block was stone, soil or ore: nothing else is ever broken.
    allowed = tp.STONE | {IRON, AIR}
    assert all(block in allowed for _cell, block in world.dug)
    # Lit every few moves once underground, never at the surface.
    assert miner.stats.torches >= 2 and all(y < 67 for _x, y, _z in world.placed)
    # The bot walks its own trail back to the entrance.
    assert miner.retreat() is True and world.pos[1] >= 68


def test_iron_beside_lava_is_never_dug_and_a_clean_vein_is(world):
    world.blocks[(6, 55, 3)] = IRON
    for dx, dy, dz in tp.NEIGHBOURS:  # lava touching the ore on every side
        world.blocks.setdefault((6 + dx, 55 + dy, 3 + dz), "minecraft:lava")
    world.blocks[(6, 55, 3)] = IRON
    world.blocks[(-5, 52, -4)] = IRON  # a clean ore on the other side
    miner = make_miner(world)

    miner.mine(1, lambda: world.raw_iron)

    assert world.raw_iron == 1
    dug_cells = {cell for cell, _b in world.dug}
    lava = {c for c, b in world.blocks.items() if b == "minecraft:lava"}
    for cell in dug_cells:
        assert not any((cell[0] + dx, cell[1] + dy, cell[2] + dz) in lava for dx, dy, dz in tp.NEIGHBOURS)


def test_a_natural_cave_is_never_entered(world):
    for x in range(-8, 9):
        for z in range(-8, 9):
            for y in (52, 53, 54):
                world.blocks.pop((x, y, z), None)  # a wide cavern below the entrance
    world.blocks[(0, 50, 0)] = IRON
    miner = make_miner(world)

    reason = miner.mine(1, lambda: world.raw_iron)

    assert world.raw_iron == 0 and reason in ("no safe route", "no reachable iron")
    assert all(cell[1] > 54 or cell[1] < 52 for cell, _b in world.dug)  # never opened a cavern cell


def test_a_hostile_mob_ends_the_trip_after_the_defence_has_had_its_chance(world):
    world.blocks[(6, 55, 3)] = IRON
    world.hostile_after_digs = 6
    miner = make_miner(world)

    with pytest.raises(tm.MineAbort) as stop:
        miner.mine(1, lambda: world.raw_iron)

    assert stop.value.reason == "hostile mob"
    assert miner.retreat() is False  # a persistent nearby threat still blocks travel
    assert world.pos in miner.trail


def _occluded_hostile(kind="zombie", distance=6.0):
    return {
        "type": f"minecraft:{kind}", "distance": distance,
        "position": {"x": distance, "y": 70, "z": 0},
        "velocity": {"x": 0, "y": 0, "z": 0},
        "can_see_player": False, "is_aggressive": False,
    }


@pytest.mark.parametrize("kind,distance", [("zombie", 6.0), ("creeper", 6.5)])
def test_occluded_calm_nearby_mob_does_not_block_mining_or_retreat(world, kind, distance):
    if kind == "creeper":
        world.state_extra = {
            "is_on_ground": True,
            "velocity": {"x": 0, "y": -0.0784, "z": 0},
        }
    world.entities = [_occluded_hostile(kind, distance)]
    miner = make_miner(world, patience=0)

    assert miner.check()["cell"] == ENTRANCE
    assert miner.hostiles() == 0

    # Give retreat one observed, open step to retrace.
    world.pos = (1, 70, 0)
    world.blocks.pop((1, 70, 0), None)
    world.blocks.pop((1, 71, 0), None)
    miner.trail = [ENTRANCE, world.pos]
    assert miner.retreat() is True
    assert world.pos == ENTRANCE


@pytest.mark.parametrize(
    "entity",
    [
        {"type": "minecraft:zombie", "distance": 5.0},
        {
            "type": "minecraft:zombie", "distance": 5.0,
            "position": {"x": 5, "y": 70, "z": 0},
            "velocity": {"x": 0, "y": 0, "z": 0},
            "can_see_player": True, "is_aggressive": True,
        },
    ],
    ids=["unknown-visibility", "visible-aggressive"],
)
def test_unknown_or_visible_nearby_hostile_still_blocks_the_trip(world, entity):
    world.entities = [entity]
    miner = make_miner(world, patience=0)

    with pytest.raises(tm.MineAbort, match="hostile mob"):
        miner.check()


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"entities": None},
        {"entities": [None]},
        {"entities": [{"type": "minecraft:zombie", "distance": "unknown"}]},
        {"entities": [{"type": "minecraft:zombie", "distance": float("inf")}]},
        {"entities": [{"type": "minecraft:zombie", "distance": True}]},
    ],
    ids=[
        "missing-list", "null-list", "malformed-entry", "malformed-distance",
        "infinite-distance", "boolean-distance",
    ],
)
def test_malformed_entity_telemetry_fails_closed(world, response):
    world.entity_response = response
    miner = make_miner(world, patience=0)

    with pytest.raises(tm.MineAbort, match="entity telemetry invalid"):
        miner.check()


@pytest.mark.parametrize(
    "response",
    [
        {"success": False, "entities": []},
        {"status": "error", "data": {"entities": []}},
        {"error": "query failed", "data": {"entities": []}},
        {"data": {"success": False, "entities": []}},
        {"data": {"error": "query failed", "entities": []}},
    ],
    ids=[
        "raw-success-false", "raw-status-error", "raw-error",
        "data-success-false", "data-error",
    ],
)
def test_entity_error_envelopes_fail_closed_even_with_empty_entities(world, response):
    world.entity_response = response

    with pytest.raises(tm.MineAbort, match="telemetry error"):
        make_miner(world, patience=0).check()


@pytest.mark.parametrize("skipped", [1, True, "0", None, -1])
def test_partial_entity_snapshots_fail_closed(world, skipped):
    world.entity_response = {"entities": [], "skipped_count": skipped}
    with pytest.raises(tm.MineAbort, match="entity telemetry incomplete"):
        make_miner(world).check()


def test_entity_query_error_fails_closed_during_retreat(world):
    world.entity_error = True
    world.pos = (1, 70, 0)
    world.blocks.pop((1, 70, 0), None)
    world.blocks.pop((1, 71, 0), None)
    miner = make_miner(world, patience=0)
    miner.trail = [ENTRANCE, world.pos]

    assert miner.retreat() is False
    assert world.pos == (1, 70, 0)


@pytest.mark.parametrize(
    "bad_state",
    [
        {},
        {"block_position": {"x": 0}},
        {
            "block_position": {"x": 0, "y": 70, "z": 0},
            "health": "unknown", "is_dead": False,
            "dimension": "minecraft:overworld",
        },
    ],
    ids=["missing-state", "missing-position", "malformed-health"],
)
def test_missing_or_malformed_player_telemetry_fails_closed(world, bad_state):
    world.state_response = bad_state
    miner = make_miner(world, patience=0)

    with pytest.raises(tm.MineAbort):
        miner.check()


@pytest.mark.parametrize(
    "coordinate",
    [True, float("inf"), float("nan")],
    ids=["boolean", "infinite", "not-a-number"],
)
def test_boolean_or_nonfinite_player_coordinates_fail_closed(world, coordinate):
    world.state_response = {
        "block_position": {"x": coordinate, "y": 70, "z": 0},
        "health": 20, "is_dead": False, "dimension": "minecraft:overworld",
    }

    with pytest.raises(tm.MineAbort, match="player telemetry invalid"):
        make_miner(world).check()


@pytest.mark.parametrize(
    "response",
    [
        {"success": False, "data": {
            "block_position": {"x": 0, "y": 70, "z": 0}, "health": 20,
            "is_dead": False, "dimension": "minecraft:overworld",
        }},
        {"status": "error", "data": {
            "block_position": {"x": 0, "y": 70, "z": 0}, "health": 20,
            "is_dead": False, "dimension": "minecraft:overworld",
        }},
        {"data": {"success": False,
                   "block_position": {"x": 0, "y": 70, "z": 0}, "health": 20,
                   "is_dead": False, "dimension": "minecraft:overworld"}},
        {"data": {"error": "state query failed",
                   "block_position": {"x": 0, "y": 70, "z": 0}, "health": 20,
                   "is_dead": False, "dimension": "minecraft:overworld"}},
    ],
    ids=["raw-success-false", "raw-status-error", "data-success-false", "data-error"],
)
def test_state_error_envelopes_fail_closed_even_with_valid_state(world, response):
    world.state_response = response

    with pytest.raises(tm.MineAbort, match="telemetry error"):
        make_miner(world).check()


def test_success_status_wrappers_remain_valid_for_state_and_entity_reads(world):
    world.state_response = {
        "status": "ok",
        "data": {
            "block_position": {"x": 0, "y": 70, "z": 0}, "health": 20,
            "is_dead": False, "dimension": "minecraft:overworld",
        },
    }
    world.entity_response = {"status": "ok", "data": {"entities": []}}

    miner = make_miner(world)
    assert miner.check()["cell"] == ENTRANCE
    assert miner.hostiles() == 0


def test_player_state_query_error_fails_closed(world):
    world.state_error = True

    with pytest.raises(tm.MineAbort):
        make_miner(world).check()


def test_low_health_and_the_clock_stop_the_trip(world):
    world.blocks[(6, 55, 3)] = IRON
    world.health = 9.0
    with pytest.raises(tm.MineAbort) as stop:
        make_miner(world).mine(1, lambda: world.raw_iron)
    assert stop.value.reason == "health low"

    world.health = 20.0
    with pytest.raises(tm.MineAbort) as stop:
        make_miner(world, seconds=-1.0).mine(1, lambda: world.raw_iron)
    assert stop.value.reason == "time"


def test_a_structure_block_in_the_way_aborts_instead_of_breaking_it(world):
    world.blocks[(1, 70, 0)] = "minecraft:chest"
    miner = make_miner(world)
    move = tp.Move(ENTRANCE, (1, 70, 0), ((1, 70, 0), (1, 71, 0)), ((1, 70, 0),))
    with pytest.raises(tm.MineAbort) as stop:
        miner.advance(move)
    assert stop.value.reason.startswith("unexpected block") and world.dug == []


def test_a_recorded_spine_is_walked_and_its_cells_count_as_our_own(world):
    # A tunnel from an earlier trip: its open cells must not read as a cave.
    spine = [(0, 70, 0), (1, 69, 0), (2, 68, 0), (3, 67, 0)]
    for frm, to in zip(spine, spine[1:]):
        for cell in tp.required_cells(frm, to):
            world.blocks.pop(cell, None)
    miner = make_miner(world, spine=spine)

    assert tm.own_cells_of(spine) >= {(2, 68, 0), (2, 69, 0), (3, 67, 0), (3, 68, 0)}
    miner.descend(spine)
    assert world.pos == (3, 67, 0)
    assert world.allow_break == "true"


def test_descent_and_retreat_attach_a_fresh_corridor_defense_to_each_hop(world, monkeypatch):
    from baritone_client.common.tunnel_travel_defense import CorridorStepDefense

    spine = [(0, 70, 0), (1, 69, 0), (2, 68, 0)]
    for frm, to in zip(spine, spine[1:]):
        for cell in tp.required_cells(frm, to):
            world.blocks.pop(cell, None)
    real_goto = world.goto
    callbacks = []

    def supervised(client, x, y, z, **kwargs):
        callback = kwargs.get("on_defense")
        assert isinstance(callback, CorridorStepDefense)
        callbacks.append(((x, y, z), callback.origin, callback.target))
        return real_goto(client, x, y, z, **kwargs)

    monkeypatch.setattr("baritone_client.common.navigation.goto", supervised)
    miner = make_miner(world, spine=spine)

    miner.descend(spine)
    assert world.pos == spine[-1]
    assert miner.retreat() is True
    assert world.pos == spine[0]
    assert [call[0] for call in callbacks] == [spine[1], spine[2], spine[1], spine[0]]
    assert all(origin != target for _goal, origin, target in callbacks)


def test_spine_traversal_retries_a_transient_waypoint_failure_and_never_skips_cells(world, monkeypatch):
    spine = [(0, 70, 0), (1, 69, 0), (2, 68, 0)]
    for frm, to in zip(spine, spine[1:]):
        for cell in tp.required_cells(frm, to):
            world.blocks.pop(cell, None)
    real_goto = world.goto
    calls = []

    def transient(client, x, y, z, **kw):
        assert world.allow_break == "false"
        calls.append((x, y, z))
        if len(calls) == 1:
            return False
        return real_goto(client, x, y, z, **kw)

    monkeypatch.setattr("baritone_client.common.navigation.goto", transient)
    miner = make_miner(world, spine=spine)
    miner.descend(spine)

    assert calls == [(1, 69, 0), (1, 69, 0), (2, 68, 0)]
    assert world.pos == spine[-1] and world.allow_break == "true"


def test_allow_break_changes_are_observed_even_when_game_thread_applies_them_late(world, monkeypatch):
    spine = [(0, 70, 0), (1, 69, 0)]
    for cell in tp.required_cells(*spine):
        world.blocks.pop(cell, None)
    world.allow_break_apply_after_reads = 2
    real_goto = world.goto

    def observe_disabled(client, *args, **kwargs):
        assert world.allow_break == "false"
        return real_goto(client, *args, **kwargs)

    monkeypatch.setattr("baritone_client.common.navigation.goto", observe_disabled)
    miner = make_miner(world, spine=spine)

    miner.descend(spine)

    assert world.pos == spine[-1]
    assert world.allow_break == "true"
    assert world.allow_break_writes == ["false", "true"]


def test_allow_break_that_never_applies_refuses_tunnel_movement(world, monkeypatch):
    spine = [(0, 70, 0), (1, 69, 0)]
    for cell in tp.required_cells(*spine):
        world.blocks.pop(cell, None)
    world.never_apply_allow_break = True
    moves = []
    monkeypatch.setattr(
        "baritone_client.common.navigation.goto",
        lambda *_a, **_k: moves.append(1) or True,
    )
    miner = make_miner(world, spine=spine)

    with pytest.raises(tm.MineAbort, match="cannot verify digging is disabled"):
        miner.descend(spine)

    assert moves == []
    assert world.pos == spine[0]
    assert world.allow_break == "true"


def test_retreat_requires_exact_xyz_and_refuses_an_off_trail_position(world):
    spine = [(0, 70, 0), (1, 69, 0), (2, 68, 0)]
    for frm, to in zip(spine, spine[1:]):
        for cell in tp.required_cells(frm, to):
            world.blocks.pop(cell, None)
    miner = make_miner(world, spine=spine)
    miner.descend(spine)

    world.pos = (2, 68, 1)  # matching height is not proof of reaching the entrance
    assert miner.retreat() is False
    assert world.pos == (2, 68, 1)


def test_recorded_spine_refuses_a_floor_removed_since_last_trip(world):
    spine = [(0, 70, 0), (1, 69, 0)]
    for cell in tp.required_cells(*spine):
        world.blocks.pop(cell, None)
    world.blocks.pop((1, 68, 0), None)
    miner = make_miner(world, spine=spine)
    with pytest.raises(tm.MineAbort, match="no longer supported"):
        miner.descend(spine)
    assert world.pos == spine[0]
    assert world.dug == []
    assert world.allow_break == "true"


def _open_spine(world, spine):
    for frm, to in zip(spine, spine[1:]):
        for cell in tp.required_cells(frm, to):
            world.blocks.pop(cell, None)


MID_SPINE = [(0, 70, 0), (1, 69, 0), (2, 68, 0), (3, 67, 0), (4, 66, 0), (5, 65, 0)]


def test_a_step_broken_partway_down_mines_from_the_last_good_step(world, capsys):
    """Live A1 2026-11-02: 83 steps down, two floorless steps, back with 0 iron."""
    _open_spine(world, MID_SPINE)
    world.blocks.pop((4, 65, 0))  # step 4 lost its floor
    world.blocks[(3, 55, 3)] = IRON
    miner = make_miner(world, spine=MID_SPINE)

    miner.descend(MID_SPINE)

    assert world.pos == (3, 67, 0) and world.allow_break == "true"
    assert miner.trail == MID_SPINE[:4]
    assert "saved route broken at step 4 (4, 66, 0); mining from step 3 (3, 67, 0)" in capsys.readouterr().out
    assert miner.mine(1, lambda: world.raw_iron) == "quota"
    assert (4, 65, 0) not in {cell for cell, _b in world.dug}
    assert miner.retreat() is True and world.pos == MID_SPINE[0]


@pytest.mark.parametrize("unread", ["minecraft:void_air", ""])
def test_an_unread_step_is_not_proof_the_route_is_broken(world, unread):
    _open_spine(world, MID_SPINE)
    world.blocks[(4, 65, 0)] = unread
    miner = make_miner(world, spine=MID_SPINE)

    with pytest.raises(tm.MineAbort, match="no longer supported") as raised:
        miner.descend(MID_SPINE)
    assert not isinstance(raised.value, tm.RouteBroken) or raised.value.index < 1
    assert miner.trail == MID_SPINE


def test_retreat_never_turns_a_broken_step_into_mining(world):
    _open_spine(world, MID_SPINE)
    miner = make_miner(world, spine=MID_SPINE)
    miner.descend(MID_SPINE)
    world.blocks.pop((2, 67, 0))  # a step behind the bot lost its floor

    assert miner.retreat() is False
    assert miner.trail == MID_SPINE


def test_retreat_walks_each_recorded_cell_and_proves_the_entrance(world):
    spine = [(0, 70, 0), (1, 69, 0), (2, 68, 0)]
    for frm, to in zip(spine, spine[1:]):
        for cell in tp.required_cells(frm, to):
            world.blocks.pop(cell, None)
    miner = make_miner(world, spine=spine)
    miner.descend(spine)

    assert miner.retreat() is True
    assert world.pos == spine[0] and world.allow_break == "true"


def test_long_verified_return_gets_time_for_each_checked_hop(world, monkeypatch):
    spine = [(x, 70, 0) for x in range(201)]
    for x, _, z in spine:
        world.blocks[(x, 69, z)] = STONE
    miner = make_miner(world, spine=spine)
    world.pos = spine[-1]
    clock = [1000.0]
    monkeypatch.setattr(tm.time, "monotonic", lambda: clock[0])
    real_goto = world.goto

    def slow_hop(*args, **kwargs):
        clock[0] += 10.0
        return real_goto(*args, **kwargs)

    monkeypatch.setattr("baritone_client.common.navigation.goto", slow_hop)
    assert miner.retreat()
    assert clock[0] == 3000.0  # exceeds the old twenty-minute cap
    assert world.pos == spine[0] and not world.dug


def test_travel_budget_covers_retained_routes_and_stays_bounded():
    assert tm.route_travel_seconds([ENTRANCE]) == 240
    assert tm.route_travel_seconds([ENTRANCE] * 400) == 4788
    assert tm.route_travel_seconds([ENTRANCE] * 1000) == 4800


@pytest.mark.parametrize("remaining", [0, 1, 16, 32, 33])
def test_dig_keeps_pickaxe_return_reserve(world, remaining):
    world.pick_damage = 131 - remaining
    target = (0, 69, 0)
    miner = make_miner(world)
    if remaining <= 32:
        with pytest.raises(tm.MineAbort, match="pickaxe"):
            miner.dig(target)
        assert world.dug == []
    else:
        miner.dig(target)
        assert len(world.dug) == 1


@pytest.mark.parametrize("case", ["missing", "boolean", "error", "stale", "selected", "wrong_tool", "mapping", "duplicate"])
def test_uncertain_held_tool_never_starts_a_dig(world, case):
    original = world.transport.dispatch
    def dispatch(route, payload):
        result = original(route, payload)
        if route == "get_inventory":
            if case == "missing": result["inventory"][0].pop("max_damage")
            elif case == "boolean": result["inventory"][0]["damage"] = False
            elif case == "error": result["success"] = False
            elif case == "stale": result["snapshot_valid"] = False
            elif case == "selected": result["selected_slot"] = False
            elif case == "wrong_tool": result["inventory"][0]["id"] = "minecraft:stone_sword"
            elif case == "mapping": result["inventory"] = {"minecraft:stone_pickaxe": 1}
            elif case == "duplicate": result["inventory"].append(dict(result["inventory"][0]))
        return result
    world.transport.dispatch = dispatch
    with pytest.raises(tm.MineAbort, match="durability unverified"):
        make_miner(world).dig((0, 69, 0))
    assert world.dug == []


def test_checkpoint_failure_does_not_interrupt_verified_return(world):
    spine = [(0, 70, 0), (1, 70, 0)]
    world.pos = spine[-1]

    def broken_checkpoint(_trail):
        raise OSError("disk unavailable")

    miner = make_miner(world, spine=spine, on_progress=broken_checkpoint)
    assert miner.retreat()
    assert world.pos == spine[0] and not world.dug


def test_adjacent_ore_under_a_recorded_route_cell_is_not_mined(world):
    ore = (1, 70, 0)
    world.blocks[ore] = IRON
    miner = make_miner(world)
    miner.trail.append((1, 71, 0))  # ore is the support floor for this old cell
    view = miner.view()

    assert not miner.mine_adjacent(view, ENTRANCE, lambda: world.raw_iron)
    assert world.blocks[ore] == IRON
    assert world.raw_iron == 0 and not world.dug


def test_advance_refuses_a_plan_that_would_remove_any_recorded_floor(world):
    miner = make_miner(world)
    world.pos = (1, 70, 0)
    miner.trail.append(world.pos)
    move = tp.Move(
        world.pos, (1, 70, 1), ((1, 70, 1), (1, 71, 1)),
        ((1, 69, 0),),
    )

    with pytest.raises(tm.MineAbort, match="recorded tunnel floor"):
        miner.advance(move)
    assert world.blocks[(1, 69, 0)] == "minecraft:grass_block"
    assert not world.dug


def test_torch_placement_gives_up_after_three_misses_but_mining_goes_on(world, monkeypatch):
    world.blocks[(6, 55, 3)] = IRON
    attempts = []
    monkeypatch.setattr(
        "baritone_client.common.harness_ops.place_block_exact",
        lambda *_a, **_k: attempts.append(1) or False,
    )
    miner = make_miner(world)

    miner.mine(1, lambda: world.raw_iron)

    assert world.raw_iron == 1 and len(attempts) == 3 and miner.stats.torches == 0


def test_rubble_is_thrown_away_when_the_pack_runs_low_and_the_trip_goes_on(world, monkeypatch):
    world.blocks[(6, 55, 3)] = IRON
    world.used_slots = 35  # only one slot free
    thrown = []

    def drop(_client, items, max_stacks=None, retain_counts=None):
        thrown.append(set(items))
        world.used_slots = 20
        return 8

    monkeypatch.setattr("baritone_client.common.inventory.drop_items", drop)

    miner = make_miner(world)
    miner.mine(1, lambda: world.raw_iron)

    assert world.raw_iron == 1 and thrown
    assert "minecraft:cobblestone" in thrown[0] and "minecraft:iron_ore" not in thrown[0]
    assert not any(item.endswith(("pickaxe", "sword", "torch", "ingot")) for item in thrown[0])


def test_a_pack_that_cannot_be_cleared_ends_the_trip_cleanly(world, monkeypatch):
    world.blocks[(6, 55, 3)] = IRON
    world.used_slots = 35
    monkeypatch.setattr("baritone_client.common.inventory.drop_items", lambda *_a, **_k: 0)
    assert make_miner(world).mine(1, lambda: world.raw_iron) == "inventory full"
    assert world.dug == []  # never starts digging with nowhere to put the drops


def test_invalid_inventory_snapshot_is_zero_free_slots_and_fails_closed():
    broken = SimpleNamespace(
        transport=SimpleNamespace(dispatch=lambda route, _payload: {} if route == "get_inventory" else {})
    )
    assert tm.free_slots(broken) == 0

    missing_slot = SimpleNamespace(transport=SimpleNamespace(dispatch=lambda route, _payload: {
        "inventory": [{"id": "minecraft:stone", "count": 64}]
    }))
    assert tm.free_slots(missing_slot) == 0


@pytest.mark.parametrize("flags", [{"snapshot_valid": False}, {"error": "unavailable"}])
def test_invalid_inventory_envelope_cannot_supply_capacity(flags):
    client = SimpleNamespace(transport=SimpleNamespace(dispatch=lambda *_a: {
        **flags, "data": {"inventory": []},
    }))
    assert tm.free_slots(client) == 0


def test_tunnel_travel_refuses_unverified_stop_and_keeps_digging_disabled(world):
    spine = [ENTRANCE, (1, 69, 0)]
    for cell in tp.required_cells(*spine):
        world.blocks.pop(cell, None)
    world.pathing = True
    miner = make_miner(world, spine=spine)
    with pytest.raises(tm.MineAbort, match="stop was not verified"):
        miner.descend(spine)
    assert world.allow_break == "false"
    # Owed, not silently dropped: the home-surface retry restores it.
    assert miner.client._home_route_owed == {"allowBreak": "true"}
