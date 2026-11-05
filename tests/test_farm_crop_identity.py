"""Sanitized controller tests for preserving the wheat plot's crop identity."""

from types import SimpleNamespace

import pytest

from baritone_client.common import farming
from baritone_client.common import farm_crop_identity as identity


def _crop(crop_id, age):
    return {"id": crop_id, "state": {"age": age}}


def _irrigated_plot_blocks(origin=(0, 64, 0)):
    x, y, z = origin
    blocks = {}
    for dx in range(-2, 3):
        for dz in range(-2, 3):
            blocks[(x + dx, y, z + dz)] = {"id": "minecraft:farmland"}
            blocks[(x + dx, y + 1, z + dz)] = {"id": "minecraft:air"}
    blocks[(x, y, z)] = {
        "id": "minecraft:water", "state": {"level": "0"}
    }
    blocks[(x - 1, y + 1, z)] = _crop("minecraft:wheat", 7)
    return blocks


def test_harvest_repairs_only_observed_mature_wheat_replaced_by_immature_crop(monkeypatch):
    position = (-1, 65, 0)
    mature_carrot = (1, 65, 0)
    blocks = _irrigated_plot_blocks()
    blocks.update({
        position: _crop("minecraft:wheat", 7),
        mature_carrot: _crop("minecraft:carrots", 7),
        (-1, 64, 0): {"id": "minecraft:farmland"},
    })
    calls = []
    wheat_count = [0]

    def dispatch(route, payload):
        calls.append((route, payload))
        if route == "get_block":
            pos = (payload["x"], payload["y"], payload["z"])
            return blocks.get(pos, {"id": "minecraft:air"})
        if route == "farm":
            blocks[position] = _crop("minecraft:carrots", 1)
            wheat_count[0] = 1
        if route == "cancel":
            return {"cancelled": True}
        if route == "get_state":
            return {
                "is_pathing": False,
                "is_on_ground": True,
                "position": {"x": -1.0, "y": 65.0, "z": 0.0},
            }
        if route == "dig_block":
            pos = (payload["x"], payload["y"], payload["z"])
            blocks[pos] = {"id": "minecraft:air"}
        return {}

    client = SimpleNamespace(transport=SimpleNamespace(dispatch=dispatch))
    monkeypatch.setattr(farming, "goto", lambda *_a, **_k: True)
    def count_item(_client, item):
        return wheat_count[0] if item == "minecraft:wheat" else 1

    monkeypatch.setattr(farming, "count_item", count_item)
    monkeypatch.setattr(farming, "farm_surface_safe", lambda *_a: True)
    monkeypatch.setattr(
        "baritone_client.common.farm_planting._within_block_reach",
        lambda *_a: True,
    )

    planted = []

    def plant(_client, x, y, z):
        planted.append((x, y, z))
        blocks[(x, y + 1, z)] = _crop("minecraft:wheat", 0)
        return True

    monkeypatch.setattr(farming, "_till_and_plant_tile", plant)
    monkeypatch.setattr(farming, "replant_empty_wheat_tiles", lambda *_a: 0)
    monkeypatch.setattr(identity.time, "sleep", lambda _seconds: None)

    assert identity.run_wheat_farm_harvest(client, 0, 64, 0, 8) is True
    assert blocks[position]["id"] == "minecraft:wheat"
    assert blocks[mature_carrot]["id"] == "minecraft:carrots"
    assert planted == [(-1, 64, 0)]
    digs = [payload for route, payload in calls if route == "dig_block"]
    assert digs == [{"x": -1, "y": 65, "z": 0, "face": "UP", "max_ticks": 80}]


def test_crop_snapshot_fails_closed_on_an_unknown_block_read():
    def dispatch(route, _payload):
        if route == "get_block":
            return {}
        return {}

    client = SimpleNamespace(transport=SimpleNamespace(dispatch=dispatch))

    assert identity._capture_mature_wheat(client, 0, 64, 0) is None


