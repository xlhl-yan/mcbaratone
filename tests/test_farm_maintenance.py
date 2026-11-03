"""Post-harvest repairs and bounded selection of unusable sites."""

from types import SimpleNamespace

import pytest

from baritone_client.common import farming, home_surface
from baritone_client.automator.food_recovery_state import checkpointed_wheat_farm_origin
from baritone_client.automator.common import crop_opportunity


def _irrigated_blocks():
    blocks = {}
    for dx in range(-2, 3):
        for dz in range(-2, 3):
            blocks[(dx, 64, dz)] = {"id": "minecraft:farmland"}
            blocks[(dx, 65, dz)] = {"id": "minecraft:air"}
    blocks[(0, 64, 0)] = {
        "id": "minecraft:water", "state": {"level": "0"}
    }
    blocks[(-1, 65, 0)] = {
        "id": "minecraft:wheat", "state": {"age": "7"}
    }
    return blocks


def test_replant_repairs_only_bare_observed_soil(monkeypatch):
    blocks = {(1, 64, 0): "minecraft:grass_block",
              (-1, 64, 0): "minecraft:farmland",
              (-1, 65, 0): "minecraft:wheat",
              (0, 64, 1): "minecraft:farmland",
              (0, 65, 1): "minecraft:water"}
    monkeypatch.setattr(farming, "_block_id", lambda _c, x, y, z: blocks.get((x,y,z), "minecraft:air"))
    planted = []
    monkeypatch.setattr(
        farming, "plant_farm_tiles",
        lambda _c, tiles, **_kwargs: planted.extend(tiles) or len(tiles),
    )
    assert farming.replant_empty_wheat_tiles(None, 0, 64, 0) == 1
    assert planted == [(1, 64, 0)]


def test_replant_preserves_observed_carrot_plot_instead_of_seeding_wheat(monkeypatch):
    blocks = {
        (0, 65, 1): "minecraft:carrots",
        (1, 64, 0): "minecraft:farmland",
    }
    monkeypatch.setattr(
        farming, "_block_id",
        lambda _c, x, y, z: blocks.get((x, y, z), "minecraft:air"),
    )
    planted = []

    def plant(_client, tiles, *, crop_item):
        planted.append((tiles, crop_item))
        return 1

    monkeypatch.setattr(farming, "plant_farm_tiles", plant)

    assert farming.replant_empty_wheat_tiles(None, 0, 64, 0) == 1
    assert planted == [([(1, 64, 0)], "minecraft:carrot")]


def test_empty_legacy_wheat_plot_keeps_its_explicit_crop_default(monkeypatch):
    monkeypatch.setattr(
        farming, "_block_id",
        lambda _c, _x, y, _z: "minecraft:grass_block" if y == 64 else "minecraft:air",
    )
    selected = []
    monkeypatch.setattr(
        farming, "plant_farm_tiles",
        lambda _c, tiles, **kwargs: selected.append(kwargs["crop_item"]) or len(tiles),
    )

    assert farming.replant_empty_wheat_tiles(None, 0, 64, 0, size=3) == 8
    assert selected == ["minecraft:wheat_seeds"] * 8


@pytest.mark.parametrize("delta", [0, 1])
def test_harvest_stops_before_explicit_replant_and_requires_delta(monkeypatch, delta):
    calls = []
    clock = [0]
    blocks = _irrigated_blocks()

    def dispatch(route, payload):
        if route == "get_block":
            position = (payload["x"], payload["y"], payload["z"])
            return blocks.get(position, {"id": "minecraft:air"})
        calls.append(route)
        return {"cancelled": True} if route == "cancel" else {}

    client = SimpleNamespace(transport=SimpleNamespace(dispatch=dispatch))
    monkeypatch.setattr(farming, "goto", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "baritone_client.common.farm_crop_identity._capture_mature_wheat",
        lambda *_a: (),
    )
    monkeypatch.setattr(
        "baritone_client.common.farm_crop_identity._stopped_after_cancel",
        lambda *_a: True,
    )
    monkeypatch.setattr(farming.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(farming.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0]+seconds))
    monkeypatch.setattr(farming, "count_item", lambda *_a: 8 + (delta if clock[0] else 0))
    monkeypatch.setattr(farming, "replant_empty_wheat_tiles", lambda *_a: calls.append("replant"))
    assert farming.harvest_wheat_farm(client, 0, 64, 0) is bool(delta)
    assert calls == ["farm", "cancel", "replant"]


