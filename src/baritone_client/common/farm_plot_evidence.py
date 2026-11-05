"""Fresh, read-only evidence that a saved crop-plot height is valid."""

from collections.abc import Mapping

from .tasks import PlayerDeathDetected, SurvivalRecoveryRequired


_PLOT_SIZE = 5
_CROPS = {
    "minecraft:wheat",
    "minecraft:carrots",
    "minecraft:potatoes",
    "minecraft:beetroots",
}


def _block_data(client, position):
    """Return a block only when the complete bridge observation is usable."""
    try:
        response = client.transport.dispatch(
            "get_block", dict(zip(("x", "y", "z"), position))
        )
    except (PlayerDeathDetected, SurvivalRecoveryRequired):
        raise
    except Exception:
        return None
    if not isinstance(response, Mapping):
        return None
    for envelope in (response, response.get("data")):
        if isinstance(envelope, Mapping) and (
            envelope.get("success") is False
            or str(envelope.get("status", "")).casefold()
            in {"error", "failed", "failure"}
            or envelope.get("error") not in (None, "", False)
        ):
            return None
    block = response.get("data", response)
    if not isinstance(block, Mapping):
        return None
    block_id = block.get("id")
    if not isinstance(block_id, str) or not block_id.startswith("minecraft:"):
        return None
    return block


def verified_crop_plot_plane(client, origin) -> bool:
    """Freshly verify irrigation, soil, and crops on a saved 5x5 plane.

    ``origin`` is the soil-level center recorded by farm setup. Water may be
    off-center, but must be a level-0 source on that plane. At least one
    farmland tile must support a recognized crop at ``y + 1``. Any missing or
    error response in the declared footprint fails closed; stale metadata or
    a partial observation never authorizes native harvesting.
    """
    if (
        not isinstance(origin, (tuple, list))
        or len(origin) != 3
        or any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in origin
        )
    ):
        return False

    x, y, z = origin
    half = _PLOT_SIZE // 2
    source_water = False
    farmland = False
    crop_on_farmland = False
    for dx in range(-half, half + 1):
        for dz in range(-half, half + 1):
            soil_position = (x + dx, y, z + dz)
            soil = _block_data(client, soil_position)
            crop = _block_data(client, (x + dx, y + 1, z + dz))
            if soil is None or crop is None:
                return False

            if soil["id"] == "minecraft:farmland":
                farmland = True
                if crop["id"] in _CROPS:
                    crop_on_farmland = True
            elif soil["id"] == "minecraft:water":
                state = soil.get("state")
                level = state.get("level") if isinstance(state, Mapping) else None
                if not isinstance(level, bool) and str(level) == "0":
                    source_water = True

    return source_water and farmland and crop_on_farmland
