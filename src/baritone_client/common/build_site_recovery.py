"""Bounded relocation and excavation for stalled base-site discovery."""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Optional

from .escape_recovery import destination_safe
from .movement_recovery import block_position


_GROUND_BLOCKS = [
    "minecraft:grass_block",
    "minecraft:dirt",
    "minecraft:coarse_dirt",
    "minecraft:podzol",
    "minecraft:sand",
    "minecraft:gravel",
    "minecraft:stone",
]


def unprovisioned_remote_build_site(
    client: Any,
    *,
    site: tuple[int, int, int],
    inventory_summary: dict[str, int],
    max_distance: float = 384.0,
) -> bool:
    """Reject a long house return that cannot be completed without starving."""
    from .combat import EMERGENCY_FOOD_ITEMS

    transport = getattr(client, "transport", None)
    if transport is None:
        return False
    current = block_position(transport.dispatch("get_state", {}))
    horizontal_distance_sq = (
        (current[0] - site[0]) ** 2 + (current[2] - site[2]) ** 2
    )
    if horizontal_distance_sq <= max_distance**2:
        return False
    return not any(
        int(inventory_summary.get(item_id, 0) or 0) > 0
        for item_id in EMERGENCY_FOOD_ITEMS
    )


def relocate_build_site_search(
    client: Any,
    *,
    attempt: int,
    goto: Optional[Callable[..., bool]] = None,
    search_radius: int = 64,
    attempt_limit: int = 8,
    minimum_y: Optional[int] = None,
    candidate_validator: Optional[Callable[..., bool]] = None,
    candidate_scorer: Optional[Callable[..., float]] = None,
    minimum_candidate_score: Optional[float] = None,
) -> bool:
    """Move far enough to load a different dry-surface candidate view."""
    if goto is None:
        from .navigation import goto

    origin = block_position(client.transport.dispatch("get_state", {}))
    try:
        response = client.transport.dispatch(
            "find_blocks",
            {
                "blocks": _GROUND_BLOCKS,
                "radius": int(search_radius),
                "limit": 4096,
            },
        )
    except Exception:
        return False
    highest_by_column = {}
    for block in response.get("found", []):
        x, y, z = int(block["x"]), int(block["y"]) + 1, int(block["z"])
        highest_by_column[(x, z)] = max(y, highest_by_column.get((x, z), -64))
    candidates = []
    for (x, z), y in highest_by_column.items():
        if minimum_y is not None and y < int(minimum_y):
            continue
        distance_sq = (x - origin[0]) ** 2 + (z - origin[2]) ** 2
        if 16**2 <= distance_sq <= search_radius**2:
            candidate = (x, y, z)
            candidates.append((distance_sq, candidate))
    ranked = [
        candidate
        for _distance, candidate in sorted(
            candidates,
            key=lambda item: (abs(item[1][1] - origin[1]), item[0]),
        )
    ]
    if ranked:
        offset = max(0, int(attempt) - 1) % len(ranked)
        ranked = ranked[offset:] + ranked[:offset]
    def try_candidate(candidate: tuple[int, int, int]) -> bool:
        print(f"  Relocating build-site search to {candidate}")
        if not goto(client, *candidate, timeout=120.0, tolerance=3.0):
            return False
        current = block_position(client.transport.dispatch("get_state", {}))
        moved_sq = (
            (current[0] - origin[0]) ** 2
            + (current[2] - origin[2]) ** 2
        )
        return bool(
            moved_sq >= 12**2
            and destination_safe(client, *current)
            and (candidate_validator is None or candidate_validator(client, current))
        )

    if candidate_scorer is None:
        for candidate in ranked[:attempt_limit]:
            if not destination_safe(client, *candidate):
                continue
            if candidate_validator is not None and not candidate_validator(client, candidate):
                continue
            if try_candidate(candidate):
                return True
        return False

    qualified = []
    for rank, candidate in enumerate(ranked[:attempt_limit]):
        if not destination_safe(client, *candidate):
            continue
        if candidate_validator is not None and not candidate_validator(client, candidate):
            continue
        try:
            score = float(candidate_scorer(client, candidate)) if candidate_scorer else 0.0
        except Exception:
            continue
        if minimum_candidate_score is not None and score < float(minimum_candidate_score):
            continue
        qualified.append((score, rank, candidate))
    if candidate_scorer is not None:
        qualified.sort(key=lambda item: (-item[0], item[1]))
    for score, _rank, candidate in qualified:
        print(f"  Home candidate {candidate} scored {score:.0f}/100")
        if try_candidate(candidate):
            return True
    return False


def excavate_surface_egress(
    client: Any,
    *,
    origin: tuple[int, int, int],
    expected_y: int,
    timeout_per_attempt: float = 45.0,
    avoid: Optional[tuple[float, float, float]] = None,
    avoid_radius: float = 0.0,
) -> Optional[tuple[int, int, int]]:
    """Tunnel diagonally upward when walk-only and ``#surface`` routes fail.

    Targets within ``avoid_radius`` of ``avoid`` (a house) are skipped.
    """
    ox, oy, oz = origin
    for command in (
        "#set allowBreak true",
        "#set allowPlace true",
        "#set allowDownward false",
    ):
        client.transport.dispatch("chat", {"message": command})
    for dx, dz in ((8, 0), (-8, 0), (0, 8), (0, -8)):
        target = {"x": ox + dx, "y": expected_y, "z": oz + dz, "radius": 2}
        if avoid is not None and math.hypot(target["x"] - avoid[0], target["z"] - avoid[2]) <= avoid_radius:
            continue
        print(
            "  Surface routes were unreachable; excavating an upward egress "
            f"toward ({target['x']}, {target['y']}, {target['z']})"
        )
        client.transport.dispatch("tunnel", target)
        deadline = time.monotonic() + max(0.0, timeout_per_attempt)
        checks = 0
        while time.monotonic() < deadline:
            state = client.transport.dispatch("get_state", {})
            current = block_position(state)
            if current[1] >= expected_y - 3 and current[1] >= oy + 3:
                client.transport.dispatch("cancel", {})
                return current
            checks += 1
            if checks >= 2 and not state.get("is_pathing", True):
                break
            time.sleep(0.5)
        client.transport.dispatch("cancel", {})
    return None
