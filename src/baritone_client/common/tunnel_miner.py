"""Dig planned tunnels with the bridge and bring the bot home alive.

``tunnel_planner`` decides what is safe to dig; this module does the digging
and every check that needs the live world: health, hostiles, the block that is
actually there right before it is broken, and whether the bot really arrived.
Anything unexpected raises ``MineAbort`` and the caller retraces its own trail
to the surface. It never uses Baritone's ``mine``, which dives into caves.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from .tunnel_planner import (
    FLUIDS, GRAVITY, HAZARDS, IRON_ORES, PLANTS, STONE, Cell, Move, View,
    approach_cells, coarse_waypoint, is_ore, loop_erase, ore_is_safe, plan,
    required_cells, is_support,
)

OPEN_BLOCKS = frozenset({"minecraft:air", "minecraft:cave_air"})
HOSTILES = frozenset(
    "minecraft:" + name
    for name in (
        "zombie", "zombie_villager", "husk", "drowned", "skeleton", "stray",
        "bogged", "spider", "cave_spider", "creeper", "silverfish", "witch",
        "pillager", "vindicator", "phantom", "slime",
    )
)
VIEW_RADIUS = 8
ABORT_HEALTH = 10.0
HOSTILE_RADIUS = 7.0
#: How long the defence reflex gets to deal with a mob before the trip ends.
HOSTILE_PATIENCE = 12.0
#: What digging fills the pack with. Safe to throw away mid-trip: it is rubble.
DIGGING_JUNK = frozenset(
    "minecraft:" + name
    for name in (
        "cobblestone", "dirt", "coarse_dirt", "rooted_dirt", "grass_block", "podzol",
        "mud", "clay", "andesite", "diorite", "granite", "tuff", "calcite",
        "cobbled_deepslate", "deepslate", "sand", "sandstone", "gravel",
        "dripstone_block", "smooth_basalt", "moss_block",
    )
)
TORCH_EVERY = 7
#: Kept inside the planner's window: a cell more than 6 blocks from the bot is unplannable.
PLAN_MOVES = 5
MAX_TRAIL = 400
MIN_FREE_SLOTS = 3
PICKAXE_RETURN_RESERVE = 32
MINING_PICKAXES = {f"minecraft:{tier}_pickaxe" for tier in ("stone", "iron", "diamond", "netherite")}


def route_travel_seconds(cells: Sequence[Cell]) -> float:
    """Bound travel by the saved route, including the longest retained trail."""
    return min(4800.0, max(240.0, 12.0 * (len(cells) - 1)))


class MineAbort(Exception):
    """The trip must stop and retrace; ``reason`` says why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RouteBroken(MineAbort):
    """A saved step is proven broken; ``index`` is the last cell reached."""

    def __init__(self, reason: str, index: int) -> None:
        super().__init__(reason)
        self.index = index


@dataclass
class TripStats:
    moves: int = 0
    dug: int = 0
    torches: int = 0
    reason: str = ""


