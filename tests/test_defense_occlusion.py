import math
from types import SimpleNamespace

import pytest

from baritone_client.common import combat
from baritone_client.common.defense import assess_threats


PLAYER = {
    "entity_id": 99,
    "block_position": {"x": 0, "y": 80, "z": 0},
    "health": 20,
    "food_level": 20,
    "armor_count": 4,
}
GROUNDED_PLAYER = {
    **PLAYER,
    "is_on_ground": True,
    "velocity": {"x": 0, "y": -0.0784000015258789, "z": 0},
}


def sheltered_mob(kind="skeleton", distance=18):
    return {
        "id": 42,
        "type": f"minecraft:{kind}",
        "distance": distance,
        "position": {"x": distance, "y": 80, "z": 0},
        "velocity": {"x": 0, "y": 0, "z": 0},
        "is_aggressive": False,
        "can_see_player": False,
    }


@pytest.mark.parametrize("kind,distance", [("skeleton", 18), ("zombie", 8), ("husk", 12)])
def test_occluded_calm_nonclosing_mobs_do_not_displace_sheltered_work(kind, distance):
    assert assess_threats([sheltered_mob(kind, distance)], PLAYER) == []


def vertical_cave_mob(*, mob_y=14):
    return {
        **sheltered_mob("skeleton", 10),
        "position": {"x": 8, "y": mob_y, "z": 0},
        "velocity": {"x": -0.2, "y": 0, "z": 0},
    }


def grounded_surface_player(*, on_ground=True):
    return {
        **PLAYER,
        "block_position": {"x": 0, "y": 20, "z": 0},
        "velocity": {"x": 0, "y": -0.0784, "z": 0},
        "is_on_ground": on_ground,
    }


def test_distant_calm_occluded_cave_mob_does_not_preempt_grounded_surface_work():
    # The mob is moving toward the player, but remains six blocks below.
    assert assess_threats(
        [vertical_cave_mob()], grounded_surface_player()
    ) == []


def test_vertical_suppression_requires_six_blocks_of_separation():
    mob = vertical_cave_mob(mob_y=14.01)

    assert assess_threats([mob], grounded_surface_player())


def test_vertical_suppression_does_not_expand_to_mobs_above_player():
    assert assess_threats([vertical_cave_mob(mob_y=26)], grounded_surface_player())


def test_vertical_suppression_rejects_abnormal_grounded_descent_velocity():
    player = grounded_surface_player()
    player["velocity"]["y"] = -0.2
    assert assess_threats([vertical_cave_mob()], player)


@pytest.mark.parametrize("on_ground", [False, None])
def test_vertical_suppression_requires_explicit_grounded_player(on_ground):
    assert assess_threats(
        [vertical_cave_mob()], grounded_surface_player(on_ground=on_ground)
    )


@pytest.mark.parametrize("velocity", [None, {}, {"x": 0, "y": 0, "z": float("nan")}])
def test_vertical_suppression_requires_complete_finite_player_velocity(velocity):
    player = {**grounded_surface_player(), "velocity": velocity}

    assert assess_threats([vertical_cave_mob()], player)


def test_vertical_suppression_rechecks_fresh_visibility_and_aggression():
    mob = vertical_cave_mob()
    player = grounded_surface_player()
    assert assess_threats([mob], player) == []

    mob["can_see_player"] = True
    assert assess_threats([mob], player)

    mob["can_see_player"] = False
    mob["is_aggressive"] = True
    assert assess_threats([mob], player)


def test_same_level_calm_occluded_mob_approaching_player_stays_actionable():
    mob = sheltered_mob("skeleton", 10)
    mob["velocity"]["x"] = -0.2

    assert assess_threats([mob], PLAYER)


