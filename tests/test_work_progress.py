from types import SimpleNamespace

from baritone_client.automator.work_progress import (
    productive_snapshot,
    record_productive_attempt,
)


def _state(custom_data=None):
    return SimpleNamespace(custom_data=custom_data or {})


def test_snapshot_ignores_movement_and_attempt_cursors():
    state = _state(
        {
            "food_worker": {
                "attempts": 9,
                "plot_cursor": 4,
                "plots": 2,
                "prepared_food_banked": 18,
            },
            "last_position": [100, 70, 200],
        }
    )

    assert productive_snapshot(state) == {
        "food_worker.plots": 2,
        "food_worker.prepared_food_banked": 18,
    }


def test_snapshot_accepts_only_verified_productive_scheduler_outcomes():
    state = _state(
        {
            "adaptive_scheduler": {
                "opportunities": {
                    "crop_farm": {
                        "attempts": 8,
                        "successful_cycles": 3,
                        "verified_delta_total": 12,
                    },
                    "food_recovery": {
                        "successful_cycles": 7,
                        "verified_delta_total": 9,
                    },
                    "end_dimension_entry": {
                        "successful_cycles": 2,
                    },
                }
            }
        }
    )

    assert productive_snapshot(state) == {
        "adaptive_scheduler.crop_farm.verified_delta_total": 12,
    }


def test_unchanged_attempts_raise_durable_escalation_level():
    state = _state()
    before = {"wood_worker.logs_banked": 8}

    results = [
        record_productive_attempt(
            state,
            "wood_farm",
            before,
            before,
            now=float(index),
        )
        for index in range(1, 6)
    ]

    assert [result.escalation_level for result in results] == [1, 1, 2, 2, 3]
    assert state.custom_data["productive_work"]["no_progress_streak"] == 5


def test_verified_counter_delta_resets_no_progress_streak():
    state = _state()
    before = {"iron_worker.iron_banked": 10}
    record_productive_attempt(state, "iron_mine", before, before, now=1.0)

    result = record_productive_attempt(
        state,
        "iron_mine",
        before,
        {"iron_worker.iron_banked": 17},
        detail="banked seven iron",
        now=2.0,
    )

    assert result.progressed
    assert result.delta == {"iron_worker.iron_banked": 7}
    assert result.no_progress_streak == 0
    assert state.custom_data["productive_work"]["last_progress_at"] == 2.0


def test_success_text_without_counter_delta_is_not_productive_progress():
    state = _state()

    result = record_productive_attempt(
        state,
        "nether_supply",
        {},
        {},
        detail="command started successfully",
        now=4.0,
    )

    assert not result.progressed
    assert result.no_progress_streak == 1


def test_cycles_and_defense_success_cannot_hide_a_stalled_campaign():
    state = _state({"food_worker": {"cycles": 10}, "adaptive_scheduler": {
        "opportunities": {
            "food_production": {"successful_cycles": 10, "verified_delta_total": 0},
            "self_defense": {"successful_cycles": 10, "verified_delta_total": 3},
        },
    }})
    before = productive_snapshot(state)
    state.custom_data["food_worker"]["cycles"] += 1
    for record in state.custom_data["adaptive_scheduler"]["opportunities"].values():
        record["successful_cycles"] += 1
    result = record_productive_attempt(state, "food_production", before, productive_snapshot(state))
    assert not result.progressed
    assert result.no_progress_streak == 1


def test_equipment_and_edible_crop_deltas_are_productive():
    state = _state({"food_worker": {"other_edible_crops_harvested": 4},
                    "adaptive_scheduler": {"opportunities": {
                        "armor_upkeep": {"verified_delta_total": 2}}}})
    assert productive_snapshot(state) == {
        "food_worker.other_edible_crops_harvested": 4,
        "adaptive_scheduler.armor_upkeep.verified_delta_total": 2,
    }
