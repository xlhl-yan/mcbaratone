from types import SimpleNamespace
from unittest.mock import patch

from baritone_client.actions.combat import CombatAction
from baritone_client.automator.systems import SafetySystem
from baritone_client.common import combat, escape_recovery
from baritone_client.common.defense import (
    DefenseMode,
    DefenseRuntime,
    EscapeCandidate,
    assess_threats,
    choose_defense_action,
    plan_escape_candidates,
)


def _entity(entity_id, entity_type, distance, x, z, **extra):
    entity = {
        "id": entity_id,
        "type": f"minecraft:{entity_type}",
        "distance": distance,
        "position": {"x": x, "y": 64, "z": z},
        "velocity": {"x": 0, "y": 0, "z": 0},
    }
    entity.update(extra)
    return entity


def test_threat_classifier_uses_exact_registry_paths():
    threats = assess_threats(
        [
            _entity(1, "pig", 3, 3, 0),
            _entity(2, "piglin_brute", 8, 8, 0),
        ],
        {"block_position": {"x": 0, "y": 64, "z": 0}},
    )

    assert [threat.entity_type for threat in threats] == ["piglin_brute"]


def test_conditional_enderman_requires_aggression_or_contact():
    distant = _entity(1, "enderman", 8, 8, 0)
    angry = _entity(2, "enderman", 9, 9, 0, angry_at_player=True)

    threats = assess_threats(
        [distant, angry],
        {"block_position": {"x": 0, "y": 64, "z": 0}},
    )

    assert [threat.entity["id"] for threat in threats] == [2]


def test_daytime_spider_in_melee_range_overrides_stale_calm_metadata():
    """A spider at contact range is unsafe even between target updates."""
    distant = _entity(1, "spider", 8, 8, 0, is_aggressive=False)
    touching = _entity(2, "spider", 3, 3, 0, is_aggressive=False)
    state = {
        "world_time": 1000,
        "block_position": {"x": 0, "y": 64, "z": 0},
    }

    threats = assess_threats([distant, touching], state)

    assert [threat.entity["id"] for threat in threats] == [2]


def test_closing_ranged_threat_outranks_nearer_basic_melee():
    zombie = _entity(1, "zombie", 5, 5, 0)
    skeleton = _entity(2, "skeleton", 9, 9, 0)
    skeleton["velocity"]["x"] = -0.5

    threats = assess_threats(
        [zombie, skeleton],
        {"block_position": {"x": 0, "y": 64, "z": 0}},
    )

    assert threats[0].entity_type == "skeleton"
    assert threats[0].closing_speed > 0


def test_policy_evades_multiple_urgent_threats_even_when_equipped():
    threats = assess_threats(
        [
            _entity(1, "zombie", 4, 4, 0),
            _entity(2, "husk", 7, 0, 7),
        ],
        {"block_position": {"x": 0, "y": 64, "z": 0}},
    )

    decision = choose_defense_action(
        threats,
        health=20,
        armor_count=4,
        has_weapon=True,
        runtime=DefenseRuntime(),
        now=10,
    )

    assert decision.mode == DefenseMode.EVADE
    assert "multiple" in decision.reason


def test_policy_allows_only_bounded_melee_when_ready():
    threats = assess_threats(
        [_entity(1, "zombie", 5, 5, 0)],
        {"block_position": {"x": 0, "y": 64, "z": 0}},
    )

    decision = choose_defense_action(
        threats,
        health=20,
        armor_count=4,
        has_weapon=True,
        runtime=DefenseRuntime(),
        now=10,
    )

    assert decision.mode == DefenseMode.ENGAGE


def test_distant_non_closing_threat_is_alert_only():
    threats = assess_threats(
        [_entity(1, "zombie", 14, 14, 0)],
        {"block_position": {"x": 0, "y": 64, "z": 0}},
    )

    decision = choose_defense_action(
        threats,
        health=20,
        armor_count=4,
        has_weapon=False,
        runtime=DefenseRuntime(),
        now=10,
    )

    assert decision.mode == DefenseMode.ALERT


def test_distant_high_severity_threat_with_urgent_melee_forces_evasion():
    threats = assess_threats(
        [
            _entity(1, "skeleton", 14, 14, 0),
            _entity(2, "zombie", 4, 4, 0),
        ],
        {"block_position": {"x": 0, "y": 64, "z": 0}},
    )

    decision = choose_defense_action(
        threats,
        health=20,
        armor_count=4,
        has_weapon=True,
        runtime=DefenseRuntime(),
        now=10,
    )

    assert decision.mode == DefenseMode.EVADE
    assert "multiple" in decision.reason
    assert decision.primary.entity["id"] == 2
    assert decision.primary.distance == 4