def _unwrap(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    data = value.get("data", value)
    return data if isinstance(data, dict) else {}


def own_cells_of(spine: Sequence[Cell]) -> Set[Cell]:
    """Every open cell a recorded spine implies, so old tunnels are not 'caves'."""
    own: Set[Cell] = set(spine)
    for frm, to in zip(spine, spine[1:]):
        if abs(frm[0] - to[0]) + abs(frm[2] - to[2]) == 1 and abs(frm[1] - to[1]) <= 1:
            own.update(required_cells(frm, to))
            own.add((frm[0], frm[1] + 1, frm[2]))
    return own


def free_slots(client: Any) -> int:
    """Empty main-inventory slots (the hotbar and the 27 above it)."""
    from ..inventory_evidence import free_carried_slots

    return free_carried_slots(client.transport.dispatch("get_inventory", {}))


class TunnelMiner:
    def __init__(
        self,
        client: Any,
        *,
        surface_y: int,
        deadline: float,
        spine: Sequence[Cell] = (),
        entrance: Optional[Cell] = None,
        min_y: int = 12,
        count_fn: Optional[Callable[[str], int]] = None,
        patience: float = HOSTILE_PATIENCE,
        on_progress: Optional[Callable[[Sequence[Cell]], None]] = None,
    ) -> None:
        self.client = client
        self.surface_y = int(surface_y)
        self.deadline = float(deadline)
        self.min_y = int(min_y)
        self.patience = float(patience)
        self.trail: List[Cell] = list(spine) or ([entrance] if entrance else [])
        self.own: Set[Cell] = own_cells_of(self.trail)
        self._protected_floor_cells: Set[Cell] = {
            (cell[0], cell[1] - 1, cell[2]) for cell in self.trail
        }
        self.skip: Set[Cell] = set()
        self.stats = TripStats()
        self._since_torch = 0
        self._torch_failures = 0
        self._count = count_fn or self._default_count
        self.on_progress = on_progress

    # -- bridge access ------------------------------------------------------
    def _call(self, route: str, payload: Optional[dict] = None) -> Dict[str, Any]:
        try:
            response = self.client.transport.dispatch(route, payload or {})
        except Exception as exc:
            raise MineAbort(f"bridge {route} failed: {exc}") from exc
        if route in {"get_state", "get_entities"}:
            for envelope in (response, response.get("data") if isinstance(response, dict) else None):
                if isinstance(envelope, dict) and (
                    envelope.get("success") is False
                    or str(envelope.get("status", "")).lower() == "error"
                    or envelope.get("error")
                ):
                    raise MineAbort(f"bridge {route} telemetry error")
        return _unwrap(response)

    def _default_count(self, item: str) -> int:
        from .inventory import count_item

        return int(count_item(self.client, item) or 0)

    def state(self) -> Dict[str, Any]:
        live = self._call("get_state")
        position = live.get("block_position") or live.get("position") or {}
        try:
            coordinates = []
            for axis in ("x", "y", "z"):
                coordinate = position[axis]
                if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float)):
                    raise ValueError("coordinate must be numeric")
                coordinate_value = float(coordinate)
                if not math.isfinite(coordinate_value):
                    raise ValueError("coordinate must be finite")
                coordinates.append(int(coordinate_value))
            live["cell"] = tuple(coordinates)
            health = live["health"]
            health_value = float(health)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise MineAbort("player telemetry invalid") from exc
        if (
            isinstance(health, bool)
            or not isinstance(health, (int, float))
            or not math.isfinite(health_value)
            or health_value < 0
            or not isinstance(live.get("is_dead"), bool)
            or not isinstance(live.get("dimension"), str)
            or not live["dimension"]
        ):
            raise MineAbort("player telemetry invalid")
        return live

    def cell(self) -> Cell:
        return self.state()["cell"]

    def block(self, cell: Cell) -> str:
        data = self._call("get_block", {"x": cell[0], "y": cell[1], "z": cell[2]})
        return str(data.get("id") or data.get("block") or "")

    def view(self) -> View:
        data = self._call("get_view", {"radius": VIEW_RADIUS})
        voxels = {
            (int(v["x"]), int(v["y"]), int(v["z"])): str(v.get("id", ""))
            for v in data.get("voxels", [])
        }
        return View(voxels, self.cell(), VIEW_RADIUS)

    def free_slots(self) -> int:
        return free_slots(self.client)

    def make_room(self) -> int:
        """Throw away dug rubble to free pack space; returns slots freed."""
        from .inventory import drop_items

        before = self.free_slots()
        try:
            drop_items(self.client, sorted(DIGGING_JUNK), max_stacks=8)
        except Exception as exc:
            print(f"TUNNEL MINER: could not drop rubble ({exc})")
        return max(0, self.free_slots() - before)

    # -- safety -------------------------------------------------------------
    def hostiles(self, player_state: Optional[Dict[str, Any]] = None) -> int:
        """Count actionable nearby threats using the canonical defense policy.

        Missing or malformed observations must stop a trip, not masquerade as
        an empty area. The miner keeps its tighter seven-block work radius,
        while ``assess_threats`` applies the shared visibility/aggression rules.
        """
        from .defense import assess_threats

        if player_state is None:
            player_state = self.state()
        if not isinstance(player_state, dict) or "cell" not in player_state:
            raise MineAbort("player telemetry invalid")

        observation = self._call(
            "get_entities", {"radius": int(HOSTILE_RADIUS) + 3}
        )
        skipped = observation.get("skipped_count", 0)
        if isinstance(skipped, bool) or not isinstance(skipped, int) or skipped != 0:
            raise MineAbort("entity telemetry incomplete")
        entities = observation.get("entities")
        if not isinstance(entities, list):
            raise MineAbort("entity telemetry invalid")
        for entity in entities:
            if not isinstance(entity, dict) or not isinstance(entity.get("type"), str):
                raise MineAbort("entity telemetry invalid")
            distance = entity.get("distance")
            try:
                distance_value = float(distance)
            except (TypeError, ValueError, OverflowError) as exc:
                raise MineAbort("entity telemetry invalid") from exc
            if (isinstance(distance, bool) or not isinstance(distance, (int, float))
                    or not math.isfinite(distance_value) or distance_value < 0):
                raise MineAbort("entity telemetry invalid")

        return sum(
            1
            for threat in assess_threats(entities, player_state)
            if threat.distance <= HOSTILE_RADIUS
        )

    def check(self, *, returning: bool = False) -> Dict[str, Any]:
        """Raise ``MineAbort`` unless it is still safe to keep digging."""
        live = self.state()
        if live.get("is_dead") or float(live.get("health", 0) or 0) <= 0:
            raise MineAbort("died")
        if "overworld" not in str(live.get("dimension", "minecraft:overworld")):
            raise MineAbort("left the overworld")
        if float(live.get("health", 20) or 0) < ABORT_HEALTH:
            raise MineAbort("health low")
        if not returning and time.monotonic() > self.deadline:
            raise MineAbort("time")
        if self.hostiles(live):
            # The safety system owns fighting; give it a moment, then leave.
            waited = 0.0
            while waited < self.patience:
                time.sleep(1.0)
                waited += 1.0
                live = self.state()
                if float(live.get("health", 20) or 0) < ABORT_HEALTH:
                    raise MineAbort("health low")
                if not self.hostiles(live):
                    break
            else:
                raise MineAbort("hostile mob")
        return live

    # -- actions ------------------------------------------------------------
    def dig(self, cell: Cell) -> None:
        """Break one cell, after confirming what is really there."""
        block = self.block(cell)
        if block in OPEN_BLOCKS:
            return
        if not block or block == "minecraft:void_air":
            raise MineAbort("terrain not loaded")
        if (
            block in FLUIDS or block in HAZARDS or block in GRAVITY
            or not (block in STONE or block in PLANTS or is_ore(block))
        ):
            raise MineAbort(f"unexpected block {block}")
        for attempt in range(2):
            self.equip()  # Recheck the held tool before every block/retry.
            self._call("dig_block", {"x": cell[0], "y": cell[1], "z": cell[2], "max_ticks": 160})
            deadline = time.monotonic() + 9.0
            while time.monotonic() < deadline:
                time.sleep(0.3)
                if self.block(cell) in OPEN_BLOCKS:
                    self.stats.dug += 1
                    self.own.add(cell)  # what we dug is our tunnel, not a cave
                    return
        raise MineAbort(f"could not dig {block}")

    def equip(self) -> None:
        from .resources import equip_best_pickaxe
        from ..inventory_evidence import unwrap_inventory, valid_inventory

        if not equip_best_pickaxe(self.client, pickaxe_ids=sorted(MINING_PICKAXES)):
            raise MineAbort("no pickaxe")
        try:
            raw = self.client.transport.dispatch("get_inventory", {})
        except Exception as exc:
            raise MineAbort("pickaxe durability unverified") from exc
        data = unwrap_inventory(raw)
        if (not valid_inventory(raw) or not isinstance(data, dict) or raw.get("success") is False
                or data.get("success") is False or data.get("snapshot_valid") is not True
                or not isinstance(data.get("inventory"), list)):
            raise MineAbort("pickaxe durability unverified")
        selected = data.get("selected_slot")
        rows = data.get("inventory", [])
        matches = [r for r in rows if type(r.get("slot")) is int and r["slot"] == selected] if type(selected) is int and 0 <= selected < 9 else []
        held = matches[0] if len(matches) == 1 else {}
        maximum, damage = held.get("max_damage"), held.get("damage")
        if (held.get("id") not in MINING_PICKAXES or held.get("count", 0) <= 0
                or type(maximum) is not int or type(damage) is not int or not 0 <= damage < maximum):
            raise MineAbort("pickaxe durability unverified")
        if maximum - damage <= PICKAXE_RETURN_RESERVE:
            raise MineAbort("pickaxe return reserve")

    def step(self, target: Cell) -> None:
        from .navigation import goto

        for tolerance in (0.8, 0.5):
            goto(self.client, *target, timeout=25, check_interval=0.5, tolerance=tolerance)
            if self.cell() == target:
                return
        raise MineAbort("did not arrive")

    def torch(self, cell: Cell, move_to: Cell) -> None:
        if self._since_torch < TORCH_EVERY or cell[1] >= self.surface_y - 3:
            return
        if self._torch_failures >= 3 or self._count("minecraft:torch") <= 0:
            return
        from . import harness_ops

        if harness_ops.place_block_exact(self.client, *cell, "minecraft:torch", allow_break=False):
            self.stats.torches += 1
            self._since_torch = 0
            self._torch_failures = 0
        else:
            self._torch_failures += 1  # stop trying after three misses in a row
        if self.cell() != move_to:
            self.step(move_to)  # placing may have shuffled the bot

    def advance(self, move: Move) -> None:
        """Dig and enter one cell of a planned path."""
        here = self.check()["cell"]
        if here != move.frm:
            raise MineAbort("moved off the planned path")
        if not self.trail or self.trail[-1] != here:
            self.trail = loop_erase(self.trail + [here])  # e.g. entered a cell off
        protected_floors = self._protected_floors(here)
        if any(cell in protected_floors for cell in move.dig):
            raise MineAbort("planned dig would remove a recorded tunnel floor")
        if move.dig:
            self.equip()
        for cell in move.dig:
            self.dig(cell)
        self.step(move.to)
        self.own.update(move.required)
        self._protected_floor_cells.add((move.to[0], move.to[1] - 1, move.to[2]))
        self.trail = loop_erase(self.trail + [move.to])[-MAX_TRAIL:]
        self.stats.moves += 1
        self._since_torch += 1
        self.torch(move.frm, move.to)
        self._note_progress()

    def _note_progress(self) -> None:
        if self.on_progress is not None:
            try:
                self.on_progress(self.trail)
            except Exception as exc:
                # Checkpoint I/O must never interrupt a safe return walk.
                print(f"TUNNEL MINER: progress checkpoint failed ({exc})")

    def execute(self, path: Sequence[Move]) -> None:
        for move in path:
            self.advance(move)

    # -- movement over our own tunnel --------------------------------------
    def _hop(
        self, cells: Sequence[Cell], *, timeout: int = 40,
        returning: bool = False, return_deadline: Optional[float] = None,
    ) -> None:
        from .navigation import goto
        from .home_surface import _read_break_setting, _write_break_setting

        if not cells:
            return
        try:
            setting = _read_break_setting(self.client)
            if setting == "true":
                # The bridge acknowledges the request before the game thread
                # applies it; the shared helper polls for observed state.
                _write_break_setting(self.client, "false")
        except Exception as exc:
            raise MineAbort(f"cannot verify digging is disabled for tunnel travel: {exc}") from exc
        try:
            here = self.cell()
            for index, target in enumerate(cells):
                beyond = cells[index + 1] if index + 1 < len(cells) else None
                if return_deadline is not None and time.monotonic() >= return_deadline:
                    raise MineAbort("return route time limit")
                dx, dy, dz = (target[i] - here[i] for i in range(3))
                if abs(dx) + abs(dz) != 1 or abs(dy) > 1:
                    raise MineAbort("recorded tunnel contains a non-adjacent waypoint")
                arrived = False
                for _attempt in range(2):
                    self.check(returning=returning)
                    if return_deadline is not None and time.monotonic() >= return_deadline:
                        raise MineAbort("return route time limit")
                    from .tunnel_travel_defense import TARGET_UNSUPPORTED, CorridorStepDefense, broken_step

                    on_defense = CorridorStepDefense(
                        self.client, here, target, self.block, beyond=beyond
                    )
                    if not on_defense.preflight():
                        reason = on_defense.abort_reason or "tunnel corridor is unsafe"
                        if (not returning and reason == TARGET_UNSUPPORTED
                                and broken_step(target, self.block)):
                            raise RouteBroken(reason, index)
                        raise MineAbort(reason)
                    goto(
                        self.client, *target,
                        timeout=min(timeout, max(1, int(return_deadline - time.monotonic())))
                        if return_deadline is not None else timeout,
                        check_interval=0.5, tolerance=0.8,
                        on_defense=on_defense,
                    )
                    if on_defense.abort_reason is not None:
                        raise MineAbort(on_defense.abort_reason)
                    observed = self.cell()
                    if observed == target:
                        arrived = True
                        break
                if not arrived:
                    raise MineAbort("blocked while following the tunnel")
                here = target
                self._note_progress()
        finally:
            # Navigation must have stopped before restoring a setting that
            # could let Baritone break terrain on its next path.
            # An unverified stop still leaves digging off, but owes it back:
            # silently leaving it off is how A1 was trapped in a shaft.
            from .home_surface import _owe

            try:
                live = self._call("get_state")
            except MineAbort:
                if setting == "true":
                    _owe(self.client, {"allowBreak": "true"})
                raise
            if live.get("is_pathing") is not False:
                if setting == "true":
                    _owe(self.client, {"allowBreak": "true"})
                raise MineAbort("tunnel travel stop was not verified")
            if setting == "true":
                try:
                    _write_break_setting(self.client, "true")
                except Exception as exc:
                    _owe(self.client, {"allowBreak": "true"})
                    raise MineAbort(f"could not restore allowBreak after tunnel travel: {exc}") from exc

    def descend(self, spine: Sequence[Cell]) -> None:
        """Walk a recorded spine from the entrance to its end.

        A step proven broken partway down ends the descent at the last good
        step: mining starts there and the saved route is cut to it. Live A1
        2026-11-02 walked 83 steps down to y=48, found two floorless steps,
        and climbed all the way back with 0 iron, as every trip would.
        """
        if len(spine) > 1:
            if self.cell() != spine[0]:
                raise MineAbort("not at the recorded tunnel entrance")
            try:
                self._hop(spine[1:])
            except RouteBroken as broken:
                reached = tuple(spine[broken.index])
                if broken.index < 1 or self.cell() != reached:
                    raise  # the entrance prefix has its own skip; keep the route
                print(f"TUNNEL MINER: saved route broken at step {broken.index + 1} "
                      f"{tuple(spine[broken.index + 1])}; mining from step {broken.index} {reached}")
                self.trail = [tuple(cell) for cell in spine[: broken.index + 1]]
                self._note_progress()

    def retreat(self) -> bool:
        """Retrace our own trail to the entrance; True when the bot got there."""
        path = list(reversed(loop_erase(self.trail)))
        try:
            here = self.cell()
            forward = list(reversed(path))
            if here not in forward:
                return False
            index = forward.index(here)
            route = list(reversed(forward[: index + 1]))
            if len(route) > 1:
                # Every cell receives fresh corridor checks and exact XYZ
                # verification. A fixed four minutes strands long saved
                # routes even when every step succeeds. Still bound the
                # complete return and each individual hop.
                deadline = time.monotonic() + route_travel_seconds(route)
                self._hop(route[1:], timeout=20, returning=True, return_deadline=deadline)
            return self.cell() == forward[0]
        except MineAbort as exc:
            print(f"TUNNEL MINER: retreat stopped ({exc.reason})")
            return False

    # -- the mining loop ----------------------------------------------------
    def pick_target(self, here: Cell) -> Optional[Cell]:
        found = self._call(
            "find_blocks", {"blocks": sorted(IRON_ORES), "radius": 48, "limit": 400}
        ).get("found", [])
        best: Optional[Tuple[float, Cell]] = None
        for entry in found:
            ore = (int(entry["x"]), int(entry["y"]), int(entry["z"]))
            if ore in self.skip or ore[1] < self.min_y or ore[1] > self.surface_y - 3:
                continue
            cost = abs(ore[0] - here[0]) + abs(ore[2] - here[2]) + 1.5 * abs(ore[1] - here[1])
            if best is None or cost < best[0]:
                best = (cost, ore)
        return best[1] if best else None

    def mine_adjacent(self, view: View, here: Cell, raw_now: Callable[[], int]) -> bool:
        """Mine any safe iron ore touching the bot's feet or head cell."""
        standing = self.own | {here, (here[0], here[1] + 1, here[2])}
        protected_floors = self._protected_floors(here)
        for ore in sorted(view.iron_ores() - self.skip):
            touching = (
                abs(ore[0] - here[0]) + abs(ore[2] - here[2]) == 1 and ore[1] in (here[1], here[1] + 1)
            )
            if not touching:
                continue
            if ore in protected_floors:
                self.skip.add(ore)
                continue
            if not ore_is_safe(view, ore, standing, self.surface_y):
                self.skip.add(ore)
                continue
            before = raw_now()
            self.equip()
            self.dig(ore)
            time.sleep(1.2)  # the drop pops out and is picked up from the next cell
            self.skip.add(ore)
            if raw_now() <= before:
                print(f"TUNNEL MINER: {ore} dug but no raw iron arrived")
            return True
        return False

    def _protected_floors(self, here: Cell) -> Set[Cell]:
        """Floors for the complete saved/current route, across plan batches."""
        return self._protected_floor_cells | {
            (cell[0], cell[1] - 1, cell[2])
            for cell in set(self.trail) | {here}
        }

    def mine(self, raw_goal: int, raw_now: Callable[[], int]) -> str:
        """Dig toward iron until the goal, the clock or a safety limit."""
        misses = 0
        while True:
            self.check()
            if raw_now() >= raw_goal:
                return "quota"
            if self.free_slots() < MIN_FREE_SLOTS:
                self.make_room()
                if self.free_slots() < MIN_FREE_SLOTS:
                    return "inventory full"
            view = self.view()
            here = view.center
            print(
                f"TUNNEL MINER: at {here}, {self.surface_y - here[1]} below the entrance; "
                f"{self.stats.moves} moves, {self.stats.dug} dug, {self.stats.torches} torches, "
                f"{raw_now()} raw iron"
            )
            if self.mine_adjacent(view, here, raw_now):
                continue
            ores = set()
            protected_floors = self._protected_floors(here)
            for ore in view.iron_ores() - self.skip:
                if ore in protected_floors:
                    self.skip.add(ore)
                elif ore_is_safe(view, ore, self.own, self.surface_y):
                    ores.add(ore)
                else:
                    self.skip.add(ore)
            goals = {cell for ore in ores for cell in approach_cells(ore)}
            path = plan(
                view, here, goals, self.own, self.surface_y,
                protected_floors=protected_floors,
            ) if goals else None
            if goals and not path:
                # Unreachable from here: forget ores we could have planned to.
                self.skip.update(o for o in ores if any(view.inside(c) for c in approach_cells(o)))
            if not path:
                target = self.pick_target(here)
                if target is None:
                    return "no reachable iron"
                if self._count("minecraft:torch") <= 0 and here[1] < self.surface_y - 12:
                    return "out of torches"
                for steps, prefer_z in ((PLAN_MOVES, False), (PLAN_MOVES, True), (3, False)):
                    waypoint = coarse_waypoint(here, target, steps, prefer_z=prefer_z)
                    if waypoint != here:
                        path = plan(
                            view, here, {waypoint}, self.own, self.surface_y,
                            protected_floors=protected_floors,
                        )
                        if path:
                            break
                if not path:
                    self.skip.add(target)
                    misses += 1
                    if misses >= 3:
                        return "no safe route"
                    continue
            misses = 0
            self.execute(path[:PLAN_MOVES])


__all__ = [
    "DIGGING_JUNK", "HOSTILES", "MineAbort", "RouteBroken", "TripStats", "TunnelMiner", "free_slots",
    "own_cells_of",
]
