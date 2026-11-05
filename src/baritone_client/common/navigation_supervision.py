"""Bounded, telemetry-aware navigation loops.

The public entry points remain in :mod:`navigation`; this module owns the
supervision lifecycle so movement helpers stay small and composable.
"""

from __future__ import annotations

import math
import time

from .tasks import PlayerDeathDetected
from .home_surface import below_home_surface, home_route_floor, protect_home_route


_NO_MOVEMENT_TIMEOUT_SECONDS = 30.0
_POSITION_PROGRESS_EPSILON = 0.05


def _finish_navigation(client, outcome: str, evidence: dict) -> None:
    """Publish one terminal navigation outcome when telemetry supports it."""
    try:
        client._last_navigation_outcome = outcome
        client._last_navigation_evidence = dict(evidence)
    except Exception:
        pass
    try:
        from .. import observability

        finish = getattr(observability, "finish_navigation", None)
        if finish is not None:
            finish(outcome, dict(evidence))
    except Exception:
        pass


def _cancel_once(client, cancelled: list[bool], *, last_state=None) -> dict:
    """Cancel once, then use one bounded post-cancel read as evidence."""
    if cancelled[0]:
        return dict(getattr(client, "_last_navigation_cancel", {}))
    cancelled[0] = True
    evidence = {"cancel_requested": True, "cancel_status": "unknown"}
    try:
        client.transport.dispatch("cancel", {})
    except Exception as exc:
        evidence["cancel_error"] = type(exc).__name__
    read_started = time.monotonic()
    try:
        try:
            state = client.transport.dispatch("get_state", {}, timeout=2.0)
        except TypeError:
            # Older test doubles and transports do not expose a timeout kwarg.
            # The first call cannot have dispatched in that case, so this is
            # still one post-cancel state read from the bridge.
            state = client.transport.dispatch("get_state", {})
    except Exception as exc:
        evidence["stop_read_error"] = type(exc).__name__
        state = None
    try:
        read_elapsed = time.monotonic() - read_started
    except Exception:
        read_elapsed = 0.0
    if read_elapsed > 2.0:
        evidence["stop_read_timeout_seconds"] = round(read_elapsed, 3)
    elif isinstance(state, dict):
        valid = True
        position = state.get("block_position", state.get("position"))
        if position is not None:
            try:
                valid = all(
                    math.isfinite(float(position[key]))
                    for key in ("x", "y", "z")
                )
            except (KeyError, TypeError, ValueError):
                valid = False
        if "health" in state:
            try:
                valid = valid and math.isfinite(float(state["health"]))
            except (TypeError, ValueError):
                valid = False
        evidence["stop_state"] = dict(state)
        if valid and state.get("is_pathing") is False:
            evidence["cancel_status"] = "stopped"
        elif not valid:
            evidence["stop_read_error"] = "malformed_state"
    try:
        client._last_navigation_cancel = dict(evidence)
    except Exception:
        pass
    return evidence


def _end_navigation(client, cancelled, outcome: str, *, evidence=None, last_state=None) -> bool:
    """Finish one navigation, preserving observed arrival before cleanup."""
    evidence = dict(evidence or {})
    if outcome == "arrived":
        _finish_navigation(client, outcome, evidence)
        _cancel_once(client, cancelled, last_state=last_state)
        return True
    cancel_evidence = _cancel_once(client, cancelled, last_state=last_state)
    evidence.update(cancel_evidence)
    if cancel_evidence.get("cancel_status") != "stopped":
        evidence["requested_outcome"] = outcome
        outcome = "unknown"
    _finish_navigation(client, outcome, evidence)
    return False


def _sleep_until(navigation, seconds: float, deadline: float) -> None:
    navigation.time.sleep(
        min(max(0.0, float(seconds)), max(0.0, deadline - navigation.time.monotonic()))
    )


