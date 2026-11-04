"""Keep surface-bound home routes out of the underground home footprint.

The saved anchor identifies the route, not proof of a floor or shelter. This
guard constrains protected home routes, but does not promise a safe route or
excavation.
"""

import math
import time
from functools import wraps
from inspect import signature
from .escape_recovery import _HAZARDS, _passable
from .tasks import PlayerDeathDetected, SurvivalRecoveryRequired


HOME_RADIUS = 24.0
HOME_FLOOR_MARGIN = 2.0


def bind_home_surface(client, state, *, controller_start=False):
    """Install a finite home anchor, clearing stale state on invalid input.

    ``controller_start``: Baritone saves ``#set`` changes, so never inherit a
    crashed run's water ban or a guard's conservative route settings.
    """
    custom = getattr(state, "custom_data", {})
    anchor = custom.get("base_location") if isinstance(custom, dict) else None
    valid = isinstance(anchor, (list, tuple)) and len(anchor) == 3
    try:
        valid = valid and all(not isinstance(v, bool) and math.isfinite(float(v)) for v in anchor)
        client._protected_home_anchor = tuple(float(v) for v in anchor) if valid else None
    except (TypeError, ValueError):
        client._protected_home_anchor = None
    from .water_hazards import allow_water, bind_drowning_hazards

    bind_drowning_hazards(client, state)
    if controller_start:
        try:
            allow_water(client, force=True)
            reset_guarded_route_settings(client)
        except Exception as exc:
            print(f"Route setting reset deferred: {exc}")
    elif getattr(client, "_home_route_owed", None):
        restore_owed_home_route_settings(client)


def home_route_floor(client, x, y, z):
    """Guard only homeward surface goals, not explicit underground mining."""
    work = getattr(client, "_protected_surface_work", None)
    if work is not None and math.hypot(x - work[0], z - work[2]) <= HOME_RADIUS:
        return work
    anchor = getattr(client, "_protected_home_anchor", None)
    if anchor is None:
        return None
    hx, hy, hz = anchor
    floor = hy - HOME_FLOOR_MARGIN
    if math.hypot(x - hx, z - hz) > HOME_RADIUS or y < floor:
        return None
    return hx, floor, hz


#: Open sky proves surface ground this far under the flat floor; deeper,
#: the flat floor alone decides.
_OPEN_SKY_DEPTH = 12.0
_ROOF_VIEW = 8
_ROOF_REACH = 6
_ROOF_TTL = 20.0


def below_home_surface(guard, position, client=None, *, anywhere=False):
    """A cave elsewhere is not this guard's concern.

    With ``client``, ground under open sky is surface however low it lies.
    Live A1's house sits on a hill: its yard, farm and mine entrance lie
    2-5 blocks under the flat floor, so 152 walks home in one night were
    aborted as "underground", and the egress then dug staircases into the
    yard. ``anywhere``: outside home, a proven roof still counts, so a
    route out of a mine tunnel has not yet reached the surface.
    """
    if guard is None:
        return False
    hx, floor, hz = guard
    if position["y"] >= floor:
        return False
    if math.hypot(position["x"] - hx, position["z"] - hz) > HOME_RADIUS:
        return anywhere and client is not None and _under_roof(client, position) is True
    if client is None or position["y"] < floor - _OPEN_SKY_DEPTH:
        return True
    return _under_roof(client, position) is not False


def _under_roof(client, position):
    """One fresh view: True sealed, False open sky, None unknown.

    A roof over the bot's own column alone is not a sealed pocket. Live A1
    resumed on grass at (-422,75,-1) under one stray cobblestone at y=78,
    open sky all round, and dug an egress staircase into its own yard.
    Open sky over a cell it can walk to in ``_EXIT_STEPS`` steps also proves
    the surface; a tunnel under solid terrain has no such cell.
    """
    cell = tuple(int(position[axis]) for axis in ("x", "y", "z"))
    now = time.monotonic()
    cache = getattr(client, "_home_roof_cache", None)
    if not isinstance(cache, dict) or len(cache) > 256:
        cache = client._home_roof_cache = {}
    if cell in cache and cache[cell][1] > now:
        return cache[cell][0]
    try:
        view = client.transport.dispatch("get_view", {"radius": _ROOF_VIEW})
        if view.get("success") is False:
            raise ValueError("view refused")
        # get_view omits air, so an absent cell is air or unknown.
        blocks = {
            (int(v["x"]), int(v["y"]), int(v["z"])): str(v.get("id", "")).split(":")[-1]
            for v in view.get("data", view)["voxels"]
        }
        roofed = _roofed(blocks, cell) and not _open_exit(blocks, cell)
    except Exception:
        roofed = None  # unproven sky keeps the flat floor
    cache[cell] = (roofed, now + _ROOF_TTL)
    return roofed


