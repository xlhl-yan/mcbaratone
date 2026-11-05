"""Plan safe 1x2 tunnels through a local block map. Pure logic, no bridge calls.

A walkable tunnel cell is a *feet* cell: the cell and the one above it must be
open, and the cell below must be solid ground. Moving one cell horizontally
may also step one cell down or up (a staircase), which needs one extra
clearance cell. ``required_cells`` is the single source of truth for that
geometry; the miner digs exactly those cells and nothing else.

Safety is decided here, before any block is touched:

* never open or stand beside lava or water (a flooded tunnel kills a bot);
* never break into a natural cave (open air below the surface that our own
  tunnels did not make), because a cave is where the skeletons are;
* never dig a column with gravel or sand over its head;
* only break stone, soil, plants and ores. A chest, spawner, rail, plank or
  anything else unrecognised makes the cell impassable, which also keeps the
  planner out of mineshafts and structures.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

Cell = Tuple[int, int, int]

#: How far below the entrance an unowned open cell still counts as surface
#: air rather than a cave.
OPEN_SURFACE_MARGIN = 2

DIRS: Tuple[Tuple[int, int], ...] = ((1, 0), (-1, 0), (0, 1), (0, -1))
NEIGHBOURS: Tuple[Cell, ...] = (
    (1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1),
)

FLUIDS = frozenset(
    "minecraft:" + name
    for name in (
        "water", "lava", "bubble_column", "kelp", "kelp_plant", "seagrass",
        "tall_seagrass",
    )
)
#: Never a tunnel cell and never a floor. Includes fire, webs and magma.
HAZARDS = frozenset(
    "minecraft:" + name
    for name in (
        "fire", "soul_fire", "magma_block", "powder_snow", "cobweb",
        "sweet_berry_bush", "cactus", "campfire", "soul_campfire",
        "pointed_dripstone", "budding_amethyst", "spawner", "trial_spawner",
        "sculk_shrieker", "sculk_sensor", "sculk_catalyst", "bedrock",
    )
)
GRAVITY = frozenset(
    "minecraft:" + name for name in ("sand", "red_sand", "gravel", "suspicious_sand", "suspicious_gravel")
)
STONE = frozenset(
    "minecraft:" + name
    for name in (
        "stone", "cobblestone", "mossy_cobblestone", "deepslate",
        "cobbled_deepslate", "andesite", "diorite", "granite", "tuff",
        "calcite", "smooth_basalt", "dripstone_block", "dirt", "coarse_dirt",
        "rooted_dirt", "grass_block", "podzol", "mycelium", "mud", "clay",
        "moss_block", "sandstone", "red_sandstone", "snow_block",
    )
)
PLANTS = frozenset(
    "minecraft:" + name
    for name in (
        "short_grass", "tall_grass", "fern", "large_fern", "dead_bush", "snow",
        "moss_carpet",
    )
)
IRON_ORES = frozenset({"minecraft:iron_ore", "minecraft:deepslate_iron_ore"})
#: Torches have no collision. Ours light the tunnel, and the bot walks through.
TORCHES = frozenset(
    "minecraft:" + name for name in ("torch", "wall_torch", "soul_torch", "soul_wall_torch")
)

COST_OPEN = 0.0
COST_PLANT = 1.0
COST_STONE = 3.0
COST_ORE = 4.0


def is_ore(block_id: str) -> bool:
    return block_id.endswith("_ore") and not block_id.startswith("minecraft:nether_")


def is_support(block_id: str) -> bool:
    """A block that can hold the bot up as a floor."""
    return block_id in STONE or block_id in GRAVITY or is_ore(block_id)


@dataclass(frozen=True)
class Move:
    frm: Cell
    to: Cell
    required: Tuple[Cell, ...]
    dig: Tuple[Cell, ...]


class View:
    """A local block map: ``voxels`` holds every non-air block; absent = open."""

    def __init__(self, voxels: Dict[Cell, str], center: Cell, radius: int) -> None:
        self.voxels = voxels
        self.center = center
        self.radius = radius

    def inside(self, cell: Cell) -> bool:
        """Usable as a tunnel cell: its floor and every neighbour stay in view.

        A cell on the map edge has neighbours we cannot see, and lava hides
        there, so planning keeps two blocks clear of the edge.
        """
        cx, cy, cz = self.center
        limit = self.radius - 2
        return (
            abs(cell[0] - cx) <= limit
            and abs(cell[1] - cy) <= limit
            and abs(cell[2] - cz) <= limit
        )

    def block(self, cell: Cell) -> str:
        return self.voxels.get(cell, "")

    def near_fluid(self, cell: Cell) -> bool:
        x, y, z = cell
        if self.block(cell) in FLUIDS:
            return True
        return any(
            self.block((x + dx, y + dy, z + dz)) in FLUIDS for dx, dy, dz in NEIGHBOURS
        )

    def iron_ores(self) -> Set[Cell]:
        return {cell for cell, block in self.voxels.items() if block in IRON_ORES}


def approach_cells(ore: Cell) -> List[Cell]:
    """Feet cells beside ``ore`` from which it can be mined without entering it."""
    x, y, z = ore
    cells: List[Cell] = []
    for dx, dz in DIRS:
        cells.append((x + dx, y, z + dz))      # ore at feet height
        cells.append((x + dx, y - 1, z + dz))  # ore at head height
    return cells


def ore_is_safe(view: View, ore: Cell, own: Set[Cell], surface_y: int) -> bool:
    """Breaking ``ore`` must not let in lava, water, gravel or a cave."""
    x, y, z = ore
    if view.near_fluid(ore) or view.block((x, y + 1, z)) in GRAVITY:
        return False
    for dx, dy, dz in NEIGHBOURS:
        neighbour = (x + dx, y + dy, z + dz)
        if neighbour in view.voxels:
            continue
        if neighbour not in own and neighbour[1] < surface_y - OPEN_SURFACE_MARGIN:
            # Open and not ours. Our approach cell is open and in ``own`` by
            # the time we mine from it, so anything else here is a cave.
            return False
    return True


def required_cells(frm: Cell, to: Cell) -> Tuple[Cell, ...]:
    """Cells that must be open for a one-cell horizontal move, with its stair."""
    fx, fy, fz = frm
    tx, ty, tz = to
    cells = [to, (tx, ty + 1, tz)]
    if ty == fy - 1:
        cells.append((tx, fy + 1, tz))  # headroom over the transit cell
    elif ty == fy + 1:
        cells.append((fx, fy + 2, fz))  # headroom to step up
    return tuple(cells)


def cell_cost(
    view: View, cell: Cell, own: Set[Cell], surface_y: int
) -> Optional[float]:
    """Cost of passing through ``cell``; ``None`` makes it impassable."""
    if not view.inside(cell):
        return None
    block = view.voxels.get(cell)
    if block is None:
        # Open air is fine in our own tunnels and near the surface. Anywhere
        # else it is a natural cave, which we never enter.
        if cell in own or cell[1] >= surface_y - OPEN_SURFACE_MARGIN:
            return COST_OPEN
        return None
    if block in TORCHES:
        return COST_OPEN if cell in own else None
    if block in FLUIDS or block in HAZARDS or block in GRAVITY:
        return None
    if block in PLANTS:
        return COST_PLANT
    if is_ore(block):
        return COST_ORE
    if block in STONE:
        return COST_STONE
    return None


def evaluate_move(
    view: View, frm: Cell, to: Cell, own: Set[Cell], surface_y: int
) -> Optional[Move]:
    required = required_cells(frm, to)
    total = 0.0
    dig: List[Cell] = []
    for cell in required:
        cost = cell_cost(view, cell, own, surface_y)
        if cost is None or view.near_fluid(cell):
            return None
        total += cost
        if view.voxels.get(cell) not in (None, *TORCHES):
            dig.append(cell)
    floor = (to[0], to[1] - 1, to[2])
    floor_block = view.voxels.get(floor, "")
    if not is_support(floor_block) or view.near_fluid(floor):
        return None
    # Gravel or sand over the head of any dug column would fall into the tunnel.
    columns: Dict[Tuple[int, int], int] = {}
    for x, y, z in required:
        columns[(x, z)] = max(columns.get((x, z), y), y)
    for (x, z), top in columns.items():
        if view.block((x, top + 1, z)) in GRAVITY:
            return None
    # Dig from the top down so nothing is ever left hanging over the bot.
    dig.sort(key=lambda cell: -cell[1])
    return Move(frm, to, required, tuple(dig))


def plan(
    view: View,
    start: Cell,
    goals: Iterable[Cell],
    own: Set[Cell],
    surface_y: int,
    *,
    max_cost: float = 250.0,
    protected_floors: Iterable[Cell] = (),
) -> Optional[List[Move]]:
    """Cheapest safe path from ``start`` to any goal cell, or ``None``.

    The search remembers, per path, which cells earlier moves stand on and
    which they dug, so it never digs out a stair it needs for the way home
    and never plans to stand on a cell it already dug. That is what makes a
    spiral down to ore directly beneath the bot a valid plan.
    """
    goal_set = set(goals)
    if not goal_set:
        return None
    best: Dict[Cell, float] = {start: 0.0}
    came: Dict[Cell, Tuple[Cell, Move]] = {}
    # A trip may plan in several batches. Floors supporting the route from
    # earlier batches are just as important as floors laid down in this path.
    initial_floors = frozenset(protected_floors)
    floors: Dict[Cell, frozenset] = {start: initial_floors}
    dug: Dict[Cell, frozenset] = {start: frozenset()}
    heap: List[Tuple[float, Cell]] = [(0.0, start)]
    reached: Optional[Cell] = None
    while heap:
        distance, cell = heapq.heappop(heap)
        if distance > best.get(cell, float("inf")):
            continue
        if cell in goal_set and cell != start:
            reached = cell
            break
        for dx, dz in DIRS:
            for dy in (0, -1, 1):
                nxt = (cell[0] + dx, cell[1] + dy, cell[2] + dz)
                move = evaluate_move(view, cell, nxt, own, surface_y)
                if move is None:
                    continue
                floor = (nxt[0], nxt[1] - 1, nxt[2])
                if floor in dug[cell] or any(c in floors[cell] for c in move.dig):
                    continue
                step = 1.0 + sum(
                    cell_cost(view, c, own, surface_y) or 0.0 for c in move.required
                )
                cost = distance + step
                if cost > max_cost or cost >= best.get(nxt, float("inf")):
                    continue
                best[nxt] = cost
                came[nxt] = (cell, move)
                floors[nxt] = floors[cell] | {floor}
                dug[nxt] = dug[cell] | set(move.dig)
                heapq.heappush(heap, (cost, nxt))
    if reached is None:
        return None
    path: List[Move] = []
    node = reached
    while node != start:
        node, move = came[node]
        path.append(move)
    path.reverse()
    return path if path_is_consistent(path, protected_floors=initial_floors) else None


def path_is_consistent(
    path: Sequence[Move], *, protected_floors: Iterable[Cell] = ()
) -> bool:
    """No move may dig a prior or protected route floor."""
    floors: Set[Cell] = set(protected_floors)
    dug: Set[Cell] = set()
    for move in path:
        if any(cell in floors for cell in move.dig):
            return False
        dug.update(move.dig)
        floor = (move.to[0], move.to[1] - 1, move.to[2])
        if floor in dug:
            return False
        floors.add(floor)
    return True


def coarse_waypoint(
    current: Cell, target: Cell, steps: int = 7, *, prefer_z: bool = False
) -> Cell:
    """A feet cell ``steps`` greedy staircase moves from ``current`` to ``target``."""
    x, y, z = current
    tx, ty, tz = target
    for _ in range(steps):
        dx, dy, dz = tx - x, ty - y, tz - z
        if dx == 0 and dz == 0:
            if dy == 0:
                break
            dx = 1  # a staircase must move sideways while it changes level
        along_z = abs(dz) > abs(dx) or (abs(dz) == abs(dx) and prefer_z)
        if along_z and dz != 0:
            z += 1 if dz > 0 else -1
        elif dx != 0:
            x += 1 if dx > 0 else -1
        else:
            z += 1 if dz > 0 else -1
        if dy:
            y += 1 if dy > 0 else -1
    return (x, y, z)


def loop_erase(trail: Sequence[Cell]) -> List[Cell]:
    """The simple path through a walk: erase every loop it makes."""
    out: List[Cell] = []
    where: Dict[Cell, int] = {}
    for cell in trail:
        if cell in where:
            keep = where[cell]
            for dropped in out[keep + 1:]:
                where.pop(dropped, None)
            del out[keep + 1:]
        else:
            where[cell] = len(out)
            out.append(cell)
    return out


def cells_of_path(path: Sequence[Move]) -> Set[Cell]:
    cells: Set[Cell] = set()
    for move in path:
        cells.update(move.required)
    return cells


__all__ = [
    "Cell", "DIRS", "FLUIDS", "GRAVITY", "HAZARDS", "IRON_ORES", "Move", "TORCHES", "View",
    "approach_cells", "cell_cost", "cells_of_path", "coarse_waypoint", "evaluate_move",
    "is_ore", "is_support", "loop_erase", "ore_is_safe", "path_is_consistent", "plan",
    "required_cells",
]
