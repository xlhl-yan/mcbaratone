"""Long-running retry policy survives restart and unrelated production."""

import json
from types import SimpleNamespace

import pytest

from baritone_client.automator.work_progress import record_productive_attempt
from baritone_client.automator.work_retry import retry_cooldown
from baritone_client.automator.food_opportunity import reachable_farm_location


def test_repeated_empty_cycles_back_off_across_restart_and_other_work():
    state = SimpleNamespace(custom_data={})
    delays = []
    for index in range(12):
        record_productive_attempt(state, "food_production", {}, {}, now=index * 1000)
        record_productive_attempt(state, "wood_farm", {}, {"logs_banked": 1}, now=index * 1000 + 1)
        state = SimpleNamespace(custom_data=json.loads(json.dumps(state.custom_data)))
        delays.append(retry_cooldown(state, "food_production", 120))
    assert delays[:4] == [120, 240, 240, 480]
    assert delays[-1] == 900
    record_productive_attempt(state, "food_production", {}, {"bread_crafted": 1})
    assert retry_cooldown(state, "food_production", 120) == 120


@pytest.mark.parametrize("kind", ["self_defense", "food_recovery", "armor_upkeep", "house_upkeep"])
def test_production_backoff_does_not_delay_survival_or_equipment(kind):
    state = SimpleNamespace(custom_data={})
    for _ in range(100):
        record_productive_attempt(state, kind, {}, {})
    assert retry_cooldown(state, kind, 60) == 60


def test_maintained_nearby_farm_precedes_incidental_crop():
    signals = SimpleNamespace(position=(0, 70, 0), known_farm_location=(8, 69, 8), crop_location=(1, 69, 1))
    assert reachable_farm_location(signals) == (8, 69, 8)
    signals.known_farm_location = (200, 69, 0)
    assert reachable_farm_location(signals) == (1, 69, 1)


def test_unavailable_farm_and_bad_ledger_do_not_invent_work():
    signals = SimpleNamespace(position=(0, 70, 0), known_farm_location=None, crop_location=None)
    assert reachable_farm_location(signals) is None
    for custom in (None, {"productive_work": []}, {"productive_work": {"by_kind": {"food_production": {"no_progress_streak": "bad"}}}}):
        assert retry_cooldown(SimpleNamespace(custom_data=custom), "food_production", 120) == 120
