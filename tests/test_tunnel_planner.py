"""The tunnel planner digs only safe, walkable 1x2 staircases."""

from baritone_client.common import tunnel_planner as tp

STONE = "minecraft:stone"
IRON = "minecraft:iron_ore"


def make_view(radius=8, center=(0, 70, 0), surface=69, mutate=None):
    """Stone below ``surface``, grass on top, air above."""
    voxels = {}
    cx, cy, cz = center
    for x in range(cx - radius, cx + radius + 1):
        for z in range(cz - radius, cz + radius + 1):
            for y in range(cy - radius, surface + 1):
                voxels[(x, y, z)] = "minecraft:grass_block" if y == surface else STONE
    if mutate:
        mutate(voxels)
    return tp.View(voxels, center, radius)


def test_required_cells_cover_each_stair_shape():
    assert tp.required_cells((0, 70, 0), (1, 70, 0)) == ((1, 70, 0), (1, 71, 0))
    down = tp.required_cells((0, 70, 0), (1, 69, 0))
    assert set(down) == {(1, 69, 0), (1, 70, 0), (1, 71, 0)}
    up = tp.required_cells((0, 70, 0), (1, 71, 0))
    assert set(up) == {(1, 71, 0), (1, 72, 0), (0, 72, 0)}


def test_stairs_down_to_an_ore_and_never_dig_a_floor():
    view = make_view(mutate=lambda v: v.__setitem__((5, 64, 0), IRON))
    path = tp.plan(view, (0, 70, 0), {(5, 64, 0)}, set(), 70)
    assert path is not None and path[-1].to == (5, 64, 0)
    assert tp.path_is_consistent(path)
    for move in path:
        for cell in move.dig:
            assert view.block(cell) in tp.STONE | {IRON, "minecraft:grass_block"}
        floor = (move.to[0], move.to[1] - 1, move.to[2])
        # Standing on ground that no earlier move dug out.
        assert view.block(floor) in (STONE, "minecraft:grass_block")


def test_later_plan_batches_preserve_floors_from_the_existing_route():
    view = make_view(mutate=lambda v: v.__setitem__((5, 64, 0), IRON))
    start, goal = (0, 70, 0), {(5, 64, 0)}
    first = tp.plan(view, start, goal, set(), 70)
    assert first is not None
    protected = {cell for move in first for cell in move.dig}

    later = tp.plan(view, start, goal, set(), 70, protected_floors=protected)

    assert later is None or all(not (set(move.dig) & protected) for move in later)
    assert later is None or tp.path_is_consistent(later, protected_floors=protected)


def test_path_consistency_rejects_digging_a_floor_from_a_previous_batch():
    move = tp.Move(
        (0, 70, 0), (1, 70, 0), ((1, 70, 0), (1, 71, 0)),
        ((1, 69, 0),),
    )
    assert not tp.path_is_consistent([move], protected_floors={(1, 69, 0)})


def test_lava_beside_a_cell_makes_it_impassable_and_the_planner_goes_around():
    def lava_wall(v):
        v[(5, 64, 0)] = IRON
        for y in range(60, 69):
            v[(3, y, 1)] = "minecraft:lava"  # a lava curtain beside the direct line

    view = make_view(mutate=lava_wall)
    path = tp.plan(view, (0, 70, 0), {(5, 64, 0)}, set(), 70)
    assert path is not None
    lava = {c for c, b in view.voxels.items() if b == "minecraft:lava"}
    for move in path:
        for cell in move.required + ((move.to[0], move.to[1] - 1, move.to[2]),):
            for dx, dy, dz in tp.NEIGHBOURS:
                assert (cell[0] + dx, cell[1] + dy, cell[2] + dz) not in lava


def test_enclosed_by_lava_has_no_path():
    def ring(v):
        v[(5, 64, 0)] = IRON
        for x in range(3, 8):
            for y in range(60, 69):
                for z in range(-3, 4):
                    v[(x, y, z)] = "minecraft:lava"
        v[(5, 64, 0)] = IRON

    assert tp.plan(make_view(mutate=ring), (0, 70, 0), {(5, 64, 0)}, set(), 70) is None


def test_natural_cave_is_never_entered_but_our_own_tunnel_is():
    # A pre-dug level tunnel at y=60..61 from x=0..6; ore just past its end.
    tunnel = {(x, y, 0) for x in range(0, 7) for y in (60, 61)}

    def dug(v):
        for cell in tunnel:
            v.pop(cell, None)
        v[(8, 60, 0)] = IRON

    view = make_view(center=(3, 60, 0), mutate=dug)
    # Unowned open cells are a cave: the planner never routes through them.
    detour = tp.plan(view, (0, 60, 0), {(8, 60, 0)}, set(), 70)
    assert detour is None or not (tp.cells_of_path(detour) & (tunnel - {(0, 60, 0), (0, 61, 0)}))
    # The same cells are fine once they are known to be our own tunnel.
    path = tp.plan(view, (0, 60, 0), {(8, 60, 0)}, tunnel, 70)
    assert path is not None and path[-1].to == (8, 60, 0)
    assert [m.to for m in path][:6] == [(x, 60, 0) for x in range(1, 7)]


