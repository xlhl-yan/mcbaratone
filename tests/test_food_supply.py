from types import SimpleNamespace
import pytest

from baritone_client.common import food_supply


def _state(data):
    return SimpleNamespace(custom_data=data)


def _inventory(monkeypatch, counts):
    monkeypatch.setattr(food_supply, "get_inventory", lambda _client: dict(counts))
    monkeypatch.setattr(food_supply, "_bread_output_capacity", lambda _client: 128)
    monkeypatch.setattr(food_supply, "_survival_ready", lambda _client: True)
    monkeypatch.setattr(
        food_supply,
        "find_farm_surface_near",
        lambda _client, x, y, z: (x, y, z),
    )


def test_legacy_crop_farm_locations_join_the_worker_rotation():
    state = _state(
        {
            "farm_location": [-426, 79, -20],
            "structures": {
                "food_source": {
                    "type": "starter_crop_farm",
                    "location": [-426, 79, -20],
                    "plots": [[-429, 80, -23, "minecraft:wheat"]],
                }
            },
        }
    )

    assert food_supply._plots(state) == [(-426, 79, -20)]


def test_retired_legacy_crop_farm_is_not_imported_as_a_worker_plot():
    state = _state(
        {
            "farm_location": [-426, 79, -20],
            "structures": {
                "food_source": {
                    "type": "starter_crop_farm",
                    "location": [-426, 79, -20],
                    "verified": False,
                    "failed_visits": 2,
                }
            },
        }
    )

    assert food_supply._plots(state) == []


def test_retired_crop_is_pruned_from_persisted_worker_rotation():
    state = _state(
        {
            "food_worker": {"farm_plots": [{"origin": [-426, 79, -20]}]},
            "structures": {
                "food_source": {
                    "type": "starter_crop_farm",
                    "location": [-426, 79, -20],
                    "verified": False,
                }
            },
        }
    )

    assert food_supply._plots(state) == []


def test_after_existing_plots_are_immature_expand_another_plot(monkeypatch):
    counts = {"minecraft:wheat": 0, "minecraft:wheat_seeds": 12}
    _inventory(monkeypatch, counts)
    monkeypatch.setattr(food_supply, "eat_until_hunger", lambda *_a, **_k: True)
    monkeypatch.setattr(food_supply, "harvest_wheat_farm", lambda *_a, **_k: False)
    created = []
    def establish(_client, x, y, z, **_kwargs):
        created.append((x, y, z)); return (x, y, z)
    monkeypatch.setattr(food_supply, "establish_wheat_farm", establish)
    monkeypatch.setattr(food_supply, "resolve_storage_location", lambda *_a, **_k: None)
    result = food_supply.run_food_cycle(object(), _state({"wheat_farm": {"origin": [0, 64, 0]}}))
    assert result.success and result.plots == 1 and result.crops_replanted == 1
    assert created == [(32, 64, 0)]


def test_verified_growing_sheltered_farm_waits_without_stock_or_expansion(monkeypatch):
    counts = {"minecraft:wheat": 2, "minecraft:wheat_seeds": 12}
    _inventory(monkeypatch, counts)
    monkeypatch.setattr(food_supply, "eat_until_hunger", lambda *_a, **_k: True)
    monkeypatch.setattr(food_supply, "harvest_wheat_farm", lambda *_a, **_k: False)
    monkeypatch.setattr(food_supply, "local_farm_wait_reason", lambda *_a: "growing wheat")
    monkeypatch.setattr(food_supply, "_stock_seeds", lambda *_a: pytest.fail("stay at crop source"))
    monkeypatch.setattr(food_supply, "establish_wheat_farm", lambda *_a, **_k: pytest.fail("no expansion"))
    monkeypatch.setattr(food_supply, "resolve_storage_location", lambda *_a, **_k: None)
    client = SimpleNamespace(transport=SimpleNamespace(dispatch=lambda *_a: {}))
    state = _state({"wheat_farm": {"origin": [0, 64, 0]}})
    result = food_supply.run_food_cycle(client, state)
    assert not result.success and result.plots == 0 and result.wheat_harvested == 0
    assert state.custom_data["food_worker"]["cycles"] == 0


