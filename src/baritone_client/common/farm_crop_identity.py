"""Preserve a wheat plot when Baritone replants a different carried crop."""

from __future__ import annotations

import time
from collections.abc import Mapping
from math import isfinite


_WHEAT = "minecraft:wheat"
_OTHER_CROPS = {
    "minecraft:carrots", "minecraft:potatoes", "minecraft:beetroots",
}
_AIR = {"minecraft:air", "minecraft:cave_air"}
_CROP_ITEMS = {
    "minecraft:carrots": "minecraft:carrot",
    "minecraft:potatoes": "minecraft:potato",
    "minecraft:beetroots": "minecraft:beetroot_seeds",
}


def _successful_data(response):
    """Unwrap only mappings that are not explicit transport/command errors."""
    if not isinstance(response, Mapping):
        return None
    for envelope in (response, response.get("data")):
        if not isinstance(envelope, Mapping):
            continue
        if envelope.get("success") is False:
            return None
        if str(envelope.get("status", "")).lower() in {"error", "failed", "failure"}:
            return None
        if envelope.get("error") not in (None, "", False):
            return None
    data = response.get("data", response)
    return data if isinstance(data, Mapping) else None


def _read_block(client, position):
    response = client.transport.dispatch(
        "get_block", {"x": position[0], "y": position[1], "z": position[2]}
    )
    data = _successful_data(response)
    return data if data is not None and isinstance(data.get("id"), str) and data["id"] else None


def _age(block):
    state = block.get("state")
    raw_age = state.get("age") if isinstance(state, Mapping) else None
    if isinstance(raw_age, bool):
        return None
    if isinstance(raw_age, int):
        age = raw_age
    elif isinstance(raw_age, str) and raw_age.isdecimal():
        age = int(raw_age)
    else:
        return None
    return age if 0 <= age <= 7 else None