@protect_home_route(safe_movement=True)
def goto(client, x: int, y: int, z: int, timeout: int = 120,
         check_interval: float = 2.0, tolerance: float = 3.0,
         on_tick=None, on_defense=None, defense_check_interval: float = 0.5,
         radius: int = 0) -> bool:
    """Navigate to exact coordinates under fresh-state safety supervision.

    ``radius`` > 0 asks Baritone for any stand within that many blocks of the
    target (``GoalNear``) instead of the block itself, for targets the player
    cannot occupy, such as a water source raised in a spill.
    """
    from . import navigation

    defense_check_interval = max(0.1, float(defense_check_interval))
    cancelled = [False]
    goal_active = False
    last_state = None
    try:
        client._last_navigation_survival_abort = False
        try:
            initial_state = navigation._verified_navigation_state(
                client.transport.dispatch("get_state", {})
            )
        except navigation._UnsafeNavigationTelemetry:
            return False
        if navigation._refuse_critical_long_travel(client, x, y, z, state=initial_state):
            return False
        home_guard = home_route_floor(client, x, y, z)
        # A player already below home, or underground anywhere, may attempt
        # an exact upward exit. Once the floor is reached, dropping back
        # below it must abort the route.
        surface_reached = not below_home_surface(
            home_guard, initial_state["block_position"], client, anywhere=True)
        payload = {"x": x, "y": y, "z": z}
        if int(radius) > 0:
            payload["radius"] = int(radius)
        navigation._dispatch_indeterminate_goal(client, "goto", payload)
        goal_active = True
        state_window = navigation._VerifiedStateWindow(initial_state)
        deadline = navigation.time.monotonic() + max(0.0, float(timeout))
        progress_deadline = min(
            deadline, navigation.time.monotonic() + _NO_MOVEMENT_TIMEOUT_SECONDS
        )
        last_position = None
        last_state = initial_state
        idle_unpathing_checks = 0
        while True:
            now = navigation.time.monotonic()
            if now >= deadline:
                return _end_navigation(client, cancelled, "timeout", last_state=last_state)
            if on_tick:
                on_tick()
            state = state_window.read(client)
            if state is None:
                _sleep_until(navigation, min(float(check_interval), defense_check_interval), deadline)
                continue
            last_state = state
            below = below_home_surface(home_guard, state["block_position"], client)
            if below and surface_reached:
                return _end_navigation(client, cancelled, "home_surface_abort", last_state=state)
            # Tunnel cells outside home are still underground, not surface.
            surface_reached = surface_reached or not below_home_surface(
                home_guard, state["block_position"], client, anywhere=True)
            from .combat import survival_tick

            if survival_tick(client, state):
                client._last_navigation_survival_abort = True
                return _end_navigation(client, cancelled, "survival_abort", last_state=state)
            if navigation.run_navigation_defense(client, on_defense):
                return _end_navigation(client, cancelled, "defense_abort", last_state=state)
            position = state["block_position"]
            px, py, pz = position["x"], position["y"], position["z"]
            distance = ((px - x) ** 2 + (py - y) ** 2 + (pz - z) ** 2) ** 0.5
            if distance <= tolerance and not below:
                return _end_navigation(
                    client, cancelled, "arrived",
                    evidence={"observed_arrival": True, "position": dict(position),
                              "distance": distance, "tolerance": float(tolerance)},
                    last_state=state,
                )
            current_position = (float(px), float(py), float(pz))
            is_pathing = state.get("is_pathing")
            if is_pathing is False and current_position == last_position:
                idle_unpathing_checks += 1
            elif is_pathing is False:
                idle_unpathing_checks = 1
            else:
                idle_unpathing_checks = 0
            if (last_position is None or
                    math.dist(current_position, last_position) > _POSITION_PROGRESS_EPSILON):
                progress_deadline = min(
                    deadline, navigation.time.monotonic() + _NO_MOVEMENT_TIMEOUT_SECONDS
                )
            last_position = current_position
            if idle_unpathing_checks >= 3:
                return _end_navigation(client, cancelled, "stalled", last_state=state)
            if navigation.time.monotonic() >= progress_deadline:
                return _end_navigation(client, cancelled, "stalled", last_state=state)
            _sleep_until(navigation, min(float(check_interval), defense_check_interval), deadline)
    except PlayerDeathDetected:
        _end_navigation(client, cancelled, "death", last_state=last_state)
        raise
    except navigation._UnsafeNavigationTelemetry:
        if goal_active:
            _end_navigation(client, cancelled, "telemetry_invalid", last_state=last_state)
        return False
    except Exception:
        if goal_active:
            _end_navigation(client, cancelled, "error", last_state=last_state)
        raise


