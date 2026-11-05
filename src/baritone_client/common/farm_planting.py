"""Bounded, close-range planting without weakening block visibility checks."""

from .tasks import PlayerDeathDetected, SurvivalRecoveryRequired
from math import dist, isfinite


def _within_block_reach(client, x, y, z):
    """Prefer a nearby standing point; the bridge still verifies the real ray."""
    try:
        live = client.transport.dispatch("get_state", {})
        position = live["position"]
        eyes = (float(position["x"]), float(position["y"]) + 1.62, float(position["z"]))
        return (live.get("is_dead") is False and float(live["health"]) >= 14
                and all(isfinite(value) for value in eyes)
                and dist(eyes, (x + .5, y + .5, z + .5)) <= 4.0)
    except (PlayerDeathDetected, SurvivalRecoveryRequired):
        raise
    except Exception:
        return False


def _plant_non_wheat_crop(client, x, y, z, crop_item):
    """Plant one observed crop type on empty tillable soil."""
    from . import farming
    from .inventory import count_item, select_item

    if farming._block_id(client, x, y + 1, z) not in {"minecraft:air", "minecraft:cave_air"}:
        return False
    soil = farming._block_id(client, x, y, z)
    if soil in {"minecraft:dirt", "minecraft:grass_block"}:
        if count_item(client, "minecraft:wooden_hoe") == 0 and not farming.craft(
            client, "minecraft:wooden_hoe", 1
        ):
            return False
        if not select_item(client, "minecraft:wooden_hoe", allow_swap=True):
            return False
        client.transport.dispatch("look_at", {"x": x + .5, "y": y + 1.0, "z": z + .5})
        client.transport.dispatch("interact_block", {"x": x, "y": y, "z": z})
        for _ in range(4):
            soil = farming._block_id(client, x, y, z)
            if soil == "minecraft:farmland":
                break
            farming.time.sleep(0.1)
    if soil != "minecraft:farmland" or count_item(client, crop_item) < 1:
        return False
    if not select_item(client, crop_item, allow_swap=True):
        return False
    client.transport.dispatch("look_at", {"x": x + .5, "y": y + 1.0, "z": z + .5})
    client.transport.dispatch("interact_block", {"x": x, "y": y, "z": z})
    expected_crop = {
        "minecraft:carrot": "minecraft:carrots",
        "minecraft:potato": "minecraft:potatoes",
        "minecraft:beetroot_seeds": "minecraft:beetroots",
    }[crop_item]
    for _ in range(4):
        if farming._block_id(client, x, y + 1, z) == expected_crop:
            return True
        farming.time.sleep(0.1)
    return False


def plant_farm_tiles(client, tiles, *, crop_item="minecraft:wheat_seeds"):
    """Plant from a reachable stand first, avoiding needless crop trampling.

    Wheat remains the default for existing callers. Maintenance may pass a
    directly observed carrot, potato, or beetroot crop's planting item.
    """
    from . import farming

    if crop_item not in {
        "minecraft:wheat_seeds", "minecraft:carrot", "minecraft:potato",
        "minecraft:beetroot_seeds",
    }:
        return 0
    planted = 0
    for x, y, z in tiles:
        if farming.count_item(client, crop_item) < 1:
            break
        try:
            plant = (
                (lambda: farming._till_and_plant_tile(client, x, y, z))
                if crop_item == "minecraft:wheat_seeds"
                else (lambda: _plant_non_wheat_crop(client, x, y, z, crop_item))
            )
            if _within_block_reach(client, x, y, z) and plant():
                planted += 1
                continue
            if not farming.goto(client, x, y + 1, z, timeout=15,
                                tolerance=1.5, radius=1):
                continue
            if plant():
                planted += 1
        except (PlayerDeathDetected, SurvivalRecoveryRequired):
            raise
        except Exception as exc:
            print(f"  Farm planting skipped {(x, y, z)}: {exc}")
    return planted
