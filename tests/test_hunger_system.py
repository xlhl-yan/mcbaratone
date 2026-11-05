
import unittest
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import sys
import os

# Add src to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

from baritone_client.automator.systems import HungerSystem, SafetySystem
from baritone_client.automator.coordination_hub import CoordinationHub, EventType
from baritone_client.common import combat
from baritone_client.common.combat_action import exclusive_combat_action

class TestHungerSystem(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_coordination = MagicMock(spec=CoordinationHub)
        # Mock transport dispatch
        self.mock_client.transport.dispatch = MagicMock()
        
        self.hunger_system = HungerSystem(self.mock_client, self.mock_coordination)

    def test_eating_trigger(self):
        # Setup low hunger
        state_response = {"food_level": 10, "food": 10} # < 18 check

        # Setup inventory with apple already in the hotbar. select_hotbar_item
        # reads back selected_slot after select_slot to verify the switch
        # actually happened, so the fixture must track that state like a
        # real bridge would rather than return one static snapshot forever.
        selected_slot = {"value": 0}

        def dispatch_side_effect(command, params, timeout=None):
            if command == "get_state":
                return state_response
            if command == "get_inventory":
                return {
                    "inventory": [
                        {"id": "minecraft:stone", "count": 64, "slot": 0},
                        {"id": "minecraft:apple", "count": 5, "slot": 8},
                    ],
                    "offhand": [],
                    "selected_slot": selected_slot["value"],
                }
            if command == "select_slot":
                selected_slot["value"] = params["slot"]
                return {"success": True}
            if command == "use_item":
                return {"used": True}
            return {}

        self.mock_client.transport.dispatch.side_effect = dispatch_side_effect

        # Run tick without spending real time in the held-use quarantine.
        with patch("baritone_client.common.combat_action.time.sleep"):
            self.hunger_system.tick()

        # Verify get_state called
        self.mock_client.transport.dispatch.assert_any_call("get_state", {}, timeout=1.0)

        # Verify get_inventory called (because hunger < 18)
        self.mock_client.transport.dispatch.assert_any_call("get_inventory", {})

        # Verify select_slot called for apple (slot 8) - Checking logic in EatAction
        # EatAction logic: check inventory, find apple at slot 8.
        # Current slot defaults to 0 in mock if not specified.
        # EatAction should switch to slot 8.
        self.mock_client.transport.dispatch.assert_any_call("select_slot", {"slot": 8})

        # Verify use_item called
        self.mock_client.transport.dispatch.assert_any_call(
            "use_item", {"hand": "MAIN_HAND", "duration_ms": 1600}
        )

    def test_eating_food_stranded_outside_the_hotbar_gets_swapped_in(self):
        """Food outside the hotbar must be swapped in and eaten, not abandoned.

        Live A1 2026-09-06: at critical health, HungerSystem picked
        cooked_beef and cooked_porkchop, both carried in main-inventory slot
        16, and failed to eat either -- "Food ... must be in hotbar (0-8) to
        eat" -- immediately before a death. EatAction's own swap-to-hotbar
        logic had never been finished (a comment above the fail read "SAFETY
        FALLBACK: only eat if in hotbar for now") even though
        harness_ops.select_hotbar_item already does exactly this move
        safely elsewhere in the codebase.
        """
        inventory_state = {
            0: {"id": "minecraft:air", "count": 0},
            16: {"id": "minecraft:cooked_beef", "count": 3},
        }
        selected_slot = {"value": 0}

        def dispatch_side_effect(command, params, timeout=None):
            if command == "get_state":
                return {"food_level": 5}
            if command == "get_inventory":
                return {
                    "inventory": [
                        {"id": data["id"], "count": data["count"], "slot": slot}
                        for slot, data in inventory_state.items()
                    ],
                    "offhand": [],
                    "selected_slot": selected_slot["value"],
                }
            if command == "close_screen":
                return {}
            if command == "inventory_click":
                # SWAP: button is the raw hotbar index, slot is the menu slot
                # (identity-mapped for the 9-35 main-inventory range used here).
                inv_slot, target_hotbar = params["slot"], params["button"]
                inventory_state[target_hotbar], inventory_state[inv_slot] = (
                    inventory_state[inv_slot], inventory_state[target_hotbar],
                )
                return {"clicked": True}
            if command == "select_slot":
                selected_slot["value"] = params["slot"]
                return {"success": True}
            if command == "use_item":
                return {"used": True}
            return {}

        self.mock_client.transport.dispatch.side_effect = dispatch_side_effect

        with patch("baritone_client.common.combat_action.time.sleep"):
            self.hunger_system.tick()

        self.mock_client.transport.dispatch.assert_any_call(
            "inventory_click", {"slot": 16, "type": "SWAP", "button": 0}
        )
        self.mock_client.transport.dispatch.assert_any_call("select_slot", {"slot": 0})
        self.mock_client.transport.dispatch.assert_any_call(
            "use_item", {"hand": "MAIN_HAND", "duration_ms": 1600}
        )
        self.assertEqual(inventory_state[0]["id"], "minecraft:cooked_beef")

    def test_no_food_found(self):
        # Setup low hunger
        self.mock_client.transport.dispatch.side_effect = None
        self.mock_client.transport.dispatch.return_value = {"food_level": 10}
        
        def dispatch_side_effect(command, params, timeout=None):
            if command == "get_state":
                return {"food_level": 10}
            if command == "get_inventory":
                return {"inventory": [{"id": "minecraft:stone", "count": 1}], "offhand": []}
            return {}
            
        self.mock_client.transport.dispatch.side_effect = dispatch_side_effect
        
        # Run tick
        self.hunger_system.tick()
        
        # Should call get_inventory
        self.mock_client.transport.dispatch.assert_any_call("get_inventory", {})
        
        # Should NOT call use_item
        with self.assertRaises(AssertionError):
            self.mock_client.transport.dispatch.assert_any_call(
                "use_item", {"hand": "MAIN_HAND", "duration_ms": 1600}
            )

    def test_safety_system_signals_death_without_background_respawn(self):
        calls = []

        def dispatch(command, params, timeout=None):
            calls.append((command, params))
            if command == "get_state":
                return {"health": 0, "is_dead": True}
            return {}

        self.mock_client.transport.dispatch.side_effect = dispatch
        safety = SafetySystem(self.mock_client, self.mock_coordination)

        safety.tick()

        assert not any(command == "respawn" for command, _params in calls)
        event = self.mock_coordination.broadcast.call_args.args[0]
        assert event.event_type == EventType.PLAYER_DEATH

    def test_hunger_defers_while_combat_owns_item_use(self):
        ready = threading.Event()
        release = threading.Event()

        def hold_combat_action():
            with exclusive_combat_action(self.mock_client):
                ready.set()
                release.wait(timeout=2)

        owner = threading.Thread(target=hold_combat_action)
        owner.start()
        assert ready.wait(timeout=1)
        self.hunger_system.try_eat(10)
        release.set()
        owner.join(timeout=2)

        self.mock_client.transport.dispatch.assert_not_called()

    def test_combat_ownership_is_shared_by_clients_on_one_transport(self):
        transport = SimpleNamespace(dispatch=MagicMock())
        first = SimpleNamespace(transport=transport)
        second = SimpleNamespace(transport=transport)
        ready = threading.Event()
        release = threading.Event()

        def hold_first_client():
            with exclusive_combat_action(first):
                ready.set()
                release.wait(timeout=2)

        owner = threading.Thread(target=hold_first_client)
        owner.start()
        self.assertTrue(ready.wait(timeout=1))
        with exclusive_combat_action(second, blocking=False) as acquired:
            self.assertFalse(acquired)
        release.set()
        owner.join(timeout=2)
        self.assertFalse(owner.is_alive())

    def test_combat_defers_through_ambiguous_background_eating_release(self):
        quarantine_started = threading.Event()
        release = threading.Event()

        def dispatch(command, params, timeout=None):
            if command == "get_inventory":
                return {
                    "inventory": [
                        {"id": "minecraft:apple", "count": 2, "slot": 0}
                    ],
                    "offhand": [],
                    "selected_slot": 0,
                }
            if command == "use_item":
                return {}  # The bridge outcome is ambiguous after dispatch.
            return {}

        def hold_quarantine(seconds):
            if seconds >= 1.6:
                quarantine_started.set()
                release.wait(timeout=2)

        self.mock_client.transport.dispatch.side_effect = dispatch
        with patch(
            "baritone_client.common.combat_action.time.sleep",
            side_effect=hold_quarantine,
        ):
            owner = threading.Thread(target=self.hunger_system.try_eat, args=(10,))
            owner.start()
            self.assertTrue(quarantine_started.wait(timeout=1))
            with exclusive_combat_action(
                self.mock_client, blocking=False
            ) as acquired:
                self.assertFalse(acquired)
            release.set()
            owner.join(timeout=2)

        self.assertFalse(owner.is_alive())
        with exclusive_combat_action(self.mock_client, blocking=False) as acquired:
            self.assertTrue(acquired)
        self.mock_client.transport.dispatch.assert_any_call(
            "use_item", {"hand": "MAIN_HAND", "duration_ms": 1600}
        )

    def test_combat_defers_through_ambiguous_healing_release(self):
        quarantine_started = threading.Event()
        release = threading.Event()
        result = []

        def dispatch(command, params, timeout=None):
            if command == "get_state":
                return {"health": 5, "food_level": 20}
            if command == "use_item":
                return {}  # It may still have scheduled the future release.
            return {}

        def hold_quarantine(seconds):
            if seconds >= 2.5:
                quarantine_started.set()
                release.wait(timeout=2)

        self.mock_client.transport.dispatch.side_effect = dispatch
        with patch.object(
            combat, "count_item", return_value=1
        ), patch.object(combat, "select_item", return_value=True):
            sleep_patch = patch(
                "baritone_client.common.combat_action.time.sleep",
                side_effect=hold_quarantine,
            )
            with sleep_patch:
                owner = threading.Thread(
                    target=lambda: result.append(
                        combat.heal_if_needed(self.mock_client, threshold=10)
                    )
                )
                owner.start()
                self.assertTrue(quarantine_started.wait(timeout=1))
                with exclusive_combat_action(
                    self.mock_client, blocking=False
                ) as acquired:
                    self.assertFalse(acquired)
                release.set()
                owner.join(timeout=2)

        self.assertFalse(owner.is_alive())
        self.assertEqual(result, [True])
        with exclusive_combat_action(self.mock_client, blocking=False) as acquired:
            self.assertTrue(acquired)
        self.mock_client.transport.dispatch.assert_any_call(
            "use_item", {"hand": "MAIN_HAND", "duration_ms": 2500}
        )

    def test_malformed_present_snapshot_skip_count_fails_closed(self):
        self.assertEqual(combat._snapshot_skipped_count({}), 0)
        self.assertEqual(
            combat._snapshot_skipped_count({"skipped_count": "unknown"}), 1
        )
        self.assertEqual(combat._snapshot_skipped_count({"skipped_count": -1}), 1)

    def _clear_terrain_client(self):
        """A SimpleNamespace client with vine-free terrain and full health.

        Uses SimpleNamespace rather than self.mock_client: a bare MagicMock
        auto-creates a truthy attribute for any unset flag name, which would
        make SUPERVISED_COMBAT_FLAG/_DEFENSE_GUARD checks always read True.
        """
        def dispatch(command, params=None, timeout=None):
            if command == "get_state":
                return {"health": 15, "block_position": {"x": 0, "y": 64, "z": 0}}
            if command == "get_block":
                return {"id": "minecraft:air"}
            return {}

        return SimpleNamespace(transport=SimpleNamespace(dispatch=dispatch))

    def test_safety_system_calls_defend_or_flee_when_nothing_owns_combat(self):
        """The ambient defense gap: only goto's own tick called defend_or_flee,
        so mining, harvesting, and other stationary work had no threat check
        at all. Live A1 2026-09-06: 6 deaths in under two hours, every one
        with a shield equipped but never raised and a tool -- not a weapon --
        in hand, always mid stationary work.
        """
        client = self._clear_terrain_client()
        safety = SafetySystem(client, self.mock_coordination)

        with patch("baritone_client.common.combat.defend_or_flee") as mock_defend:
            safety.tick()

        mock_defend.assert_called_once_with(client)

    def test_safety_system_skips_defense_during_supervised_combat(self):
        """A fight already owns the decision; a second, uncoordinated
        evade/attack call against the same target must not fire alongside it.
        """
        from baritone_client.common.combat_melee import SUPERVISED_COMBAT_FLAG

        client = self._clear_terrain_client()
        setattr(client, SUPERVISED_COMBAT_FLAG, True)
        safety = SafetySystem(client, self.mock_coordination)

        with patch("baritone_client.common.combat.defend_or_flee") as mock_defend:
            safety.tick()

        mock_defend.assert_not_called()

    def test_safety_system_skips_defense_during_navigation_defense_callback(self):
        """goto's own periodic defense tick already covers this instant."""
        from baritone_client.common.navigation import _DEFENSE_GUARD

        client = self._clear_terrain_client()
        setattr(client, _DEFENSE_GUARD, True)
        safety = SafetySystem(client, self.mock_coordination)

        with patch("baritone_client.common.combat.defend_or_flee") as mock_defend:
            safety.tick()

        mock_defend.assert_not_called()

    def test_safety_system_defers_defense_while_combat_owns_item_use(self):
        """Mirrors test_hunger_defers_while_combat_owns_item_use: the same
        non-blocking lock must defer ambient defense too, not just eating.
        """
        client = self._clear_terrain_client()
        ready = threading.Event()
        release = threading.Event()

        def hold_combat_action():
            with exclusive_combat_action(client):
                ready.set()
                release.wait(timeout=2)

        owner = threading.Thread(target=hold_combat_action)
        owner.start()
        self.assertTrue(ready.wait(timeout=1))

        safety = SafetySystem(client, self.mock_coordination)
        with patch("baritone_client.common.combat.defend_or_flee") as mock_defend:
            safety.try_defend()
        release.set()
        owner.join(timeout=2)

        mock_defend.assert_not_called()

if __name__ == '__main__':
    unittest.main()


def _hunger_with(items):
    client = MagicMock()
    client.transport.dispatch = MagicMock(
        side_effect=lambda command, params, timeout=None: (
            {"inventory": items, "offhand": []} if command == "get_inventory" else {}
        )
    )
    return HungerSystem(client, MagicMock(spec=CoordinationHub))


def test_raw_beef_is_kept_for_cooking_until_genuinely_hungry():
    """Live A1 2026-09-28 ate every hunted beef raw at food 14-17."""
    hunger = _hunger_with([{"id": "minecraft:beef", "count": 3, "slot": 1}])
    eaten = []
    with patch(
        "baritone_client.automator.actions.EatAction",
        lambda food: SimpleNamespace(
            execute=lambda _c: eaten.append(food) or SimpleNamespace(success=True, message="")
        ),
    ):
        HungerSystem.try_eat.__wrapped__(hunger, 15)
        assert eaten == []
        HungerSystem.try_eat.__wrapped__(hunger, 12)
        assert eaten == ["minecraft:beef"]


def test_cooked_food_is_still_eaten_whenever_below_threshold():
    hunger = _hunger_with([
        {"id": "minecraft:beef", "count": 3, "slot": 1},
        {"id": "minecraft:bread", "count": 1, "slot": 2},
    ])
    eaten = []
    with patch(
        "baritone_client.automator.actions.EatAction",
        lambda food: SimpleNamespace(
            execute=lambda _c: eaten.append(food) or SimpleNamespace(success=True, message="")
        ),
    ):
        HungerSystem.try_eat.__wrapped__(hunger, 16)
    assert eaten == ["minecraft:bread"]


def _hunger_tick(screen, food):
    """One HungerSystem.tick() with the given open screen; returns what it did."""
    client = MagicMock()
    sent = []

    def dispatch(command, params, timeout=None):
        sent.append(command)
        if command == "get_state":
            return {"food_level": food}
        if command == "get_screen":
            return {"type": screen}
        if command == "get_inventory":
            return {"inventory": [{"id": "minecraft:bread", "count": 3, "slot": 8}], "offhand": [], "selected_slot": 8}
        return {}

    client.transport.dispatch = MagicMock(side_effect=dispatch)
    hunger = HungerSystem(client, MagicMock(spec=CoordinationHub))
    attempts = []
    hunger.try_eat = lambda level, **_kwargs: attempts.append(level)
    hunger.tick()
    return attempts, sent


def test_hunger_waits_while_a_chest_screen_is_open():
    """Live A1 2026-10-01: eating closed the chest a deposit was using."""
    attempts, _sent = _hunger_tick("GenericContainerScreenHandler", food=15)
    assert attempts == []


def test_hunger_eats_normally_with_only_the_player_inventory_open():
    for screen in ("InventoryMenu", "PlayerScreenHandler", ""):
        attempts, _sent = _hunger_tick(screen, food=15)
        assert attempts == [15], screen


def test_a_starving_bot_eats_even_with_a_chest_open():
    attempts, _sent = _hunger_tick("GenericContainerScreenHandler", food=4)
    assert attempts == [4]


def test_an_unreadable_screen_never_blocks_eating():
    client = MagicMock()

    def dispatch(command, params, timeout=None):
        if command == "get_state":
            return {"food_level": 12}
        raise RuntimeError("bridge hiccup")

    client.transport.dispatch = MagicMock(side_effect=dispatch)
    hunger = HungerSystem(client, MagicMock(spec=CoordinationHub))
    attempts = []
    hunger.try_eat = lambda level, **_kwargs: attempts.append(level)
    hunger.tick()
    assert attempts == [12]