def test_uncertain_native_farm_start_is_cancelled_without_crop_repair(monkeypatch):
    calls = []
    blocks = _irrigated_plot_blocks()

    def dispatch(route, payload):
        calls.append(route)
        if route == "get_block":
            pos = (payload["x"], payload["y"], payload["z"])
            return blocks.get(pos, {"id": "minecraft:air"})
        if route == "farm":
            raise TimeoutError("response lost after transmission")
        return {"cancelled": True}

    client = SimpleNamespace(transport=SimpleNamespace(dispatch=dispatch))
    monkeypatch.setattr(farming, "goto", lambda *_a, **_k: True)
    monkeypatch.setattr(identity, "_capture_mature_wheat", lambda *_a: ())
    monkeypatch.setattr(farming, "count_item", lambda *_a: 0)
    assert identity.run_wheat_farm_harvest(client, 0, 64, 0, 8) is False
    assert calls[-2:] == ["farm", "cancel"]


@pytest.mark.parametrize(
    "cancel_response",
    [
        {"cancelled": False},
        {"success": False, "cancelled": True},
    ],
)
def test_unacknowledged_cancel_never_repairs_crop_identity(
    monkeypatch, cancel_response
):
    position = (-1, 65, 0)
    blocks = _irrigated_plot_blocks()
    blocks[position] = _crop("minecraft:wheat", 7)
    calls = []
    farm_started = [False]

    def dispatch(route, payload):
        calls.append(route)
        if route == "get_block":
            pos = (payload["x"], payload["y"], payload["z"])
            return blocks.get(pos, {"id": "minecraft:air"})
        if route == "farm":
            blocks[position] = _crop("minecraft:carrots", 1)
            farm_started[0] = True
        if route == "cancel":
            return cancel_response
        return {}

    client = SimpleNamespace(transport=SimpleNamespace(dispatch=dispatch))
    monkeypatch.setattr(farming, "goto", lambda *_a, **_k: True)
    monkeypatch.setattr(
        farming, "count_item",
        lambda *_a: 1 if farm_started[0] else 0,
    )
    monkeypatch.setattr(farming, "farm_surface_safe", lambda *_a: True)
    monkeypatch.setattr(identity.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        farming,
        "replant_empty_wheat_tiles",
        lambda *_a: (_ for _ in ()).throw(AssertionError("must wait for confirmed stop")),
    )

    assert identity.run_wheat_farm_harvest(client, 0, 64, 0, 8) is False
    assert "dig_block" not in calls


def test_stale_height_fails_closed_before_crop_snapshot_or_native_harvest(monkeypatch):
    actual_plot_y = 65
    blocks = _irrigated_plot_blocks((0, actual_plot_y, 0))
    calls = []

    def dispatch(route, payload):
        calls.append(route)
        if route == "get_block":
            pos = (payload["x"], payload["y"], payload["z"])
            return blocks.get(pos, {"id": "minecraft:air"})
        return {}

    client = SimpleNamespace(transport=SimpleNamespace(dispatch=dispatch))
    monkeypatch.setattr(farming, "goto", lambda *_a, **_k: True)
    monkeypatch.setattr(
        identity,
        "_capture_mature_wheat",
        lambda *_a: pytest.fail("must reject stale height before crop snapshot"),
    )
    monkeypatch.setattr(
        farming,
        "count_item",
        lambda *_a: pytest.fail("must reject stale height before inventory reads"),
    )

    assert identity.run_wheat_farm_harvest(client, 0, 64, 0, 8) is False
    assert "farm" not in calls
    assert "cancel" not in calls