@pytest.mark.parametrize(
    "change",
    [
        {"can_see_player": True},
        {"can_see_player": None},
        {"can_see_player": 0},
        {"is_aggressive": True},
        {"is_aggressive": None},
        {"target_id": 99},
        {"distance": 5},
        {"distance": "invalid"},
        {"distance": float("inf")},
        {"velocity": {"x": -0.2, "y": 0, "z": 0}},
        {"velocity": {}},
        {"velocity": {"x": "invalid", "y": 0, "z": 0}},
        {"position": {}},
        {"type": "minecraft:creeper", "distance": 5},
        {"type": "minecraft:creeper", "can_see_player": None},
        {"type": "minecraft:creeper", "is_aggressive": None},
        {"type": "minecraft:creeper", "angry_at_player": True},
        {"type": "minecraft:creeper", "is_attacking": True},
        {"type": "minecraft:creeper", "target_id": 101},
        {"type": "minecraft:warden"},
        {"type": "minecraft:vex"},
        {"type": "minecraft:arrow", "distance": 1},
    ],
)
def test_occlusion_exception_does_not_hide_actionable_or_uncertain_threats(change):
    mob = dict(sheltered_mob(), **change)
    assert assess_threats([mob], PLAYER)


def test_distant_stationary_occluded_calm_creeper_does_not_force_evasion():
    creeper = sheltered_mob("creeper", 9.2)

    assert assess_threats([creeper], GROUNDED_PLAYER) == []


def test_grounded_gravity_does_not_make_distant_creeper_look_closing():
    player = {
        "entity_id": 99,
        "block_position": {"x": 7, "y": 12, "z": 7},
        "velocity": {"x": 0, "y": -0.0784000015258789, "z": 0},
        "is_on_ground": True,
        "health": 20,
    }
    creeper = {
        "id": 42,
        "type": "minecraft:creeper",
        "distance": 15.556,
        "position": {"x": 0, "y": 0, "z": 0},
        "velocity": {"x": 0, "y": 0, "z": 0},
        "is_aggressive": False,
        "can_see_player": False,
    }

    assert assess_threats([creeper], player) == []


def test_occluded_creeper_still_threatens_when_moving_toward_player():
    creeper = sheltered_mob("creeper", 9.2)
    creeper["velocity"]["x"] = -0.2

    assert assess_threats([creeper], GROUNDED_PLAYER)


def test_occluded_creeper_still_threatens_when_moving_sideways():
    creeper = sheltered_mob("creeper", 9.2)
    creeper["velocity"]["z"] = 0.1

    assert assess_threats([creeper], GROUNDED_PLAYER)


def test_airborne_player_descent_keeps_creeper_actionable():
    player = {
        **GROUNDED_PLAYER,
        "is_on_ground": False,
    }
    creeper = {
        **sheltered_mob("creeper", 15.448),
        "position": {"x": 0, "y": 64.552, "z": 0},
    }

    assert assess_threats([creeper], player)


def test_grounded_player_horizontal_approach_does_not_make_stationary_creeper_closing():
    player = {
        **GROUNDED_PLAYER,
        "velocity": {"x": 0.2, "y": -0.0784000015258789, "z": 0},
    }
    creeper = sheltered_mob("creeper", 8)

    assert assess_threats([creeper], player) == []


def test_stationary_creeper_reactivates_when_fresh_visibility_or_aggression_changes():
    player = {
        **GROUNDED_PLAYER,
        "velocity": {"x": 0.2, "y": -0.0784000015258789, "z": 0},
    }
    creeper = sheltered_mob("creeper", 8)
    assert assess_threats([creeper], player) == []

    creeper["can_see_player"] = True
    assert assess_threats([creeper], player)

    creeper["can_see_player"] = False
    creeper["is_aggressive"] = True
    assert assess_threats([creeper], player)


def test_player_motion_does_not_hide_creeper_at_contact_boundary():
    player = {
        **GROUNDED_PLAYER,
        "velocity": {"x": 0.2, "y": -0.0784000015258789, "z": 0},
    }
    creeper = sheltered_mob("creeper", 6)

    assert assess_threats([creeper], player)


