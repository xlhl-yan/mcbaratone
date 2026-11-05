from types import SimpleNamespace

import pytest

from baritone_client.common import food_return as recovery
from baritone_client.common.tasks import PlayerDeathDetected


def world():
    live = {"is_dead": False, "health": 20, "dimension": "minecraft:overworld",
            "is_pathing": False, "is_on_ground": True,
            "block_position": {"x": 30, "y": 70, "z": 0}}
    client = SimpleNamespace(transport=SimpleNamespace(dispatch=lambda *_a: live))
    state = SimpleNamespace(custom_data={"base_location": [0, 70, 0],
                                        "structures": {"starter_house": {"origin": [-3, 69, -3]}}})
    return client, state, live


def test_zero_output_food_attempt_returns_and_verifies_arrival(monkeypatch):
    client, state, live = world()
    def travel(_client, *xyz):
        live["block_position"] = dict(zip(("x", "y", "z"), xyz))
        return False  # route result is weaker evidence than the postcondition
    monkeypatch.setattr(recovery, "_return", travel)
    outcome = SimpleNamespace(success=False, detail="water bucket ray obstructed")
    run = recovery.return_after_food_cycle(lambda *_a: outcome)
    assert run(client, state) is outcome
    assert state.custom_data["food_return"]["verified"]
    assert "productive_work" not in state.custom_data


def test_acknowledged_return_without_displacement_stays_unverified(monkeypatch):
    client, state, _live = world()
    monkeypatch.setattr(recovery, "_return", lambda *_a: True)
    assert not recovery.return_from_food_attempt(client, state)
    assert not state.custom_data["food_return"]["verified"]


def test_return_waits_for_observed_stop_before_starting_a_route(monkeypatch):
    client, state, live = world()
    states = [dict(live, is_pathing=True), live]
    client.transport.dispatch = lambda route, _payload: live if route == "stop" else states.pop(0) if states else live
    trips = []
    monkeypatch.setattr(recovery.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(recovery, "_return", lambda *_a: trips.append(1))
    recovery.return_from_food_attempt(client, state)
    assert trips == [1]


def test_death_yields_to_survival_instead_of_starting_a_return(monkeypatch):
    client, state, _live = world()
    monkeypatch.setattr(recovery, "_return", lambda *_a: pytest.fail("survival owns this recovery"))
    def died(*_a):
        raise PlayerDeathDetected("died")
    with pytest.raises(PlayerDeathDetected):
        recovery.return_after_food_cycle(died)(client, state)


@pytest.mark.parametrize("case", ["dead", "hurt", "dimension", "unknown"])
def test_unsafe_or_unknown_state_never_starts_return(monkeypatch, case):
    client, state, live = world()
    if case == "dead":live["is_dead"] = True
    elif case == "hurt":live["health"] = 3
    elif case == "dimension":live["dimension"] = "minecraft:the_nether"
    else:live.pop("block_position")
    monkeypatch.setattr(recovery, "_return", lambda *_a: pytest.fail("unsafe return"))
    assert not recovery.return_from_food_attempt(client, state)


def test_nearby_cave_position_is_not_a_home_arrival(monkeypatch):
    client,state,live=world()
    live["block_position"]={"x":0,"y":67,"z":0}
    trips=[]
    monkeypatch.setattr(recovery,"_return",lambda *_a:trips.append(1))
    assert not recovery.return_from_food_attempt(client,state)
    assert trips==[1] and not state.custom_data["food_return"]["verified"]
