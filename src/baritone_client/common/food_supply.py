"""Bounded, repeatable wheat-to-bread supply work for survival roles."""

from __future__ import annotations

from .food_preparation import _bread_output_capacity, _prepare_bread
from dataclasses import dataclass
from math import ceil, hypot, sqrt
import math
from typing import Any, Mapping, Optional, Sequence, Tuple

from .farm_maintenance import plot_on_cooldown
from .combat import eat_until_hunger
from .farming import (
    _block_id,
    establish_wheat_farm,
    find_farm_surface_near,
    find_natural_crop_center,
    harvest_wheat_farm,
)
from .inventory import (
    craft,
    deposit_excess_to_chest,
    get_inventory,
    resolve_storage_location,
)
from .navigation import find_nearby_block, find_water_source
from .survival_farm import fed_enough_for_home_farm, local_farm_wait_reason
from .farm_site_travel import (
    approach_candidate as _approach_candidate,
    horizontal_distance as _horizontal_distance,
)
from .food_workstation import craft_bread_at_saved_home
from .house_door_travel import prepare_house_door_for_departure
from .food_return import return_after_food_cycle
from .inventory_disposal import shed_surplus_seeds


WHEAT = "minecraft:wheat"
SEEDS = "minecraft:wheat_seeds"
BREAD = "minecraft:bread"
OTHER_EDIBLE_CROPS = (
    "minecraft:carrot",
    "minecraft:potato",
    "minecraft:beetroot",
)
#: Farms must stay within one short walk of the durable base anchor. A plot
#: further out is not an asset: the worker stops visiting it, the harvest step
#: times out travelling, and the fleet accumulates farms it never uses. At 64
#: the siting search still offers eight distinct sites -- enough that rejected
#: ground never stalls expansion -- and the furthest is a ~15 second walk.
MAX_ANCHOR_RADIUS = 64
#: Beyond this the worker plainly never arrived at the plot; harvest travel
#: uses a 4-block tolerance, so anything much larger is a failed journey.
UNREACHED_PLOT_DISTANCE = 24.0
#: A worker that has drifted hundreds of blocks needs more than the harvest
#: step's 120s travel budget to get home. Kept well short of a full journey on
#: purpose: this blocks one scheduling tick, and partial progress still counts
#: because the next cycle starts closer than the last.
RETURN_HOME_TIMEOUT = 150
#: Seed top-up runs every cycle, so it gets a short leash; partial stock is
#: still progress and the next cycle tries again.
SEED_STOCK_TIMEOUT = 45
#: A cycle should prepare at most one output stack before yielding to safety.
MAX_BREAD_PER_CYCLE = 64


@dataclass(frozen=True)
class FoodCycleResult:
    """Only inventory- or world-witnessed work credited by one food cycle."""

    success: bool
    detail: str
    cycles: int = 0
    plots: int = 0
    crop_tiles: int = 0
    wheat_harvested: int = 0
    crops_replanted: int = 0
    bread_crafted: int = 0
    prepared_food_banked: int = 0
    total_plots: int = 0
    total_crop_tiles: int = 0
    total_wheat_harvested: int = 0
    other_edible_crops_harvested: int = 0
    total_other_edible_crops_harvested: int = 0
    total_crops_replanted: int = 0
    total_bread_crafted: int = 0
    total_prepared_food_banked: int = 0

    @property
    def total_food_banked(self) -> int:
        """Compatibility name for scheduler/team-supply consumers."""
        return self.total_prepared_food_banked


def _count(inventory: Mapping[str, Any], item: str) -> int:
    try:
        return max(0, int(inventory.get(item, 0) or 0))
    except (TypeError, ValueError):
        return 0


def _position(value: Any) -> Optional[Tuple[int, int, int]]:
    if isinstance(value, Mapping):
        value = value.get("origin", value.get("position", value.get("location")))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) >= 3:
        try:
            return tuple(int(value[i]) for i in range(3))
        except (TypeError, ValueError):
            return None
    return None


