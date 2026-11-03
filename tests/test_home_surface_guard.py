"""Homeward surface routes must not accept the cave below home as arrival."""

from types import SimpleNamespace

import pytest

from baritone_client.common import combat, home_surface, navigation


@pytest.mark.parametrize("horizontal", [False, True])
def test_homeward_route_cancels_on_drop_before_defense_or_arrival(monkeypatch, horizontal):
    calls = []
    reads = 0
    settings = {
        "allowBreak": "true",
        "allowParkour": "true",
        "maxFallHeightNoWater": "3",
    }

    def dispatch(route, payload, **kwargs):
        nonlocal reads
        calls.append((route, payload))
        if route == "settings":
            name = payload.get("get", payload.get("set"))
            if "set" in payload:
                settings[name] = payload["value"]
            return {"key": name, "value": settings[name]}
        if route == "get_state":
            reads += 1
            return {"health": 20, "food_level": 20, "is_pathing": False,
                    "block_position": {"x": 100, "y": 64 if reads == 1 else 61, "z": 100}}
        return {}

    client = SimpleNamespace(transport=SimpleNamespace(dispatch=dispatch),
                             _protected_home_anchor=(100, 64, 100))
    monkeypatch.setattr(combat, "survival_tick", lambda *_a: pytest.fail("guard must run first"))
    result = (navigation.goto_xz(client, 100, 100) if horizontal else
              navigation.goto(client, 100, 64, 100, tolerance=8))
    assert not result
    assert client._last_navigation_outcome == "home_surface_abort"
    assert sum(route == "cancel" for route, _ in calls) == 1
    assert settings == {
        "allowBreak": "true",
        "allowParkour": "true",
        "maxFallHeightNoWater": "3",
    }


def test_guard_preserves_explicit_mining_and_outside_home_routes():
    client = SimpleNamespace(_protected_home_anchor=(100, 64, 100))
    assert home_surface.home_route_floor(client, 100, 50, 100) is None
    assert home_surface.home_route_floor(client, 160, 64, 100) is None
    floor = home_surface.home_route_floor(client, 100, 64, 100)
    assert not home_surface.below_home_surface(floor, {"x": 160, "y": 50, "z": 100})
    assert home_surface.below_home_surface(floor, {"x": 100, "y": 61, "z": 100})


@pytest.mark.parametrize("anchor", [[1, float("nan"), 1], [True, 64, 1], [1, 2], None])
def test_invalid_anchor_is_not_installed(anchor):
    client = SimpleNamespace(_protected_home_anchor=(100, 64, 100))
    home_surface.bind_home_surface(client, SimpleNamespace(custom_data={"base_location": anchor}))
    assert client._protected_home_anchor is None


def test_unverified_setting_does_not_start_movement():
    calls = []
    client = SimpleNamespace(_protected_home_anchor=(100, 64, 100),
        transport=SimpleNamespace(dispatch=lambda r, p: calls.append((r, p)) or {}))
    assert not navigation.goto(client, 100, 64, 100)
    assert not any(r == "goto" for r, _ in calls)


def test_requested_setting_without_application_does_not_start(monkeypatch):
    calls = []
    def dispatch(route, payload):
        calls.append((route, payload))
        if route == "settings":
            return {"value": "true"}
        return {"is_pathing": False}
    client = SimpleNamespace(_protected_home_anchor=(100, 64, 100),
                             transport=SimpleNamespace(dispatch=dispatch))
    monkeypatch.setattr(home_surface.time, "sleep", lambda *_a: None)
    assert not navigation.goto(client, 100, 64, 100)
    assert not any(r == "goto" for r, _ in calls)


def test_unverified_stop_keeps_digging_disabled():
    setting = "true"
    def dispatch(route, payload):
        nonlocal setting
        if route == "settings":
            setting = payload.get("value", setting)
            return {"value": setting}
        return {"is_pathing": True}
    client = SimpleNamespace(_protected_home_anchor=(100, 64, 100),
                             transport=SimpleNamespace(dispatch=dispatch))
    @home_surface.protect_home_route()
    def uncertain_route(client, x, y, z):
        return False
    assert not uncertain_route(client, 100, 64, 100)
    assert setting == "false"


