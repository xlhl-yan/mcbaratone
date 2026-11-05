from types import SimpleNamespace

import pytest

from baritone_client.automator import adaptive_scheduler as adaptive
from baritone_client.automator import aid_response
from baritone_client.automator import safety_recovery
from baritone_client.automator import specialty_scheduler as specialty
from baritone_client.automator.adaptive_scheduler import (
    AdaptiveScheduler,
    GameSignals,
    LocalOpportunity,
    OpportunityKind,
    OpportunityResult,
    collect_game_signals,
    score_phase,
)
from baritone_client.automator.objective import (
    ObjStatus,
    ObjectivePlanner,
    default_objectives,
)
from baritone_client.automator.state_manager import Phase
from baritone_client.automator.end_readiness import FleetRole
from baritone_client.common.forestry import WoodCycleResult
from baritone_client.common.storage_organizer import QuartermasterCycleResult


@pytest.fixture(autouse=True)
def _isolate_scheduler_from_armor_inventory(monkeypatch):
    """Keep scheduler unit tests from polling a live-like inventory client."""
    monkeypatch.setattr(
        adaptive.armor_upkeep,
        "select_armor_opportunity",
        lambda *_args, **_kwargs: None,
    )


def _state(custom_data=None):
    return SimpleNamespace(custom_data=custom_data or {})


def _signals(**overrides):
    values = {
        "observed": True,
        "entities_observed": True,
        "dimension": "minecraft:overworld",
        "health": 20.0,
        "food": 20,
        "world_time": 1000,
        "position": (0, 64, 0),
        "inventory": {},
        "adult_animals": {},
    }
    values.update(overrides)
    return GameSignals(**values)


def _post_food_planner():
    planner = ObjectivePlanner(default_objectives())
    planner.restore(
        [
            Phase.BRIDGE_CHECK,
            Phase.SPAWN_BOOTSTRAP,
            Phase.INITIAL_GATHERING,
            Phase.BOOT_SEQUENCE,
            Phase.BASE_CONSTRUCTION,
            Phase.FOOD_AND_IRON,
        ]
    )
    return planner


def test_no_live_signal_preserves_stable_nether_priority():
    planner = _post_food_planner()
    signals = GameSignals()

    selected = planner.select(
        planner.runnable(),
        utility=lambda objective: AdaptiveScheduler.objective_score(
            objective, signals
        ),
    )

    assert selected.phase is Phase.NETHER_AND_BLAZE


def test_observe_publishes_m1_need_without_changing_the_signal(monkeypatch):
    state = _state()
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), state)
    signals = _signals(food=0)
    published = []
    monkeypatch.setattr(adaptive, "collect_game_signals", lambda *_args: signals)
    monkeypatch.setattr(
        adaptive.mutual_aid,
        "publish_requests",
        lambda received_state, received_signals: published.append(
            (received_state, received_signals)
        ),
    )

    assert scheduler.observe() is signals
    assert published == [(state, signals)]


def test_breedable_leather_animals_prioritize_enchanting_sibling():
    planner = _post_food_planner()
    signals = _signals(
        adult_animals={"cow": 2},
        inventory={"minecraft:wheat": 2},
    )

    selected = planner.select(
        planner.runnable(),
        utility=lambda objective: AdaptiveScheduler.objective_score(
            objective, signals
        ),
    )

    assert selected.phase is Phase.ENCHANTING_PIPELINE
    assert "renewable leather pair" in " ".join(
        score_phase(selected.phase, signals).reasons
    )


def test_villagers_and_crops_prioritize_villager_infrastructure():
    planner = _post_food_planner()
    signals = _signals(
        adult_villagers=2,
        crop_location=(4, 64, 4),
        inventory={"minecraft:wheat": 18},
    )

    selected = planner.select(
        planner.runnable(),
        utility=lambda objective: AdaptiveScheduler.objective_score(
            objective, signals
        ),
    )

    assert selected.phase is Phase.VILLAGER_INFRA


def test_same_runnable_frontier_produces_different_work_per_bot_state():
    planner = _post_food_planner()
    animal_bot = _signals(
        adult_animals={"cow": 2},
        inventory={"minecraft:wheat": 2},
    )
    village_bot = _signals(
        adult_villagers=2,
        inventory={"minecraft:bread": 6},
    )
    nether_bot = _signals(dimension="minecraft:the_nether")

    def choose(signals):
        return planner.select(
            planner.runnable(),
            utility=lambda objective: AdaptiveScheduler.objective_score(
                objective, signals
            ),
        ).phase

    assert choose(animal_bot) is Phase.ENCHANTING_PIPELINE
    assert choose(village_bot) is Phase.VILLAGER_INFRA
    assert choose(nether_bot) is Phase.NETHER_AND_BLAZE