def test_no_permanent_terminal_plot_cap(monkeypatch):
    counts = {"minecraft:wheat": 0, "minecraft:wheat_seeds": 64}
    _inventory(monkeypatch, counts)
    monkeypatch.setattr(food_supply, "eat_until_hunger", lambda *_a, **_k: True)
    monkeypatch.setattr(food_supply, "harvest_wheat_farm", lambda *_a, **_k: False)
    monkeypatch.setattr(food_supply, "establish_wheat_farm", lambda _c, x, y, z, **_k: (x, y, z))
    monkeypatch.setattr(food_supply, "resolve_storage_location", lambda *_a, **_k: None)
    state = _state(
        {
            "wheat_farm": {"origin": [0, 64, 0]},
            "food_worker": {
                "farm_plots": [
                    {"origin": [index * 40, 64, 0]} for index in range(20)
                ]
            },
        }
    )
    result = food_supply.run_food_cycle(object(), state)
    assert result.plots == 1
    assert len(state.custom_data["food_worker"]["farm_plots"]) == 21


def test_harvest_and_bank_are_credited_only_by_inventory_delta(monkeypatch):
    counts = {"minecraft:wheat": 1, "minecraft:bread": 10}
    def inventory(_client): return dict(counts)
    monkeypatch.setattr(food_supply, "get_inventory", inventory)
    monkeypatch.setattr(food_supply, "_survival_ready", lambda _client: True)
    monkeypatch.setattr(food_supply, "eat_until_hunger", lambda *_a, **_k: True)
    def harvest(*_a, **_k): counts["minecraft:wheat"] += 4; return True
    monkeypatch.setattr(food_supply, "harvest_wheat_farm", harvest)
    monkeypatch.setattr(food_supply, "craft", lambda *_a, **_k: False)
    monkeypatch.setattr(food_supply, "resolve_storage_location", lambda *_a, **_k: (1, 64, 1))
    def deposit(*_a, **_k): counts["minecraft:bread"] -= 2; return 1
    monkeypatch.setattr(food_supply, "deposit_excess_to_chest", deposit)
    result = food_supply.run_food_cycle(object(), _state({"wheat_farm": {"origin": [0, 64, 0]}}))
    assert (result.wheat_harvested, result.prepared_food_banked) == (4, 2)
    assert result.total_food_banked == 2


def test_direct_crop_harvest_is_progress_but_not_wheat_or_bread(monkeypatch):
    counts = {"minecraft:carrot": 1, "minecraft:wheat": 0}
    monkeypatch.setattr(food_supply, "get_inventory", lambda _client: dict(counts))
    monkeypatch.setattr(food_supply, "_survival_ready", lambda _client: True)
    monkeypatch.setattr(food_supply, "eat_until_hunger", lambda *_a, **_k: True)
    monkeypatch.setattr(
        food_supply, "harvest_wheat_farm",
        lambda *_a, **_k: counts.__setitem__("minecraft:carrot", 4) or True,
    )
    monkeypatch.setattr(food_supply, "_stock_seeds", lambda *_a, **_k: 0)
    monkeypatch.setattr(food_supply, "establish_wheat_farm", lambda *_a, **_k: None)
    monkeypatch.setattr(food_supply, "resolve_storage_location", lambda *_a, **_k: None)

    result = food_supply.run_food_cycle(
        object(), _state({"wheat_farm": {"origin": [0, 64, 0]}})
    )

    assert result.success
    assert result.other_edible_crops_harvested == 3
    assert result.total_other_edible_crops_harvested == 3
    assert result.wheat_harvested == 0
    assert result.bread_crafted == 0


def test_known_plot_inspection_is_round_robin_and_bounded(monkeypatch):
    _inventory(monkeypatch, {})
    monkeypatch.setattr(food_supply, "eat_until_hunger", lambda *_a, **_k: True)
    seen = []
    monkeypatch.setattr(
        food_supply,
        "harvest_wheat_farm",
        lambda _client, x, y, z, **_kwargs: seen.append((x, y, z)),
    )
    monkeypatch.setattr(food_supply, "establish_wheat_farm", lambda *_a, **_k: None)
    monkeypatch.setattr(food_supply, "resolve_storage_location", lambda *_a, **_k: None)
    plots = [{"origin": [index * 40, 64, 0]} for index in range(5)]
    state = _state({"food_worker": {"farm_plots": plots}})
    food_supply.run_food_cycle(object(), state)
    food_supply.run_food_cycle(object(), state)
    assert seen == [
        (0, 64, 0),
        (40, 64, 0),
        (80, 64, 0),
        (120, 64, 0),
        (160, 64, 0),
        (0, 64, 0),
    ]


