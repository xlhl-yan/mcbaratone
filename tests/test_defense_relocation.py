from types import SimpleNamespace

import pytest

from baritone_client.common import combat, defense_relocation as recovery, escape_recovery
from baritone_client.common.defense import (
    DefenseRuntime,
    EscapeCandidate,
    choose_defense_action,
)
from baritone_client.common.tasks import PlayerDeathDetected


@pytest.fixture
def route(monkeypatch):
    class Transport:
        def __init__(self):
            self.calls = []
            self.setting = "true"
            self.live = {"position": {"x": -15, "y": 65, "z": 0}, "health": 20,
                         "is_dead": False, "is_pathing": False}

        def dispatch(self, command, payload, **kwargs):
            self.calls.append((command, payload))
            if command == "get_state":
                return {"block_position": {"x": 0, "y": 64, "z": 0}, "health": 20,
                        "is_pathing": False}
            if command == "settings":
                if "set" in payload:
                    self.setting = payload["value"]
                return {"value": self.setting}
            return {}
    transport = Transport()
    client = SimpleNamespace(transport=transport)
    threat = {"id": 1, "type": "minecraft:creeper", "distance": 5,
              "position": {"x": 5, "y": 64, "z": 0}}
    monkeypatch.setattr(escape_recovery, "surface_adjusted_candidates",
                        lambda *_a, **_k: [EscapeCandidate(-16, 65, 0, 1)])
    monkeypatch.setattr(combat, "_get_combat_snapshot", lambda *_a, **_k:
                        {"player": transport.live, "entities": [], "skipped_count": 0})
    monkeypatch.setattr(recovery.time, "sleep", lambda _: None)
    return client, threat


def test_verified_escape_stops_and_restores_excavation(route):
    client, threat = route
    assert recovery.relocate(client, threat)
    assert ("goal", {"x": -16, "y": 65, "z": 0}) in client.transport.calls
    assert ("settings", {"set": "allowBreak", "value": "false"}) in client.transport.calls
    assert ("cancel", {}) in client.transport.calls
    assert client.transport.setting == "true"


def test_endpoint_below_home_floor_does_not_start(route):
    client, threat = route
    client._protected_home_anchor = (0, 70, 0)
    assert not recovery.relocate(client, threat)
    assert not any(command == "goal" for command, _ in client.transport.calls)


def test_route_descent_cancels_even_when_threat_disappears(route):
    client, threat = route
    client.transport.live["position"]["y"] = 60
    assert not recovery.relocate(client, threat)
    assert ("cancel", {}) in client.transport.calls


def test_unknown_terrain_never_uses_horizontal_fallback(route, monkeypatch):
    client, threat = route
    monkeypatch.setattr(escape_recovery, "surface_adjusted_candidates", lambda *_a, **_k: [])
    assert not recovery.relocate(client, threat)
    assert not any(command == "goal" for command, _ in client.transport.calls)


def test_new_urgent_threat_prevents_escape_success(route, monkeypatch):
    client, threat = route
    monkeypatch.setattr(combat, "_get_combat_snapshot", lambda *_a, **_k:
                        {"player": client.transport.live, "entities": [dict(threat, distance=2)]})
    assert not recovery.relocate(client, threat, timeout=.01)


def test_death_is_not_swallowed(route, monkeypatch):
    client, threat = route
    monkeypatch.setattr(combat, "ensure_alive", lambda *_a:
                        (_ for _ in ()).throw(PlayerDeathDetected("dead")))
    with pytest.raises(PlayerDeathDetected):
        recovery.relocate(client, threat)


def test_uncertain_stop_keeps_excavation_disabled(route, monkeypatch):
    client, threat = route
    original = client.transport.dispatch
    def dispatch(command, payload, **kwargs):
        response = original(command, payload, **kwargs)
        if command == "get_state":
            response["is_pathing"] = True
        return response
    monkeypatch.setattr(client.transport, "dispatch", dispatch)
    assert not recovery.relocate(client, threat)
    assert client.transport.setting == "false"
    # Live A1 2026-10-22: this left digging off for 11 days, sealed underground.
    assert client._home_route_owed == {"allowBreak": "true"}


def test_a_verified_relocation_owes_nothing(route):
    client, threat = route
    recovery.relocate(client, threat)
    assert client.transport.setting == "true"
    assert not getattr(client, "_home_route_owed", None)


def test_active_surface_work_floor_applies_outside_home_radius():
    client = SimpleNamespace(
        transport=SimpleNamespace(),
        _protected_home_anchor=(0, 70, 0),
        _protected_surface_work=(30, 75, 0),
    )
    position = {"x": 30, "y": 72, "z": 0}

    assert recovery.relocation_floor(client, position) == 75


def test_escape_floor_does_not_ratchet_down_and_clears_on_fresh_clear():
    runtime = DefenseRuntime()
    client = SimpleNamespace(
        transport=SimpleNamespace(),
        _mcbaratone_defense_runtime=runtime,
    )

    first = recovery.relocation_floor(
        client, {"x": 0, "y": 77.9375, "z": 0}
    )
    retry = recovery.relocation_floor(client, {"x": 0, "y": 75, "z": 0})

    assert first == retry == 75.9375
    assert runtime.escape_floor == first

    choose_defense_action(
        [],
        health=20,
        armor_count=0,
        has_weapon=False,
        runtime=runtime,
    )

    assert runtime.escape_floor is None
    assert recovery.relocation_floor(client, {"x": 0, "y": 60, "z": 0}) == 58