def test_completed_nether_supplier_runs_recurring_supply_when_safe(tmp_path, monkeypatch):
    planner = _post_food_planner()
    planner._by_phase[Phase.NETHER_AND_BLAZE].status = ObjStatus.DONE
    state = SimpleNamespace(
        custom_data={},
        checkpoint_dir=tmp_path / "Bot16" / "controller",
    )
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), state)
    monkeypatch.setattr(
        scheduler,
        "observe",
        lambda: _signals(dimension="minecraft:the_nether"),
    )
    opportunity = scheduler.select_local_opportunity(
        _signals(dimension="minecraft:the_nether"),
        tuple(planner.completed_phases()), now=1000, role=FleetRole.NETHER_SUPPLY,
    )

    assert opportunity is not None
    assert opportunity.kind is OpportunityKind.NETHER_SUPPLY


def test_completed_nether_supplier_safety_holds_without_graph_churn(tmp_path, monkeypatch):
    planner = _post_food_planner()
    planner._by_phase[Phase.NETHER_AND_BLAZE].status = ObjStatus.DONE
    state = SimpleNamespace(custom_data={}, checkpoint_dir=tmp_path / "Bot16" / "controller")
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), state)
    monkeypatch.setattr(scheduler, "observe", lambda: _signals(dimension="minecraft:the_nether", health=8))

    decision = scheduler.next_step(planner)

    assert decision.objective is None
    assert decision.role_hold is True
    assert "recurring supply cycle" in decision.summary


def test_live_signal_never_bypasses_graph_prerequisites():
    planner = ObjectivePlanner(default_objectives())
    signals = _signals(adult_villagers=20, crop_location=(1, 64, 1))

    selected = planner.select(
        planner.runnable(),
        utility=lambda objective: AdaptiveScheduler.objective_score(
            objective, signals
        ),
    )

    assert selected.phase is Phase.BRIDGE_CHECK


def test_animal_farm_is_selected_when_pair_and_food_are_local():
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), _state())
    opportunity = scheduler.select_local_opportunity(
        _signals(
            adult_animals={"cow": 2},
            inventory={"minecraft:wheat": 2},
        ),
        [Phase.SPAWN_BOOTSTRAP],
        now=1000.0,
    )

    assert opportunity is not None
    assert opportunity.kind is OpportunityKind.ANIMAL_FARM
    assert opportunity.animal_type == "cow"


def test_crop_farm_is_selected_when_crop_patch_can_replenish_food():
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), _state())
    opportunity = scheduler.select_local_opportunity(
        _signals(
            crop_location=(5, 64, 5),
            crop_block="minecraft:wheat",
            inventory={"minecraft:wheat_seeds": 4},
        ),
        [Phase.SPAWN_BOOTSTRAP],
        now=1000.0,
    )

    assert opportunity is not None
    assert opportunity.kind is OpportunityKind.CROP_FARM
    assert opportunity.location == (5, 64, 5)


def test_clear_hostiles_aid_is_scored_alongside_balanced_local_work(monkeypatch):
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), _state())
    aid = LocalOpportunity(
        OpportunityKind.CLEAR_HOSTILES_AID, 250, "nearby bot needs a cleared area",
        location=(8, 64, 8), aid_request_id="aid-1",
    )
    monkeypatch.setattr(
        aid_response, "select_clear_hostiles_opportunity", lambda *_args, **_kwargs: aid
    )

    opportunity = scheduler.select_local_opportunity(
        _signals(adult_animals={"cow": 2}, inventory={"minecraft:wheat": 2}),
        [Phase.SPAWN_BOOTSTRAP], now=1000.0,
    )

    assert opportunity is aid


def test_unproductive_aid_attempt_records_no_progress(monkeypatch):
    state = _state()
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), state)
    opportunity = LocalOpportunity(
        OpportunityKind.CLEAR_HOSTILES_AID, 250, "test aid", aid_request_id="aid-1"
    )
    monkeypatch.setattr(scheduler, "observe", lambda: _signals())
    monkeypatch.setattr(
        aid_response, "run_scheduled_clear_hostiles",
        lambda *_args: (False, "hostiles did not fall", 0, 0),
    )

    result = scheduler.run_local_opportunity(opportunity)

    assert not result.success
    assert state.custom_data["productive_work"]["no_progress_streak"] == 1


def test_wood_role_selects_forestry_only_after_safe_bootstrap():
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), _state())
    signals = _signals()

    assert scheduler.select_local_opportunity(
        signals,
        [Phase.SPAWN_BOOTSTRAP],
        now=1000.0,
        role=FleetRole.WOOD_SUPPLY,
    ) is None
    opportunity = scheduler.select_local_opportunity(
        signals,
        [Phase.SPAWN_BOOTSTRAP, Phase.BOOT_SEQUENCE],
        now=1000.0,
        role=FleetRole.WOOD_SUPPLY,
    )

    assert opportunity is not None
    assert opportunity.kind is OpportunityKind.WOOD_FARM


def test_hostile_specialist_runs_defensive_recovery_before_a_role_hold():
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), _state())

    opportunity = scheduler.select_local_opportunity(
        _signals(nearby_hostiles=2, health=11.0),
        [Phase.SPAWN_BOOTSTRAP, Phase.BOOT_SEQUENCE],
        now=1000.0,
        role=FleetRole.WOOD_SUPPLY,
    )

    assert opportunity is not None
    assert opportunity.kind is OpportunityKind.SELF_DEFENSE


