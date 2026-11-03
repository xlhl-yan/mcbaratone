from types import SimpleNamespace

import pytest

from baritone_client.common.farm_plot_evidence import verified_crop_plot_plane


ORIGIN = (31, 70, -12)


def _client(blocks):
    calls = []

    def dispatch(route, payload):
        assert route == "get_block"
        position = tuple(payload[axis] for axis in ("x", "y", "z"))
        calls.append(position)
        return blocks.get(position, {"id": "minecraft:air"})

    return SimpleNamespace(transport=SimpleNamespace(dispatch=dispatch)), calls


def _plot(*, water=(-2, 1), crops=None):
    if crops is None:
        crops = {
            (0, 0): "minecraft:wheat",
            (1, 0): "minecraft:carrots",
            (0, 1): "minecraft:potatoes",
        }
    blocks = {}
    for dx in range(-2, 3):
        for dz in range(-2, 3):
            blocks[(ORIGIN[0] + dx, ORIGIN[1], ORIGIN[2] + dz)] = {
                "id": "minecraft:farmland"
            }
            crop = crops.get((dx, dz))
            blocks[(ORIGIN[0] + dx, ORIGIN[1] + 1, ORIGIN[2] + dz)] = {
                "id": crop or "minecraft:air"
            }
    if water is not None:
        dx, dz = water
        blocks[(ORIGIN[0] + dx, ORIGIN[1], ORIGIN[2] + dz)] = {
            "id": "minecraft:water", "state": {"level": "0"}
        }
        blocks[(ORIGIN[0] + dx, ORIGIN[1] + 1, ORIGIN[2] + dz)] = {
            "id": "minecraft:air"
        }
    return blocks


def test_accepts_valid_mixed_crop_plot_with_off_center_source_water():
    client, calls = _client(_plot())

    assert verified_crop_plot_plane(client, ORIGIN)
    assert len(calls) == 50
    assert len(set(calls)) == 50


def test_accepts_valid_all_carrot_plot_without_requiring_wheat():
    crops = {
        (dx, dz): "minecraft:carrots"
        for dx in range(-2, 3)
        for dz in range(-2, 3)
    }
    client, _calls = _client(_plot(crops=crops))

    assert verified_crop_plot_plane(client, ORIGIN)


def test_rejects_stale_plane_one_block_below_real_plot():
    blocks = _plot()
    # At the stale height the expected crop layer is the actual farmland,
    # while the source water and crops are one block above the inspected soil.
    client, _calls = _client(blocks)

    stale_origin = (ORIGIN[0], ORIGIN[1] - 1, ORIGIN[2])
    assert not verified_crop_plot_plane(client, stale_origin)


def test_rejects_unirrigated_or_crop_empty_plot():
    no_water, _calls = _client(_plot(water=None))
    no_crops, _calls = _client(_plot(crops={}))

    assert not verified_crop_plot_plane(no_water, ORIGIN)
    assert not verified_crop_plot_plane(no_crops, ORIGIN)


@pytest.mark.parametrize(
    "water_state",
    [None, {"level": "1"}, {"level": 1}, {"level": True}],
)
def test_rejects_non_source_or_unverified_water(water_state):
    blocks = _plot(water=None)
    position = (ORIGIN[0] - 2, ORIGIN[1], ORIGIN[2] + 1)
    blocks[position] = {"id": "minecraft:water", "state": water_state}
    client, _calls = _client(blocks)

    assert not verified_crop_plot_plane(client, ORIGIN)


def test_rejects_any_unknown_or_error_block_read():
    blocks = _plot()
    bad_position = (ORIGIN[0] + 2, ORIGIN[1] + 1, ORIGIN[2] - 2)
    for unknown in (
        {"success": False, "status": "error", "error": "unavailable"},
        {"data": {"success": False, "error": "stale"}},
        {"data": {"id": ""}},
        {},
    ):
        client, _calls = _client(blocks)
        original_dispatch = client.transport.dispatch

        def dispatch(route, payload, *, unknown=unknown):
            position = tuple(payload[axis] for axis in ("x", "y", "z"))
            if position == bad_position:
                return unknown
            return original_dispatch(route, payload)

        client.transport.dispatch = dispatch
        assert not verified_crop_plot_plane(client, ORIGIN)


@pytest.mark.parametrize("origin", [None, (1, 2), (1, True, 3), (1, 2.0, 3)])
def test_rejects_invalid_origin_without_reading_world(origin):
    client, calls = _client(_plot())

    assert not verified_crop_plot_plane(client, origin)
    assert calls == []