def test_survival_recovery_failure_holds_before_farm_travel(monkeypatch):
    _inventory(monkeypatch, {})
    monkeypatch.setattr(food_supply, "eat_until_hunger", lambda *_a, **_k: False)
    monkeypatch.setattr(
        food_supply,
        "harvest_wheat_farm",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not travel")),
    )
    state = _state({"wheat_farm": {"origin": [0, 64, 0]}})
    result = food_supply.run_food_cycle(object(), state)
    assert not result.success
    assert state.custom_data["food_worker"]["attempts"] == 1
    assert "survival recovery" in result.detail


def test_failed_expansion_site_is_retired_before_next_attempt(monkeypatch):
    _inventory(monkeypatch, {"minecraft:wheat_seeds": 20})
    monkeypatch.setattr(food_supply, "eat_until_hunger", lambda *_a, **_k: True)
    monkeypatch.setattr(food_supply, "harvest_wheat_farm", lambda *_a, **_k: False)
    attempted = []
    def fail(_client, x, y, z, **_kwargs):
        attempted.append((x, y, z)); return None
    monkeypatch.setattr(food_supply, "establish_wheat_farm", fail)
    monkeypatch.setattr(food_supply, "resolve_storage_location", lambda *_a, **_k: None)
    state = _state({"wheat_farm": {"origin": [0, 64, 0]}})
    food_supply.run_food_cycle(object(), state)
    food_supply.run_food_cycle(object(), state)
    assert attempted == [(32, 64, 0), (0, 64, 32)]
    assert state.custom_data["food_worker"]["failed_plot_sites"] == [
        [32, 64, 0],
        [0, 64, 32],
    ]


def test_retired_legacy_plot_is_not_resurrected_next_cycle():
    state = _state(
        {
            "base_location": [500, 70, 500],
            "wheat_farm": {"origin": [0, 64, 0]},
            "food_worker": {"farm_plots": [{"origin": [0, 64, 0]}]},
        }
    )

    retired = food_supply._retire_distant_plots(
        state,
        state.custom_data["food_worker"],
        (500, 70, 500),
    )

    assert retired == [[0, 64, 0]]
    assert "wheat_farm" not in state.custom_data
    assert food_supply._plots(state) == []


def test_distant_plot_is_retired_even_when_worker_reached_it(monkeypatch):
    _inventory(monkeypatch, {"minecraft:wheat_seeds": 10})
    monkeypatch.setattr(food_supply, "eat_until_hunger", lambda *_a, **_k: True)
    monkeypatch.setattr(
        food_supply,
        "harvest_wheat_farm",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("distant plot must retire before harvest")
        ),
    )
    returned = []
    monkeypatch.setattr(
        food_supply,
        "_return_to_anchor",
        lambda _client, anchor: returned.append(anchor) or True,
    )
    state = _state(
        {
            "base_location": [500, 70, 500],
            "wheat_farm": {"origin": [0, 64, 0]},
            "food_worker": {"farm_plots": [{"origin": [0, 64, 0]}]},
        }
    )

    result = food_supply.run_food_cycle(object(), state)

    assert not result.success
    assert "retired 1 farm plot" in result.detail
    assert "returned to base" in result.detail
    assert returned == [(500, 70, 500)]
    assert "wheat_farm" not in state.custom_data


def test_frontier_keeps_expanding_past_four_verified_plots(monkeypatch):
    _inventory(monkeypatch, {"minecraft:wheat_seeds": 64})
    monkeypatch.setattr(food_supply, "eat_until_hunger", lambda *_a, **_k: True)
    monkeypatch.setattr(food_supply, "harvest_wheat_farm", lambda *_a, **_k: False)
    monkeypatch.setattr(
        food_supply,
        "establish_wheat_farm",
        lambda _client, x, y, z, **_kwargs: (x, y, z),
    )
    monkeypatch.setattr(food_supply, "resolve_storage_location", lambda *_a, **_k: None)
    state = _state({"wheat_farm": {"origin": [0, 64, 0]}})
    results = [food_supply.run_food_cycle(object(), state) for _ in range(6)]
    assert all(result.plots == 1 for result in results)
    assert len(state.custom_data["food_worker"]["farm_plots"]) == 7