def test_safe_movement_policy_is_verified_before_route_and_restored_after_stop():
    settings = {
        "allowBreak": "true",
        "allowParkour": "true",
        "maxFallHeightNoWater": "3",
    }
    calls = []

    def dispatch(route, payload):
        calls.append((route, payload))
        if route == "settings":
            name = payload.get("get", payload.get("set"))
            if "set" in payload:
                settings[name] = payload["value"]
            return {"key": name, "value": settings[name]}
        if route == "get_state":
            return {"is_pathing": False}
        return {}

    client = SimpleNamespace(
        transport=SimpleNamespace(dispatch=dispatch),
        _protected_home_anchor=(1, 64, 1),
    )

    @home_surface.protect_home_route(safe_movement=True)
    def route(client, x, y, z):
        assert settings == {
            "allowBreak": "false",
            "allowParkour": "false",
            "maxFallHeightNoWater": "1",
        }
        return True

    assert route(client, 1, 64, 1)
    assert settings == {
        "allowBreak": "true",
        "allowParkour": "true",
        "maxFallHeightNoWater": "3",
    }
    first_update = next(i for i, (r, p) in enumerate(calls) if r == "settings" and "set" in p)
    assert all(r == "settings" for r, _p in calls[:first_update])
    assert calls[first_update][1] == {"set": "allowBreak", "value": "false"}
    assert calls[-1][0] == "settings"


def test_safe_movement_settings_are_untouched_outside_the_home_guard():
    settings = {"allowBreak": "true", "allowParkour": "true", "maxFallHeightNoWater": "3"}
    calls = []

    def dispatch(route, payload):
        calls.append((route, payload))
        if route == "settings":
            name = payload.get("get", payload.get("set"))
            if "set" in payload:
                settings[name] = payload["value"]
            return {"key": name, "value": settings[name]}
        return {"is_pathing": False}

    client = SimpleNamespace(
        transport=SimpleNamespace(dispatch=dispatch),
        _protected_home_anchor=(100, 64, 100),
    )

    @home_surface.protect_home_route(safe_movement=True)
    def route(client, x, y, z):
        return True

    assert route(client, 200, 64, 200)
    assert not calls
    assert settings == {
        "allowBreak": "true",
        "allowParkour": "true",
        "maxFallHeightNoWater": "3",
    }


@pytest.mark.parametrize("bad", [None, "unknown", "NaN", "1.5"])
def test_safe_movement_unknown_or_malformed_prior_value_refuses_route(bad, monkeypatch):
    settings = {"allowBreak": "true", "allowParkour": "true", "maxFallHeightNoWater": bad}
    calls = []

    def dispatch(route, payload):
        calls.append((route, payload))
        if route == "settings":
            name = payload.get("get", payload.get("set"))
            return {"key": name, "value": settings[name]}
        return {"is_pathing": False}

    monkeypatch.setattr(home_surface.time, "sleep", lambda *_a: None)
    client = SimpleNamespace(
        transport=SimpleNamespace(dispatch=dispatch),
        _protected_home_anchor=(1, 64, 1),
    )

    @home_surface.protect_home_route(safe_movement=True)
    def route(client, x, y, z):
        pytest.fail("unsafe settings must not start movement")

    assert not route(client, 1, 64, 1)
    assert not any(
        r == "settings"
        and ((p.get("set") == "allowParkour" and p.get("value") == "false")
             or (p.get("set") == "maxFallHeightNoWater" and p.get("value") == "1"))
        for r, p in calls
    )
    assert settings["allowBreak"] == "true"


