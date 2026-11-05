"""Deciding what a block read at a catalogued container coordinate proves.

The bridge answers ``get_block`` for any coordinate in a chunk the client has
not loaded with ``minecraft:void_air``, and with an empty id when the read
itself failed. Neither says anything about whether a container is still there.

Treating those as "absent" is destructive and effectively irreversible:
``StorageCatalog.mark_missing`` retires the entry, and ``list_containers``,
``find_item`` and ``item_count`` all filter ``status!='missing'``, so nothing
ever re-probes a coordinate once it has been written off.

Measured on A1Bot 2026-08-16: 29 of 49 catalogued containers had been retired,
13 through this path, including (-351, 79, 95) which the same bot had opened
successfully an hour earlier. With its nearby storage erased it set out on a
~400 block walk to a spawn-era chest to unload a full inventory, and
FOOD_AND_IRON made no progress for hours.

The same rule is applied to terrain in ``terraform_verify`` and to portals in
``nether``; this module is the storage-side statement of it.
"""

from __future__ import annotations

import time
from typing import Any, Optional, Tuple


_UNREADABLE_BLOCK_IDS = frozenset({"", "minecraft:void_air"})

# Every block the storage catalog is allowed to register. Testing for "chest"
# alone rejected the barrels the catalog happily stores, so a bot would walk to
# a verified barrel, decide the chest was missing, and walk to the next entry.
_STORAGE_CONTAINER_TOKENS = ("chest", "barrel", "shulker_box")


def is_storage_container(block_id: Any) -> bool:
    """Return whether a live block id is a container the catalog tracks."""
    value = str(block_id or "")
    return any(token in value for token in _STORAGE_CONTAINER_TOKENS)


def classify_container_read(block_id: Any) -> str:
    """Classify a live block read taken at a catalogued container coordinate.

    Returns ``"container"``, ``"absent"`` or ``"unknown"``. Only ``"absent"``
    may be acted on destructively -- ``"unknown"`` means the chunk was never
    loaded, so the coordinate has not actually been observed at all.
    """
    value = str(block_id or "")
    if value in _UNREADABLE_BLOCK_IDS:
        return "unknown"
    return "container" if is_storage_container(value) else "absent"


def resolve_container_block(
    client: Any, position: Tuple[int, int, int]
) -> Optional[str]:
    """Return a block id at a catalogued coordinate that is safe to judge.

    Reads the coordinate, and when the read proves nothing -- the chunk is not
    loaded -- paths close enough to load it and reads again. Returns ``None``
    when the chunk could never be reached, in which case the caller must
    abandon the attempt rather than the catalog entry, because it still has no
    evidence either way. Long returns can legitimately exceed one navigation
    timeout, so ``load_storage_chunk`` issues bounded legs while each leg makes
    progress; that avoids both abandoning a real distant base and waiting
    forever on an unreachable one.
    """
    from .navigation import goto
    from .storage_safety import load_storage_chunk

    x, y, z = (int(axis) for axis in position)
    read = client.transport.dispatch("get_block", {"x": x, "y": y, "z": z})
    block = read.get("id", "")
    if classify_container_read(block) != "unknown":
        return block
    print(f"STORAGE: loading saved chest chunk at {(x, y, z)}")
    if not load_storage_chunk(client, (x, y, z), goto):
        print("STORAGE: could not reach saved chest chunk")
        return None
    return client.transport.dispatch("get_block", {"x": x, "y": y, "z": z}).get(
        "id", ""
    )


def forget_missing_container(
    client: Any, position, state=None, *, block_id: Any = None
) -> None:
    """Retire a catalogued coordinate that genuinely no longer holds a container.

    Without this the entry is offered again on the very next pass, so a bot
    walks 15-38m to a phantom chest, finds nothing, walks to the next one and
    round again -- and because the catalog is shared, every bot in the fleet
    repeats the same tour. Live 2026-08-01 all four bots were doing exactly
    that: mostly stationary, a handful of log lines per minute, no work done.

    ``block_id`` is the read that motivated the call. When it proves nothing
    the entry is left intact; see the module docstring for what that cost.
    """
    if block_id is not None and classify_container_read(block_id) == "unknown":
        print(
            f"STORAGE: keeping catalogued container at {tuple(position)}; "
            "chunk is not loaded so its absence is unproven"
        )
        return
    try:
        from .storage_catalog import catalog_for

        dimension = client.transport.dispatch("get_state", {}).get(
            "dimension", "minecraft:overworld"
        )
        catalog_for(client, state).mark_missing(
            tuple(int(axis) for axis in position),
            dimension=str(dimension),
            observed_block_id=block_id,
        )
        print(f"STORAGE: forgetting missing container at {tuple(position)}")
    except Exception as exc:
        print(f"STORAGE: missing-container catalog update deferred ({exc})")


#: Only incidental ground cover can be cleared for storage interaction.
#: Terrain and workstations may be shelter, a floor, or another owner's build.
#: Their presence requires another access route, never automatic demolition.
CLEARABLE_OCCLUDERS = frozenset(
    "minecraft:" + name
    for name in (
        "snow", "short_grass", "tall_grass", "fern",
        "large_fern", "dead_bush", "vine", "leaf_litter",
    )
)