def test_expansion_moves_beyond_four_rejected_cardinal_sites(monkeypatch):
    _inventory(monkeypatch, {"minecraft:wheat_seeds": 64})
    monkeypatch.setattr(food_supply, "eat_until_hunger", lambda *_a, **_k: True)
    monkeypatch.setattr(food_supply, "harvest_wheat_farm", lambda *_a, **_k: False)
    attempted = []

    def reject(_client, x, y, z, **_kwargs):
        attempted.append((x, y, z))
        return None

    monkeypatch.setattr(food_supply, "establish_wheat_farm", reject)
    monkeypatch.setattr(
        food_supply, "resolve_storage_location", lambda *_a, **_k: None
    )
    state = _state({"wheat_farm": {"origin": [0, 64, 0]}})

    for _ in range(7):
        food_supply.run_food_cycle(object(), state)

    # Intent: rejected cardinals must not stall siting forever. The original
    # assertion also required moving beyond 32 blocks, which is the behaviour
    # that let plots march outward until the worker could no longer reach them
    # (Bot18 ended 461 blocks from its own farm). Expansion must still explore
    # many distinct sites, but all of them within one short walk of base.
    from math import hypot
    assert len(set(attempted)) > 4
    assert all(
        hypot(x, z) <= food_supply.MAX_ANCHOR_RADIUS + 1
        for x, _y, z in attempted
    ), attempted


def test_exhausted_empty_frontier_is_reopened_once_after_setup_repair():
    anchor = (546, 79, -304)
    rejected = [
        [578, 79, -304],
        [546, 79, -272],
        [514, 79, -304],
        [546, 79, -336],
    ]
    worker = {
        "failed_plot_sites": rejected,
        "expansion_cursor": 114,
    }

    candidate = food_supply._next_plot_candidate(
        anchor, [], worker, 32, 3
    )

    assert candidate == (578, 79, -304)
    assert worker["failed_plot_sites"] == []
    assert worker["failed_site_reset_anchor"] == list(anchor)


def test_exhausted_frontier_reopens_with_an_existing_unproductive_plot():
    anchor = (100, 70, 100)
    rejected = [[132, 70, 100], [100, 70, 132], [68, 70, 100], [100, 70, 68]]
    worker = {
        "failed_plot_sites": rejected, "expansion_cursor": 10000,
        "failed_site_reset_anchor": list(anchor),
        "exhausted_frontier_streak": food_supply.EXHAUSTED_FRONTIER_RETRY_STREAK - 1,
    }
    assert food_supply._next_plot_candidate(anchor, [anchor], worker, 32, 3) == (132, 70, 100)
    assert worker["failed_plot_sites"] == []


def test_last_three_wheat_are_food_not_a_permanent_uncraftable_reserve(monkeypatch):
    counts = {food_supply.WHEAT: 3, food_supply.SEEDS: 8}
    _inventory(monkeypatch, counts)
    monkeypatch.setattr(food_supply, "eat_until_hunger", lambda *_a, **_k: True)
    monkeypatch.setattr(food_supply, "_stock_seeds", lambda *_a: 0)
    monkeypatch.setattr(food_supply, "resolve_storage_location", lambda *_a, **_k: None)

    def craft(_client, item, amount):
        assert item == food_supply.BREAD and amount == 1
        counts[food_supply.WHEAT] = 0
        counts[food_supply.BREAD] = 1
        return True

    monkeypatch.setattr(food_supply, "craft", craft)
    monkeypatch.setattr(
        food_supply,
        "craft_bread_at_saved_home",
        lambda _client, _state, _anchor, amount, **kwargs:
            kwargs["craft"](object(), food_supply.BREAD, amount),
    )
    result = food_supply.run_food_cycle(object(), _state({}))
    assert result.success and result.bread_crafted == 1


def test_failed_home_workstation_does_not_erase_harvest_credit(monkeypatch):
    counts = {food_supply.WHEAT: 0, food_supply.BREAD: 0}
    _inventory(monkeypatch, counts)
    monkeypatch.setattr(food_supply, "eat_until_hunger", lambda *_a, **_k: True)
    monkeypatch.setattr(
        food_supply, "harvest_wheat_farm",
        lambda *_a, **_k: counts.__setitem__(food_supply.WHEAT, 7) or True,
    )
    monkeypatch.setattr(food_supply, "resolve_storage_location", lambda *_a, **_k: None)
    home_anchor = (10, 65, 10)
    state = _state({
        "base_location": list(home_anchor),
        "food_worker": {"farm_plots": [{"origin": list(home_anchor)}]},
    })
    client = SimpleNamespace(transport=SimpleNamespace(dispatch=lambda *_a: {
        "block_position": {"x": 10, "y": 65, "z": 10},
    }))
    attempted = []

    def refused(_client, _state, anchor, count, **_kwargs):
        attempted.append((anchor, count))
        return False  # unknown/unreachable home workstation

    monkeypatch.setattr(food_supply, "craft_bread_at_saved_home", refused)
    result = food_supply.run_food_cycle(client, state)

    assert attempted == [(home_anchor, 2)]
    assert result.wheat_harvested == 7 and result.bread_crafted == 0
    assert counts[food_supply.WHEAT] == 7 and counts[food_supply.BREAD] == 0