def test_recovery_hysteresis_prevents_immediate_clear():
    runtime = DefenseRuntime()
    runtime.hold_recovery(8, now=10)

    held = choose_defense_action(
        [],
        health=20,
        armor_count=4,
        has_weapon=True,
        runtime=runtime,
        now=12,
    )
    cleared = choose_defense_action(
        [],
        health=20,
        armor_count=4,
        has_weapon=True,
        runtime=runtime,
        now=19,
    )

    assert held.mode == DefenseMode.RECOVER
    assert cleared.mode == DefenseMode.CLEAR


def test_escape_candidates_move_away_from_combined_threat_pressure():
    state = {"block_position": {"x": 0, "y": 64, "z": 0}}
    threats = assess_threats(
        [
            _entity(1, "zombie", 4, 4, 0),
            _entity(2, "skeleton", 6, 0, 6),
        ],
        state,
    )

    candidates = plan_escape_candidates(state["block_position"], threats)

    assert candidates[0].x < 0
    assert candidates[0].z < 0


class EscapeTransport:
    def __init__(self, threat, *, hazardous_primary=False):
        self.threat = threat
        self.hazardous_primary = hazardous_primary
        self.calls = []
        self.entity_reads = 0
        self.setting = "true"
        self.player_y = 64

    def dispatch(self, route, payload, **_kwargs):
        self.calls.append((route, payload))
        if route == "get_state":
            return {
                "health": 20,
                "world_time": 13000,
                "block_position": {"x": 0, "y": self.player_y, "z": 0},
                "is_pathing": False,
            }
        if route == "settings":
            if "set" in payload:
                self.setting = payload["value"]
            return {"value": self.setting}
        if route == "get_combat_snapshot":
            return {
                "player": {
                    "health": 20,
                    "block_position": {"x": -6, "y": self.player_y, "z": 0},
                },
                "entities": [],
                "skipped_count": 0,
            }
        if route == "get_entities":
            self.entity_reads += 1
            if self.entity_reads == 1:
                return {"entities": [self.threat]}
            return {"entities": []}
        if route == "get_block":
            if (
                self.hazardous_primary
                and payload["x"] == -30
                and payload["y"] == 64
                and payload["z"] == 0
            ):
                return {"id": "minecraft:lava"}
            if payload["y"] == 63:
                return {"id": "minecraft:stone"}
            return {"id": "minecraft:air"}
        return {}


def test_run_away_verifies_target_is_no_longer_nearby():
    threat = _entity(1, "zombie", 10, 4, 0)
    transport = EscapeTransport(threat)
    client = SimpleNamespace(transport=transport)

    assert combat.run_away(client, threat, timeout=1)
    assert any(route == "goal" for route, _payload in transport.calls)


def test_run_away_rejects_lava_endpoint_before_pathing():
    threat = _entity(1, "zombie", 10, 4, 0)
    transport = EscapeTransport(threat, hazardous_primary=True)
    client = SimpleNamespace(transport=transport)

    assert combat.run_away(client, threat, timeout=1)
    goals = [payload for route, payload in transport.calls if route == "goal"]
    assert goals
    assert goals[0] != {"x": -30, "y": 64, "z": 0}


def test_run_away_filters_drop_candidates_and_disables_excavation(monkeypatch):
    threat = _entity(1, "zombie", 10, 4, 0)
    transport = EscapeTransport(threat)
    client = SimpleNamespace(transport=transport)
    candidates = [
        EscapeCandidate(-30, 61, 0, 2.0),
        EscapeCandidate(-31, 64, 0, 1.0),
    ]
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_a, **_k: [threat])
    monkeypatch.setattr(escape_recovery, "plan_escape_candidates", lambda *_a, **_k: candidates)
    monkeypatch.setattr(escape_recovery, "destination_safe", lambda *_a, **_k: True)
    observed_floor = []

    def verify(*_args, **kwargs):
        observed_floor.append(kwargs["minimum_y"])
        return True

    monkeypatch.setattr(escape_recovery, "verify_escape_from_threats", verify)

    assert combat.run_away(client, threat, timeout=1)

    goals = [payload for route, payload in transport.calls if route == "goal"]
    assert goals == [{"x": -31, "y": 64, "z": 0}]
    assert observed_floor == [62.0]
    assert ("settings", {"set": "allowBreak", "value": "false"}) in transport.calls
    assert transport.setting == "true"