def test_safe_movement_lost_readback_refuses_route_and_restores_after_stop(monkeypatch):
    settings = {
        "allowBreak": "true",
        "allowParkour": "true",
        "maxFallHeightNoWater": "3",
    }
    calls = []

    def dispatch(route, payload):
        calls.append((route, payload))
        if route == "settings":
            name = payload.get("get", payload.get("set"))
            if "set" in payload and name != "allowParkour":
                settings[name] = payload["value"]
            return {"key": name, "value": settings[name]}
        if route == "get_state":
            return {"is_pathing": False}
        return {}

    monkeypatch.setattr(home_surface.time, "sleep", lambda *_a: None)
    client = SimpleNamespace(
        transport=SimpleNamespace(dispatch=dispatch),
        _protected_home_anchor=(1, 64, 1),
    )

    @home_surface.protect_home_route(safe_movement=True)
    def route(client, x, y, z):
        pytest.fail("movement must not start without the requested readback")

    assert not route(client, 1, 64, 1)
    assert settings == {
        "allowBreak": "true",
        "allowParkour": "true",
        "maxFallHeightNoWater": "3",
    }
    assert not any(
        r == "settings" and p.get("set") == "maxFallHeightNoWater" and p.get("value") == "1"
        for r, p in calls
    )


@pytest.mark.parametrize(
    "failure",
    [
        {"success": False, "key": "allowParkour", "value": "true"},
        {"status": "error", "data": {"key": "allowParkour", "value": "true"}},
        {"data": {"error": "read failed", "key": "allowParkour", "value": "true"}},
    ],
    ids=["raw-success-false", "raw-status-error", "nested-error"],
)
def test_safe_movement_failed_setting_envelopes_refuse_route(failure, monkeypatch):
    settings = {"allowBreak": "true", "allowParkour": "true", "maxFallHeightNoWater": "3"}
    calls = []

    def dispatch(route, payload):
        calls.append((route, payload))
        if route == "settings":
            name = payload.get("get", payload.get("set"))
            if "set" in payload:
                settings[name] = payload["value"]
            if payload.get("get") == "allowParkour":
                return failure
            return {"key": name, "value": settings[name]}
        if route == "get_state":
            return {"is_pathing": False}
        return {}

    monkeypatch.setattr(home_surface.time, "sleep", lambda *_a: None)
    client = SimpleNamespace(
        transport=SimpleNamespace(dispatch=dispatch),
        _protected_home_anchor=(1, 64, 1),
    )

    @home_surface.protect_home_route(safe_movement=True)
    def route(client, x, y, z):
        pytest.fail("failed setting telemetry must not start movement")

    assert not route(client, 1, 64, 1)
    assert settings["allowParkour"] == "true"
    assert not any(
        r == "settings" and p.get("set") in {"allowParkour", "maxFallHeightNoWater"}
        for r, p in calls
    )


@pytest.mark.parametrize(
    "failure",
    [
        {"success": False, "key": "allowBreak", "value": "true"},
        {"data": {"status": "error", "key": "allowBreak", "value": "true"}},
    ],
    ids=["raw-failure", "nested-failure"],
)
def test_safe_movement_failed_allow_break_read_refuses_route(failure):
    calls = []

    def dispatch(route, payload):
        calls.append((route, payload))
        if route == "settings" and payload.get("get") == "allowBreak":
            return failure
        return {"is_pathing": False}

    client = SimpleNamespace(
        transport=SimpleNamespace(dispatch=dispatch),
        _protected_home_anchor=(1, 64, 1),
    )

    @home_surface.protect_home_route(safe_movement=True)
    def route(client, x, y, z):
        pytest.fail("failed allowBreak telemetry must not start movement")

    assert not route(client, 1, 64, 1)
    assert not any(r == "settings" and "set" in p for r, p in calls)


