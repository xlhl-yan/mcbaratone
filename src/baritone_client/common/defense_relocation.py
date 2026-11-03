"""Observed, height-bounded fallback relocation without terrain excavation."""

import math
import time

from .home_surface import HOME_RADIUS, _read_break_setting, _write_break_setting
from .tasks import PlayerDeathDetected


def relocation_floor(client, position):
    """Retain the current level and active protected surface floors."""
    floor = float(position["y"]) - 2
    anchor = getattr(client, "_protected_home_anchor", None)
    if anchor and math.hypot(position["x"] - anchor[0], position["z"] - anchor[2]) <= HOME_RADIUS:
        floor = max(floor, anchor[1] - 2)
    work = getattr(client, "_protected_surface_work", None)
    if work is not None:
        try:
            if (
                len(work) == 3
                and all(math.isfinite(float(value)) for value in work)
                and math.hypot(position["x"] - work[0], position["z"] - work[2])
                <= HOME_RADIUS
            ):
                floor = max(floor, float(work[1]))
        except (TypeError, ValueError, OverflowError):
            pass

    runtime = getattr(client, "_mcbaratone_defense_runtime", None)
    if runtime is None:
        runtime = getattr(client.transport, "_mcbaratone_defense_runtime", None)
    previous = getattr(runtime, "escape_floor", None)
    if previous is not None:
        try:
            if math.isfinite(float(previous)):
                floor = max(floor, float(previous))
        except (TypeError, ValueError, OverflowError):
            pass
    if runtime is not None and hasattr(runtime, "escape_floor"):
        runtime.escape_floor = floor
    return floor


def relocate(client, threat, *, distance=28, timeout=30):
    """Choose a supported endpoint, forbid digging, and verify real escape."""
    from . import combat as api
    from .escape_recovery import separation_from, surface_adjusted_candidates
    from .defense import assess_threats, plan_escape_candidates

    previous = None
    verified = False
    try:
        state = client.transport.dispatch("get_state", {})
        position = state.get("block_position", state.get("position", {}))
        if not position or api.entity_position(threat) is None:
            return False
        if not all(math.isfinite(float(position[axis])) for axis in ("x", "y", "z")):
            return False
        floor = relocation_floor(client, position)
        initial = separation_from(threat, position)
        gain = max(8.0, min(distance * .5, 12.0))
        planned = plan_escape_candidates(position, assess_threats([threat], state), distance=distance)
        candidates = surface_adjusted_candidates(client, planned, radius=16)
        candidates = [c for c in candidates if c.y >= floor and
                      separation_from(threat, {"x": c.x, "z": c.z}) >= initial + gain]
        if not candidates:
            print("DEFENSE: relocation refused: no observed height-safe landing")
            return False
        previous = _read_break_setting(client)
        if previous == "true":
            _write_break_setting(client, "false")
        target = candidates[0]
        client.transport.dispatch("goal", {"x": target.x, "y": target.y, "z": target.z})
        client.transport.dispatch("chat", {"message": "#path"})
        deadline = time.monotonic() + timeout
        clear = 0
        while time.monotonic() < deadline:
            time.sleep(.5)
            api.ensure_alive(client)
            snapshot = api._get_combat_snapshot(client, radius=40)
            if snapshot is None or snapshot.get("skipped_count", 0):
                clear = 0
                continue
            live = snapshot["player"]
            here = live.get("position", live.get("block_position", {}))
            if not all(math.isfinite(float(here[axis])) for axis in ("x", "y", "z")):
                return False
            if not math.isfinite(float(live["health"])):
                return False
            if float(here["y"]) < floor or float(live["health"]) < 12:
                return False
            enemies = assess_threats(snapshot["entities"], live)
            original = next((e for e in snapshot["entities"] if e.get("id") == threat.get("id")), None)
            separated = original is None or separation_from(original, here) >= initial + gain
            moved = math.hypot(here["x"] - position["x"], here["z"] - position["z"]) >= 3
            clear = clear + 1 if separated and moved and not any(e.distance < 12 for e in enemies) else 0
            if clear >= 2:
                print("DEFENSE: relocation separation verified on height-safe route")
                verified = True
                break
    except PlayerDeathDetected:
        raise
    except Exception as exc:
        print(f"DEFENSE: relocation refused ({exc})")
        return False
    finally:
        # Even a successful escape must not leave its goal running into work.
        try:
            api._stop_for_defense(client)
        except Exception:
            # No verified cancellation means neither restoration nor success.
            verified = False
            previous = None
        if previous is not None:
            restored = previous != "true"
            try:
                stopped = client.transport.dispatch("get_state", {})
                if stopped.get("is_pathing") is not False:
                    verified = False
                elif previous == "true":
                    _write_break_setting(client, previous)
                    restored = True
            except Exception:
                verified = False  # Unknown stop: leave excavation disabled.
            if not restored:
                # Excavation stays off for now, but is owed back. Leaving it
                # off silently kept live A1 from digging for 11 days after one
                # relocation on 2026-10-22, until it was sealed underground.
                from .home_surface import _owe

                _owe(client, {"allowBreak": "true"})
                print("DEFENSE: relocation stop unverified; digging stays off until it can be restored")
    return verified