def test_defensive_recovery_requires_a_measured_hostile_reduction(monkeypatch):
    state = _state()
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), state)
    opportunity = LocalOpportunity(
        OpportunityKind.SELF_DEFENSE, 320, "two nearby hostiles"
    )
    observations = iter((_signals(nearby_hostiles=2), _signals(nearby_hostiles=0)))
    monkeypatch.setattr(scheduler, "observe", lambda: next(observations))
    monkeypatch.setattr(safety_recovery, "defend_or_flee", lambda *_args, **_kwargs: True)

    result = scheduler.run_local_opportunity(opportunity)

    assert result.success
    assert (result.before, result.after) == (2, 0)
    # Escaping a threat is safety recovery; it does not advance production.
    assert state.custom_data["productive_work"]["no_progress_streak"] == 1


def test_iron_role_selects_bounded_mining_after_initial_setup_even_at_night():
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), _state())
    signals = _signals(world_time=18000)

    assert scheduler.select_local_opportunity(
        signals,
        [Phase.SPAWN_BOOTSTRAP],
        now=1000.0,
        role=FleetRole.IRON_SUPPLY,
    ) is None
    opportunity = scheduler.select_local_opportunity(
        signals,
        [Phase.SPAWN_BOOTSTRAP, Phase.INITIAL_GATHERING],
        now=1000.0,
        role=FleetRole.IRON_SUPPLY,
    )

    assert opportunity is not None
    assert opportunity.kind is OpportunityKind.IRON_MINE


def test_iron_role_selects_food_recovery_before_holding_when_hungry():
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), _state())
    opportunity = scheduler.select_local_opportunity(
        _signals(food=10),
        [Phase.SPAWN_BOOTSTRAP, Phase.INITIAL_GATHERING],
        now=1000.0,
        role=FleetRole.IRON_SUPPLY,
    )

    assert opportunity is not None
    assert opportunity.kind is OpportunityKind.FOOD_RECOVERY


def test_balanced_agent_selects_food_recovery_before_retrying_objectives():
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), _state())

    opportunity = scheduler.select_local_opportunity(
        _signals(food=11),
        [Phase.SPAWN_BOOTSTRAP, Phase.INITIAL_GATHERING],
        now=1000.0,
        role=FleetRole.BALANCED,
    )

    assert opportunity is not None
    assert opportunity.kind is OpportunityKind.FOOD_RECOVERY


def test_balanced_agent_services_prepared_food_blocker_before_progression():
    state = _state(
        {"strategic_state": {"blocking_condition": "pickaxe, prepared_food_32"}}
    )
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), state)

    opportunity = scheduler.select_local_opportunity(
        _signals(food=20, inventory={}),
        [Phase.SPAWN_BOOTSTRAP, Phase.INITIAL_GATHERING, Phase.BOOT_SEQUENCE],
        now=1000.0,
        role=FleetRole.BALANCED,
    )

    assert opportunity is not None
    assert opportunity.kind is OpportunityKind.FOOD_PRODUCTION
    assert opportunity.assigned_role == FleetRole.BALANCED.value


def test_balanced_agent_does_not_overproduce_prepared_food():
    state = _state(
        {"strategic_state": {"blocking_condition": "pickaxe, prepared_food_32"}}
    )
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), state)

    assert scheduler.select_local_opportunity(
        _signals(food=20, inventory={"minecraft:bread": 32}),
        [Phase.SPAWN_BOOTSTRAP, Phase.INITIAL_GATHERING, Phase.BOOT_SEQUENCE],
        now=1000.0,
        role=FleetRole.BALANCED,
    ) is None


@pytest.mark.parametrize("strategic", [{}, {"blocking_condition": None}, {"blocking_condition": "pickaxe"}])
def test_balanced_expedition_food_uses_live_reserve_without_saved_blocker(strategic):
    scheduler = AdaptiveScheduler(
        SimpleNamespace(), SimpleNamespace(), _state({"strategic_state": strategic})
    )
    opportunity = scheduler.select_local_opportunity(
        _signals(inventory={"minecraft:bread": 7, "minecraft:wheat": 18}),
        _post_food_planner().completed_phases(),
        now=1000.0,
        role=FleetRole.BALANCED,
    )
    assert opportunity is not None
    assert opportunity.kind is OpportunityKind.FOOD_PRODUCTION
    assert opportunity.assigned_role == "balanced"


def test_balanced_food_production_requires_observed_inventory():
    from baritone_client.automator.food_opportunity import select_balanced_food_production_opportunity

    assert select_balanced_food_production_opportunity(
        GameSignals(), _state({"strategic_state": {"blocking_condition": "prepared_food_32"}}),
        _post_food_planner().completed_phases(), True,
    ) is None


@pytest.mark.parametrize("ready,completed", [
    (False, [Phase.SPAWN_BOOTSTRAP, Phase.INITIAL_GATHERING, Phase.BOOT_SEQUENCE, Phase.FOOD_AND_IRON]),
    (True, [Phase.SPAWN_BOOTSTRAP, Phase.INITIAL_GATHERING, Phase.FOOD_AND_IRON]),
    (True, [Phase.SPAWN_BOOTSTRAP, Phase.INITIAL_GATHERING, Phase.BOOT_SEQUENCE]),
])
def test_live_reserve_recovery_preserves_cooldown_and_bootstrap(ready, completed):
    from baritone_client.automator.food_opportunity import select_balanced_food_production_opportunity

    assert select_balanced_food_production_opportunity(
        _signals(inventory={"minecraft:bread": 7}), _state(), completed, ready,
    ) is None