@pytest.mark.parametrize("pathing", [True, None])
def test_safe_movement_settings_stay_conservative_without_verified_stop(pathing):
    settings = {
        "allowBreak": "true",
        "allowParkour": "true",
        "maxFallHeightNoWater": "3",
    }

    def dispatch(route, payload):
        if route == "settings":
            name = payload.get("get", payload.get("set"))
            if "set" in payload:
                settings[name] = payload["value"]
            return {"key": name, "value": settings[name]}
        if route == "get_state":
            return {} if pathing is None else {"is_pathing": pathing}
        return {}

    client = SimpleNamespace(
        transport=SimpleNamespace(dispatch=dispatch),
        _protected_home_anchor=(1, 64, 1),
    )

    @home_surface.protect_home_route(safe_movement=True)
    def uncertain_route(client, x, y, z):
        return True

    assert not uncertain_route(client, 1, 64, 1)
    assert settings == {
        "allowBreak": "false",
        "allowParkour": "false",
        "maxFallHeightNoWater": "1",
    }


@pytest.mark.parametrize(
    "failed_state",
    [
        {"success": False, "is_pathing": False},
        {"status": "error", "data": {"is_pathing": False}},
        {"data": {"error": "state read failed", "is_pathing": False}},
    ],
    ids=["raw-success-false", "raw-status-error", "nested-error"],
)
def test_failed_stop_state_envelopes_never_restore_prior_settings(failed_state):
    settings = {"allowBreak": "true", "allowParkour": "true", "maxFallHeightNoWater": "3"}

    def dispatch(route, payload):
        if route == "settings":
            name = payload.get("get", payload.get("set"))
            if "set" in payload:
                settings[name] = payload["value"]
            return {"key": name, "value": settings[name]}
        if route == "get_state":
            return failed_state
        return {}

    client = SimpleNamespace(
        transport=SimpleNamespace(dispatch=dispatch),
        _protected_home_anchor=(1, 64, 1),
    )

    @home_surface.protect_home_route(safe_movement=True)
    def route(client, x, y, z):
        return True

    assert not route(client, 1, 64, 1)
    assert settings == {
        "allowBreak": "false",
        "allowParkour": "false",
        "maxFallHeightNoWater": "1",
    }


def test_death_propagates_through_home_route_cleanup():
    from baritone_client.common.tasks import PlayerDeathDetected
    client = SimpleNamespace(_protected_home_anchor=(100, 64, 100),
        transport=SimpleNamespace(dispatch=lambda r, p: {"value": "false"}))
    @home_surface.protect_home_route()
    def dead(client, x, y, z):
        raise PlayerDeathDetected("dead")
    with pytest.raises(PlayerDeathDetected):
        dead(client, 100, 64, 100)


# -- leaked conservative settings (A1 2026-10-15..21) ------------------------
def _settings_client(settings, *, pathing, lag_reads=0):
    """A bridge that applies a setting only after ``lag_reads`` read-backs."""
    pending = {}
    reads = {"n": 0}

    def dispatch(route, payload=None):
        if route == "settings":
            name = payload.get("get", payload.get("set"))
            if "set" in payload:
                pending[name] = [payload["value"], lag_reads]
            elif name in pending:
                pending[name][1] -= 1
                if pending[name][1] < 0:
                    settings[name] = pending.pop(name)[0]
            return {"key": name, "value": settings[name]}
        if route == "get_state":
            return {"is_pathing": pathing["value"]}
        return {}

    return SimpleNamespace(transport=SimpleNamespace(dispatch=dispatch), _protected_home_anchor=(1, 64, 1))


def _route(safe_movement=True):
    @home_surface.protect_home_route(safe_movement=safe_movement)
    def route(client, x, y, z):
        return True
    return route


def test_a_lagging_bridge_still_confirms_the_restore(monkeypatch):
    """Three 50 ms reads called correct restores 'not observed' on a busy client."""
    monkeypatch.setattr(home_surface.time, "sleep", lambda *_a: None)
    settings = {"allowBreak": "true", "allowParkour": "true", "maxFallHeightNoWater": "3"}
    client = _settings_client(settings, pathing={"value": False}, lag_reads=5)
    assert _route()(client, 1, 64, 1)
    assert settings == {"allowBreak": "true", "allowParkour": "true", "maxFallHeightNoWater": "3"}


