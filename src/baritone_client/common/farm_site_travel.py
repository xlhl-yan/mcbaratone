"""Bring a farm worker to a planned plot site before its terrain is judged."""

from __future__ import annotations

from math import hypot
from typing import Any, Sequence
from .home_surface import protect_home_route


#: `get_view` only reports a small cube around the player, so a planned site
#: can only be resolved to terrain once the worker is standing near it.
CANDIDATE_VIEW_DISTANCE = 6.0
#: Candidates sit within the siting radius of base, so from base this is a
#: short walk; a site that cannot be reached in this time is a poor site.
CANDIDATE_TRAVEL_TIMEOUT = 60


def horizontal_distance(client: Any, target: Sequence[int]) -> float:
    """Horizontal distance from the worker to a point, NaN when unknown."""
    try:
        snapshot = client.transport.dispatch("get_state", {})
        position = snapshot.get("block_position", snapshot.get("position", {})) or {}
        return hypot(
            float(position["x"]) - float(target[0]),
            float(position["z"]) - float(target[2]),
        )
    except Exception:
        return float("nan")


def approach_candidate(client: Any, candidate: Sequence[int]) -> None:
    """Walk to a planned site so its terrain is inside the observed view.

    Siting used to resolve the candidate from wherever the worker stood. The
    view is a few blocks around the player, so every site further away read
    as "no tillable ground" and was blacklisted. A worker hundreds of blocks
    from base rejected every site around it that way, then held progression
    for weeks waiting on a farm it could never site.
    """
    separation = horizontal_distance(client, candidate)
    if separation != separation or separation <= CANDIDATE_VIEW_DISTANCE:
        return
    try:
        anchor = getattr(client, "_protected_home_anchor", None)
        _surface_candidate_trip(client, int(candidate[0]),
                                int(anchor[1] if anchor else candidate[1]),
                                int(candidate[2]))
    except Exception:
        pass


@protect_home_route(surface_work=True)
def _surface_candidate_trip(client, x, y, z):
    from .navigation import goto_xz

    return goto_xz(client, x, z, timeout=CANDIDATE_TRAVEL_TIMEOUT,
                   tolerance=CANDIDATE_VIEW_DISTANCE - 2)


__all__ = [
    "CANDIDATE_TRAVEL_TIMEOUT",
    "CANDIDATE_VIEW_DISTANCE",
    "approach_candidate",
    "horizontal_distance",
]