def test_balanced_food_cooldown_holds_instead_of_selecting_nether(monkeypatch):
    planner = _post_food_planner()
    state = _state(
        {
            "adaptive_scheduler": {
                "opportunities": {
                    "food_recovery": {"last_attempt": 990.0, "success": False},
                    "food_production": {"last_attempt": 990.0, "success": False},
                }
            },
        }
    )
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), state)
    monkeypatch.setattr(adaptive.time, "time", lambda: 1000.0)
    monkeypatch.setattr(
        scheduler,
        "observe",
        lambda: _signals(food=11, inventory={"minecraft:wheat_seeds": 9}),
    )

    decision = scheduler.next_step(planner)

    assert decision.objective is None
    assert decision.role_hold
    assert "prepared food reserve is 0/32" in decision.summary
    assert "waiting for the next bounded food cycle" in decision.summary


def test_balanced_food_hold_clears_when_live_reserve_reaches_target(monkeypatch):
    planner = _post_food_planner()
    state = _state()
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), state)
    monkeypatch.setattr(
        scheduler,
        "observe",
        lambda: _signals(food=20, inventory={"minecraft:bread": 32}),
    )

    decision = scheduler.next_step(planner)

    assert decision.objective is not None
    assert decision.objective.phase is Phase.NETHER_AND_BLAZE


def test_village_food_role_recovers_hunger_before_production():
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), _state())

    opportunity = scheduler.select_local_opportunity(
        _signals(food=10),
        [Phase.SPAWN_BOOTSTRAP, Phase.INITIAL_GATHERING, Phase.BOOT_SEQUENCE],
        now=1000.0,
        role=FleetRole.VILLAGE_FOOD,
    )

    assert opportunity is not None
    assert opportunity.kind is OpportunityKind.FOOD_RECOVERY


def test_village_food_role_selects_recurring_production_after_infrastructure():
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), _state())

    opportunity = scheduler.select_local_opportunity(
        _signals(),
        [Phase.SPAWN_BOOTSTRAP, Phase.INITIAL_GATHERING, Phase.BOOT_SEQUENCE],
        now=1000.0,
        role=FleetRole.VILLAGE_FOOD,
    )

    assert opportunity is not None
    assert opportunity.kind is OpportunityKind.FOOD_PRODUCTION


def test_local_farming_is_skipped_when_hostile_or_before_boot():
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), _state())
    signals = _signals(
        nearby_hostiles=1,
        adult_animals={"cow": 2},
        inventory={"minecraft:wheat": 2},
    )

    defensive = scheduler.select_local_opportunity(
        signals, [Phase.SPAWN_BOOTSTRAP], now=1000.0
    )
    assert defensive is not None
    assert defensive.kind is OpportunityKind.SELF_DEFENSE
    assert scheduler.select_local_opportunity(
        _signals(
            adult_animals={"cow": 2},
            inventory={"minecraft:wheat": 2},
        ),
        [],
        now=1000.0,
    ) is None
    assert scheduler.select_local_opportunity(
        _signals(
            world_time=13000,
            adult_animals={"cow": 2},
            inventory={"minecraft:wheat": 2},
        ),
        [Phase.SPAWN_BOOTSTRAP],
        now=1000.0,
    ) is None


def test_opportunity_cooldown_prevents_repeating_same_failed_work():
    state = _state(
        {
            "adaptive_scheduler": {
                "opportunities": {
                    "animal_farm": {"last_attempt": 900.0, "success": False}
                }
            }
        }
    )
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), state)

    assert scheduler.select_local_opportunity(
        _signals(
            adult_animals={"cow": 2},
            inventory={"minecraft:wheat": 2},
        ),
        [Phase.SPAWN_BOOTSTRAP],
        now=1000.0,
    ) is None


def test_distant_checkpointed_farm_does_not_cause_side_trip():
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), _state())

    assert scheduler.select_local_opportunity(
        _signals(
            known_farm_location=(200, 64, 0),
            inventory={"minecraft:wheat_seeds": 4},
        ),
        [Phase.SPAWN_BOOTSTRAP],
        now=1000.0,
    ) is None