#: Walking steps searched for open sky from under a roof.
_EXIT_STEPS = 2
_NO_FLOOR = frozenset({"water", "lava", "bubble_column", "powder_snow"})


def _roofed(blocks, cell):
    x, y, z = cell
    return any(
        not blocks[(x, h, z)].endswith("_leaves")
        for h in range(y + 2, y + 2 + _ROOF_REACH) if (x, h, z) in blocks
    )


def _clear(blocks, x, z, low, high):
    return all(_passable(blocks.get((x, h, z), "air")) for h in range(low, high + 1))


def _stands(blocks, x, y, z):
    """A proven floor under clear, hazard-free feet and head."""
    floor = blocks.get((x, y - 1, z))
    if not floor or floor in _NO_FLOOR or _passable(floor):
        return False  # no voxel: air or unknown, never a proven floor
    cells = (floor, blocks.get((x, y, z), ""), blocks.get((x, y + 1, z), ""))
    if any(token in name for name in cells for token in _HAZARDS):
        return False
    return _clear(blocks, x, z, y, y + 1)


def _open_exit(blocks, start):
    """Whether a walk of ``_EXIT_STEPS`` reaches a cell under open sky."""
    seen, frontier = {start}, [start]
    for _ in range(_EXIT_STEPS):
        reached = []
        for x, y, z in frontier:
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                for ny in (y, y + 1, y - 1):
                    nxt = (x + dx, ny, z + dz)
                    top = max(y, ny) + 1  # a step needs headroom in both columns
                    if (nxt in seen or not _stands(blocks, *nxt)
                            or not _clear(blocks, x, z, y, top)
                            or not _clear(blocks, nxt[0], nxt[2], ny, top)):
                        continue
                    if not _roofed(blocks, nxt):
                        return True
                    seen.add(nxt)
                    reached.append(nxt)
        frontier = reached
    return False


def _checked_response_data(response, operation):
    data = response
    seen = set()
    while isinstance(data, dict) and id(data) not in seen:
        seen.add(id(data))
        if (data.get("success") is False or str(data.get("status", "")).lower() == "error"
                or data.get("error")):
            raise ValueError(f"{operation} returned an error response")
        nested = data.get("data")
        if not isinstance(nested, dict):
            return data
        data = nested
    raise ValueError(f"{operation} response is malformed")


def _read_break_setting(client, *, strict=False):
    response = client.transport.dispatch("settings", {"get": "allowBreak"})
    if strict:
        response = _checked_response_data(response, "allowBreak read")
    value = response.get("value") if isinstance(response, dict) else None
    if value not in ("true", "false"):
        raise ValueError("allowBreak setting unavailable or malformed")
    return value


def _write_break_setting(client, value, *, strict=False):
    response = client.transport.dispatch("settings", {"set": "allowBreak", "value": value})
    if strict:
        _checked_response_data(response, "allowBreak write")
    # The bridge replies 'requested', not 'applied'. Never start on that alone.
    for _ in range(_OBSERVE_POLLS):
        observed = (
            _read_break_setting(client, strict=True)
            if strict else _read_break_setting(client)
        )
        if observed == value:
            return
        time.sleep(_OBSERVE_INTERVAL)
    raise ValueError("allowBreak update was not observed")


_SAFE_ROUTE_SETTINGS = {"allowParkour": "false", "maxFallHeightNoWater": "1"}
#: The bridge applies a setting on a later client tick. Three 50 ms reads
#: reported correct restores as "not observed" whenever the client lagged:
#: A1 logged nine between 2026-10-15 and 10-18, and the last one left
#: digging disabled for three days. Wait about a second instead.
_OBSERVE_POLLS = 20
_OBSERVE_INTERVAL = 0.05
#: Settings a guard turns conservative for a while. A crash or an unverified
#: stop between "off" and "back on" is saved by Baritone and outlives the
#: controller; A1 then spent 25 hours sealed in a 1x1 shaft it could not dig
#: out of (2026-10-19..21). Controller start returns them to their defaults.
_GUARDED_SETTINGS = ("allowBreak", "allowSprint", "allowParkour", "maxFallHeightNoWater")


