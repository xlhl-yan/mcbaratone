"""Select local crafting workstations using full observed XYZ distance."""

import math


def nearest_local_crafting_table(
    client,
    found_tables,
    *,
    maximum_distance: float = 12.0,
):
    """Reject loaded tables that require a survival-expensive commute."""
    try:
        state = client.transport.dispatch("get_state", {})
        position = state.get("block_position", state.get("position", {}))
        coordinates = [position[axis] for axis in ("x", "y", "z")]
        if any(isinstance(value, bool) for value in coordinates):
            return None
        px = float(position["x"])
        py = float(position["y"])
        pz = float(position["z"])
        if not all(math.isfinite(value) for value in (px, py, pz)):
            return None
    except (KeyError, TypeError, ValueError):
        return None

    candidates = []
    for table in found_tables:
        try:
            x = int(table["x"])
            y = int(table["y"])
            z = int(table["z"])
        except (KeyError, TypeError, ValueError):
            continue
        distance = ((x - px) ** 2 + (y - py) ** 2 + (z - pz) ** 2) ** 0.5
        if distance <= float(maximum_distance):
            candidates.append((distance, x, y, z))
    if not candidates:
        return None
    _, x, y, z = min(candidates)
    return (x, y, z)