def test_an_unverified_stop_is_owed_and_the_next_route_restores_the_originals(monkeypatch):
    """The old guard re-read the leftover 'false' and kept it forever."""
    monkeypatch.setattr(home_surface.time, "sleep", lambda *_a: None)
    settings = {"allowBreak": "true", "allowParkour": "true", "maxFallHeightNoWater": "3"}
    pathing = {"value": True}
    client = _settings_client(settings, pathing=pathing)
    assert not _route()(client, 1, 64, 1)
    assert settings == {"allowBreak": "false", "allowParkour": "false", "maxFallHeightNoWater": "1"}
    assert client._home_route_owed == {"allowBreak": "true", "allowParkour": "true", "maxFallHeightNoWater": "3"}

    pathing["value"] = False
    assert _route()(client, 1, 64, 1)
    assert settings == {"allowBreak": "true", "allowParkour": "true", "maxFallHeightNoWater": "3"}
    assert client._home_route_owed == {}


def test_owed_settings_are_restored_once_nothing_is_moving(monkeypatch):
    monkeypatch.setattr(home_surface.time, "sleep", lambda *_a: None)
    settings = {"allowBreak": "true", "allowParkour": "true", "maxFallHeightNoWater": "3"}
    pathing = {"value": True}
    client = _settings_client(settings, pathing=pathing)
    _route()(client, 1, 64, 1)
    state = SimpleNamespace(custom_data={"base_location": [1, 64, 1]})

    home_surface.bind_home_surface(client, state)  # still pathing: keep waiting
    assert settings["allowBreak"] == "false"
    pathing["value"] = False
    client._home_route_active = 1  # another guarded route is underway
    home_surface.bind_home_surface(client, state)
    assert settings["allowBreak"] == "false"
    client._home_route_active = 0
    home_surface.bind_home_surface(client, state)
    assert settings == {"allowBreak": "true", "allowParkour": "true", "maxFallHeightNoWater": "3"}
    assert client._home_route_owed == {}


def test_a_callers_deliberate_no_digging_is_left_alone(monkeypatch):
    """base.py and the phases turn digging off around their own work."""
    monkeypatch.setattr(home_surface.time, "sleep", lambda *_a: None)
    settings = {"allowBreak": "false"}
    client = _settings_client(settings, pathing={"value": False})
    assert _route(safe_movement=False)(client, 1, 64, 1)
    assert settings == {"allowBreak": "false"}
    assert not getattr(client, "_home_route_owed", None)


def test_controller_start_resets_every_guarded_setting(monkeypatch):
    monkeypatch.setattr(home_surface.time, "sleep", lambda *_a: None)
    chats = []
    client = SimpleNamespace(transport=SimpleNamespace(
        dispatch=lambda route, payload=None: chats.append(payload["message"]) if route == "chat" else {}))
    client._home_route_owed = {"allowBreak": "true"}
    home_surface.bind_home_surface(client, SimpleNamespace(custom_data={}), controller_start=True)
    for name in ("allowBreak", "allowSprint", "allowParkour", "maxFallHeightNoWater"):
        assert f"#set reset {name}" in chats
    assert client._home_route_owed == {}



# -- sealed below the home floor (A1 2026-10-28..11-02) ------------------------
def _below_home_client(position):
    calls = []

    def dispatch(route, payload=None):
        calls.append((route, payload))
        if route == "get_state":
            return {"block_position": dict(zip(("x", "y", "z"), position)), "is_pathing": False}
        if route == "settings":
            name = payload.get("get", payload.get("set"))
            return {"key": name, "value": "true" if name == "allowBreak" else ("false" if name == "allowParkour" else "3")}
        return {}

    client = SimpleNamespace(transport=SimpleNamespace(dispatch=dispatch), _protected_home_anchor=(-411, 79, -12))
    return client, calls