def test_collect_game_signals_reads_entities_inventory_and_crop(monkeypatch):
    class Transport:
        def dispatch(self, route, payload=None, **_kwargs):
            if route == "get_state":
                return {
                    "health": 20,
                    "food_level": 18,
                    "experience_level": 27,
                    "dimension": "minecraft:overworld",
                    "block_position": {"x": 10, "y": 64, "z": 10},
                }
            if route == "get_entities":
                return {
                    "entities": [
                        {"type": "minecraft:cow", "is_baby": False},
                        {"type": "minecraft:cow", "is_baby": False},
                        {"type": "minecraft:villager", "is_baby": False},
                    ]
                }
            if route == "get_block":
                return {"id": "minecraft:wheat"}
            return {}

    client = SimpleNamespace(transport=Transport())
    resources = SimpleNamespace(
        refresh_inventory=lambda: {"minecraft:wheat": 3}
    )
    monkeypatch.setattr(
        adaptive,
        "find_nearby_block",
        lambda *_args, **_kwargs: (12, 64, 12),
    )

    signals = collect_game_signals(client, resources, _state())

    assert signals.observed
    assert signals.position == (10, 64, 10)
    assert signals.adult_animals == {"cow": 2}
    assert signals.adult_villagers == 1
    assert signals.crop_location == (12, 64, 12)
    assert signals.experience_level == 27


def test_collect_game_signals_ignores_hostile_sealed_far_below_player(monkeypatch):
    """A cave mob must not starve all surface farming as SELF_DEFENSE work."""

    class Transport:
        @staticmethod
        def dispatch(route, _payload=None, **_kwargs):
            if route == "get_state":
                return {
                    "health": 20,
                    "food_level": 18,
                    "dimension": "minecraft:overworld",
                    "block_position": {"x": -410, "y": 79, "z": -17},
                }
            if route == "get_entities":
                return {
                    "entities": [
                        {
                            "id": 1,
                            "type": "minecraft:creeper",
                            "distance": 20.3,
                            "position": {"x": -400, "y": 61, "z": -17},
                            "is_aggressive": False,
                            "can_see_player": False,
                        }
                    ]
                }
            return {}

    client = SimpleNamespace(transport=Transport())
    resources = SimpleNamespace(refresh_inventory=lambda: {})
    monkeypatch.setattr(adaptive, "find_nearby_block", lambda *_a, **_k: None)

    signals = collect_game_signals(client, resources, _state())

    assert signals.entities_observed
    assert signals.nearby_hostiles == 0
    assert signals.safe_for_local_work


def test_animal_opportunity_records_verified_herd_growth(monkeypatch):
    state = _state()
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), state)
    observations = iter(
        [
            _signals(adult_animals={"cow": 2}),
            _signals(adult_animals={"cow": 3}),
        ]
    )
    monkeypatch.setattr(scheduler, "observe", lambda: next(observations))
    monkeypatch.setattr(adaptive, "breed_pair", lambda *_args, **_kwargs: True)
    opportunity = LocalOpportunity(
        OpportunityKind.ANIMAL_FARM,
        100,
        "test herd",
        animal_type="cow",
    )

    result = scheduler.run_local_opportunity(opportunity)

    assert result.success
    assert result.before == 2
    assert result.after == 3
    record = state.custom_data["adaptive_scheduler"]["opportunities"][
        "animal_farm"
    ]
    assert record["success"] is True
    ledger = state.custom_data["productive_work"]
    assert ledger["progress_events"] == 1
    assert ledger["no_progress_streak"] == 0