def test_food_cycle_does_not_leave_house_through_unverified_closed_door(monkeypatch):
    _inventory(monkeypatch, {"minecraft:wheat": 0, "minecraft:wheat_seeds": 8})
    monkeypatch.setattr(food_supply, "eat_until_hunger", lambda *_a, **_k: True)
    monkeypatch.setattr(
        food_supply, "prepare_house_door_for_departure", lambda *_a: False
    )
    monkeypatch.setattr(
        food_supply, "harvest_wheat_farm",
        lambda *_a, **_k: pytest.fail("farm travel must wait for a verified doorway"),
    )
    state = _state({
        "base_location": [10, 65, 10],
        "structures": {"starter_house": {"origin": [9, 65, 9]}},
    })
    client = SimpleNamespace(transport=SimpleNamespace(dispatch=lambda *_a: {}))

    result = food_supply.run_food_cycle(client, state)

    assert not result.success
    assert "door" in result.detail


def test_farm_expansion_rejects_cave_soil_and_returns_home(monkeypatch):
    monkeypatch.setattr(food_supply, "_approach_candidate", lambda *_a: None)
    monkeypatch.setattr(food_supply, "find_farm_surface_near", lambda *_a: (32, 50, 0))
    returned = []
    monkeypatch.setattr(food_supply, "_return_to_anchor", lambda _c, anchor: returned.append(anchor) or True)
    monkeypatch.setattr(food_supply, "establish_wheat_farm", lambda *_a, **_k: pytest.fail("no cave farm"))
    assert food_supply._establish_candidate(object(), _state({"base_location": [0, 70, 0]}), (32, 70, 0), 5) is None
    assert returned == [(0, 70, 0)]


def test_exhausted_frontier_reopens_again_after_the_one_shot_reset_is_spent():
    """A second exhaustion at the same anchor must not deadlock forever.

    Live A1: anchor (-413,78,-15) spent its one allotted reset weeks into a
    run, then _next_plot_candidate returned None on every one of 4000+ later
    calls over the following week -- the same transient setup failure (no
    bucket, no iron) can recur, and the one-shot reset had nothing left to
    give. This drives the same anchor through a second full exhaustion and
    asserts the frontier reopens again instead of returning None forever.
    """
    anchor = (546, 79, -304)
    rejected = [
        [578, 79, -304],
        [546, 79, -272],
        [514, 79, -304],
        [546, 79, -336],
    ]
    worker = {
        "failed_plot_sites": list(rejected),
        "expansion_cursor": 114,
        # This anchor already spent its one-shot reset.
        "failed_site_reset_anchor": list(anchor),
    }

    seen = []
    for _ in range(food_supply.EXHAUSTED_FRONTIER_RETRY_STREAK):
        candidate = food_supply._next_plot_candidate(anchor, [], worker, 32, 3)
        seen.append(candidate)
        if candidate is not None:
            # A real candidate must count against the blacklist next time,
            # same as the live worker does when establishment then fails.
            worker["failed_plot_sites"].append(list(candidate))

    assert seen[:-1] == [None] * (food_supply.EXHAUSTED_FRONTIER_RETRY_STREAK - 1)
    assert seen[-1] == (578, 79, -304)
    # The reset call itself cleared the blacklist; only this test's own
    # post-loop bookkeeping (mirroring the live worker re-rejecting a site)
    # put the just-returned candidate back.
    assert worker["failed_plot_sites"] == [[578, 79, -304]]
    assert worker["exhausted_frontier_streak"] == 0


def test_first_checkpointed_plot_prefers_observed_natural_soil(monkeypatch):
    state = _state({"food_worker": {"farm_plots": []}})
    state.checkpoint_dir = "checkpoint"
    monkeypatch.setattr(
        food_supply,
        "find_natural_crop_center",
        lambda *_args: ((559, 74, -284), False),
    )
    established = []
    monkeypatch.setattr(
        food_supply,
        "establish_wheat_farm",
        lambda _client, *position, **_kwargs: established.append(position)
        or position,
    )

    result = food_supply._establish_candidate(
        object(), state, (546, 79, -272), 5
    )

    assert result == (559, 74, -284)
    assert established == [(559, 74, -284)]


