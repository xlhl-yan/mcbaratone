"""Bounded safety actions that must run before ordinary fleet work."""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from ..common.combat import _defense_runtime, defend_or_flee
from ..common.combat_action import exclusive_combat_action
from ..common.defense import DefenseMode
from .local_opportunity import LocalOpportunity, OpportunityKind

# `defend_or_flee` advances the shared defense state machine by exactly one
# tick. Its EVADE branch is explicitly written on the assumption that a
# threat still present after a failed escape gets retried "immediately on
# the next tick" (see combat.py, next to should_escalate_to_combat) -- that
# is how DefenseRuntime.consecutive_evade_failures is meant to reach its
# escalation threshold. Every other caller of defend_or_flee in this
# codebase loops it tightly inside one continuous action, so that
# assumption holds for them. This opportunity handler was the exception: the
# adaptive scheduler only reaches SELF_DEFENSE once per outer cycle
# (observed 10-14s, gated by opportunity priority against everything else),
# so a single call here starved the escalation counter of the fast retries
# it needs -- a bot could spend minutes alternating between a live threat
# and ordinary fleet work, only ever escalating to fight-back once health
# separately dropped into the unrelated critical-health recovery gate.
# Retrying here, inside this one bounded opportunity call, gives the state
# machine the "next tick" it already assumes, without changing the shared
# state machine, its thresholds, or any other (already tightly-looping)
# caller's behavior.
_MAX_TICKS = 6
_TICK_BUDGET_SECONDS = 20.0
#: After a pass in which the defense state machine found nothing to act on,
#: trust that verdict this long unless more hostiles show up. Without it a
#: distant, non-aggressive mob inside the 24-block count re-selected
#: SELF_DEFENSE every cycle and preempted all other work: live A1 logged
#: "resolved (clear); nearby hostiles 1->1" 74 times in ten minutes for a
#: skeleton 22 blocks away, while food, armour and lighting never ran.
#: Real threats are still met by the defense that runs on every movement
#: tick and in the safety system.
#:
#: A pass that leaves the bot unharmed earns the same window, even when the
#: threat persists. Live A1 2026-10-16 stood still at full health for 35+
#: minutes beside a skeleton it could neither escape nor reach: 776 evades,
#: every pass "deferred" (controls held, or hostiles 1->1), no other work.
#: The window ends early if health drops below where it was granted.
QUIET_SECONDS = 45.0
_quiet = {"until": 0.0, "hostiles": 0, "health": 0.0}


def _health(signals: object) -> Optional[float]:
    """Observed health, or None when the signal is missing or not alive."""
    try:
        value = float(getattr(signals, "health", None))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _quiet_for(hostiles: int, health: Optional[float]) -> None:
    _quiet.update(until=time.monotonic() + QUIET_SECONDS, hostiles=hostiles, health=health or 0.0)


def select_self_defense(signals: object) -> LocalOpportunity | None:
    """Offer defense before a nearby hostile can become a repeated role hold."""
    if not getattr(signals, "observed", False) or not getattr(signals, "entities_observed", False):
        return None
    if "overworld" not in str(getattr(signals, "dimension", "") or ""):
        return None
    hostiles = int(getattr(signals, "nearby_hostiles", 0) or 0)
    if hostiles <= 0:
        return None
    health = _health(signals)
    if (time.monotonic() < _quiet["until"] and hostiles <= _quiet["hostiles"]
            and (health is None or health >= _quiet["health"])):
        return None
    return LocalOpportunity(
        OpportunityKind.SELF_DEFENSE, 320,
        f"{hostiles} nearby hostile(s) require a supervised defensive recovery",
    )


def _resolved_mode(client: Any) -> Optional[DefenseMode]:
    """Best-effort peek at the shared runtime's mode after acting.

    Returns None for client doubles with no ``transport`` (unit tests that
    monkeypatch ``defend_or_flee`` directly and never touch the real state
    machine); callers must treat None as "unknown", not "clear".
    """
    try:
        return _defense_runtime(client).mode
    except AttributeError:
        return None


def run_self_defense(
    client: Any, observe: Callable[[], Any]
) -> tuple[bool, str, int, int]:
    """Run bounded defensive ticks while exclusively owning this client.

    SafetySystem runs in a background thread and also advances defense. Keep
    the complete scheduled retry window under the same per-transport action
    lock, so the background tick defers instead of issuing duplicate movement
    and escape commands during this opportunity.
    """
    first = observe()
    before = int(getattr(first, "nearby_hostiles", 0) or 0)
    health_before = _health(first)
    with exclusive_combat_action(client, blocking=False) as acquired:
        if not acquired:
            # The owner (usually the background safety tick) is defending.
            last = observe()
            after = int(getattr(last, "nearby_hostiles", 0) or 0)
            detail = "defense deferred while another client action owns controls"
            if _unharmed(health_before, _health(last)):
                _quiet_for(after, _health(last))
                detail += f"; unharmed, yielding to other work for {QUIET_SECONDS:.0f}s"
            return False, detail, before, after
        deadline = time.monotonic() + _TICK_BUDGET_SECONDS
        intervened = False
        for _ in range(_MAX_TICKS):
            acted = defend_or_flee(client, allow_safe_recovery_movement=True)
            intervened = intervened or acted
            if not acted or time.monotonic() >= deadline:
                break
    last = observe()
    after = int(getattr(last, "nearby_hostiles", 0) or 0)
    health_after = _health(last)
    # `nearby_hostiles` is a 24m-radius entity count, but a verified evade
    # (escape_recovery.run_away) only needs to clear a much shorter
    # separation before the state machine calls it resolved (RECOVER) --
    # a threat can still sit inside the wider count after a real, working
    # escape. Trusting the count delta alone mislabeled that as "deferred".
    # Prefer the state machine's own resolution when it's observable.
    mode = _resolved_mode(client)
    resolved = mode in (DefenseMode.CLEAR, DefenseMode.RECOVER)
    success = bool(intervened and (resolved or after < before))
    yielded = ""
    if not intervened and mode is DefenseMode.CLEAR:
        # Nothing here warranted action: stop preempting other work for a
        # while, unless the number of hostiles grows.
        _quiet_for(after, health_after)
    elif not success and _unharmed(health_before, health_after):
        _quiet_for(after, health_after)
        yielded = f"; unharmed, yielding to other work for {QUIET_SECONDS:.0f}s"
    else:
        _quiet.update(until=0.0, hostiles=0, health=0.0)
    if after < before:
        detail = f"nearby hostiles reduced {before}->{after}"
    elif resolved and mode is not None:
        detail = f"defensive recovery resolved ({mode.value}); nearby hostiles {before}->{after}"
    else:
        detail = f"defensive recovery attempted; nearby hostiles {before}->{after}"
    return success, detail + yielded, before, after


def _unharmed(before: Optional[float], after: Optional[float]) -> bool:
    """Both readings known and no health lost across the pass."""
    return before is not None and after is not None and after >= before


__all__ = ["run_self_defense", "select_self_defense"]