def test_crop_opportunity_verifies_produce_increase(monkeypatch):
    calls = []

    class Transport:
        def dispatch(self, route, payload=None):
            calls.append((route, payload or {}))
            return {}

    client = SimpleNamespace(transport=Transport())
    scheduler = AdaptiveScheduler(client, SimpleNamespace(), _state())
    inventories = iter(
        [
            {"minecraft:wheat": 0, "minecraft:wheat_seeds": 4},
            {"minecraft:wheat": 3, "minecraft:wheat_seeds": 4},
        ]
    )
    monkeypatch.setattr(adaptive, "get_inventory", lambda _client: next(inventories))
    monkeypatch.setattr(adaptive, "goto", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(adaptive.time, "sleep", lambda _seconds: None)
    opportunity = LocalOpportunity(
        OpportunityKind.CROP_FARM,
        80,
        "test crops",
        location=(5, 64, 5),
    )

    result = scheduler.run_local_opportunity(opportunity, crop_timeout=1.0)

    assert result.success
    assert result.detail == "crop produce increased"
    assert (
        "farm",
        {
            "range": 8,
            "x": 5,
            "y": 64,
            "z": 5,
            "replant": True,
        },
    ) in calls
    assert ("cancel", {}) in calls


def test_crop_opportunity_rebuilds_farm_when_nothing_to_harvest_or_replant(monkeypatch):
    """A farm harvested down to bare ground has nothing for #farm to act on.

    Confirmed live on A1 2026-08-14: soil gone in one tile, plain
    grass_block (never tilled) in the rest -- every cycle reported "no
    harvest or planting change" forever. run_crop_opportunity must fall
    back to rebuilding the patch instead of retrying the same no-op.
    """
    client = SimpleNamespace(transport=SimpleNamespace(dispatch=lambda *_a, **_k: {}))
    state = SimpleNamespace(custom_data={})
    scheduler = AdaptiveScheduler(client, SimpleNamespace(), state)
    monkeypatch.setattr(adaptive, "get_inventory", lambda _client: {"minecraft:wheat": 0})
    monkeypatch.setattr(adaptive, "goto", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(adaptive.time, "sleep", lambda _seconds: None)

    rebuild_calls = []

    def fake_establish(client_arg, x, y, z, size=5, state=None):
        rebuild_calls.append((client_arg, x, y, z, state))
        return (x, y, z)

    monkeypatch.setattr(
        "baritone_client.common.farming.establish_wheat_farm", fake_establish
    )
    opportunity = LocalOpportunity(
        OpportunityKind.CROP_FARM, 80, "test crops", location=(5, 64, 5),
    )

    result = scheduler.run_local_opportunity(opportunity, crop_timeout=0.1)

    assert result.success
    assert result.detail == "no mature crop; re-tilled and replanted the patch"
    assert rebuild_calls == [(client, 5, 64, 5, state)]


def test_crop_opportunity_fails_when_rebuild_also_finds_nothing(monkeypatch):
    client = SimpleNamespace(
        transport=SimpleNamespace(
            dispatch=lambda route, _payload: (
                {"inventory": [], "armor": [], "offhand": []}
                if route == "get_inventory"
                else {}
            )
        )
    )
    scheduler = AdaptiveScheduler(client, SimpleNamespace(), _state())
    monkeypatch.setattr(adaptive, "get_inventory", lambda _client: {"minecraft:wheat": 0})
    monkeypatch.setattr(adaptive, "goto", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(adaptive.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "baritone_client.common.farming.establish_wheat_farm",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "baritone_client.common.farming.reestablish_wheat_farm",
        lambda *_a, **_k: None,
    )
    opportunity = LocalOpportunity(
        OpportunityKind.CROP_FARM, 80, "test crops", location=(5, 64, 5),
    )

    result = scheduler.run_local_opportunity(opportunity, crop_timeout=0.1)

    assert not result.success
    assert result.detail == "no harvest, planting, or rebuild change was observed"


def test_crop_opportunity_relocates_an_unusable_saved_patch(monkeypatch):
    client = SimpleNamespace(
        transport=SimpleNamespace(
            dispatch=lambda route, _payload: (
                {"inventory": [], "armor": [], "offhand": []}
                if route == "get_inventory"
                else {}
            )
        )
    )
    state = _state()
    scheduler = AdaptiveScheduler(client, SimpleNamespace(), state)
    monkeypatch.setattr(adaptive, "get_inventory", lambda _client: {})
    monkeypatch.setattr(adaptive, "goto", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(adaptive.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "baritone_client.common.farming.establish_wheat_farm",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "baritone_client.common.farming.reestablish_wheat_farm",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "baritone_client.common.farming.relocate_wheat_farm",
        lambda *_args, **_kwargs: (20, 64, 20),
    )
    opportunity = LocalOpportunity(
        OpportunityKind.CROP_FARM, 80, "test crops", location=(5, 64, 5),
    )

    result = scheduler.run_local_opportunity(opportunity, crop_timeout=0.1)

    assert result.success
    assert result.detail == "unusable crop patch was relocated and replanted"


def test_wood_opportunity_records_banked_log_progress(monkeypatch):
    state = _state()
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), state)
    monkeypatch.setattr(
        adaptive,
        "run_wood_cycle",
        lambda *_args, **_kwargs: WoodCycleResult(
            True,
            "harvested 24 logs, planted 5 saplings, banked 24 logs",
            logs_harvested=24,
            saplings_planted=5,
            logs_banked=24,
            total_logs_banked=24,
        ),
    )
    opportunity = LocalOpportunity(
        OpportunityKind.WOOD_FARM,
        200,
        "test forestry",
    )

    result = scheduler.run_local_opportunity(opportunity)

    assert result.success
    assert result.before == 0
    assert result.after == 24
    assert "planted 5 saplings" in result.detail


def test_iron_opportunity_records_banked_team_supply(monkeypatch):
    state = _state()
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), state)
    monkeypatch.setattr(
        adaptive,
        "run_scheduled_iron_cycle",
        lambda *_args, **_kwargs: (
            True,
            "mined 24 raw iron, smelted 24 ingots, banked 24",
            0,
            24,
        ),
    )
    opportunity = LocalOpportunity(
        OpportunityKind.IRON_MINE,
        200,
        "test iron mining",
    )

    result = scheduler.run_local_opportunity(opportunity)

    assert result.success
    assert result.before == 0
    assert result.after == 24
    assert "smelted 24 ingots" in result.detail


def test_food_recovery_opportunity_records_hunger_progress(monkeypatch):
    state = _state()
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), state)
    monkeypatch.setattr(
        adaptive.food_opportunity,
        "run_scheduled_food_recovery",
        lambda *_args, **_kwargs: (
            True,
            "recovered food from a checkpointed source",
            10,
            16,
        ),
    )
    opportunity = LocalOpportunity(
        OpportunityKind.FOOD_RECOVERY,
        220,
        "test hunger recovery",
    )

    result = scheduler.run_local_opportunity(opportunity)

    assert result.success
    assert result.before == 10
    assert result.after == 16
    ledger = state.custom_data["productive_work"]
    assert ledger.get("progress_events", 0) == 0
    assert ledger["no_progress_streak"] == 1