def test_first_checkpointed_plot_reuses_existing_irrigation(monkeypatch):
    state = _state({"food_worker": {"farm_plots": []}})
    state.checkpoint_dir = "checkpoint"
    client = SimpleNamespace(transport=SimpleNamespace(dispatch=lambda *_a, **_k: {}))
    monkeypatch.setattr(
        food_supply, "find_nearby_block", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        food_supply,
        "find_water_source",
        lambda *_args, **_kwargs: (555, 72, -264),
    )
    monkeypatch.setattr(
        food_supply,
        "_block_id",
        lambda _client, x, y, z: (
            "minecraft:grass_block"
            if y == 71 and 553 <= x <= 557 and -266 <= z <= -262
            else "minecraft:air"
        ),
    )
    established = []
    monkeypatch.setattr(
        food_supply,
        "establish_wheat_farm",
        lambda _client, *position, **_kwargs: established.append(position)
        or position,
    )

    assert food_supply._establish_candidate(
        client, state, (546, 79, -272), 5
    ) == (555, 71, -264)
    assert established == [(555, 71, -264)]


def test_first_checkpointed_plot_adopts_existing_wheat(monkeypatch):
    state = _state({"food_worker": {"farm_plots": []}})
    state.checkpoint_dir = "checkpoint"
    client = SimpleNamespace(transport=SimpleNamespace(dispatch=lambda *_a, **_k: {}))
    monkeypatch.setattr(
        food_supply,
        "find_nearby_block",
        lambda _client, blocks, **_kwargs: (553, 75, -266)
        if blocks == ["minecraft:wheat"]
        else None,
    )
    monkeypatch.setattr(
        food_supply,
        "_block_id",
        lambda _client, x, y, z: "minecraft:farmland"
        if (x, y, z) == (553, 74, -266)
        else "minecraft:air",
    )
    monkeypatch.setattr(
        food_supply,
        "establish_wheat_farm",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not construct over an existing crop")
        ),
    )

    assert food_supply._establish_candidate(
        client, state, (546, 79, -272), 5
    ) == (553, 74, -266)


def test_first_plot_probes_current_column_inside_base_radius(monkeypatch):
    state = _state(
        {"base_location": [546, 79, -304], "food_worker": {"farm_plots": []}}
    )
    state.checkpoint_dir = "checkpoint"
    client = SimpleNamespace(
        transport=SimpleNamespace(
            dispatch=lambda *_args, **_kwargs: {
                "block_position": {"x": 559, "y": 75, "z": -284}
            }
        )
    )
    monkeypatch.setattr(
        food_supply,
        "find_farm_surface_near",
        lambda _client, x, y, z, **_kwargs: (x, 73, z),
    )
    monkeypatch.setattr(
        food_supply,
        "find_natural_crop_center",
        lambda *_args: (None, False),
    )
    monkeypatch.setattr(
        food_supply,
        "establish_wheat_farm",
        lambda _client, *position, **_kwargs: position,
    )

    assert food_supply._establish_candidate(
        client, state, None, 5
    ) == (559, 73, -284)


def test_dead_state_fails_closed_before_self_feed_or_farm_travel(monkeypatch):
    _inventory(monkeypatch, {})
    monkeypatch.setattr(food_supply, "_survival_ready", lambda _client: False)
    monkeypatch.setattr(
        food_supply,
        "eat_until_hunger",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not eat")),
    )
    result = food_supply.run_food_cycle(object(), _state({}))
    assert not result.success
    assert "unsafe" in result.detail


def test_missing_live_state_fails_closed():
    client = SimpleNamespace(
        transport=SimpleNamespace(dispatch=lambda *_args, **_kwargs: {})
    )

    assert not food_supply._survival_ready(client)


def _positioned_client(position):
    def dispatch(route, _params):
        assert route == "get_state"
        return {"block_position": dict(position)}

    return SimpleNamespace(transport=SimpleNamespace(dispatch=dispatch))