def test_fresh_safety_system_binds_checkpoint_home_for_escape_floor(monkeypatch):
    threat = _entity(1, "zombie", 10, 4, 0)
    transport = EscapeTransport(threat)
    transport.player_y = 72
    client = SimpleNamespace(transport=transport)
    state_manager = SimpleNamespace(custom_data={"base_location": [0, 70, 0]})
    safety = SafetySystem(
        client,
        SimpleNamespace(broadcast=lambda _event: None),
        state_manager=state_manager,
    )

    with patch("baritone_client.common.combat.defend_or_flee") as defend:
        safety.tick()

    defend.assert_called_once_with(client)
    assert client._protected_home_anchor == (0.0, 70.0, 0.0)

    candidates = [
        EscapeCandidate(-30, 67, 0, 2.0),
        EscapeCandidate(-31, 72, 0, 1.0),
    ]
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_a, **_k: [threat])
    monkeypatch.setattr(escape_recovery, "plan_escape_candidates", lambda *_a, **_k: candidates)
    monkeypatch.setattr(escape_recovery, "destination_safe", lambda *_a, **_k: True)
    monkeypatch.setattr(escape_recovery, "verify_escape_from_threats", lambda *_a, **_k: True)

    assert combat.run_away(client, threat, timeout=1)
    goals = [payload for route, payload in transport.calls if route == "goal"]
    assert goals == [{"x": -31, "y": 72, "z": 0}]


def test_run_away_does_not_start_when_allow_break_setting_is_unknown(monkeypatch):
    threat = _entity(1, "zombie", 10, 4, 0)
    transport = EscapeTransport(threat)
    transport.setting = "unknown"
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_a, **_k: [threat])

    assert not combat.run_away(client, threat, timeout=1)
    assert not any(route == "goal" for route, _payload in transport.calls)
    assert client._last_escape_failure_reason == "error"


def test_run_away_keeps_digging_disabled_when_stop_is_unverified(monkeypatch):
    threat = _entity(1, "zombie", 10, 4, 0)
    transport = EscapeTransport(threat)
    client = SimpleNamespace(transport=transport)
    original_dispatch = transport.dispatch
    state_reads = {"count": 0}

    def dispatch(route, payload):
        response = original_dispatch(route, payload)
        if route == "get_state":
            state_reads["count"] += 1
            if state_reads["count"] > 1:
                response["is_pathing"] = True
        return response

    monkeypatch.setattr(transport, "dispatch", dispatch)
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_a, **_k: [threat])
    monkeypatch.setattr(escape_recovery, "verify_escape_from_threats", lambda *_a, **_k: True)

    assert not combat.run_away(client, threat, timeout=1)
    assert transport.setting == "false"
    assert client._last_escape_failure_reason == "route_stop_unverified"
    assert ("chat", {"message": "#stop"}) in transport.calls
    # Live A1 2026-10-21: this used to leave digging off for 16 hours.
    assert client._home_route_owed == {"allowBreak": "true"}


def test_an_unverified_flee_stop_is_restored_once_nothing_moves(monkeypatch):
    from baritone_client.common import home_surface

    threat = _entity(1, "zombie", 10, 4, 0)
    transport = EscapeTransport(threat)
    client = SimpleNamespace(transport=transport)
    original_dispatch = transport.dispatch
    moving = {"value": True}
    state_reads = {"count": 0}

    def dispatch(route, payload=None):
        response = original_dispatch(route, payload)
        if route == "get_state":
            state_reads["count"] += 1
            if state_reads["count"] > 1:
                response["is_pathing"] = moving["value"]
        return response

    monkeypatch.setattr(transport, "dispatch", dispatch)
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_a, **_k: [threat])
    monkeypatch.setattr(escape_recovery, "verify_escape_from_threats", lambda *_a, **_k: True)
    monkeypatch.setattr(home_surface.time, "sleep", lambda *_a: None)
    combat.run_away(client, threat, timeout=1)
    assert transport.setting == "false"

    moving["value"] = False
    assert home_surface.restore_owed_home_route_settings(client)
    assert transport.setting == "true"
    assert client._home_route_owed == {}


def test_escape_verification_fails_when_fresh_position_crosses_floor(monkeypatch):
    threat = _entity(1, "zombie", 10, 4, 0)
    transport = EscapeTransport(threat)
    client = SimpleNamespace(transport=transport)
    monkeypatch.setattr(
        combat,
        "_get_combat_snapshot",
        lambda *_a, **_k: {
            "player": {
                "health": 20,
                "block_position": {"x": -2, "y": 61, "z": 0},
            },
            "entities": [],
            "skipped_count": 0,
        },
    )

    assert not escape_recovery.verify_escape_from_threats(
        client,
        {1: 4.0},
        {"x": 0, "y": 64, "z": 0},
        timeout=1,
        minimum_gain=5,
        minimum_y=62,
    )