def test_food_production_records_banked_food_progress(monkeypatch):
    state = _state()
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), state)
    monkeypatch.setattr(
        adaptive.food_opportunity,
        "run_village_food_production",
        lambda *_args, **_kwargs: (True, "banked 24 bread", 0, 24),
    )
    opportunity = LocalOpportunity(
        OpportunityKind.FOOD_PRODUCTION,
        210,
        "test village production",
    )

    result = scheduler.run_local_opportunity(opportunity)

    assert result.success
    assert result.after == 24
    assert state.custom_data["adaptive_scheduler"]["food_banked"] == 24


def test_balanced_food_production_records_carried_reserve_progress(monkeypatch):
    state = _state()
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), state)
    monkeypatch.setattr(
        adaptive.food_opportunity,
        "run_balanced_food_production",
        lambda *_args, **_kwargs: (True, "crafted 4 bread", 0, 4),
    )
    opportunity = LocalOpportunity(
        OpportunityKind.FOOD_PRODUCTION,
        215,
        "prepared food reserve is 0/32",
        assigned_role=FleetRole.BALANCED.value,
    )

    result = scheduler.run_local_opportunity(opportunity)

    assert result.success
    assert result.before == 0
    assert result.after == 4
    assert "food_banked" not in state.custom_data["adaptive_scheduler"]


def test_ready_village_food_role_runs_production_instead_of_reopening_infra(
    monkeypatch, tmp_path
):
    planner = _post_food_planner()
    state = SimpleNamespace(
        custom_data={}, checkpoint_dir=tmp_path / "Bot18" / "controller"
    )
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), state)
    opportunity = LocalOpportunity(
        OpportunityKind.FOOD_PRODUCTION, 210, "test village production"
    )
    result = OpportunityResult(opportunity, True, "banked 24 bread", 0, 24)
    monkeypatch.setattr(scheduler, "observe", lambda: _signals())
    monkeypatch.setattr(
        scheduler, "select_local_opportunity", lambda *_args, **_kwargs: opportunity
    )
    monkeypatch.setattr(scheduler, "run_local_opportunity", lambda *_args: result)

    decision = scheduler.next_step(planner)

    assert decision.local_work
    assert decision.objective is None


def test_village_food_cooldown_holds_without_rearming_villager_infra(
    monkeypatch, tmp_path
):
    planner = _post_food_planner()
    state = SimpleNamespace(
        custom_data={
            "camp_holds": {"streak": 11, "reason": "cooldown"},
            "adaptive_scheduler": {
                "opportunities": {
                    "food_production": {"last_attempt": 950.0, "success": True}
                }
            }
        },
        checkpoint_dir=tmp_path / "Bot18" / "controller",
    )
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), state)
    monkeypatch.setattr(adaptive.time, "time", lambda: 1000.0)
    monkeypatch.setattr(scheduler, "observe", lambda: _signals())

    decision = scheduler.next_step(planner)

    assert decision.objective is None
    assert decision.role_hold
    assert "waiting for recurring food production" in decision.summary
    assert state.custom_data["camp_holds"]["streak"] == 0


def test_recorded_phase_decision_is_explainable():
    state = _state()
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), state)
    objective = _post_food_planner()._by_phase[Phase.ENCHANTING_PIPELINE]
    signals = _signals(
        adult_animals={"cow": 2},
        inventory={"minecraft:wheat": 2},
    )

    scheduler.record_decision(objective, signals)

    decision = state.custom_data["adaptive_scheduler"]["last_decision"]
    assert decision["phase"] == "ENCHANTING_PIPELINE"
    assert decision["score"] > 10
    assert any("leather" in reason for reason in decision["signals"])


def test_next_step_runs_local_work_before_selecting_a_long_phase(monkeypatch):
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), _state())
    opportunity = LocalOpportunity(
        OpportunityKind.ANIMAL_FARM,
        100,
        "local renewable herd",
        animal_type="cow",
    )
    result = OpportunityResult(
        opportunity,
        True,
        "new offspring observed",
        2,
        3,
    )
    monkeypatch.setattr(scheduler, "observe", lambda: _signals())
    monkeypatch.setattr(
        scheduler,
        "select_local_opportunity",
        lambda *_args, **_kwargs: opportunity,
    )
    monkeypatch.setattr(
        scheduler,
        "run_local_opportunity",
        lambda *_args, **_kwargs: result,
    )

    decision = scheduler.next_step(_post_food_planner())

    assert decision.local_work
    assert decision.objective is None
    assert "animal_farm verified" in decision.summary


def test_quartermaster_role_selects_leased_storage_work(monkeypatch):
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), _state())
    monkeypatch.setattr(specialty, "quartermaster_work_available", lambda *_args: True)

    opportunity = scheduler.select_local_opportunity(
        _signals(),
        [Phase.SPAWN_BOOTSTRAP, Phase.BOOT_SEQUENCE],
        role=FleetRole.QUARTERMASTER,
        now=1000.0,
    )

    assert opportunity is not None
    assert opportunity.kind is OpportunityKind.STORAGE_MAINTENANCE
    assert opportunity.assigned_role == "quartermaster"