@protect_home_route(horizontal=True, safe_movement=True)
def goto_xz(client, x: int, z: int, timeout: int = 120,
            check_interval: float = 1.0, tolerance: float = 6.0,
            on_defense=None, defense_check_interval: float = 0.5) -> bool:
    """Navigate to a horizontal column while Baritone chooses terrain Y."""
    from . import navigation

    defense_check_interval = max(0.1, float(defense_check_interval))
    cancelled = [False]
    goal_active = False
    last_state = None
    try:
        client._last_navigation_survival_abort = False
        try:
            initial_state = navigation._verified_navigation_state(
                client.transport.dispatch("get_state", {})
            )
        except navigation._UnsafeNavigationTelemetry:
            return False
        initial_y = round(initial_state["block_position"]["y"])
        if navigation._refuse_critical_long_travel(client, x, initial_y, z, state=initial_state):
            return False
        anchor = getattr(client, "_protected_home_anchor", None)
        home_guard = home_route_floor(client, x, anchor[1] if anchor else initial_y, z)
        if below_home_surface(home_guard, initial_state["block_position"], client):
            return False  # A column goal cannot prove upward egress.
        navigation._dispatch_indeterminate_goal(client, "chat", {"message": f"#goto {x} {z}"})
        goal_active = True
        state_window = navigation._VerifiedStateWindow(initial_state)
        deadline = navigation.time.monotonic() + max(0.0, float(timeout))
        progress_deadline = min(
            deadline, navigation.time.monotonic() + _NO_MOVEMENT_TIMEOUT_SECONDS
        )
        last_position = None
        last_state = initial_state
        idle_unpathing_checks = 0
        while True:
            now = navigation.time.monotonic()
            if now >= deadline:
                return _end_navigation(client, cancelled, "timeout", last_state=last_state)
            state = state_window.read(client)
            if state is None:
                _sleep_until(navigation, min(float(check_interval), defense_check_interval), deadline)
                continue
            last_state = state
            if below_home_surface(home_guard, state["block_position"], client):
                return _end_navigation(client, cancelled, "home_surface_abort", last_state=state)
            from .combat import survival_tick

            if survival_tick(client, state):
                client._last_navigation_survival_abort = True
                return _end_navigation(client, cancelled, "survival_abort", last_state=state)
            if navigation.run_navigation_defense(client, on_defense):
                return _end_navigation(client, cancelled, "defense_abort", last_state=state)
            position = state["block_position"]
            px, pz = float(position["x"]), float(position["z"])
            distance = math.hypot(px - x, pz - z)
            if distance <= tolerance:
                return _end_navigation(
                    client, cancelled, "arrived",
                    evidence={"observed_arrival": True, "position": dict(position),
                              "distance": distance, "tolerance": float(tolerance)},
                    last_state=state,
                )
            current_position = (px, pz)
            is_pathing = state.get("is_pathing")
            if is_pathing is False and current_position == last_position:
                idle_unpathing_checks += 1
            elif is_pathing is False:
                idle_unpathing_checks = 1
            else:
                idle_unpathing_checks = 0
            if (last_position is None or
                    math.dist(current_position, last_position) > _POSITION_PROGRESS_EPSILON):
                progress_deadline = min(
                    deadline, navigation.time.monotonic() + _NO_MOVEMENT_TIMEOUT_SECONDS
                )
            last_position = current_position
            if idle_unpathing_checks >= 3:
                return _end_navigation(client, cancelled, "stalled", last_state=state)
            if navigation.time.monotonic() >= progress_deadline:
                return _end_navigation(client, cancelled, "stalled", last_state=state)
            _sleep_until(navigation, min(float(check_interval), defense_check_interval), deadline)
    except PlayerDeathDetected:
        _end_navigation(client, cancelled, "death", last_state=last_state)
        raise
    except navigation._UnsafeNavigationTelemetry:
        if goal_active:
            _end_navigation(client, cancelled, "telemetry_invalid", last_state=last_state)
        return False
    except Exception:
        if goal_active:
            _end_navigation(client, cancelled, "error", last_state=last_state)
        raise
