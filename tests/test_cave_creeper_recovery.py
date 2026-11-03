"""Hidden cave creepers must not trap grounded work in repeated failed escapes."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from baritone_client.common import combat
from baritone_client.common.defense import assess_threats


def snapshot():
    return {
        "player": {
            "entity_id": 7, "health": 20, "food_level": 19,
            "position": {"x": 0.1, "y": 80.0, "z": 0.8},
            "block_position": {"x": 0, "y": 80, "z": 0},
            "is_on_ground": True, "is_dead": False,
            "velocity": {"x": 0, "y": -0.0784, "z": 0},
        },
        "entities": [{
            "id": 42, "type": "minecraft:creeper", "distance": 5.315,
            "position": {"x": -0.5, "y": 75, "z": 2.5},
            "velocity": {"x": 0, "y": 0, "z": 0},
            "is_aggressive": False, "can_see_player": False,
        }],
        "skipped_count": 0,
    }


def test_grounded_hidden_stationary_cave_creeper_leaves_work_clear():
    frame = snapshot()
    assert assess_threats(frame["entities"], frame["player"]) == []


@pytest.mark.parametrize("change", [
    {"can_see_player": True}, {"can_see_player": None},
    {"is_aggressive": True}, {"is_aggressive": None},
    {"is_attacking": True}, {"is_attacking": None},
    {"angry_at_player": True}, {"angry_at_player": None}, {"target_id": 7},
    {"target_id": 99}, {"velocity": {"x": 0.1, "y": 0, "z": 0}},
    {"velocity": {}}, {"distance": 5}, {"distance": 4.9},
    {"distance": 6},  # inconsistent with the observed position
    {"position": {"x": -0.5, "y": 75.01, "z": 2.5}},
    # Two blocks up leaves no sealing layer; three or more up is #96's
    # sealed-ceiling exception (test_defense_occlusion).
    {"position": {"x": -0.5, "y": 82, "z": 4.6}, "distance": 4.336},
    {"position": {"x": -0.5, "y": 80, "z": 2.5}},
    {"position": {"x": True, "y": 75, "z": 2.5}},
    {"position": {"x": float("nan"), "y": 75, "z": 2.5}},
    {"position": {}},
])
def test_contact_moving_visible_or_uncertain_cave_creeper_remains_actionable(change):
    frame = snapshot()
    frame["entities"][0].update(change)
    assert assess_threats(frame["entities"], frame["player"])


@pytest.mark.parametrize("change", [
    {"is_on_ground": False}, {"is_on_ground": None},
    {"velocity": {"x": 0, "y": -0.2, "z": 0}},
    {"velocity": {"x": -0.4, "y": -0.0784, "z": 0.4}},
    {"velocity": {}}, {"position": {}},
])
def test_airborne_approaching_or_unknown_player_keeps_defense(change):
    frame = snapshot()
    frame["player"].update(change)
    assert assess_threats(frame["entities"], frame["player"])


def test_repeated_supervisor_ticks_resume_work_then_reactivate_on_visibility():
    frame = snapshot()
    mutations = []

    def dispatch(route, payload):
        if route == "get_combat_snapshot":
            return deepcopy(frame)
        if route == "get_block":
            return {"id": "minecraft:air"}
        mutations.append(route)
        raise AssertionError(f"Hidden cave creeper must not force mutation: {route}")

    client = SimpleNamespace(transport=SimpleNamespace(dispatch=dispatch))
    for _ in range(8):
        assert combat.defend_or_flee(client) is False
        assert client._mcbaratone_defense_runtime.mode.value == "clear"
    assert mutations == []
    frame["entities"][0]["can_see_player"] = True
    assert assess_threats(frame["entities"], frame["player"])