@pytest.mark.parametrize(
    "velocity",
    [
        None,
        {},
        {"x": 0, "y": float("nan"), "z": 0},
        {"x": 0, "y": -0.2, "z": 0},
        {"x": 0, "y": 5, "z": 0},
    ],
)
def test_unknown_or_nonfinite_grounded_player_velocity_keeps_creeper_actionable(velocity):
    player = {**GROUNDED_PLAYER, "velocity": velocity}
    creeper = sheltered_mob("creeper", 9.2)

    assert assess_threats([creeper], player)


@pytest.mark.parametrize("field", ["can_see_player", "is_aggressive", "velocity", "position"])
def test_missing_shelter_evidence_keeps_the_existing_defense_policy(field):
    mob = sheltered_mob()
    del mob[field]
    assert assess_threats([mob], PLAYER)


def test_missing_player_geometry_keeps_the_existing_defense_policy():
    assert assess_threats([sheltered_mob()], {})


def test_defense_keeps_working_inside_shelter_without_mutating_or_equipping():
    class Transport:
        def dispatch(self, route, payload):
            if route == "get_combat_snapshot":
                return {"player": PLAYER, "entities": [sheltered_mob()], "skipped_count": 0}
            if route == "get_block":
                return {"id": "minecraft:air"}
            raise AssertionError(f"Sheltered defense must not mutate: {route}")

    client = SimpleNamespace(transport=Transport())
    assert combat.defend_or_flee(client) is False
    assert client._mcbaratone_defense_runtime.mode.value == "clear"
    client._mcbaratone_defense_runtime.entered_at -= 20
    assert combat.defend_or_flee(client) is False


def test_fresh_visibility_observation_reactivates_defense():
    mob = sheltered_mob()
    assert assess_threats([mob], PLAYER) == []
    mob["can_see_player"] = True
    assert assess_threats([mob], PLAYER)[0].entity["id"] == 42



def buried_skeleton(**velocity):
    """Live A1 2026-10-16: sealed in a pocket four blocks below a standing bot."""
    return {
        **sheltered_mob("skeleton", 5.1),
        "position": {"x": 2, "y": 16, "z": 4},
        "velocity": {"x": 0, "y": 0, "z": 0, **velocity},
    }


def test_a_buried_motionless_skeleton_is_not_closing_on_a_standing_bot():
    """The grounded phantom fall (-0.0784) read as ~0.06 closing."""
    assert assess_threats([buried_skeleton()], grounded_surface_player()) == []


def test_a_buried_skeleton_that_really_moves_closer_still_counts():
    assert assess_threats([buried_skeleton(x=-0.2, z=-0.3)], grounded_surface_player())


@pytest.mark.parametrize("on_ground", [False, None])
def test_the_phantom_fall_is_only_ignored_for_a_grounded_bot(on_ground):
    assert assess_threats([buried_skeleton()], grounded_surface_player(on_ground=on_ground))


# -- a sealed pocket on the tunnel ceiling (live A1 2026-11-03) ----------------
TUNNEL_PLAYER = {
    **PLAYER,
    "position": {"x": -437.7, "y": 66.0, "z": 6.6},
    "block_position": {"x": -438, "y": 66, "z": 6},
    "is_on_ground": True,
    "velocity": {"x": 0, "y": -0.0784000015258789, "z": 0},
}


def _at(x, y, z):
    """A mob position with the distance the bridge would report from it."""
    here = TUNNEL_PLAYER["position"]
    return {"position": {"x": x, "y": y, "z": z},
            "distance": math.dist((x, y, z), (here["x"], here["y"], here["z"]))}