def test_far_worker_walks_home_instead_of_blacklisting_unseen_sites(monkeypatch):
    # Every site was judged from hundreds of blocks away, read as empty
    # ground, and was blacklisted.
    _inventory(monkeypatch, {"minecraft:wheat_seeds": 9})
    monkeypatch.setattr(food_supply, "eat_until_hunger", lambda *_a, **_k: True)
    monkeypatch.setattr(food_supply, "_stock_seeds", lambda *_a, **_k: 0)
    monkeypatch.setattr(
        food_supply,
        "_establish_candidate",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("must not site a plot from far outside the base")
        ),
    )
    returned = []
    monkeypatch.setattr(
        food_supply,
        "_return_to_anchor",
        lambda _client, anchor: returned.append(anchor) or False,
    )
    state = _state({"base_location": [200, 78, -40], "food_worker": {}})

    result = food_supply.run_food_cycle(
        _positioned_client({"x": 600, "y": 120, "z": 30}), state
    )

    assert not result.success
    assert "needs the base" in result.detail
    assert returned == [(200, 78, -40)]
    worker = state.custom_data["food_worker"]
    assert worker["failed_plot_sites"] == []
    assert int(worker.get("expansion_cursor", 0) or 0) == 0


def test_worker_near_base_still_sites_a_plot(monkeypatch):
    _inventory(monkeypatch, {"minecraft:wheat_seeds": 9})
    monkeypatch.setattr(food_supply, "eat_until_hunger", lambda *_a, **_k: True)
    monkeypatch.setattr(food_supply, "_stock_seeds", lambda *_a, **_k: 0)
    monkeypatch.setattr(food_supply, "_approach_candidate", lambda *_a: None)
    monkeypatch.setattr(
        food_supply,
        "_return_to_anchor",
        lambda *_a: (_ for _ in ()).throw(AssertionError("already home")),
    )
    monkeypatch.setattr(
        food_supply,
        "establish_wheat_farm",
        lambda _client, x, y, z, **_kwargs: (x, y, z),
    )
    monkeypatch.setattr(food_supply, "resolve_storage_location", lambda *_a, **_k: None)
    state = _state({"base_location": [200, 78, -40], "food_worker": {}})

    result = food_supply.run_food_cycle(
        _positioned_client({"x": 193, "y": 78, "z": -35}), state
    )

    assert result.success and result.plots == 1


def test_candidate_is_approached_before_its_terrain_is_resolved(monkeypatch):
    events = []
    client = _positioned_client({"x": 200, "y": 78, "z": -40})
    monkeypatch.setattr(
        "baritone_client.common.navigation.goto_xz",
        lambda _client, x, z, **_kwargs: events.append(("goto", x, z)) or True,
    )

    def surface(_client, x, y, z):
        events.append(("surface", x, z))
        return (x, y, z)

    monkeypatch.setattr(food_supply, "find_farm_surface_near", surface)
    monkeypatch.setattr(
        food_supply,
        "establish_wheat_farm",
        lambda _client, *position, **_kwargs: position,
    )

    placed = food_supply._establish_candidate(
        client, _state({"food_worker": {"farm_plots": []}}), (232, 78, -40), 5
    )

    assert placed == (232, 78, -40)
    assert events == [("goto", 232, -40), ("surface", 232, -40)]


def test_candidate_in_view_is_resolved_without_travel(monkeypatch):
    client = _positioned_client({"x": 230, "y": 78, "z": -39})
    monkeypatch.setattr(
        "baritone_client.common.navigation.goto_xz",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no travel")),
    )
    monkeypatch.setattr(
        food_supply, "find_farm_surface_near", lambda _c, x, y, z: (x, y, z)
    )
    monkeypatch.setattr(
        food_supply,
        "establish_wheat_farm",
        lambda _client, *position, **_kwargs: position,
    )

    assert food_supply._establish_candidate(
        client, _state({"food_worker": {"farm_plots": []}}), (232, 78, -40), 5
    ) == (232, 78, -40)


def test_first_plot_does_not_reuse_flowing_water_as_irrigation(monkeypatch):
    # Flowing spill water is not irrigation the farm can keep: the center sits
    # under it and ensure_farm_water refuses the site on every cycle.
    state = _state({"food_worker": {"farm_plots": []}})
    state.checkpoint_dir = "checkpoint"
    client = SimpleNamespace(transport=SimpleNamespace(dispatch=lambda *_a, **_k: {}))
    lookups = []
    monkeypatch.setattr(
        food_supply, "find_nearby_block", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        food_supply,
        "find_water_source",
        lambda *_args, **_kwargs: lookups.append(_kwargs) or None,
    )
    monkeypatch.setattr(
        food_supply, "find_natural_crop_center", lambda *_args: (None, False)
    )

    assert food_supply._establish_candidate(client, state, None, 5) is None
    assert lookups == [{"radius": 20}]