def test_surface_escape_scan_is_bounded_for_active_combat():
    calls = []

    class Transport:
        def dispatch(self, route, payload, timeout=None):
            calls.append((route, payload, timeout))
            return {"found": []}

    client = SimpleNamespace(transport=Transport())
    planned = [EscapeCandidate(24, 64, 0, 1.0)]

    assert escape_recovery.surface_adjusted_candidates(client, planned) == []
    assert calls == [
        (
            "find_blocks",
            {
                "blocks": [
                    "minecraft:grass_block",
                    "minecraft:dirt",
                    "minecraft:coarse_dirt",
                    "minecraft:podzol",
                ],
                "radius": 16,
                "limit": 512,
            },
            2.0,
        )
    ]


def test_run_away_never_starts_broad_surface_scan_under_attack(monkeypatch):
    threat = _entity(1, "zombie", 6, 6, 0)
    client = SimpleNamespace(transport=EscapeTransport(threat))
    monkeypatch.setattr(
        escape_recovery, "destination_safe", lambda *_args: False
    )
    monkeypatch.setattr(
        escape_recovery,
        "surface_adjusted_candidates",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("active combat must not start a broad block scan")
        ),
    )

    assert combat.run_away(client, threat, timeout=1) is False
    assert client._last_escape_failure_reason == "no_safe_endpoint"


def test_escape_endpoint_does_not_confuse_named_solids_with_plants():
    class Transport:
        def __init__(self, feet):
            self.feet = feet

        def dispatch(self, route, payload, **_kwargs):
            assert route == "get_block"
            return {
                "id": {
                    63: "minecraft:dirt",
                    64: self.feet,
                    65: "minecraft:air",
                }[payload["y"]]
            }

    for unsafe in (
        "minecraft:grass_block",
        "minecraft:snow_block",
        "minecraft:powder_snow",
    ):
        client = SimpleNamespace(transport=Transport(unsafe))
        assert not escape_recovery.destination_safe(client, 0, 64, 0)

    client = SimpleNamespace(transport=Transport("minecraft:short_grass"))
    assert escape_recovery.destination_safe(client, 0, 64, 0)


def test_defense_remains_in_recovery_after_successful_escape(monkeypatch):
    threat = _entity(1, "zombie", 5, 5, 0)

    class Transport:
        def __init__(self):
            self.calls = []

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {
                    "health": 20,
                    "block_position": {"x": 0, "y": 64, "z": 0},
                }
            return {}

    client = SimpleNamespace(transport=Transport())
    scans = iter(([threat], []))
    healed = []
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_args, **_kwargs: next(scans))
    monkeypatch.setattr(combat, "get_equipped_armor", lambda _client: {})
    monkeypatch.setattr(combat, "run_away", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        combat,
        "heal_if_needed",
        lambda *_args, **_kwargs: healed.append(True) or False,
    )
    # Recovery eats first: a full hunger bar is what enables natural regen.
    # Healing is the fallback for when no food could be eaten, so force that
    # branch here rather than relying on the mock's absent food field.
    monkeypatch.setattr(combat, "eat_until_hunger", lambda *_args, **_kwargs: False)

    assert combat.defend_or_flee(client)
    assert combat.defend_or_flee(client)
    assert healed == [True]
    assert client._mcbaratone_defense_runtime.mode == DefenseMode.RECOVER


def test_recovery_prefers_eating_over_healing(monkeypatch):
    """A successful feed must not also burn a heal on the same tick."""
    threat = _entity(1, "zombie", 5, 5, 0)

    class Transport:
        def __init__(self):
            self.calls = []

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {
                    "health": 20,
                    "block_position": {"x": 0, "y": 64, "z": 0},
                }
            return {}

    client = SimpleNamespace(transport=Transport())
    scans = iter(([threat], []))
    healed = []
    monkeypatch.setattr(combat, "scan_for_threats", lambda *_args, **_kwargs: next(scans))
    monkeypatch.setattr(combat, "get_equipped_armor", lambda _client: {})
    monkeypatch.setattr(combat, "run_away", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        combat,
        "heal_if_needed",
        lambda *_args, **_kwargs: healed.append(True) or False,
    )
    monkeypatch.setattr(combat, "eat_until_hunger", lambda *_args, **_kwargs: True)

    assert combat.defend_or_flee(client)
    assert combat.defend_or_flee(client)
    assert healed == []
    assert client._mcbaratone_defense_runtime.mode == DefenseMode.RECOVER


