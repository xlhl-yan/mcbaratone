"""Return from a bounded food attempt even when it produced no bread."""

import time
from functools import wraps

from .home_surface import bind_home_surface, protect_home_route
from .navigation import allow_recovery_navigation
from .tasks import PlayerDeathDetected, SurvivalRecoveryRequired


def _position(client):
    from .food_workstation import _fresh_safe_position

    return _fresh_safe_position(client, require_grounded=True)


@allow_recovery_navigation
@protect_home_route(surface_work=True, safe_movement=True)
def _return(client, x, y, z, state):
    from .house_door_travel import return_to_saved_house

    return return_to_saved_house(client, state, (x, y, z))


def return_after_food_cycle(function):
    """Keep production credit separate from a freshly observed return."""
    @wraps(function)
    def wrapped(client, state, *args, **kwargs):
        failed_survival = False
        try:
            return function(client, state, *args, **kwargs)
        except (PlayerDeathDetected, SurvivalRecoveryRequired):
            failed_survival = True
            raise
        finally:
            if not failed_survival:
                return_from_food_attempt(client, state)
    return wrapped


def return_from_food_attempt(client, state):
    """Use the saved home only as a target; verify the actual XYZ arrival."""
    custom = getattr(state, "custom_data", {})
    if not isinstance(custom, dict) or not custom.get("structures", {}).get("starter_house"):
        return False
    bind_home_surface(client, state)
    anchor = getattr(client, "_protected_home_anchor", None)
    if anchor is None:
        return False
    record = custom.setdefault("food_return", {})
    record["verified"] = False
    from .house_door_travel import saved_house_arrival
    try:
        client.transport.dispatch("stop", {})
        position = None
        for _ in range(5):
            position = _position(client)
            if position is not None:
                break
            time.sleep(0.1)
        if position is None:
            return False
        record["last_attempt_at"] = time.time()
        if not saved_house_arrival(position, state, anchor):
            _return(client, *anchor, state)
        observed = _position(client)
        verified = saved_house_arrival(observed, state, anchor)
        record["verified"] = verified
        if verified:
            record["last_verified_at"] = time.time()
        return verified
    except (PlayerDeathDetected, SurvivalRecoveryRequired):
        raise
    except Exception as exc:
        record["verified"] = False
        record["reason"] = str(exc)
        return False
