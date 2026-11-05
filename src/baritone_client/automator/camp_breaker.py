"""Stop a blocked bot from camping in place forever.

A scheduling hold is meant to be temporary: wait out the night, let health
regenerate, let a creeper wander off. Nothing bounded it, so a bot whose
blocker never clears holds forever. Bot18 -- the fleet's food worker -- stood
healthy and well fed inside a nest of 26 hostiles for hours, unable to satisfy
``nearby_hostiles == 0`` and unable to conclude that the spot was the problem.
The fleet starved while its food worker waited politely.

Two facts made it invisible. ``escalation_level`` was computed and never read
by anything, and ``record_productive_attempt`` only fires when an attempt
actually runs -- so a bot that holds never increments a no-progress streak at
all. Camping was not merely tolerated, it was unmeasured.

The rule here: a hold is a decision to do nothing, and repeated decisions to
do nothing are a failure. After enough of them, leave. Relocation may end
badly -- the new ground may be worse, the trip may kill the bot -- but a bot
that dies resolves, while a bot that camps produces nothing and reports
nothing anyone can act on.

Movement runs as recovery navigation on purpose. The critical-health travel
gate refuses long routes below the regeneration floor, and a starving camped
bot is exactly who needs to move; gating the escape on the condition the
escape relieves is the deadlock this module exists to break.
"""

from __future__ import annotations

import time
from math import hypot
from typing import Any, Mapping, Tuple

#: Consecutive holds tolerated before the bot must move. At roughly one
#: scheduling tick per 30s this is a few minutes of genuine waiting, which
#: covers a night cycle or a wandering mob without tolerating a camp.
CAMP_HOLD_LIMIT = 12
#: How far to relocate. Far enough to leave a mob-dense pocket and reach
#: different terrain, short enough to stay bounded and interruptible.
CAMP_BREAK_DISTANCE = 96
#: Headings rotate so a bot that must break camp repeatedly fans out instead
#: of retreading one corridor.
_HEADINGS: Tuple[Tuple[int, int], ...] = (
    (1, 0), (0, 1), (-1, 0), (0, -1),
    (1, 1), (-1, -1), (1, -1), (-1, 1),
)
_LEDGER = "camp_holds"


def _ledger(state: Any) -> dict:
    custom = getattr(state, "custom_data", None)
    if not isinstance(custom, dict):
        custom = {}
        state.custom_data = custom
    ledger = custom.setdefault(_LEDGER, {})
    if not isinstance(ledger, dict):
        ledger = {}
        custom[_LEDGER] = ledger
    return ledger


def record_hold(state: Any, reason: str, *, now: float | None = None) -> int:
    """Count one decision to do nothing. Returns the consecutive streak."""
    ledger = _ledger(state)
    timestamp = time.time() if now is None else float(now)
    previous = str(ledger.get("reason", ""))
    streak = int(ledger.get("streak", 0) or 0)
    # A changed blocker means the situation is still moving; only an
    # unchanging reason indicates a genuine camp.
    ledger["streak"] = streak + 1 if reason == previous else 1
    ledger["reason"] = str(reason)
    ledger["last_hold_at"] = timestamp
    return int(ledger["streak"])


def clear_holds(state: Any) -> None:
    """Any real work proves the bot is not camped."""
    ledger = _ledger(state)
    ledger["streak"] = 0
    ledger["reason"] = ""


def hold_streak(state: Any) -> int:
    try:
        return int(_ledger(state).get("streak", 0) or 0)
    except (TypeError, ValueError):
        return 0


def should_break_camp(state: Any, *, limit: int = CAMP_HOLD_LIMIT) -> bool:
    """True once the bot has refused to act too many times in a row."""
    return hold_streak(state) >= max(1, int(limit))


def next_heading(state: Any) -> Tuple[int, int]:
    """Return the next relocation heading, rotating across attempts."""
    ledger = _ledger(state)
    index = int(ledger.get("heading_index", 0) or 0)
    ledger["heading_index"] = (index + 1) % len(_HEADINGS)
    return _HEADINGS[index % len(_HEADINGS)]