def is_clearable_occluder(block: str) -> bool:
    return block in CLEARABLE_OCCLUDERS


def clear_occluded_container_face(
    client: Any, position: Tuple[int, int, int], _block_id: str = ""
) -> None:
    """Clear incidental cover while preserving structural container surrounds."""
    cx, cy, cz = (int(axis) for axis in position)
    try:
        state = client.transport.dispatch("get_state", {})
        data = state.get("data", state)
        player = data.get("block_position") or data.get("position", {})
        px = int(round(float(player["x"])))
        pz = int(round(float(player["z"])))
    except (KeyError, TypeError, ValueError):
        print("STORAGE: could not read position to clear an occluded chest face")
        return

    dx = 0 if cx == px else (1 if px < cx else -1)
    dz = 0 if cz == pz else (1 if pz < cz else -1)
    candidates = []
    if dx:
        candidates.extend(((cx - dx, cy, cz), (cx - dx, cy + 1, cz)))
    if dz:
        candidates.extend(((cx, cy, cz - dz), (cx, cy + 1, cz - dz)))
    candidates.append((cx, cy + 1, cz))

    for bx, by, bz in candidates:
        try:
            block = client.transport.dispatch(
                "get_block", {"x": bx, "y": by, "z": bz}
            ).get("id", "")
        except Exception:
            continue
        if block in {"", "minecraft:air", "minecraft:cave_air", "minecraft:void_air"}:
            continue
        if is_storage_container(block):
            continue
        if not is_clearable_occluder(block):
            print(
                f"STORAGE: chest at {(cx, cy, cz)} face {(bx, by, bz)} is "
                f"beside {block}; leaving it (not a clearable occluder)"
            )
            continue
        print(
            f"STORAGE: chest at {(cx, cy, cz)} face {(bx, by, bz)} is "
            f"occluded by {block}; clearing it"
        )
        try:
            client.transport.dispatch(
                "look_at", {"x": bx + 0.5, "y": by + 0.5, "z": bz + 0.5}
            )
            client.transport.dispatch("break_block", {"x": bx, "y": by, "z": bz})
        except Exception as exc:
            print(f"STORAGE: could not clear occluded chest face: {exc}")
        deadline = time.time() + 6.0
        while time.time() < deadline:
            time.sleep(0.4)
            try:
                after = client.transport.dispatch(
                    "get_block", {"x": bx, "y": by, "z": bz}
                ).get("id", "")
            except Exception:
                after = ""
            if after in {"", "minecraft:air", "minecraft:cave_air", "minecraft:void_air"}:
                break


def open_verified_container(
    client: Any,
    position: Tuple[int, int, int],
    *,
    attempts: int,
    allow_recovery_access: bool,
) -> Optional[dict]:
    """Approach and open a container, returning only a verified screen."""
    from . import harness_ops
    from .navigation import goto

    cx, cy, cz = (int(axis) for axis in position)
    for sx, sy, sz in (
        (cx + 1, cy, cz), (cx + 1, cy, cz + 1), (cx, cy, cz + 1),
        (cx - 1, cy, cz), (cx, cy, cz - 1), (cx - 1, cy, cz - 1),
    ):
        stand = client.transport.dispatch(
            "get_block", {"x": sx, "y": sy, "z": sz}
        ).get("id", "")
        floor = client.transport.dispatch(
            "get_block", {"x": sx, "y": sy - 1, "z": sz}
        ).get("id", "")
        if "air" not in stand or "air" in floor:
            continue
        goto(client, sx, sy, sz, timeout=20, check_interval=0.25, tolerance=0.5)
        break

    screen: dict = {}
    try:
        if harness_ops.available() and harness_ops.open_container(
            client,
            (cx, cy, cz),
            timeout=4.0,
            attempts=attempts,
            allow_recovery_access=allow_recovery_access,
        ):
            screen = client.transport.dispatch("get_screen", {})
            data = screen.get("data", screen)
            total_slots = int(data.get("total_slots") or len(data.get("slots", [])))
            if total_slots in (63, 90):
                return screen
    except Exception as exc:
        print(f"STORAGE: verified chest opener failed ({exc}); retrying natively")

    for _attempt in range(3):
        try:
            client.transport.dispatch(
                "look_at", {"x": cx + 0.5, "y": cy + 0.5, "z": cz + 0.5}
            )
            time.sleep(0.2)
        except Exception:
            pass
        try:
            client.transport.dispatch("interact_block", {"x": cx, "y": cy, "z": cz})
        except Exception as exc:
            print(f"STORAGE: native interact_block failed ({exc})")
            continue
        for _ in range(20):
            try:
                screen = client.transport.dispatch("get_screen", {})
            except Exception:
                break
            data = screen.get("data", screen)
            total_slots = int(data.get("total_slots") or len(data.get("slots", [])))
            if total_slots in (63, 90):
                return screen
            time.sleep(0.1)
        client.transport.dispatch("close_screen", {})
    return None
