"""A clear defense verdict stops a distant idle mob from starving all work."""

import threading
from types import SimpleNamespace

import pytest

from baritone_client.automator import safety_recovery
from baritone_client.common.combat_action import exclusive_combat_action
from baritone_client.common.defense import DefenseMode


@pytest.fixture(autouse=True)
def reset_quiet():
    safety_recovery._quiet.update(until=0.0, hostiles=0, health=0.0)
    yield
    safety_recovery._quiet.update(until=0.0, hostiles=0, health=0.0)


def _signals(hostiles, health=None):
    return SimpleNamespace(
        observed=True, entities_observed=True, dimension="minecraft:overworld",
        nearby_hostiles=hostiles, health=health,
    )


def _run(monkeypatch, *, acted, mode, hostiles=1):
    monkeypatch.setattr(safety_recovery, "defend_or_flee", lambda *_a, **_k: acted)
    monkeypatch.setattr(safety_recovery, "_resolved_mode", lambda _c: mode)
    client = SimpleNamespace(transport=SimpleNamespace())
    return safety_recovery.run_self_defense(client, lambda: _signals(hostiles))


def test_clear_verdict_quiets_reselection_for_the_same_hostiles(monkeypatch):
    clock = {"t": 100.0}
    monkeypatch.setattr(safety_recovery.time, "monotonic", lambda: clock["t"])
    assert safety_recovery.select_self_defense(_signals(1)) is not None

    _run(monkeypatch, acted=False, mode=DefenseMode.CLEAR)

    assert safety_recovery.select_self_defense(_signals(1)) is None
    # More hostiles than the quiet verdict covered: defend again at once.
    assert safety_recovery.select_self_defense(_signals(2)) is not None
    # The verdict expires.
    clock["t"] += safety_recovery.QUIET_SECONDS + 1
    assert safety_recovery.select_self_defense(_signals(1)) is not None


def test_a_pass_that_acted_does_not_quiet_defense(monkeypatch):
    _run(monkeypatch, acted=True, mode=DefenseMode.CLEAR)

    assert safety_recovery.select_self_defense(_signals(1)) is not None


def test_unknown_mode_does_not_quiet_defense(monkeypatch):
    _run(monkeypatch, acted=False, mode=None)

    assert safety_recovery.select_self_defense(_signals(1)) is not None


def test_scheduled_defense_defers_while_background_action_owns_client(monkeypatch):
    client = SimpleNamespace(transport=SimpleNamespace())
    defense_calls = []
    holding_lock = threading.Event()
    release_lock = threading.Event()

    def hold_client_action():
        with exclusive_combat_action(client):
            holding_lock.set()
            release_lock.wait(timeout=2)

    holder = threading.Thread(target=hold_client_action)
    holder.start()
    assert holding_lock.wait(timeout=1)
    monkeypatch.setattr(
        safety_recovery,
        "defend_or_flee",
        lambda *_a, **_k: defense_calls.append(True) or True,
    )
    try:
        outcome = safety_recovery.run_self_defense(
            client, lambda: _signals(1)
        )
    finally:
        release_lock.set()
        holder.join(timeout=2)

    assert not holder.is_alive()
    assert defense_calls == []
    assert outcome == (
        False,
        "defense deferred while another client action owns controls",
        1,
        1,
    )


def _pass(monkeypatch, healths, *, acted=True, mode=DefenseMode.EVADE, hostiles=1):
    """One scheduled pass whose observations read ``healths`` in order."""
    readings = iter(healths)
    monkeypatch.setattr(safety_recovery, "defend_or_flee", lambda *_a, **_k: acted)
    monkeypatch.setattr(safety_recovery, "_resolved_mode", lambda _c: mode)
    client = SimpleNamespace(transport=SimpleNamespace())
    return safety_recovery.run_self_defense(client, lambda: _signals(hostiles, next(readings)))


def test_an_unharmed_stalemate_yields_to_other_work(monkeypatch):
    """Live A1 2026-10-16: full health for 35+ minutes beside a skeleton it
    could neither escape nor reach; 776 evades and no other work ran."""
    success, detail, _before, _after = _pass(monkeypatch, [20.0, 20.0])
    assert not success
    assert "unharmed, yielding to other work" in detail
    assert safety_recovery.select_self_defense(_signals(1, 20.0)) is None


def test_a_hostile_that_hurts_the_bot_keeps_defense_first(monkeypatch):
    _pass(monkeypatch, [20.0, 17.0])
    assert safety_recovery.select_self_defense(_signals(1, 17.0)) is not None


def test_damage_after_yielding_ends_the_quiet_early(monkeypatch):
    _pass(monkeypatch, [20.0, 20.0])
    assert safety_recovery.select_self_defense(_signals(1, 20.0)) is None
    assert safety_recovery.select_self_defense(_signals(1, 18.5)) is not None


def test_unknown_health_never_buys_a_quiet_window(monkeypatch):
    _pass(monkeypatch, [None, None])
    assert safety_recovery.select_self_defense(_signals(1)) is not None


def test_a_locked_out_pass_yields_when_unharmed(monkeypatch):
    client = SimpleNamespace(transport=SimpleNamespace())
    holding_lock = threading.Event()
    release_lock = threading.Event()

    def hold_client_action():
        with exclusive_combat_action(client):
            holding_lock.set()
            release_lock.wait(timeout=2)

    holder = threading.Thread(target=hold_client_action)
    holder.start()
    assert holding_lock.wait(timeout=1)
    try:
        success, detail, _b, _a = safety_recovery.run_self_defense(client, lambda: _signals(1, 20.0))
    finally:
        release_lock.set()
        holder.join(timeout=2)
    assert not success
    assert detail.startswith("defense deferred while another client action owns controls; unharmed")
    assert safety_recovery.select_self_defense(_signals(1, 20.0)) is None