def break_camp(
    client: Any,
    state: Any,
    *,
    distance: int = CAMP_BREAK_DISTANCE,
    goto: Any = None,
) -> bool:
    """Relocate away from a spot the bot has proven it cannot work in.

    Returns True when the bot actually moved. Failure is not fatal: the streak
    persists, a different heading is tried next time, and the caller may
    escalate further.
    """
    from ..common.navigation import allow_recovery_navigation
    from ..common.navigation import goto as default_goto

    travel = default_goto if goto is None else goto
    try:
        snapshot = client.transport.dispatch("get_state", {})
    except Exception:
        return False
    position = snapshot.get("block_position", snapshot.get("position", {})) or {}
    try:
        x = int(position["x"])
        y = int(position["y"])
        z = int(position["z"])
    except (KeyError, TypeError, ValueError):
        return False

    dx, dz = next_heading(state)
    target = (x + dx * int(distance), y, z + dz * int(distance))
    reason = str(_ledger(state).get("reason", "unknown"))
    print(
        f"  BREAK CAMP: held {hold_streak(state)}x on '{reason}'; "
        f"relocating {distance} blocks to {target}"
    )

    # Recovery-marked: the travel gate must not refuse the escape from the
    # very condition that triggers the gate.
    @allow_recovery_navigation
    def _travel(_client: Any) -> bool:
        return bool(travel(_client, target[0], target[1], target[2], timeout=120))

    try:
        moved = _travel(client)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"  BREAK CAMP failed: {exc}")
        return False
    if not moved:
        # Baritone can report a failed route after doing the useful part of
        # the work (live Bot18: traversed the full 96-block relocation, then
        # immediately relocated again because the stale streak remained 13).
        # Measure the postcondition before classifying the recovery as failed.
        try:
            after = client.transport.dispatch("get_state", {})
            after_position = after.get(
                "block_position", after.get("position", {})
            ) or {}
            displacement = hypot(
                float(after_position["x"]) - x,
                float(after_position["z"]) - z,
            )
            if displacement >= min(12.0, max(3.0, float(distance) / 4.0)):
                print(
                    "  BREAK CAMP: verified "
                    f"{displacement:.0f}-block relocation despite route result"
                )
                moved = True
        except (KeyError, TypeError, ValueError):
            pass
    if moved:
        clear_holds(state)
    return bool(moved)


def note_hold(client: Any, state: Any, reason: str) -> bool:
    """Record one hold and break camp if the bot has held too many times."""
    record_hold(state, reason)
    if not should_break_camp(state):
        return False
    return break_camp(client, state)


#: mutual_aid.py raises a fleet-aid request at this same threshold; a solo
#: bot has no fleet to answer it, so this is the self-rescue fallback.
ESCALATION_SELF_RESCUE_THRESHOLD = 3


def self_rescue_if_escalated(
    client: Any, state: Any, attempt: Any, *, goto: Any = None
) -> bool:
    """Break camp once work_progress reports sustained no-progress escalation.

    Repeated defensive work can monopolize the scheduler without changing
    the environment. A solo worker needs a bounded local fallback when
    repeated no-progress attempts would otherwise request fleet assistance.
    """
    level = int(getattr(attempt, "escalation_level", 0) or 0)
    if level < ESCALATION_SELF_RESCUE_THRESHOLD:
        return False
    try:
        return break_camp(client, state, goto=goto)
    except Exception as exc:  # best-effort; never kill the scheduler
        print(f"  Escalation self-rescue failed: {exc}")
        return False


__all__ = [
    "note_hold",
    "self_rescue_if_escalated",
    "CAMP_HOLD_LIMIT",
    "CAMP_BREAK_DISTANCE",
    "ESCALATION_SELF_RESCUE_THRESHOLD",
    "break_camp",
    "clear_holds",
    "hold_streak",
    "next_heading",
    "record_hold",
    "should_break_camp",
]
