from copy import deepcopy
from types import SimpleNamespace

import pytest

from baritone_client.common import combat


def snapshot(*, tick=1, grounded=False, creeper_changes=None, other_entities=()):
    player = {
        "entity_id": 99,
        "position": {"x": 0.0, "y": 80.4 if not grounded else 80.0, "z": 0.0},
        "block_position": {"x": 0, "y": 80, "z": 0},
        "velocity": {"x": 0.0, "y": -0.2 if not grounded else -0.0784, "z": 0.0},
        "is_on_ground": grounded,
        "is_dead": False,
        "dimension": "minecraft:overworld",
        "health": 20.0,
        "food_level": 20,
        "armor_count": 0,
        "fall_distance": 0.4 if not grounded else 0.0,
    }
    creeper = {
        "id": 42,
        "type": "minecraft:creeper",
        "distance": 12.0,
        "position": {"x": 6.6, "y": 70.0, "z": 0.0},
        "velocity": {"x": 0.0, "y": 0.0, "z": 0.0},
        "is_aggressive": False,
        "can_see_player": False,
    }
    creeper.update(creeper_changes or {})
    entities = [creeper, *deepcopy(list(other_entities))]
    return {
        "snapshot_version": 1,
        "tick": tick,
        "radius": 16,
        "player": player,
        "entities": entities,
        "count": len(entities),
        "skipped_count": 0,
    }


class SnapshotTransport:
    def __init__(self, values):
        self.values = iter(values)

    def dispatch(self, route, payload):
        assert route == "get_combat_snapshot"
        assert payload == {"radius": 16}
        return next(self.values)


def prepare(monkeypatch, initial, fresh):
    events = []
    client = SimpleNamespace(transport=SnapshotTransport(fresh), _test_events=events)
    monkeypatch.setattr(combat, "_get_combat_snapshot", lambda *_a, **_k: initial)
    monkeypatch.setattr(
        combat, "_stop_for_defense", lambda _client: events.append("stop")
    )
    monkeypatch.setattr(combat, "run_away", lambda *_a, **_k: pytest.fail("unexpected flee"))
    monkeypatch.setattr(combat.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(combat.combat_telemetry, "record_defense_decision", lambda *_a, **_k: None)
    monkeypatch.setattr(combat.combat_telemetry, "record_combat_action", lambda *_a, **_k: None)
    return client


def test_transient_airborne_false_positive_reassesses_untouched_grounded_snapshot(monkeypatch):
    initial = snapshot()
    landed = snapshot(tick=2, grounded=True)
    client = prepare(monkeypatch, initial, [landed])

    assert combat.defend_or_flee(client) is True
    assert client._last_defense_intervention == "airborne_threat_recheck_clear"
    assert client._mcbaratone_defense_runtime.mode.value == "clear"
    assert landed["player"]["is_on_ground"] is True


@pytest.mark.parametrize(
    "changes",
    [
        {"can_see_player": True},
        {"distance": 9.0},
        {"velocity": {"x": -0.2, "y": 0.0, "z": 0.0}},
    ],
)
def test_fresh_urgent_creeper_observation_uses_canonical_evasion(monkeypatch, changes):
    initial = snapshot()
    fresh = snapshot(tick=2, creeper_changes=changes)
    client = prepare(monkeypatch, initial, [fresh])
    calls = []
    monkeypatch.setattr(combat, "run_away", lambda *_a, **_k: calls.append(True) or True)

    assert combat.defend_or_flee(client) is True
    assert calls == [True]


def test_incomplete_fresh_observation_keeps_bot_stopped(monkeypatch):
    client = prepare(monkeypatch, snapshot(), [None])

    assert combat.defend_or_flee(client) is True
    assert client._last_defense_intervention == "airborne_threat_recheck"
    assert client._mcbaratone_defense_runtime.mode.value == "alert"


def test_persistent_airborne_case_is_bounded_and_never_starts_relocation(monkeypatch):
    initial = snapshot()
    fresh = [snapshot(tick=tick) for tick in range(2, 6)]
    client = prepare(monkeypatch, initial, fresh)
    waits = []
    monkeypatch.setattr(
        combat.time,
        "sleep",
        lambda seconds: (waits.append(seconds), client._test_events.append("wait")),
    )

    assert combat.defend_or_flee(client) is True
    assert waits == [0.1] * 4
    assert client._test_events == ["stop", "wait", "wait", "wait", "wait"]
    assert client._last_defense_intervention == "airborne_threat_recheck"


def test_new_actionable_threat_uses_fresh_canonical_assessment(monkeypatch):
    skeleton = {
        "id": 55,
        "type": "minecraft:skeleton",
        "distance": 7.0,
        "position": {"x": 0.0, "y": 80.0, "z": 7.0},
        "velocity": {"x": 0.0, "y": 0.0, "z": 0.0},
        "is_aggressive": True,
        "can_see_player": True,
    }
    client = prepare(
        monkeypatch, snapshot(),
        [snapshot(tick=2, other_entities=[skeleton])],
    )
    calls = []
    monkeypatch.setattr(
        combat,
        "run_away",
        lambda _client, entity: calls.append(entity["id"]) or True,
    )

    assert combat.defend_or_flee(client) is True
    assert calls == [55]


def test_fresh_snapshot_that_breaks_canonical_assessment_stays_stopped(monkeypatch):
    spider = {
        "id": 55,
        "type": "minecraft:spider",
        "distance": 7.0,
        "position": {"x": 0.0, "y": 80.0, "z": 7.0},
        "velocity": {"x": 0.0, "y": 0.0, "z": 0.0},
        "is_aggressive": False,
        "can_see_player": False,
    }
    fresh = snapshot(tick=2, other_entities=[spider])
    fresh["player"]["world_time"] = "unknown"
    client = prepare(monkeypatch, snapshot(), [fresh])

    assert combat.defend_or_flee(client) is True
    assert client._last_defense_intervention == "airborne_threat_recheck"
    assert client._mcbaratone_defense_runtime.mode.value == "alert"


def test_changed_creeper_identity_gets_one_canonical_reassessment_only(monkeypatch):
    changed = snapshot(tick=2)
    changed["entities"][0]["id"] = 77
    client = prepare(monkeypatch, snapshot(), [changed])
    calls = []
    monkeypatch.setattr(
        combat, "run_away", lambda _client, entity: calls.append(entity["id"]) or True
    )
    waits = []
    monkeypatch.setattr(combat.time, "sleep", lambda seconds: waits.append(seconds))

    assert combat.defend_or_flee(client) is True
    assert calls == [77]
    assert waits == [0.1]


@pytest.mark.parametrize(
    "update",
    [
        lambda value: value.update(snapshot_version=2),
        lambda value: value.update(skipped_count=1),
        lambda value: value.update(tick=1),
        lambda value: value.update(count=0),
    ],
)
def test_malformed_or_nonfresh_observations_fail_closed(monkeypatch, update):
    fresh = snapshot(tick=2)
    update(fresh)
    client = prepare(monkeypatch, snapshot(), [fresh])

    assert combat.defend_or_flee(client) is True
    assert client._last_defense_intervention == "airborne_threat_recheck"