def _plots(state: Any) -> list[Tuple[int, int, int]]:
    custom = getattr(state, "custom_data", {}) or {}
    worker = custom.get("food_worker", {}) if isinstance(custom, Mapping) else {}
    records = worker.get("farm_plots", []) if isinstance(worker, Mapping) else []
    found = [_position(record) for record in records if _position(record)]
    if not isinstance(custom, Mapping):
        return found

    # Earlier checkpoints used either a top-level farm_location or a
    # structures.food_source record instead of food_worker.farm_plots. Import
    # those locations into the worker rotation so the durable food cycle can
    # actually revisit and verify them.
    candidates = [custom.get("wheat_farm"), custom.get("farm_location")]
    structures = custom.get("structures", {})
    source = structures.get("food_source", {}) if isinstance(structures, Mapping) else {}
    retired = None
    if isinstance(source, Mapping):
        source_type = str(source.get("type", "")).lower()
        if "crop" in source_type or "wheat" in source_type or source.get("plots"):
            if source.get("verified") is False:
                retired = _position(source)
                found = [plot for plot in found if plot != retired]
            else:
                candidates.append(source)
    for candidate in candidates:
        plot = _position(candidate)
        if plot and plot != retired and plot not in found:
            found.append(plot)
    return found


def _base_anchor(state: Any) -> Optional[Tuple[int, int, int]]:
    """Return only a durable base/storage anchor, never a farm fallback."""
    custom = getattr(state, "custom_data", {}) or {}
    if isinstance(custom, Mapping):
        for key in (
            "base_location",
            "homestead_anchor",
            "base",
            "storage",
            "home",
        ):
            candidate = _position(custom.get(key))
            if candidate:
                return candidate
        structures = custom.get("structures", {})
        if isinstance(structures, Mapping):
            for key in ("storage", "home_storage"):
                candidate = _position(structures.get(key))
                if candidate:
                    return candidate
    return None


def _anchor(state: Any, plots: Sequence[Tuple[int, int, int]]) -> Optional[Tuple[int, int, int]]:
    base = _base_anchor(state)
    if base is not None:
        return base
    custom = getattr(state, "custom_data", {}) or {}
    if isinstance(custom, Mapping):
        for key in ("farm_location", "wheat_farm"):
            candidate = _position(custom.get(key))
            if candidate:
                return candidate
        structures = custom.get("structures", {})
        if isinstance(structures, Mapping):
            candidate = _position(structures.get("food_source"))
            if candidate:
                return candidate
    return plots[0] if plots else None


def _within_reach(
    candidate: Tuple[int, int, int],
    anchor: Tuple[int, int, int],
    radius: int,
) -> bool:
    """A farm is only useful if the worker can actually walk to it.

    Siting used to march outward with every rejection -- Bot18's six attempts
    landed 20, 26, 48, 51, 64 and 72 blocks from base, and the unbounded grid
    fallback keeps going. A farm the worker cannot reach in one trip produces
    nothing, and the fleet ends up owning plots it never visits again.
    """
    return hypot(candidate[0] - anchor[0], candidate[2] - anchor[2]) <= radius