def test_one_bot_can_borrow_unstaffed_quartermaster_duty(tmp_path, monkeypatch):
    controller = tmp_path / "runs" / "headlessmc" / "Bot07" / "controller"
    controller.mkdir(parents=True)
    state = SimpleNamespace(custom_data={}, checkpoint_dir=controller)
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), state)
    monkeypatch.setattr(specialty, "quartermaster_work_available", lambda *_args: True)
    monkeypatch.setattr(specialty, "select_role_opportunity", lambda *_args, **_kwargs: None)

    opportunity = scheduler.select_local_opportunity(
        _signals(),
        [
            Phase.SPAWN_BOOTSTRAP,
            Phase.INITIAL_GATHERING,
            Phase.BOOT_SEQUENCE,
        ],
        role=FleetRole.END_RUNNER,
        now=1000.0,
    )

    assert opportunity is not None
    assert opportunity.kind is OpportunityKind.STORAGE_MAINTENANCE
    assert opportunity.assigned_role == "quartermaster"


def test_quartermaster_result_records_verified_item_delta(monkeypatch):
    state = _state()
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), state)
    monkeypatch.setattr(
        adaptive,
        "run_quartermaster_cycle",
        lambda *_args: QuartermasterCycleResult(
            True,
            "moved ten ores",
            items_moved=10,
            stacks_moved=1,
            total_items_moved=10,
        ),
    )
    opportunity = LocalOpportunity(
        OpportunityKind.STORAGE_MAINTENANCE,
        230,
        "test Quartermaster",
        assigned_role="quartermaster",
    )

    result = scheduler.run_local_opportunity(opportunity)

    assert result.success
    assert result.before == 0
    assert result.after == 10
    assert state.custom_data["adaptive_scheduler"]["quartermaster_items_moved"] == 10


def test_profile_secondary_work_does_not_consume_borrowed_duty_cooldown(
    tmp_path, monkeypatch
):
    controller = tmp_path / "runs" / "headlessmc" / "Bot07" / "controller"
    controller.mkdir(parents=True)
    (controller / "fleet-role-profile.json").write_text(
        '{"primary_role":"end_runner","secondary_roles":["quartermaster"]}\n',
        encoding="utf-8",
    )
    state = SimpleNamespace(custom_data={}, checkpoint_dir=controller)
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), state)
    monkeypatch.setattr(
        adaptive,
        "run_quartermaster_cycle",
        lambda *_args: QuartermasterCycleResult(
            True, "verified storage", total_items_moved=1
        ),
    )
    opportunity = LocalOpportunity(
        OpportunityKind.STORAGE_MAINTENANCE,
        230,
        "profile Quartermaster",
        assigned_role="quartermaster",
    )

    result = scheduler.run_local_opportunity(opportunity)

    assert result.success
    assert "last_borrowed_specialty_at" not in state.custom_data["adaptive_scheduler"]


def test_balanced_role_still_runs_self_gated_safety_work_with_a_hostile_near(monkeypatch):
    # Live A1: one distant mob was almost always within 24 blocks, so the
    # zero-hostile comfort gate returned before armour or base lighting could
    # be considered, and the bot only ever defended and waited.
    planner = _post_food_planner()
    state = _state(
        {"structures": {"starter_house": {"origin": [0, 64, 0], "repaired": True}}}
    )
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), state)
    armour = LocalOpportunity(OpportunityKind.ARMOR_UPKEEP, 180, "0/4 armour worn")
    monkeypatch.setattr(
        adaptive.armor_upkeep, "select_armor_opportunity", lambda *_a, **_k: armour
    )
    monkeypatch.setattr(specialty, "select_self_defense", lambda _signals: None)
    signals = _signals(nearby_hostiles=1, inventory={"minecraft:bread": 32})
    assert not signals.safe_for_local_work

    opportunity = scheduler.select_local_opportunity(
        signals, planner.completed_phases(), now=1000.0, role=FleetRole.BALANCED
    )

    assert opportunity is armour


def test_balanced_food_production_tolerates_a_distant_hostile(monkeypatch):
    planner = _post_food_planner()
    state = _state({"strategic_state": {"blocking_condition": "prepared_food_32"}})
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), state)
    monkeypatch.setattr(specialty, "select_self_defense", lambda _signals: None)
    signals = _signals(nearby_hostiles=1, inventory={"minecraft:bread": 3})

    opportunity = scheduler.select_local_opportunity(
        signals, planner.completed_phases(), now=1000.0, role=FleetRole.BALANCED
    )

    assert opportunity is not None
    assert opportunity.kind is OpportunityKind.FOOD_PRODUCTION


def test_balanced_role_holds_when_hostiles_crowd_in(monkeypatch):
    planner = _post_food_planner()
    state = _state({"strategic_state": {"blocking_condition": "prepared_food_32"}})
    scheduler = AdaptiveScheduler(SimpleNamespace(), SimpleNamespace(), state)
    monkeypatch.setattr(specialty, "select_self_defense", lambda _signals: None)
    signals = _signals(nearby_hostiles=3, inventory={"minecraft:bread": 3})

    assert scheduler.select_local_opportunity(
        signals, planner.completed_phases(), now=1000.0, role=FleetRole.BALANCED
    ) is None
