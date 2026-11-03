"""A hungry bot goes home to its farm and storage instead of drifting away.

Live A1 2026-10-18..20: no farming for two days. The farm refused travel
below 14 food, recovery searched around wherever the bot stood (drifting it
140+ blocks into jagged peaks), and storage recovery never asked for the
3159 carrots banked at home.
"""

from types import SimpleNamespace

import pytest

from baritone_client.automator import food_opportunity
from baritone_client.automator.food_recovery_state import _STORED_FOOD_TARGETS
from baritone_client.common import food_supply, survival_farm

HOME = (-411.0, 79.0, -12.0)


def _client(food, health, x, z):
    live = {"food_level": food, "health": health, "block_position": {"x": x, "y": 90, "z": z}}
    return SimpleNamespace(transport=SimpleNamespace(dispatch=lambda *_a, **_k: dict(live))), live


def _state():
    return SimpleNamespace(custom_data={"base_location": list(HOME)})


def test_banked_carrots_and_apples_are_recovery_food():
    assert _STORED_FOOD_TARGETS["minecraft:carrot"] > 0
    assert _STORED_FOOD_TARGETS["minecraft:apple"] > 0


@pytest.fixture
def recovery(monkeypatch):
    calls = []
    monkeypatch.setattr("baritone_client.common.survival_farm.tend_local_farm_for_food", lambda *_a: calls.append("farm"))
    monkeypatch.setattr("baritone_client.common.survival_farm.local_farm_wait_reason", lambda *_a: "")
    monkeypatch.setattr(food_opportunity, "recover_food_from_known_sources", lambda *_a: calls.append("storage") or False)

    def search(_client, **kwargs):
        calls.append(("search", kwargs["exploration_center"]))
        return False

    monkeypatch.setattr(food_opportunity, "acquire_emergency_food", search)
    return calls


def test_a_hungry_able_bot_far_from_home_walks_back_before_searching(recovery, monkeypatch):
    client, live = _client(10, 17.7, -548, 58)  # live A1, 2026-10-20

    def walk_home(_client, target, origin, navigate):
        from baritone_client.common.navigation import recovery_goto

        # Staged legs: one exact goto from 154 blocks out failed in four
        # seconds live, because the home chunk was not loaded.
        assert navigate is recovery_goto and origin == (-548, 90, 58)
        recovery.append(("home", target))
        live["block_position"] = {"x": -411, "y": 79, "z": -12}
        return True

    monkeypatch.setattr("baritone_client.common.staged_approach.staged_goto", walk_home)
    food_opportunity.run_scheduled_food_recovery(client, _state())
    assert recovery[0] == ("home", (-411, 79, -12))
    assert recovery[1:3] == ["farm", "storage"]
    assert recovery[-1] == ("search", HOME), "search around home, not where the bot wandered"


def test_a_critically_hurt_bot_searches_where_it_is(recovery, monkeypatch):
    client, _live = _client(4, 6.0, -548, 58)
    monkeypatch.setattr("baritone_client.common.staged_approach.staged_goto",
                        lambda *_a, **_k: pytest.fail("too hurt for a 140-block walk"))
    food_opportunity.run_scheduled_food_recovery(client, _state())
    assert recovery[-1] == ("search", (-548.0, 90.0, 58.0))


def test_a_bot_already_near_home_does_not_walk(recovery, monkeypatch):
    client, _live = _client(10, 17.7, -420, -5)
    monkeypatch.setattr("baritone_client.common.staged_approach.staged_goto",
                        lambda *_a, **_k: pytest.fail("already in reach of storage"))
    food_opportunity.run_scheduled_food_recovery(client, _state())
    assert recovery[-1] == ("search", HOME)


def test_fetched_food_is_eaten_up_to_the_work_threshold(monkeypatch):
    client, live = _client(10, 17.7, -420, -5)
    monkeypatch.setattr("baritone_client.common.survival_farm.tend_local_farm_for_food", lambda *_a: None)
    monkeypatch.setattr("baritone_client.common.survival_farm.local_farm_wait_reason", lambda *_a: "")
    monkeypatch.setattr(food_opportunity, "recover_food_from_known_sources",
                        lambda *_a: live.update(food_level=12) or True)  # known sources stop at 12
    eaten = []

    def eat(_client, minimum_food):
        eaten.append(minimum_food)
        live["food_level"] = minimum_food
        return True

    monkeypatch.setattr(food_opportunity, "eat_until_hunger", eat)
    monkeypatch.setattr(food_opportunity, "acquire_emergency_food", lambda *_a, **_k: pytest.fail("food was found"))
    success, detail, _before, after = food_opportunity.run_scheduled_food_recovery(client, _state())
    assert success and after == 14 and eaten == [14]


@pytest.mark.parametrize(("health", "food", "able"), [
    (17.7, 10, True), (12.0, 6, True), (11.5, 10, False), (20.0, 5, False),
])
def test_hungry_but_healthy_is_enough_for_the_home_farm(health, food, able):
    client, _live = _client(food, health, 0, 0)
    assert survival_farm.fed_enough_for_home_farm(client) is able


def test_the_farm_cycle_runs_without_carried_food_when_healthy(monkeypatch):
    client, _live = _client(10, 17.7, 0, 0)
    monkeypatch.setattr(food_supply, "_survival_ready", lambda _c: True)
    monkeypatch.setattr(food_supply, "eat_until_hunger", lambda *_a, **_k: False)  # nothing to eat
    monkeypatch.setattr(food_supply, "get_inventory", lambda _c: {})
    monkeypatch.setattr(food_supply, "prepare_house_door_for_departure", lambda *_a: False)
    result = food_supply.run_food_cycle(client, SimpleNamespace(custom_data={}))
    # It got past the hunger gate and stopped only at the door check.
    assert "house door blocked" in result.detail