def _capture_mature_wheat(client, x, y, z, size=5):
    """Capture mature wheat cells only after every plot cell reads cleanly."""
    half = max(1, int(size) // 2)
    mature = []
    for dx in range(-half, half + 1):
        for dz in range(-half, half + 1):
            if not (dx or dz):
                continue
            position = (x + dx, y + 1, z + dz)
            try:
                block = _read_block(client, position)
            except Exception:
                return None
            if block is None:
                return None
            if block["id"] == _WHEAT:
                age = _age(block)
                if age is None:
                    return None
                if age == 7:
                    mature.append(position)
    return tuple(mature)


def _stopped_after_cancel(client, timeout=2.0):
    deadline = time.monotonic() + timeout
    clear_observations = 0
    while time.monotonic() < deadline:
        try:
            live = client.transport.dispatch("get_state", {})
        except Exception:
            live = None
        state = _successful_data(live)
        if state is not None and state.get("is_pathing") is False:
            clear_observations += 1
            if clear_observations >= 2:
                return True
        else:
            clear_observations = 0
        time.sleep(0.1)
    return False


def _player_at_crop_height(client, crop_y):
    """Require a grounded player at the crop's interaction plane."""
    try:
        state = _successful_data(client.transport.dispatch("get_state", {}))
        position = state.get("position") if state is not None else None
        feet_y = position.get("y") if isinstance(position, Mapping) else None
        if isinstance(feet_y, bool):
            return False
        feet_y = float(feet_y)
        return (
            isfinite(feet_y)
            and feet_y >= crop_y - 0.2
            and state.get("is_on_ground") is True
        )
    except (TypeError, ValueError, OverflowError):
        return False


def _restore_converted_wheat(client, positions):
    """Restore only mature wheat cells now holding a fresh wrong crop."""
    from . import farming
    from .farm_planting import _within_block_reach
    from .tasks import PlayerDeathDetected, SurvivalRecoveryRequired

    restored = 0
    for position in positions:
        x, crop_y, z = position
        ground = (x, crop_y - 1, z)
        try:
            if not farming.farm_surface_safe(client):
                return restored
            current = _read_block(client, position)
            soil = _read_block(client, ground)
            if (
                current is None or current["id"] not in _OTHER_CROPS
                or _age(current) not in {0, 1}
                or soil is None or soil["id"] != "minecraft:farmland"
                or farming.count_item(client, "minecraft:wheat_seeds") < 1
                or farming.count_item(client, _CROP_ITEMS[current["id"]]) < 1
            ):
                continue

            if not _stopped_after_cancel(client):
                continue
            if (
                not _player_at_crop_height(client, crop_y)
                or not _within_block_reach(client, *ground)
            ):
                if not farming.farm_surface_safe(client):
                    return restored
                if not farming.goto(
                    client, x, crop_y, z, timeout=30, tolerance=1.1, radius=0
                ):
                    continue

                # Navigation may have changed the world or stopped short;
                # re-prove every condition before any crop mutation.
                if (
                    not farming.farm_surface_safe(client)
                    or not _stopped_after_cancel(client)
                ):
                    continue
                current = _read_block(client, position)
                soil = _read_block(client, ground)
                if (
                    current is None or current["id"] not in _OTHER_CROPS
                    or _age(current) not in {0, 1}
                    or soil is None or soil["id"] != "minecraft:farmland"
                    or farming.count_item(client, "minecraft:wheat_seeds") < 1
                    or farming.count_item(client, _CROP_ITEMS[current["id"]]) < 1
                    or not _player_at_crop_height(client, crop_y)
                    or not _within_block_reach(client, *ground)
                ):
                    continue

            # Reconfirm the crop and soil at the mutation boundary; height,
            # reach, and stopped-state checks above must all still hold.
            if (
                not farming.farm_surface_safe(client)
                or not _stopped_after_cancel(client)
                or not _player_at_crop_height(client, crop_y)
                or not _within_block_reach(client, *ground)
            ):
                continue
            current = _read_block(client, position)
            soil = _read_block(client, ground)
            if (
                current is None or current["id"] not in _OTHER_CROPS
                or _age(current) not in {0, 1}
                or soil is None or soil["id"] != "minecraft:farmland"
            ):
                continue

            try:
                client.transport.dispatch(
                    "dig_block", {"x": x, "y": crop_y, "z": z,
                                  "face": "UP", "max_ticks": 80}
                )
            except Exception:
                # Reconcile an uncertain start using the exact crop block.
                pass
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if not farming.farm_surface_safe(client):
                    return restored
                after_dig = _read_block(client, position)
                if after_dig is None:
                    break
                if after_dig["id"] in _AIR:
                    break
                if after_dig["id"] not in _OTHER_CROPS:
                    break
                time.sleep(0.1)
            else:
                continue

            if after_dig is None or after_dig["id"] not in _AIR:
                continue
            soil = _read_block(client, ground)
            if soil is None or soil["id"] != "minecraft:farmland":
                continue
            try:
                wheat_planted = farming._till_and_plant_tile(client, *ground)
            except (PlayerDeathDetected, SurvivalRecoveryRequired):
                raise
            except Exception as exc:
                print(f"  Wheat identity planting failed {position}: {exc}")
                wheat_planted = False
            if not wheat_planted:
                after_plant = _read_block(client, position)
                if after_plant is not None and after_plant["id"] == _WHEAT:
                    restored += 1
                    continue
                if after_plant is not None and after_plant["id"] in _AIR:
                    from .farm_planting import _plant_non_wheat_crop

                    try:
                        _plant_non_wheat_crop(
                            client, *ground, _CROP_ITEMS[current["id"]]
                        )
                    except (PlayerDeathDetected, SurvivalRecoveryRequired):
                        raise
                    except Exception as exc:
                        print(f"  Wheat identity rollback failed {position}: {exc}")
                    rollback = _read_block(client, position)
                    if rollback is None or rollback["id"] != current["id"]:
                        print(f"  Wheat identity repair rollback unverified {position}")
                continue
            planted = _read_block(client, position)
            if planted is not None and planted["id"] == _WHEAT:
                restored += 1
        except (PlayerDeathDetected, SurvivalRecoveryRequired):
            raise
        except Exception as exc:
            print(f"  Wheat identity repair skipped {position}: {exc}")
    return restored


def run_wheat_farm_harvest(client, x, y, z, range_):
    """Run the bounded native harvest, then reconcile proven crop substitutions."""
    from . import farming
    from .farm_plot_evidence import verified_crop_plot_plane

    if not farming.goto(client, x, y + 1, z, timeout=120, tolerance=4, radius=2):
        return False
    if not verified_crop_plot_plane(client, (x, y, z)):
        print(
            "  Refusing native farm harvest: plot plane is unverified at "
            f"{(x, y, z)}."
        )
        return False
    original_wheat = _capture_mature_wheat(client, x, y, z)
    before = farming.count_item(client, _WHEAT)
    try:
        client.transport.dispatch("farm", {"range": range_})
    except Exception as exc:
        print(f"  Farm harvest dispatch failed: {exc}")
        # A lost response does not prove the native process never started.
        client.transport.dispatch("cancel", {})
        return False

    safe = True
    harvested = False
    try:
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            time.sleep(2)
            if not farming.farm_surface_safe(client):
                safe = False
                break
            if farming.count_item(client, _WHEAT) > before:
                harvested = True
                break
    finally:
        cancelled = client.transport.dispatch("cancel", {})

    cancel_result = _successful_data(cancelled)
    if (
        cancel_result is None
        or cancel_result.get("cancelled") is not True
        or not _stopped_after_cancel(client)
    ):
        return False
    if not safe:
        return False
    if original_wheat is not None:
        _restore_converted_wheat(client, original_wheat)
    farming.replant_empty_wheat_tiles(client, x, y, z)
    return harvested