def reset_guarded_route_settings(client):
    """Return guard-owned Baritone settings to their defaults at controller start."""
    for name in _GUARDED_SETTINGS:
        client.transport.dispatch("chat", {"message": f"#set reset {name}"})
    client._home_route_owed = {}
    time.sleep(0.1)  # chat settings apply on a client tick


def _owe(client, owed):
    """Remember values an unverified stop left conservative. The first value
    owed is the real original; later reads only see the leftover."""
    pending = dict(getattr(client, "_home_route_owed", None) or {})
    for name, value in owed.items():
        pending.setdefault(name, value)
    client._home_route_owed = pending


def _write_owed(client, name, value):
    if name == "allowBreak":
        _write_break_setting(client, value)
    else:
        _write_route_setting(client, name, value)


def restore_owed_home_route_settings(client):
    """Retry restores an unverified stop deferred, once nothing is moving.

    Without this the guard restored whatever it read on entry, so one leaked
    "false" was re-read and kept by every later guard: a permanent ratchet.
    """
    owed = dict(getattr(client, "_home_route_owed", None) or {})
    if not owed or getattr(client, "_home_route_active", 0):
        return not owed
    try:
        if client.transport.dispatch("get_state", {}).get("is_pathing") is not False:
            return False
        for name, value in list(owed.items()):
            _write_owed(client, name, value)
            owed.pop(name)
    except Exception:
        client._home_route_owed = owed
        return False
    client._home_route_owed = owed
    print("HOME ROUTE: restored route settings an unverified stop had left conservative")
    return True


def _read_route_setting(client, name):
    response = client.transport.dispatch("settings", {"get": name})
    response = _checked_response_data(response, f"{name} read")
    key = response.get("key")
    if not isinstance(key, str) or key.lower() != name.lower():
        raise ValueError(f"{name} setting unavailable or malformed")
    value = response.get("value")
    if not isinstance(value, str):
        raise ValueError(f"{name} setting unavailable or malformed")
    if name == "allowParkour" and value not in ("true", "false"):
        raise ValueError("allowParkour setting unavailable or malformed")
    if name == "maxFallHeightNoWater":
        try:
            if str(int(value)) != value or int(value) < 0:
                raise ValueError
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("maxFallHeightNoWater setting unavailable or malformed") from exc
    return value


def _write_route_setting(client, name, value):
    response = client.transport.dispatch("settings", {"set": name, "value": value})
    _checked_response_data(response, f"{name} write")
    for _ in range(_OBSERVE_POLLS):
        if _read_route_setting(client, name) == value:
            return
        time.sleep(_OBSERVE_INTERVAL)
    raise ValueError(f"{name} update was not observed")