def _candidate(
    anchor: Tuple[int, int, int],
    plots: Sequence[Tuple[int, int, int]],
    rejected: Sequence[Tuple[int, int, int]],
    distance: int,
    cursor: int,
    maximum_slots: int,
    max_anchor_radius: int = MAX_ANCHOR_RADIUS,
) -> Optional[Tuple[int, int, int]]:
    """Inspect a bounded rotating frontier; each verified plot adds four legs."""
    offsets = ((distance, 0), (0, distance), (-distance, 0), (0, -distance))
    frontier = list(plots) or [anchor]
    total = len(frontier) * len(offsets)
    for step in range(min(max(1, maximum_slots), total)):
        slot = (cursor + step) % total
        origin = frontier[slot // len(offsets)]
        dx, dz = offsets[slot % len(offsets)]
        candidate = (origin[0] + dx, origin[1], origin[2] + dz)
        if candidate in rejected:
            continue
        if not _within_reach(candidate, anchor, max_anchor_radius):
            continue
        if all(
            hypot(candidate[0] - plot[0], candidate[2] - plot[2]) >= distance
            for plot in plots
        ):
            return candidate
    # A farm can be surrounded by water, rock, or protected construction. If
    # all four immediate legs fail, a frontier-only search would stop forever.
    # The persisted cursor therefore also advances through an unbounded square
    # grid around the durable anchor. Each invocation still tries only the
    # bounded number of slots above plus this single fallback coordinate.
    grid_x, grid_z = _grid_offset(cursor + len(offsets))
    candidate = (
        anchor[0] + grid_x * distance,
        anchor[1],
        anchor[2] + grid_z * distance,
    )
    # The grid ring grows without limit by design, so it must be clipped to a
    # walkable radius or the worker eventually sites farms it can never visit.
    if (
        candidate not in rejected
        and _within_reach(candidate, anchor, max_anchor_radius)
        and all(
            hypot(candidate[0] - plot[0], candidate[2] - plot[2]) >= distance
            for plot in plots
        )
    ):
        return candidate
    return None


def _grid_offset(slot: int) -> Tuple[int, int]:
    """Map a durable cursor to one unique non-origin square-grid coordinate."""
    index = max(0, int(slot))
    ring = max(1, ceil((sqrt(index + 2) - 1) / 2))
    previous = (2 * ring - 1) ** 2 - 1
    position = index - previous
    perimeter = [
        *((ring, z) for z in range(-ring, ring)),
        *((x, ring) for x in range(ring, -ring, -1)),
        *((-ring, z) for z in range(ring, -ring, -1)),
        *((x, -ring) for x in range(-ring, ring)),
    ]
    east = perimeter.index((ring, 0))
    perimeter = perimeter[east:] + perimeter[:east]
    return perimeter[position % len(perimeter)]


def _retire_distant_plots(
    state: Any,
    worker: dict,
    anchor: Optional[Tuple[int, int, int]],
) -> list:
    """Drop plots the siting radius would never choose today.

    The unbounded search left plots scattered far from base. They are not
    assets: the worker cannot reach them, and keeping them in the rotation
    means every cycle burns its travel budget on a trip that cannot finish.
    Retiring one frees the next cycle to site a fresh plot near base.
    """
    if anchor is None:
        return []
    kept, retired = [], []
    for record in worker.get("farm_plots", []):
        plot = _position(record)
        if plot is None:
            continue
        if _within_reach(plot, anchor, MAX_ANCHOR_RADIUS):
            kept.append({"origin": list(plot)})
        else:
            retired.append(list(plot))
    if retired:
        worker["farm_plots"] = kept
        history = worker.setdefault("retired_plots", [])
        history.extend(retired)
        # `_plots` imports this compatibility field on every cycle. Leaving a
        # retired origin here resurrects it immediately, so the worker spends
        # every turn retiring the same unreachable farm and never sites the
        # replacement near its durable base.
        custom = getattr(state, "custom_data", None)
        if isinstance(custom, dict):
            legacy = _position(custom.get("wheat_farm"))
            if legacy in {tuple(plot) for plot in retired}:
                custom.pop("wheat_farm", None)
    return retired


def _return_to_anchor(client: Any, anchor: Optional[Tuple[int, int, int]]) -> bool:
    """Return with a separate travel budget and clear-area recovery movement.

    Long journeys exceed the harvest budget; partial travel still brings the
    next cycle closer to home. Actual threats can still interrupt the return.
    """
    if anchor is None:
        return False
    try:
        from .navigation import goto as _goto, recovery_navigation_defense

        return bool(_goto(client, anchor[0], anchor[1], anchor[2],
                          timeout=RETURN_HOME_TIMEOUT, tolerance=8.0,
                          on_defense=lambda: recovery_navigation_defense(client)))
    except Exception:
        return False


def _stock_seeds(client: Any, target: int) -> int:
    """Break grass until a seed reserve exists. Returns seeds gained.

    Bounded and best effort: failing to find grass is not a reason to abort a
    cycle, and partial stock still makes the next planting attempt cheaper.
    """
    wanted = max(0, int(target))
    if wanted <= 0:
        return 0
    try:
        from .farming import _gather_seeds

        before = _count(get_inventory(client), SEEDS)
        if before >= wanted:
            return 0
        # Bounded well under the primitive's 180s default: this now runs every
        # cycle, and a worker standing where no grass grows must not spend
        # three minutes discovering that each time.
        _gather_seeds(client, wanted, timeout=SEED_STOCK_TIMEOUT)
        return max(0, _count(get_inventory(client), SEEDS) - before)
    except Exception:
        return 0


#: Once the one-shot reset below has already fired for an anchor, keep
#: reopening its frontier every this-many further exhausted misses instead
#: of never again. Live A1: anchor (-413,78,-15) exhausted its single reset
#: weeks into a run, then returned None on every one of 4000+ later calls
#: over the following week -- the same transient setup failure (no bucket,
#: no iron) can recur, and a resource that was scarce once can still be
#: scarce the next time this exact frontier fills up.
EXHAUSTED_FRONTIER_RETRY_STREAK = 20


def _next_plot_candidate(
    anchor: Tuple[int, int, int],
    known: Sequence[Tuple[int, int, int]],
    worker: dict,
    distance: int,
    maximum_slots: int,
) -> Optional[Tuple[int, int, int]]:
    """Choose a site, periodically reopening an exhausted frontier."""
    rejected = [_position(site) for site in worker["failed_plot_sites"]]
    rejected = [site for site in rejected if site]
    cursor = int(worker.get("expansion_cursor", 0) or 0)
    candidate = _candidate(
        anchor, known, rejected, distance, cursor, maximum_slots
    )
    worker["expansion_cursor"] = cursor + 1
    anchor_key = list(anchor)
    if candidate is None and rejected:
        # Setup failures can blacklist every nearby site. A recorded plot
        # may be flooded or destroyed, so its presence cannot disable retries.
        streak = int(worker.get("exhausted_frontier_streak", 0) or 0) + 1
        if (
            worker.get("failed_site_reset_anchor") != anchor_key
            or streak >= EXHAUSTED_FRONTIER_RETRY_STREAK
        ):
            worker["failed_plot_sites"] = []
            worker["failed_site_reset_anchor"] = anchor_key
            worker["expansion_cursor"] = 1
            streak = 0
            candidate = _candidate(anchor, known, [], distance, 0, maximum_slots)
        worker["exhausted_frontier_streak"] = streak
    return candidate


def _establish_candidate(client: Any, state: Any, candidate, size: int):
    """Resolve a planned X/Z coordinate to terrain and establish one plot."""
    surface = None
    if getattr(state, "checkpoint_dir", None) and not _plots(state):
        # Adopt an existing real crop before attempting construction. The
        # industrial base can already contain a large irrigated wheat field;
        # searching only for empty soil made a fresh worker report zero plots
        # while standing beside hundreds of farmland/crop blocks.
        try:
            wheat = find_nearby_block(client, ["minecraft:wheat"], radius=32)
            if wheat is not None:
                wx, wy, wz = (int(value) for value in wheat)
                if _block_id(client, wx, wy - 1, wz) == "minecraft:farmland":
                    return (wx, wy - 1, wz)
        except Exception:
            pass
        # A prior interrupted bootstrap may already have placed its center
        # water without reaching the checkpoint write. Reuse that verified
        # irrigation instead of selecting fresh grass and spending another
        # bucket on every retry.
        try:
            water = find_water_source(client, radius=20)
            if water is not None:
                wx, wy, wz = (int(value) for value in water)
                tillable = {
                    "minecraft:dirt",
                    "minecraft:grass_block",
                    "minecraft:farmland",
                }
                usable_neighbors = sum(
                    1
                    for dx in range(-2, 3)
                    for dz in range(-2, 3)
                    if (dx or dz)
                    and _block_id(client, wx + dx, wy - 1, wz + dz) in tillable
                    and _block_id(client, wx + dx, wy, wz + dz)
                    in {
                        "minecraft:air",
                        "minecraft:cave_air",
                        "minecraft:short_grass",
                        "minecraft:tall_grass",
                        "minecraft:fern",
                        "minecraft:large_fern",
                        "minecraft:dead_bush",
                        "minecraft:snow",
                        "minecraft:wildflowers",
                        "minecraft:leaf_litter",
                    }
                )
                if (
                    _block_id(client, wx, wy - 1, wz) in tillable
                    and usable_neighbors >= 4
                ):
                    surface = (wx, wy - 1, wz)
        except Exception:
            pass
        try:
            if surface is None:
                snapshot = client.transport.dispatch("get_state", {})
                position = snapshot.get("block_position", snapshot.get("position", {}))
                local = (
                    int(float(position["x"])),
                    int(float(position["y"])),
                    int(float(position["z"])),
                )
                anchor = _base_anchor(state)
                if anchor is not None and _within_reach(
                    local, anchor, MAX_ANCHOR_RADIUS
                ):
                    surface = find_farm_surface_near(
                        client, *local, horizontal_radius=32
                    )
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
        if surface is None:
            natural, used_water = find_natural_crop_center(
                client, lambda x, y, z: _block_id(client, x, y, z)
            )
            if natural is not None and not used_water:
                surface = natural
    if surface is None:
        if candidate is None:
            return None
        _approach_candidate(client, candidate)
        surface = find_farm_surface_near(client, *candidate)
    if surface is None:
        return None
    anchor = _base_anchor(state)
    if anchor is not None and abs(surface[1] - anchor[1]) > 8:
        _return_to_anchor(client, anchor)
        return None
    return establish_wheat_farm(client, *surface, size=size, state=state)


def _survival_ready(client: Any) -> bool:
    """Refuse farm travel from dead, wrong-dimension, or critically hurt state."""
    try:
        state = client.transport.dispatch("get_state", {})
        data = state.get("data", state) if isinstance(state, Mapping) else {}
        return (
            not bool(data.get("is_dead", data.get("dead", False)))
            and "overworld" in str(data.get("dimension", ""))
            and float(data.get("health", 0) or 0) >= 12.0
        )
    except Exception:
        return False


def _worker(state: Any) -> dict:
    custom = getattr(state, "custom_data", None)
    if not isinstance(custom, dict):
        custom = {}
        state.custom_data = custom
    worker = custom.setdefault("food_worker", {})
    if not isinstance(worker, dict):
        worker = {}
        custom["food_worker"] = worker
    worker.setdefault("farm_plots", [])
    worker.setdefault("failed_plot_sites", [])
    return worker


def _flush(state: Any, client: Any) -> None:
    """Persist durable coordination state without treating a flush as output."""
    save = getattr(state, "save_checkpoint", None)
    if callable(save):
        try:
            save(get_inventory(client))
        except Exception:
            pass


def _result(
    worker: Mapping[str, Any],
    success: bool,
    detail: str,
    **values: int,
) -> FoodCycleResult:
    return FoodCycleResult(
        success,
        detail,
        **values,
        total_plots=int(worker.get("plots", 0) or 0),
        total_crop_tiles=int(worker.get("crop_tiles", 0) or 0),
        total_wheat_harvested=int(worker.get("wheat_harvested", 0) or 0),
        total_other_edible_crops_harvested=int(
            worker.get("other_edible_crops_harvested", 0) or 0
        ),
        total_crops_replanted=int(worker.get("crops_replanted", 0) or 0),
        total_bread_crafted=int(worker.get("bread_crafted", 0) or 0),
        total_prepared_food_banked=int(worker.get("prepared_food_banked", 0) or 0),
    )


def _plot_distance(client: Any, plot: Sequence[int]) -> float:
    """Straight-line distance to a plot, for reporting why a trip failed."""
    try:
        snapshot = client.transport.dispatch("get_state", {})
        position = snapshot.get("block_position", snapshot.get("position", {})) or {}
        return math.dist(
            (float(position["x"]), float(position["y"]), float(position["z"])),
            (float(plot[0]), float(plot[1]), float(plot[2])),
        )
    except Exception:
        return float("nan")


def _home_first(
    client: Any,
    state: Any,
    worker: dict,
    anchor: Optional[Tuple[int, int, int]],
    known: Sequence[Tuple[int, int, int]],
    seeds_gathered: int,
) -> Optional[FoodCycleResult]:
    """Walk a plotless worker home instead of siting from outside the base.

    Every site is planned within MAX_ANCHOR_RADIUS of base. Siting from
    further away cannot observe the ground and only blacklists good sites,
    so come home first; the next cycle sites the plot.
    """
    if anchor is None or known:
        return None
    separation = _horizontal_distance(client, anchor)
    if separation != separation or separation <= MAX_ANCHOR_RADIUS:
        return None
    went_home = _return_to_anchor(client, anchor)
    _flush(state, client)
    return _result(
        worker,
        bool(seeds_gathered),
        f"farm siting needs the base, {separation:.0f} blocks away; "
        + ("walked back to base" if went_home else "walked back toward base")
        + (f", gathered {seeds_gathered} seeds" if seeds_gathered else ""),
    )


def _waiting_for_crops(client, state, harvested):
    """Keep the live wait predicate outside the bounded cycle orchestrator."""
    return (
        harvested == 0 and _count(get_inventory(client), BREAD) < 8
        and hasattr(client, "transport")
        and local_farm_wait_reason(client, state, client.transport.dispatch("get_state", {}))
    )


def _harvest_known_plots(client, state, worker, known, limit, cursor, farm_range):
    inspected = (
        [known[(cursor + index) % len(known)] for index in range(min(limit, len(known)))]
        if known else []
    )
    harvested = other_edible = 0
    unreachable = []
    for plot in (plot for plot in inspected if not plot_on_cooldown(state, plot)):
        before = get_inventory(client)
        harvest_wheat_farm(client, *plot, range_=farm_range)
        after = get_inventory(client)
        harvested += max(0, _count(after, WHEAT) - _count(before, WHEAT))
        other_edible += sum(
            max(0, _count(after, item) - _count(before, item))
            for item in OTHER_EDIBLE_CROPS
        )
        separation = _plot_distance(client, plot)
        if separation == separation and separation > UNREACHED_PLOT_DISTANCE:
            unreachable.append((plot, separation))

    if known:
        worker["plot_cursor"] = (cursor + len(inspected)) % len(known)
    if unreachable and len(unreachable) == len(inspected) and harvested == 0:
        worker["unreachable_plots"] = [list(plot) for plot, _ in unreachable]
        nearest = min(distance for _, distance in unreachable)
        anchor = _anchor(state, known)
        retired = _retire_distant_plots(state, worker, anchor)
        if retired:
            _flush(state, client)
            return harvested, other_edible, _result(
                worker, False,
                f"retired {len(retired)} farm plot(s) beyond the "
                f"{MAX_ANCHOR_RADIUS}-block siting radius; will resite near base",
            )
        went_home = _return_to_anchor(client, anchor)
        _flush(state, client)
        return harvested, other_edible, _result(
            worker, False,
            f"could not reach {len(unreachable)} known farm plot(s); nearest is "
            f"{nearest:.0f} blocks away; "
            + ("walked back toward base" if went_home else "return to base failed"),
        )
    return harvested, other_edible, None


@return_after_food_cycle
def run_food_cycle(
    client: Any,
    state: Any,
    *,
    plot_size: int = 5,
    farm_range: int = 8,
    max_plot_distance: int = 32,
    max_plots_per_cycle: int = 3,
    max_candidate_slots: int = 3,
    personal_food_reserve: int = 8,
    seed_reserve: int = 8,
    carried_wheat_only: bool = False,
) -> FoodCycleResult:
    """Prepare carried grain first, then take one bounded farm/expansion pass."""
    worker = _worker(state)
    worker["attempts"] = int(worker.get("attempts", 0) or 0) + 1
    if not _survival_ready(client):
        _flush(state, client)
        return _result(worker, False, "survival state is unsafe for farm travel")
    try:
        safe_to_work = eat_until_hunger(client, minimum_food=14) or fed_enough_for_home_farm(client)
    except Exception:
        safe_to_work = False
    if safe_to_work and _count(get_inventory(client), WHEAT) >= 3:
        crafted, banked = _prepare_bread(client, state, personal_food_reserve, seed_reserve)
        if crafted or banked:
            values = {"cycles": 1, "bread_crafted": crafted, "prepared_food_banked": banked}
            for key, value in values.items():
                worker[key] = int(worker.get(key, 0) or 0) + value
            _flush(state, client)
            return _result(worker, True, f"crafted {crafted} bread from carried wheat; banked {banked}", **values)
        if carried_wheat_only:
            _flush(state, client)
            return _result(
                worker, False,
                "carried wheat produced no verified bread; check output space and the saved-home workstation",
            )
    if carried_wheat_only and safe_to_work:
        _flush(state, client)
        return _result(worker, False, "no craftable carried wheat remains")
    if not safe_to_work or not prepare_house_door_for_departure(
        client, state, _base_anchor(state)
    ):
        _flush(state, client)
        reason = "survival recovery could not restore food before farm travel" if not safe_to_work else "house door blocked"
        return _result(worker, False, reason)
    known = _plots(state)
    anchor = _base_anchor(state)
    retired = _retire_distant_plots(state, worker, anchor)
    if retired:
        went_home = _return_to_anchor(client, anchor)
        _flush(state, client)
        return _result(
            worker,
            False,
            f"retired {len(retired)} farm plot(s) beyond the "
            f"{MAX_ANCHOR_RADIUS}-block siting radius; "
            + (
                "returned to base for resiting"
                if went_home
                else "started bounded return to base for resiting"
            ),
        )
    known = _plots(state)
    worker["farm_plots"] = [{"origin": list(plot)} for plot in known]
    harvested = 0
    other_edible_harvested = 0
    cursor = int(worker.get("plot_cursor", 0) or 0)
    harvested, other_edible_harvested, unreachable_result = _harvest_known_plots(
        client, state, worker, known, max(1, int(max_plots_per_cycle)), cursor,
        max(1, int(farm_range)),
    )
    if unreachable_result is not None:
        return unreachable_result
    if harvested:
        # Each harvest returns more seed than replanting needs; shed it at
        # the farm instead of carrying it home into the chests.
        shed_surplus_seeds(client)

    new_plots = 0
    crop_tiles = 0
    replanted = 0
    seeds_gathered = 0
    waiting_for_crops = _waiting_for_crops(client, state, harvested)
    if harvested == 0 and not waiting_for_crops:
        anchor = _anchor(state, known)
        # Stock seeds before irrigation/travel failures can prevent collection.
        seeds_gathered = _stock_seeds(client, seed_reserve)
        home = _home_first(client, state, worker, anchor, known, seeds_gathered)
        if home is not None:
            return home
        if anchor is not None:
            candidate = _next_plot_candidate(
                anchor,
                known,
                worker,
                max(8, int(max_plot_distance)),
                max_candidate_slots,
            )
            established = _establish_candidate(
                client, state, candidate, max(3, int(plot_size) | 1)
            )
            if established:
                # The primitive verifies irrigation and at least one planted tile.
                known.append(tuple(established))
                worker["farm_plots"] = [{"origin": list(plot)} for plot in known]
                custom = getattr(state, "custom_data", {})
                if isinstance(custom, dict) and not _position(custom.get("wheat_farm")):
                    custom["wheat_farm"] = {"origin": list(established)}
                new_plots = 1
                crop_tiles = 1
                replanted = 1
            elif candidate is not None:
                worker["failed_plot_sites"].append(list(candidate))
    bread_crafted, banked = _prepare_bread(client, state, personal_food_reserve, seed_reserve)

    # Gathering seeds is real progress: it is the bootstrap the whole food
    # economy waits on, and a cycle that stocks them has not done nothing.
    success = bool(
        harvested or other_edible_harvested or new_plots or bread_crafted
        or banked or seeds_gathered
    )
    values = {
        "cycles": int(success),
        "plots": new_plots,
        "crop_tiles": crop_tiles,
        "wheat_harvested": harvested,
        "other_edible_crops_harvested": other_edible_harvested,
        "crops_replanted": replanted,
        "bread_crafted": bread_crafted,
        "prepared_food_banked": banked,
    }
    for key, value in values.items():
        worker[key] = int(worker.get(key, 0) or 0) + value
    _flush(state, client)
    detail = (
        f"harvested {harvested} wheat, established {new_plots} plot, "
        f"crafted {bread_crafted} bread, banked {banked} prepared food"
        + (f", gathered {seeds_gathered} seeds" if seeds_gathered else "")
        + (f", harvested {other_edible_harvested} other edible crops"
           if other_edible_harvested else "")
    )
    return _result(worker, success, detail, **values)


__all__ = ["FoodCycleResult", "run_food_cycle"]