def test_an_ore_ringed_by_a_cavern_is_skipped():
    def cavern(v):
        v[(5, 64, 0)] = IRON
        for dx, dy, dz in tp.NEIGHBOURS:
            v.pop((5 + dx, 64 + dy, 0 + dz), None)

    assert tp.plan(make_view(mutate=cavern), (0, 70, 0), {(5, 64, 0)}, set(), 70) is None


def test_gravel_over_the_dug_column_is_refused():
    def gravel(v):
        v[(1, 69, 0)] = "minecraft:gravel"

    view = make_view(mutate=gravel)
    assert tp.evaluate_move(view, (0, 70, 0), (1, 69, 0), set(), 70) is None
    assert tp.evaluate_move(view, (0, 70, 0), (0, 69, 1), set(), 70) is not None


def test_unknown_blocks_are_impassable_so_structures_are_avoided():
    view = make_view(mutate=lambda v: v.__setitem__((1, 70, 0), "minecraft:chest"))
    assert tp.evaluate_move(view, (0, 70, 0), (1, 70, 0), set(), 70) is None
    view = make_view(mutate=lambda v: v.__setitem__((1, 69, 0), "minecraft:oak_planks"))
    assert tp.evaluate_move(view, (0, 70, 0), (1, 69, 0), set(), 70) is None


def test_coarse_waypoint_staircases_toward_the_target():
    assert tp.coarse_waypoint((0, 70, 0), (10, 60, 0), steps=4) == (4, 66, 0)
    # Straight below still moves sideways, because a staircase cannot go straight down.
    assert tp.coarse_waypoint((0, 70, 0), (0, 60, 0), steps=2)[1] == 68
    assert tp.coarse_waypoint((0, 70, 0), (3, 70, 0), steps=9) == (3, 70, 0)


def test_loop_erase_keeps_the_simple_path_home():
    trail = [(0, 70, 0), (1, 70, 0), (2, 70, 0), (2, 70, 1), (2, 70, 0), (3, 70, 0)]
    assert tp.loop_erase(trail) == [(0, 70, 0), (1, 70, 0), (2, 70, 0), (3, 70, 0)]


def test_cells_on_the_map_edge_are_never_used_because_their_neighbours_are_unseen():
    view = make_view(radius=8, mutate=lambda v: v.__setitem__((7, 64, 0), IRON))
    assert not view.inside((7, 64, 0))
    assert tp.plan(view, (0, 70, 0), {(7, 64, 0)}, set(), 70) is None


def test_our_own_torches_are_walked_through_but_foreign_ones_are_not():
    torch = "minecraft:torch"
    view = make_view(mutate=lambda v: v.__setitem__((1, 70, 0), torch))
    own = {(1, 70, 0), (1, 71, 0)}
    move = tp.evaluate_move(view, (0, 70, 0), (1, 70, 0), own, 70)
    assert move is not None and (1, 70, 0) not in move.dig  # nothing to break
    assert tp.evaluate_move(view, (0, 70, 0), (1, 70, 0), set(), 70) is None


def test_ore_directly_below_is_reached_by_spiralling_to_a_side_cell():
    view = make_view(radius=8, center=(6, 61, 3), mutate=lambda v: v.__setitem__((6, 55, 3), IRON))
    goals = set(tp.approach_cells((6, 55, 3)))
    path = tp.plan(view, (6, 61, 3), goals, set(), 70)
    assert path is not None and tp.path_is_consistent(path)
    # Never digs a cell an earlier step stands on.
    stood = set()
    for move in path:
        assert not (set(move.dig) & stood)
        stood.add((move.to[0], move.to[1] - 1, move.to[2]))


def test_an_ore_that_opens_onto_lava_or_a_cave_is_not_safe_to_break():
    def lava(v):
        v[(6, 55, 3)] = IRON
        v[(7, 55, 3)] = "minecraft:lava"

    assert not tp.ore_is_safe(make_view(center=(6, 60, 3), mutate=lava), (6, 55, 3), set(), 70)

    def cave(v):
        v[(6, 55, 3)] = IRON
        v.pop((6, 55, 4), None)

    assert not tp.ore_is_safe(make_view(center=(6, 60, 3), mutate=cave), (6, 55, 3), set(), 70)
    assert tp.ore_is_safe(
        make_view(center=(6, 60, 3), mutate=cave), (6, 55, 3), {(6, 55, 4)}, 70
    )
    plain = make_view(center=(6, 60, 3), mutate=lambda v: v.__setitem__((6, 55, 3), IRON))
    assert tp.ore_is_safe(plain, (6, 55, 3), set(), 70)