def _repair_fixture(monkeypatch, crop_id="minecraft:carrots", age=1):
    position = (-1, 65, 0)
    ground = (-1, 64, 0)
    blocks = {position: _crop(crop_id, age), ground: {"id": "minecraft:farmland"}}
    calls = []

    def dispatch(route, payload):
        calls.append((route, payload))
        if route == "get_state":
            return client.synthetic_state[0]
        if route == "get_block":
            pos = (payload["x"], payload["y"], payload["z"])
            return blocks.get(pos, {"id": "minecraft:air"})
        if route == "dig_block":
            blocks[position] = {"id": "minecraft:air"}
        return {}

    client = SimpleNamespace(transport=SimpleNamespace(dispatch=dispatch))
    client.synthetic_state = [{
        "position": {"x": -1.0, "y": 65.0, "z": 0.0},
        "is_on_ground": True,
        "is_pathing": False,
    }]
    monkeypatch.setattr(farming, "farm_surface_safe", lambda *_a: True)
    monkeypatch.setattr(
        "baritone_client.common.farm_planting._within_block_reach",
        lambda *_a: True,
    )
    monkeypatch.setattr(identity, "_stopped_after_cancel", lambda *_a: True)
    monkeypatch.setattr(
        farming, "count_item",
        lambda _client, item: 1 if item in {
            "minecraft:wheat_seeds", "minecraft:carrot",
        } else 0,
    )

    def plant(_client, x, y, z):
        blocks[(x, y + 1, z)] = _crop("minecraft:wheat", 0)
        return True

    monkeypatch.setattr(farming, "_till_and_plant_tile", plant)
    return client, position, ground, blocks, calls


@pytest.mark.parametrize("condition", ["no-seeds", "unreachable", "changed-soil", "mature-crop"])
def test_repair_requires_seed_reach_unchanged_farmland_and_immature_crop(
    monkeypatch, condition
):
    client, position, ground, blocks, calls = _repair_fixture(
        monkeypatch, age=7 if condition == "mature-crop" else 1,
    )
    if condition == "no-seeds":
        monkeypatch.setattr(
            farming, "count_item",
            lambda _client, item: 1 if item == "minecraft:carrot" else 0,
        )
    elif condition == "unreachable":
        monkeypatch.setattr(
            "baritone_client.common.farm_planting._within_block_reach",
            lambda *_a: False,
        )
    elif condition == "changed-soil":
        blocks[ground] = {"id": "minecraft:dirt"}

    identity._restore_converted_wheat(client, [position])
    assert not any(route == "dig_block" for route, _payload in calls), condition