def test_a_failed_route_home_from_a_sealed_pocket_excavates_away_from_the_house(monkeypatch):
    """Guarded routes dig nothing, so A1 failed 416 trips home from below."""
    monkeypatch.setattr(home_surface.time, "sleep", lambda *_a: None)
    client, _calls = _below_home_client((-425, 67, 4))
    tunnels = []
    monkeypatch.setattr("baritone_client.common.build_site_recovery.excavate_surface_egress",
                        lambda c, origin, expected_y, avoid, avoid_radius: tunnels.append((origin, expected_y, avoid, avoid_radius)) or origin)

    @home_surface.protect_home_route(safe_movement=True)
    def route_home(client, x, y, z):
        return False

    assert not route_home(client, -411, 79, -12)
    assert tunnels == [((-425, 67, 4), 79, (-411, 79, -12), 8.0)]
    route_home(client, -411, 79, -12)
    assert len(tunnels) == 1, "rate-limited: once per five minutes"


@pytest.mark.parametrize("position", [(-413, 67, -10), (-425, 79, 4), (-425, 77, 4)])
def test_no_excavation_under_the_house_or_from_the_surface(monkeypatch, position):
    monkeypatch.setattr(home_surface.time, "sleep", lambda *_a: None)
    client, _calls = _below_home_client(position)
    monkeypatch.setattr("baritone_client.common.build_site_recovery.excavate_surface_egress",
                        lambda *_a, **_k: pytest.fail("must not excavate here"))

    @home_surface.protect_home_route(safe_movement=True)
    def route_home(client, x, y, z):
        return False

    route_home(client, -411, 79, -12)


def test_egress_tunnels_skip_targets_at_the_house(monkeypatch):
    from baritone_client.common import build_site_recovery

    tunnels = []
    client = SimpleNamespace(transport=SimpleNamespace(
        dispatch=lambda route, payload=None: tunnels.append(payload) if route == "tunnel" else {"block_position": {"x": 0, "y": 60, "z": 0}, "is_pathing": False}))
    monkeypatch.setattr(build_site_recovery.time, "sleep", lambda *_a: None)
    build_site_recovery.excavate_surface_egress(client, origin=(0, 60, 0), expected_y=79,
                                                timeout_per_attempt=0.01, avoid=(8, 79, 0), avoid_radius=8.0)
    assert {(t["x"], t["z"]) for t in tunnels} == {(-8, 0), (0, 8), (0, -8)}



def test_a_failed_hop_toward_the_mine_entrance_does_not_excavate(monkeypatch):
    """The mine entrance is inside the guarded radius, but it is not home."""
    monkeypatch.setattr(home_surface.time, "sleep", lambda *_a: None)
    client, _calls = _below_home_client((-425, 67, 4))
    monkeypatch.setattr("baritone_client.common.build_site_recovery.excavate_surface_egress",
                        lambda *_a, **_k: pytest.fail("a tunnel retreat hop is not a route home"))

    @home_surface.protect_home_route(safe_movement=True)
    def hop(client, x, y, z):
        return False

    hop(client, -421, 79, 8)


# -- a hilly home: open-sky yard under the flat floor (A1 2026-11-03) ----------
def _walk_home(monkeypatch, path, roofs=(), view=None):
    """goto home over scripted positions; ``roofs`` are solid cells overhead."""
    reads, views = [], []
    settings = {"allowBreak": "true", "allowParkour": "false", "maxFallHeightNoWater": "3"}

    def dispatch(route, payload=None, **_kw):
        if route == "settings":
            name = payload.get("get", payload.get("set"))
            if "set" in payload:
                settings[name] = payload["value"]
            return {"key": name, "value": settings[name]}
        if route == "get_state":
            x, y, z = path[min(len(reads), len(path) - 1)]
            reads.append((x, y, z))
            return {"health": 20, "food_level": 20, "is_pathing": False,
                    "block_position": {"x": x, "y": y, "z": z}}
        if route == "get_view":
            views.append(reads[-1])
            if view is not None:
                return view
            return {"success": True, "data": {"voxels": [
                {"x": x, "y": y, "z": z, "id": block} for (x, y, z), block in roofs]}}
        return {}

    client = SimpleNamespace(transport=SimpleNamespace(dispatch=dispatch),
                             _protected_home_anchor=(-411, 79, -12))
    monkeypatch.setattr(combat, "survival_tick", lambda *_a: False)
    monkeypatch.setattr(navigation, "run_navigation_defense", lambda *_a: False)
    monkeypatch.setattr(navigation.time, "sleep", lambda *_a: None)
    result = navigation.goto(client, -411, 79, -12, tolerance=2.0)
    return result, client._last_navigation_outcome, views