def protect_home_route(*, horizontal=False, surface_work=False, safe_movement=False):
    """Guard a homeward route's digging and optional movement settings.

    Safe movement is restricted to callers that opt in and goals inside the
    saved home/surface guard. If cleanup cannot prove the route stopped, leave
    temporary settings in their conservative state rather than alter a live goal.
    """
    def decorate(function):
        call_signature = signature(function)
        @wraps(function)
        def wrapped(*args, **kwargs):
            parameters = call_signature.bind(*args, **kwargs).arguments
            client, x, z = (parameters[key] for key in ("client", "x", "z"))
            anchor = getattr(client, "_protected_home_anchor", None)
            y = (anchor[1] if anchor else 0) if horizontal else parameters["y"]
            guarded = home_route_floor(client, x, y, z) is not None
            if surface_work and anchor is not None and y >= anchor[1] - HOME_FLOOR_MARGIN:
                guarded = True
            if not guarded:
                return function(*args, **kwargs)
            previous = None
            previous_movement = {}
            prior_work = getattr(client, "_protected_surface_work", None)
            result = False
            client._home_route_active = getattr(client, "_home_route_active", 0) + 1
            try:
                owed = getattr(client, "_home_route_owed", None) or {}
                previous = (
                    _read_break_setting(client, strict=True)
                    if safe_movement else _read_break_setting(client)
                )
                if owed.get("allowBreak") == "true":
                    previous = "true"  # our own leftover, not the caller's choice
                if safe_movement:
                    previous_movement = {
                        name: owed.get(name, _read_route_setting(client, name))
                        for name in _SAFE_ROUTE_SETTINGS
                    }
                # Read every prior value before the first write so partial
                # observations can never leave a half-applied travel policy.
                if previous == "true":
                    if safe_movement:
                        _write_break_setting(client, "false", strict=True)
                    else:
                        _write_break_setting(client, "false")
                if safe_movement:
                    for name, value in _SAFE_ROUTE_SETTINGS.items():
                        _write_route_setting(client, name, value)
                if surface_work:
                    client._protected_surface_work = (x, y - HOME_FLOOR_MARGIN, z)
                result = function(*args, **kwargs)
            except (PlayerDeathDetected, SurvivalRecoveryRequired):
                raise
            except (ValueError, RuntimeError) as exc:
                print(f"HOME ROUTE: refused unverified digging protection ({exc})")
            finally:
                client._protected_surface_work = prior_work
                client._home_route_active = max(0, getattr(client, "_home_route_active", 1) - 1)
                if previous == "true" or previous_movement:
                    owing = dict(previous_movement)
                    if previous == "true":
                        owing["allowBreak"] = "true"
                    try:
                        live = client.transport.dispatch("get_state", {})
                        if safe_movement:
                            live = _checked_response_data(live, "route stop read")
                        if live.get("is_pathing") is not False:
                            raise ValueError("route stop is unverified; safe settings remain active")
                        restore_failed = False
                        for name, value in reversed(tuple(previous_movement.items())):
                            try:
                                _write_route_setting(client, name, value)
                                owing.pop(name, None)
                            except Exception as exc:
                                restore_failed = True
                                print(f"HOME ROUTE: could not restore {name} ({exc})")
                        if previous == "true":
                            if safe_movement:
                                _write_break_setting(client, previous, strict=True)
                            else:
                                _write_break_setting(client, previous)
                            owing.pop("allowBreak", None)
                        if restore_failed:
                            result = False
                    except Exception as exc:
                        print(f"HOME ROUTE: could not restore protected route settings ({exc})")
                        result = False
                    # Restored values are no longer owed; anything left is
                    # retried once nothing is moving.
                    pending = {name: value for name, value in (getattr(client, "_home_route_owed", None) or {}).items()
                               if name not in previous_movement and not (name == "allowBreak" and previous == "true")}
                    client._home_route_owed = pending
                    if owing:
                        _owe(client, owing)
            if not result and guarded:
                excavate_out_from_below_home(client, x, z)
            return result
        return wrapped
    return decorate


#: A bot this close to home may be under the house; never excavate there.
_HOUSE_CLEARANCE = 8.0
_EGRESS_RETRY_SECONDS = 300.0


def excavate_out_from_below_home(client, goal_x, goal_z):
    """Dig an upward exit for a bot sealed in a pocket below the home floor.

    Guarded homeward routes run with digging off, so from a sealed pocket
    below the floor they have no path at all. Live A1 sat at (-425,67,4),
    21 blocks from home and 10 below its floor, failing 416 "could not get
    home" trips in three days. Tunnel to the surface away from the house;
    the next ordinary route then starts above the floor.
    """
    anchor = getattr(client, "_protected_home_anchor", None)
    now = time.monotonic()
    if anchor is None or now < getattr(client, "_below_home_egress_after", 0.0):
        return False
    if math.hypot(goal_x - anchor[0], goal_z - anchor[2]) > _HOUSE_CLEARANCE:
        return False  # only a route home; a failed hop near the mine entrance is not this
    try:
        live = client.transport.dispatch("get_state", {})
        position = live.get("block_position", live.get("position", {}))
        here = tuple(int(position[axis]) for axis in ("x", "y", "z"))
    except Exception:
        return False
    guard = home_route_floor(client, here[0], anchor[1], here[2])
    if (not below_home_surface(guard, dict(zip(("x", "y", "z"), here)), client)
            or math.hypot(here[0] - anchor[0], here[2] - anchor[2]) <= _HOUSE_CLEARANCE):
        return False
    client._below_home_egress_after = now + _EGRESS_RETRY_SECONDS
    from .build_site_recovery import excavate_surface_egress

    print(f"HOME ROUTE: sealed below the home floor at {here}; excavating an upward exit away from the house")
    return excavate_surface_egress(
        client, origin=here, expected_y=int(anchor[1]),
        avoid=anchor, avoid_radius=_HOUSE_CLEARANCE,
    ) is not None
