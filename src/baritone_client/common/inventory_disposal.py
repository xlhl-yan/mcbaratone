"""Verified inventory disposal helpers."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional


# Observe beyond vanilla's pickup delay; early empty slots are not durable.
PICKUP_SAFE_SETTLE_SECONDS = 2.5
DISPOSAL_RETRY_COOLDOWN_SECONDS = 300.0
#: Farming returns far more seeds than replanting uses. Live A1 2026-10-15
#: carried 548 (nine slots) on top of 3307 banked, so every home chest was
#: full and the iron trip's ten-slot gate refused about 350 times.
SEED_CARRY_CAP = 32
#: A thrown stack lands about two blocks ahead of the thrower.
_THROW_REACH = 2.0
#: Vanilla pickup reach is the player box inflated by one block.
_PICKUP_CLEARANCE = 2.0
#: Thrown, never dropped: an egg may hatch a chick near home, and a thrown
#: snowball is gone for good, so neither comes back as a re-picked stack.
THROWN_CLUTTER = ("minecraft:egg", "minecraft:snowball")

def drop_items(
    client: Any,
    item_ids: List[str],
    max_stacks: Optional[int] = None,
    retain_counts: Optional[Dict[str, int]] = None,
) -> int:
    """Drop verified whole stacks while preserving requested reserves."""
    from . import inventory as api

    if time.monotonic() < getattr(client, "_disposal_retry_after", 0.0):
        return 0
    dropped = 0
    reserves = {
        item_id: max(0, int(count))
        for item_id, count in (retain_counts or {}).items()
    }
    try:
        client.transport.dispatch("close_screen", {})
        time.sleep(0.1)
        raw = client.transport.dispatch("get_inventory", {})
        from ..inventory_evidence import valid_inventory
        if not valid_inventory(raw.get("data", raw)):
            raise RuntimeError("Inventory unavailable; disposal refused")
        items = raw.get("data", raw).get("inventory", [])
        totals: Dict[str, int] = {}
        for carried in items:
            item_id = carried.get("id")
            if item_id:
                totals[item_id] = totals.get(item_id, 0) + int(
                    carried.get("count", 0)
                )

        # Every rejection below used to be a silent `continue`, so a caller
        # that freed nothing could not tell "no candidates" from "the throw
        # itself is not working". Live A1 2026-08-31 logged 47 cleanup
        # failures in 20 minutes with four unprotected, unfloored stacks
        # sitting in slots 6/9/12/20 and no indication of which step refused.
        skips = {"reserved": 0, "not_emptied": 0, "no_slot_gain": 0}
        candidates = 0
        landed: List[tuple] = []
        for item in items:
            if max_stacks is not None and dropped >= max_stacks:
                break
            item_id = item.get("id")
            slot = int(item.get("slot", -1))
            count = int(item.get("count", 0))
            if item_id not in item_ids or not 0 <= slot < 36:
                continue
            candidates += 1
            if totals.get(item_id, 0) - count < reserves.get(item_id, 0):
                skips["reserved"] += 1
                continue
            free_before = api.free_inventory_slots(client)
            # Throw away from the retreat. Walking back over an earlier stack
            # is also refused, so a run of throws does not recollect itself.
            plan = _retreat_plan(client, avoid=landed)
            if plan is not None:
                landed.append(_face_away(client, plan))
            client.transport.dispatch(
                "inventory_click",
                {
                    "slot": api._player_handler_slot(slot),
                    "type": "THROW",
                    "button": 1,
                },
            )
            if not _wait_for_empty_slot(client, slot):
                skips["not_emptied"] += 1
                client._disposal_retry_after = time.monotonic() + DISPOSAL_RETRY_COOLDOWN_SECONDS
                break
            # Allow a safe retreat if one is observable; never blindly path
            # toward an edge just to avoid pickup. Durable verification still
            # decides whether disposal worked when retreat is unavailable.
            _retreat_from_drop(client, plan)
            time.sleep(PICKUP_SAFE_SETTLE_SECONDS)
            if api.free_inventory_slots(client) > free_before:
                dropped += 1
                totals[item_id] = max(0, totals.get(item_id, 0) - count)
            else:
                skips["no_slot_gain"] += 1
                client._disposal_retry_after = time.monotonic() + DISPOSAL_RETRY_COOLDOWN_SECONDS
                from ..observability import emit_event
                emit_event("disposal_repickup", item_id=item_id, slot=slot,
                           postcondition_verified=False, retry_after_seconds=DISPOSAL_RETRY_COOLDOWN_SECONDS)
                break
        if not dropped and candidates:
            detail = ", ".join(f"{name}={n}" for name, n in skips.items() if n)
            print(
                f"  Drop items: none of {candidates} candidate stack(s) left "
                f"the inventory ({detail or 'no reason recorded'})"
            )
    except Exception as exc:
        print(f"Drop items error: {exc}")
    return dropped


def _wait_for_empty_slot(client: Any, inventory_slot: int) -> bool:
    deadline = time.time() + 2.0
    while time.time() < deadline:
        raw = client.transport.dispatch("get_inventory", {})
        from ..inventory_evidence import valid_inventory
        if not valid_inventory(raw.get("data", raw)):
            return False
        items = raw.get("data", raw).get("inventory", [])
        entry = next(
            (
                value
                for value in items
                if int(value.get("slot", -1)) == inventory_slot
            ),
            None,
        )
        if (
            entry is None
            or entry.get("id") in (None, "", "minecraft:air")
            or int(entry.get("count", 0)) <= 0
        ):
            return True
        time.sleep(0.1)
    return False


def _retreat_plan(client, avoid=()):
    """Choose the longest safe straight retreat, at most three blocks.

    Returns ``(x, y, z, dx, dz, steps)`` from the current block, or ``None``
    when no direction offers even one clear step. A direction whose path
    passes within pickup reach of an ``avoid`` point (where an earlier stack
    landed) is not taken.
    """
    from .automation_utils import _block_at, _is_solid

    try:
        state = client.transport.dispatch("get_state", {})
        pos = state.get("block_position", {})
        x, y, z = (int(pos[k]) for k in ("x", "y", "z"))
    except Exception:
        return None
    air = {"minecraft:air", "minecraft:cave_air"}
    best_direction, best_n = None, 0
    try:
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            reached = 0
            for n in (1, 2, 3):
                cx, cz = x + dx * n, z + dz * n
                if any(
                    abs(cx - px) < _PICKUP_CLEARANCE and abs(cz - pz) < _PICKUP_CLEARANCE
                    for px, pz in avoid
                ):
                    break
                if (
                    _is_solid(client, cx, y - 1, cz)
                    and _block_at(client, cx, y, cz) in air
                    and _block_at(client, cx, y + 1, cz) in air
                ):
                    reached = n
                else:
                    break
            if reached > best_n:
                best_direction, best_n = (dx, dz), reached
    except Exception:
        return None
    if best_direction is None:
        return None
    return x, y, z, best_direction[0], best_direction[1], best_n


def _face_away(client, plan):
    """Look opposite the planned retreat; return where the stack should land."""
    x, y, z, dx, dz, _steps = plan
    try:
        client.transport.dispatch(
            "look_at",
            {"x": x + 0.5 - dx * 4, "y": y + 1.0, "z": z + 0.5 - dz * 4},
        )
        # The throw uses the server's rotation, which follows the next tick.
        time.sleep(0.15)
    except Exception:
        pass
    return x - dx * _THROW_REACH, z - dz * _THROW_REACH


def _retreat_from_drop(client, plan=None):
    """Step away far enough that a just-thrown item is not re-picked up.

    Vanilla gives a player-dropped item a ~2-second self-pickup delay
    specifically so the dropper can step away; PICKUP_SAFE_SETTLE_SECONDS
    waits just past that window. But the previous version required a full
    3-block clear line in one direction or did nothing at all, so a bot in a
    mined tunnel -- clear for 1 block but blocked or open-floored beyond that
    in every direction -- never moved, stood over its own drop for the whole
    delay, and auto-picked it back up the instant the delay lapsed.

    Live A1 2026-09-06 at (-300, 68, 191): every cardinal direction had a
    valid first step but failed at step 2 or 3, so disposal reported
    "no_slot_gain" and cooled down for 5 minutes, repeatedly, with 12 slots
    of duplicate gear sitting there unshed. Take the longest safe
    straight-line retreat available, even if that is only 1 block --
    reaching pickup range is what matters, not reaching 3 blocks.
    """
    from .navigation import goto

    if plan is None:
        plan = _retreat_plan(client)
    if plan is None:
        return False
    x, y, z, dx, dz, steps = plan
    try:
        return goto(client, x + dx * steps, y, z + dz * steps, timeout=6, tolerance=0.8)
    except Exception:
        return False


def shed_surplus_seeds(client: Any, keep: int = SEED_CARRY_CAP) -> int:
    """Throw whole wheat-seed stacks while keeping about ``keep`` carried.

    Seeds have no consumer beyond replanting, so banking them only fills the
    chests that ore and iron need. Returns the number of stacks thrown.
    """
    seeds = "minecraft:wheat_seeds"
    try:
        raw = client.transport.dispatch("get_inventory", {})
        items = raw.get("data", raw).get("inventory", [])
        carried = sum(
            int(item.get("count", 0)) for item in items
            if item.get("id") == seeds and 0 <= int(item.get("slot", -1)) < 36
        )
    except Exception:
        return 0
    # One full stack over the cap is the smallest surplus a whole-stack
    # throw can remove while still keeping ``keep``.
    if carried < keep + 64:
        return 0
    dropped = drop_items(client, [seeds], retain_counts={seeds: keep})
    if dropped:
        print(f"  SEEDS: shed {dropped} surplus stack(s); keeping about {keep}")
    return dropped


def throw_clutter(client: Any, items=THROWN_CLUTTER, *, limit: int = 16) -> int:
    """Throw eggs and snowballs onto open ground; return slots emptied.

    Each throw must lower the carried count, or that item stops: a held use
    the server ignored is not disposal.
    """
    from . import inventory as api

    emptied = 0
    plan = _retreat_plan(client)  # open, floored ground to throw toward
    for item in items:
        if api.count_item(client, item) <= 0 or not api.select_item(client, item, allow_swap=True):
            continue
        if plan is not None:
            x, y, z, dx, dz, _steps = plan
            client.transport.dispatch("look_at", {"x": x + 0.5 + dx * 4, "y": y + 1.0, "z": z + 0.5 + dz * 4})
            time.sleep(0.15)
        for _ in range(limit):
            before = api.count_item(client, item)
            if before <= 0:
                break
            client.transport.dispatch("use_item", {"hand": "MAIN_HAND"})
            time.sleep(0.25)
            if api.count_item(client, item) >= before:
                break
        if api.count_item(client, item) <= 0:
            emptied += 1
            print(f"  CLUTTER: threw every {item.split(':')[-1]}")
    return emptied