def test_failed_cancel_does_not_start_replant(monkeypatch):
    clock = [0]
    blocks = _irrigated_blocks()

    def dispatch(route, payload):
        if route == "get_block":
            position = (payload["x"], payload["y"], payload["z"])
            return blocks.get(position, {"id": "minecraft:air"})
        if route == "cancel":
            raise RuntimeError("unknown mutation outcome")
        return {}

    client = SimpleNamespace(transport=SimpleNamespace(dispatch=dispatch))
    monkeypatch.setattr(farming, "goto", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "baritone_client.common.farm_crop_identity._capture_mature_wheat",
        lambda *_a: (),
    )
    monkeypatch.setattr(farming.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(farming.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0]+seconds))
    monkeypatch.setattr(farming, "count_item", lambda *_a: clock[0])
    monkeypatch.setattr(farming, "replant_empty_wheat_tiles", lambda *_a: pytest.fail("must not overlap processes"))
    with pytest.raises(RuntimeError):
        farming.harvest_wheat_farm(client, 0, 64, 0)


def test_site_cooldown_selects_another_known_plot_then_expires(monkeypatch):
    now = [100]
    monkeypatch.setattr("baritone_client.automator.food_recovery_state.time.time", lambda: now[0])
    state = SimpleNamespace(custom_data={
        "farm_location": [0,64,0],
        "food_worker": {"farm_plots": [{"origin": [0,64,0]}, {"origin": [32,64,0]}]},
        "crop_site_cooldowns": {"0,64,0": 200},
    })
    assert checkpointed_wheat_farm_origin(state) == (32,64,0)
    now[0] = 201
    assert checkpointed_wheat_farm_origin(state) == (0,64,0)


def test_failed_crop_attempt_records_site_specific_cooldown(monkeypatch):
    state = SimpleNamespace(custom_data={})
    monkeypatch.setattr(crop_opportunity, "_run_crop_opportunity", lambda *_a, **_k: (False,"blocked",0,0))
    monkeypatch.setattr(crop_opportunity.time, "time", lambda: 100)
    crop_opportunity.run_crop_opportunity(None, SimpleNamespace(location=(0,64,0)), 1, state=state)
    assert state.custom_data["crop_site_cooldowns"] == {"0,64,0": 1000}


def test_surface_farm_outside_home_radius_is_still_no_dig(monkeypatch):
    settings = []
    client = SimpleNamespace(_protected_home_anchor=(0,65,0), transport=SimpleNamespace(dispatch=lambda *_a: {"is_pathing": False}))
    monkeypatch.setattr(home_surface, "_read_break_setting", lambda _c: "true")
    monkeypatch.setattr(home_surface, "_write_break_setting", lambda _c, value: settings.append(value))
    @home_surface.protect_home_route(surface_work=True)
    def farm(client, x, y, z):
        assert settings == ["false"]
        guard = home_surface.home_route_floor(client, x, y + 1, z)
        assert home_surface.below_home_surface(guard, {"x": x, "y": 60, "z": z})
        return True
    assert farm(client, 32,64,0)
    assert settings == ["false", "true"]
    assert client._protected_surface_work is None


@pytest.mark.parametrize("live", [{}, {"health":20,"air_supply":300,"block_position":{"x":0,"y":60,"z":0}}, {"health":10,"air_supply":300,"block_position":{"x":0,"y":65,"z":0}}])
def test_native_farm_safety_fails_closed_on_unknown_drop_or_injury(live):
    client = SimpleNamespace(_protected_surface_work=(0,63,0),
        transport=SimpleNamespace(dispatch=lambda *_a: live))
    assert not farming.farm_surface_safe(client)


def test_candidate_approach_keeps_home_surface_floor_outside_home_radius(monkeypatch):
    from baritone_client.common import farm_site_travel

    client = SimpleNamespace(_protected_home_anchor=(0,65,0),
        transport=SimpleNamespace(dispatch=lambda *_a: {"is_pathing": False}))
    monkeypatch.setattr(farm_site_travel, "horizontal_distance", lambda *_a: 32)
    monkeypatch.setattr(home_surface, "_read_break_setting", lambda _c: "false")
    observed = []
    monkeypatch.setattr("baritone_client.common.navigation.goto_xz", lambda c, *_a, **_k:
        observed.append(c._protected_surface_work) or True)
    farm_site_travel.approach_candidate(client, (32,64,0))
    assert observed == [(32,63,0)]
    assert client._protected_surface_work is None