def ceiling_pocket():
    def mob(kind, x, y, z, vy=0.0):
        return {"id": hash((kind, x)) % 1000, "type": f"minecraft:{kind}", **_at(x, y, z),
                "velocity": {"x": 0.0, "y": vy, "z": 0.0},
                "can_see_player": False, "is_aggressive": False, "target_id": None}

    # Positions as observed live; feet at y=69-72 over a tunnel at y=66.
    return [
        mob("zombie", -437.44, 72.0, 9.56),
        mob("creeper", -436.30, 71.0, 9.70),
        mob("creeper", -439.01, 69.0, 8.86, vy=-0.0297),
        mob("skeleton", -438.41, 69.0, 9.21, vy=-0.0322),
    ]


def test_blind_calm_mobs_sealed_over_a_tunnel_ceiling_are_not_threats():
    """A1 sat 5+ hours evading these from a 1-wide tunnel it could not leave."""
    assert sum(mob["distance"] < 5.0 for mob in ceiling_pocket()) == 2  # inside the old action range
    assert assess_threats(ceiling_pocket(), TUNNEL_PLAYER) == []


@pytest.mark.parametrize("change", [
    {"can_see_player": True},
    {"is_aggressive": True},
    {"target_id": 99},
    _at(-437.7, 69.0, 6.6),                              # 3.0m: fuse / melee range
    _at(-439.01, 68.0, 8.86),                            # no layer over the head
    {"distance": 3.5},                                   # disagrees with the positions
    {"velocity": {"x": 0.0, "y": 0.0, "z": -0.25}},      # coming toward the player
])
@pytest.mark.parametrize("kind", ["creeper", "skeleton"])
def test_a_ceiling_mob_stays_a_threat_once_any_sealing_evidence_is_missing(kind, change):
    mob = next(m for m in ceiling_pocket() if m["type"] == f"minecraft:{kind}" and m["position"]["y"] == 69.0)
    assert assess_threats([dict(mob, **change)], TUNNEL_PLAYER)


@pytest.mark.parametrize("player_change", [
    {"velocity": None},
    {"velocity": {"x": 0, "y": float("nan"), "z": 0}},
    {"position": {"x": -437.7, "y": 66.5, "z": 6.6}},  # no longer 3 below the creeper
])
@pytest.mark.parametrize("index", [2, 3])  # the creeper and the skeleton
def test_a_ceiling_mob_stays_a_threat_on_unknown_or_closer_player_state(player_change, index):
    assert assess_threats([ceiling_pocket()[index]], {**TUNNEL_PLAYER, **player_change})


def test_stepping_up_the_tunnel_staircase_keeps_sealed_mobs_quiet():
    """Live frame: airborne mid-step at +0.08 Y; evading here cancels the walk home."""
    player = {**TUNNEL_PLAYER, "is_on_ground": False,
              "velocity": {"x": -0.011, "y": 0.083, "z": 0.034}}
    assert assess_threats(ceiling_pocket(), player) == []


def test_mobs_hopping_and_wandering_in_a_sealed_pocket_stay_quiet():
    """Live frames: jumps (+0.33 / -0.52 Y), grounded gravity, 0.06 sideways strolls."""
    pocket = ceiling_pocket()
    pocket[0]["velocity"] = {"x": 0.02, "y": -0.5169, "z": 0.0}
    pocket[1]["velocity"] = {"x": 0.03, "y": -0.0784, "z": 0.05}
    pocket[2]["velocity"] = {"x": 0.0, "y": 0.3332, "z": 0.012}
    pocket[3]["velocity"] = {"x": 0.0, "y": -0.0784, "z": 0.041}
    assert assess_threats(pocket, TUNNEL_PLAYER) == []


@pytest.mark.parametrize("vy,threat", [(-0.0784, False), (0.0, False), (-0.2, True)])
def test_a_grounded_creepers_own_gravity_is_not_motion(vy, threat):
    """Mirror of #88: a still creeper reports -0.0784 Y while standing."""
    creeper = sheltered_mob("creeper", 9.2)
    creeper["velocity"]["y"] = vy
    assert bool(assess_threats([creeper], GROUNDED_PLAYER)) is threat
