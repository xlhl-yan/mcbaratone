"""Keep a long, physically advancing mine trip recoverable across restarts."""

import time


def checkpoint_progress(client, state, record, *, interval=60.0):
    """Checkpoint verified travel periodically, without crediting production."""
    from ..common.inventory import get_inventory

    last = time.monotonic()

    def save(trail):
        nonlocal last
        now = time.monotonic()
        writer = getattr(state, "save_checkpoint", None)
        if now - last < interval or not callable(writer):
            return
        record["spine"] = [list(cell) for cell in trail][-400:]
        writer(get_inventory(client))
        last = now

    return save