def test_unknown_entity_state_stops_work_instead_of_assuming_clear(monkeypatch):
    class Transport:
        def __init__(self):
            self.calls = []

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_state":
                return {
                    "health": 20,
                    "block_position": {"x": 0, "y": 64, "z": 0},
                }
            return {}

    client = SimpleNamespace(transport=Transport())
    monkeypatch.setattr(
        combat,
        "scan_for_threats",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            combat.EntityQueryError("bridge timeout")
        ),
    )

    assert combat.defend_or_flee(client)
    assert ("chat", {"message": "#stop"}) in client.transport.calls
    assert ("cancel", {}) in client.transport.calls
    assert client._mcbaratone_defense_runtime.mode == DefenseMode.ALERT


def test_defense_consumes_atomic_bridge_snapshot_without_split_reads():
    class Transport:
        def __init__(self):
            self.calls = []

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            if route == "get_combat_snapshot":
                return {
                    "snapshot_version": 1,
                    "tick": 1234,
                    "player": {
                        "health": 20,
                        "armor_count": 4,
                        "block_position": {"x": 0, "y": 64, "z": 0},
                    },
                    "entities": [],
                }
            # The drowning reflex reads the head block (dry here); this is a
            # deliberate safety read, not a split of the atomic threat snapshot.
            if route == "get_block":
                return {"id": "minecraft:air"}
            raise AssertionError(f"unexpected split observation: {route}")

    client = SimpleNamespace(transport=Transport())

    assert not combat.defend_or_flee(client)
    # Threat state still comes solely from the atomic snapshot; the only
    # additional read is the head-block check for drowning.
    assert client.transport.calls == [
        ("get_combat_snapshot", {"radius": 16}),
        ("get_block", {"x": 0, "y": 65, "z": 0}),
    ]


def test_bridge_aggression_metadata_controls_conditional_mob_policy():
    state = {"world_time": 13000, "entity_id": 99}
    calm = _entity(1, "enderman", 2, 2, 0)
    calm["is_aggressive"] = False
    angry = dict(calm, is_aggressive=True, target_id=99)

    assert assess_threats([calm], state) == []
    assert assess_threats([angry], state)[0].entity["target_id"] == 99


def test_failed_melee_reassesses_and_flees_new_highest_threat(monkeypatch):
    zombie = _entity(1, "zombie", 4, 4, 0)
    creeper = _entity(2, "creeper", 3, 0, 3)
    player = {
        "health": 20,
        "armor_count": 4,
        "block_position": {"x": 0, "y": 64, "z": 0},
    }
    snapshots = iter(
        (
            {"player": player, "entities": [zombie]},
            {"player": player, "entities": [zombie, creeper]},
        )
    )

    class Transport:
        def __init__(self):
            self.calls = []

        def dispatch(self, route, payload):
            self.calls.append((route, payload))
            return {}

    client = SimpleNamespace(transport=Transport())
    fled = []
    monkeypatch.setattr(
        combat,
        "_get_combat_snapshot",
        lambda *_args, **_kwargs: next(snapshots),
    )
    monkeypatch.setattr(combat, "equip_best_weapon", lambda _client: True)
    monkeypatch.setattr(combat, "safe_combat", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        combat,
        "run_away",
        lambda _client, target: fled.append(target) or True,
    )

    assert combat.defend_or_flee(client)
    assert fled == [creeper]


def test_combat_action_safe_combat_delegates_to_canonical_module(monkeypatch):
    client = SimpleNamespace()
    context = SimpleNamespace(client=client)
    calls = []
    monkeypatch.setattr(
        combat,
        "safe_combat",
        lambda delegated_client, target_id, **kwargs: calls.append(
            (delegated_client, target_id, kwargs)
        )
        or True,
    )

    assert CombatAction().safe_combat(
        context,
        42,
        retreat_health=11,
        max_duration=9,
    )
    assert calls == [
        (client, 42, {"retreat_health": 11, "max_duration": 9})
    ]


def test_combat_action_healing_delegates_to_canonical_module(monkeypatch):
    client = SimpleNamespace()
    context = SimpleNamespace(client=client)
    calls = []
    monkeypatch.setattr(
        combat,
        "heal_if_needed",
        lambda delegated_client, threshold: calls.append(
            (delegated_client, threshold)
        )
        or True,
    )

    assert CombatAction().heal_if_needed(context, threshold=13)
    assert calls == [(client, 13)]