def test_failed_crop_dig_does_not_attempt_to_plant(monkeypatch):
    client, position, _ground, blocks, calls = _repair_fixture(monkeypatch)
    dispatch = client.transport.dispatch

    def failed_dig(route, payload):
        if route == "dig_block":
            calls.append((route, payload))
            return {"success": False, "error": "could not start"}
        return dispatch(route, payload)

    client.transport.dispatch = failed_dig
    monkeypatch.setattr(farming, "_till_and_plant_tile", lambda *_a: pytest.fail("crop remains"))
    clock = [0.0]
    monkeypatch.setattr(identity.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        identity.time, "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    identity._restore_converted_wheat(client, [position])

    assert blocks[position]["id"] == "minecraft:carrots"
    assert any(route == "dig_block" for route, _payload in calls)


def test_failed_wheat_plant_attempt_restores_original_crop(monkeypatch):
    client, position, ground, blocks, calls = _repair_fixture(monkeypatch)
    def failed_wheat_plant(*_args):
        raise RuntimeError("InteractionRejected: target face unavailable")

    monkeypatch.setattr(farming, "_till_and_plant_tile", failed_wheat_plant)

    def restore_crop(_client, x, y, z, crop_item):
        assert crop_item == "minecraft:carrot"
        blocks[(x, y + 1, z)] = _crop("minecraft:carrots", 0)
        return True

    monkeypatch.setattr(
        "baritone_client.common.farm_planting._plant_non_wheat_crop",
        restore_crop,
    )

    assert identity._restore_converted_wheat(client, [position]) == 0
    assert blocks[position]["id"] == "minecraft:carrots"
    assert blocks[ground]["id"] == "minecraft:farmland"
    assert any(route == "dig_block" for route, _payload in calls)


@pytest.mark.parametrize("starting_position", ["below", "airborne"])
def test_unsafe_stand_navigates_to_crop_height_then_reproves_before_dig(
    monkeypatch, starting_position
):
    client, position, _ground, _blocks, calls = _repair_fixture(monkeypatch)
    if starting_position == "below":
        client.synthetic_state[0]["position"]["y"] = 60.0
    else:
        client.synthetic_state[0]["is_on_ground"] = False
    goto_calls = []

    def goto(_client, x, y, z, **kwargs):
        goto_calls.append(((x, y, z), kwargs))
        client.synthetic_state[0]["position"]["y"] = 65.0
        client.synthetic_state[0]["is_on_ground"] = True
        return True

    monkeypatch.setattr(farming, "goto", goto)

    identity._restore_converted_wheat(client, [position])

    assert goto_calls == [
        ((-1, 65, 0), {"timeout": 30, "tolerance": 1.1, "radius": 0})
    ]
    assert any(route == "dig_block" for route, _payload in calls)


@pytest.mark.parametrize("changed_fact", ["crop", "soil", "grounded", "stopped"])
def test_navigation_must_reprove_crop_soil_and_safe_stopped_pose(
    monkeypatch, changed_fact
):
    client, position, ground, blocks, calls = _repair_fixture(monkeypatch)
    client.synthetic_state[0]["position"]["y"] = 60.0

    def goto(_client, *_args, **_kwargs):
        client.synthetic_state[0]["position"]["y"] = 65.0
        if changed_fact == "crop":
            blocks[position] = _crop("minecraft:carrots", 7)
        elif changed_fact == "soil":
            blocks[ground] = {"id": "minecraft:dirt"}
        elif changed_fact == "grounded":
            client.synthetic_state[0]["is_on_ground"] = False
        elif changed_fact == "stopped":
            monkeypatch.setattr(
                identity, "_stopped_after_cancel", lambda *_a: False
            )
        return True

    monkeypatch.setattr(farming, "goto", goto)

    identity._restore_converted_wheat(client, [position])

    assert not any(route == "dig_block" for route, _payload in calls), changed_fact


def test_refused_navigation_from_low_stand_does_not_dig(monkeypatch):
    client, position, _ground, _blocks, calls = _repair_fixture(monkeypatch)
    client.synthetic_state[0]["position"]["y"] = 60.0
    monkeypatch.setattr(farming, "goto", lambda *_a, **_k: False)

    identity._restore_converted_wheat(client, [position])

    assert not any(route == "dig_block" for route, _payload in calls)


def test_block_reads_reject_error_envelopes_at_both_levels():
    for response in (
        {"success": False, "data": {"id": "minecraft:wheat"}},
        {"data": {"success": False, "id": "minecraft:wheat"}},
        {"status": "error", "data": {"id": "minecraft:wheat"}},
        {"data": {"status": "ERROR", "id": "minecraft:wheat"}},
        {"error": "unavailable", "data": {"id": "minecraft:wheat"}},
    ):
        client = SimpleNamespace(
            transport=SimpleNamespace(dispatch=lambda *_a: response)
        )
        assert identity._read_block(client, (0, 64, 0)) is None


def test_stopped_state_rejects_error_envelopes_and_still_pathing(monkeypatch):
    responses = iter((
        {"success": False, "data": {"is_pathing": False}},
        {"data": {"status": "error", "is_pathing": False}},
        {"is_pathing": True},
    ))
    client = SimpleNamespace(
        transport=SimpleNamespace(
            dispatch=lambda route, _payload: next(responses) if route == "get_state" else {}
        )
    )
    clock = [0.0]
    monkeypatch.setattr(identity.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        identity.time, "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    assert identity._stopped_after_cancel(client, timeout=0.25) is False


def test_age_parser_rejects_boolean_fractional_and_out_of_range_ages():
    for value in (True, 7.5, -1, 8, 10**100, "7.0", "8"):
        assert identity._age({"state": {"age": value}}) is None
    assert identity._age({"state": {"age": "7"}}) == 7