# From just outside the disc, through the low south yard and up to the house.
YARD_WALK = [(-418, 76, 12), (-415, 76, 11), (-414, 75, 4), (-412, 78, -6), (-411, 79, -12)]


def test_a_walk_home_through_the_open_sky_yard_is_not_underground(monkeypatch):
    """Live A1: 152 trips aborted entering the yard at y=74-76 (floor 77)."""
    result, outcome, views = _walk_home(monkeypatch, YARD_WALK,
                                        roofs=[((-414, 82, 4), "minecraft:oak_leaves")])
    assert result and outcome == "arrived"
    # One view per cell under the floor, the starting cell included.
    assert views == [(-418, 76, 12), (-415, 76, 11), (-414, 75, 4)]


@pytest.mark.parametrize("roof", [(-414, 77, 4), (-414, 82, 4)])
def test_a_roofed_dip_under_home_still_aborts(monkeypatch, roof):
    result, outcome, _views = _walk_home(monkeypatch, YARD_WALK, roofs=[(roof, "minecraft:stone")])
    assert not result and outcome == "home_surface_abort"


@pytest.mark.parametrize("view", [{}, {"success": False, "data": {"voxels": []}}, {"data": None}])
def test_unproven_sky_keeps_the_flat_floor(monkeypatch, view):
    result, outcome, _views = _walk_home(monkeypatch, YARD_WALK, view=view)
    assert not result and outcome == "home_surface_abort"


def test_a_drop_deep_under_home_aborts_without_asking_about_sky(monkeypatch):
    path = [(-418, 76, 12), (-415, 64, 11), (-411, 79, -12)]
    result, outcome, views = _walk_home(monkeypatch, path)
    assert not result and outcome == "home_surface_abort" and views == [(-418, 76, 12)]


def test_a_route_starting_underground_outside_home_may_climb_out(monkeypatch):
    """A1 sat in its own mine tunnel, outside the disc, for 5+ hours."""
    tunnel = [(-440, 64, 1), (-430, 70, 6), (-424, 74, 8), (-418, 78, 0), (-411, 79, -12)]
    roofs = [((-440, 70, 1), "minecraft:stone"), ((-430, 74, 6), "minecraft:dirt"),
             ((-424, 79, 8), "minecraft:dirt")]
    result, outcome, _views = _walk_home(monkeypatch, tunnel, roofs=roofs)
    assert result and outcome == "arrived"


def test_no_excavation_from_the_open_sky_crater(monkeypatch):
    """A1 dug staircases into its own yard from open ground at y=74-76."""
    monkeypatch.setattr(home_surface.time, "sleep", lambda *_a: None)
    client, _calls = _below_home_client((-417, 74, 6))
    plain = client.transport.dispatch
    client.transport.dispatch = lambda route, payload=None: (
        {"data": {"voxels": []}} if route == "get_view" else plain(route, payload))
    monkeypatch.setattr("baritone_client.common.build_site_recovery.excavate_surface_egress",
                        lambda *_a, **_k: pytest.fail("open sky is not a sealed pocket"))

    @home_surface.protect_home_route(safe_movement=True)
    def route_home(client, x, y, z):
        return False

    route_home(client, -411, 79, -12)