@pytest.mark.parametrize("intervened", [False, True])
def test_return_home_allows_recovery_movement_but_keeps_defense(monkeypatch, intervened):
    from baritone_client.common import combat, navigation
    client = SimpleNamespace()
    defended = []
    monkeypatch.setattr(combat, "defend_or_flee", lambda c, **kw: defended.append((c, kw)) or intervened)
    def goto(c, x, y, z, **kw):
        assert c is client and (x, y, z) == (0, 70, 0)
        assert kw["timeout"] == food_supply.RETURN_HOME_TIMEOUT
        return not kw["on_defense"]()
    monkeypatch.setattr(navigation, "goto", goto)
    assert food_supply._return_to_anchor(client, (0, 70, 0)) is (not intervened)
    assert defended == [(client, {"allow_safe_recovery_movement": True})]


def test_carried_wheat_becomes_verified_bread_before_any_farm_departure(monkeypatch):
    counts = {food_supply.WHEAT: 6, food_supply.BREAD: 1}
    _inventory(monkeypatch, counts)
    st = _state({"base_location": [0, 70, 0], "wheat_farm": {"origin": [30, 70, 0]},
                 "food_worker": {"bread_crafted": 5, "cycles": 3}})
    monkeypatch.setattr(food_supply, "eat_until_hunger", lambda *_a, **_k: True)
    monkeypatch.setattr(food_supply, "prepare_house_door_for_departure", lambda *_a: pytest.fail("use carried grain before leaving"))
    def craft_home(_client, state, anchor, count, **_kwargs):
        assert state is st and anchor == (0, 70, 0) and count == 2
        counts[food_supply.WHEAT] -= 6
        counts[food_supply.BREAD] += 2
        return True
    monkeypatch.setattr(food_supply, "craft_bread_at_saved_home", craft_home)
    monkeypatch.setattr(food_supply, "resolve_storage_location", lambda *_a, **_k: None)
    result = food_supply.run_food_cycle(object(), st)
    assert result.success and result.bread_crafted == 2 and result.wheat_harvested == 0
    assert result.total_bread_crafted == 7 and st.custom_data["food_worker"]["cycles"] == 4


@pytest.mark.parametrize("safe", [True, False])
def test_carried_grain_gets_no_production_credit_without_inventory_change(monkeypatch, safe):
    counts = {food_supply.WHEAT: 6, food_supply.BREAD: 1}
    _inventory(monkeypatch, counts)
    monkeypatch.setattr(food_supply, "_survival_ready", lambda *_a: safe)
    monkeypatch.setattr(food_supply, "eat_until_hunger", lambda *_a, **_k: True)
    attempted = []
    monkeypatch.setattr(food_supply, "craft_bread_at_saved_home", lambda *_a, **_k: attempted.append(True) or True)
    monkeypatch.setattr(food_supply, "resolve_storage_location", lambda *_a, **_k: None)
    monkeypatch.setattr(food_supply, "prepare_house_door_for_departure", lambda *_a: False)
    st = _state({"base_location": [0, 70, 0]})
    result = food_supply.run_food_cycle(object(), st)
    assert not result.success and result.bread_crafted == 0
    assert attempted == ([True] if safe else [])
    assert st.custom_data["food_worker"].get("bread_crafted", 0) == 0


@pytest.mark.parametrize("gain", [0, 4])
def test_seed_surplus_is_shed_at_the_farm_only_after_a_harvest(monkeypatch, gain):
    """Each harvest returns more seed than replanting uses; A1 carried 548."""
    counts = {"minecraft:wheat": 0, "minecraft:bread": 10}
    monkeypatch.setattr(food_supply, "get_inventory", lambda _client: dict(counts))
    monkeypatch.setattr(food_supply, "_survival_ready", lambda _client: True)
    monkeypatch.setattr(food_supply, "eat_until_hunger", lambda *_a, **_k: True)
    monkeypatch.setattr(food_supply, "harvest_wheat_farm",
                        lambda *_a, **_k: counts.__setitem__("minecraft:wheat", gain) or True)
    monkeypatch.setattr(food_supply, "local_farm_wait_reason", lambda *_a: "growing wheat")
    monkeypatch.setattr(food_supply, "craft", lambda *_a, **_k: False)
    monkeypatch.setattr(food_supply, "resolve_storage_location", lambda *_a, **_k: None)
    shed = []
    monkeypatch.setattr(food_supply, "shed_surplus_seeds", lambda client: shed.append(client) or 0)
    client = SimpleNamespace(transport=SimpleNamespace(dispatch=lambda *_a: {}))
    food_supply.run_food_cycle(client, _state({"wheat_farm": {"origin": [0, 64, 0]}}))
    assert shed == ([client] if gain else [])
