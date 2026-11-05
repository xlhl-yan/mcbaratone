
import threading
import time
import logging
from abc import ABC, abstractmethod
from typing import Optional

from .coordination_hub import CoordinationHub, SystemEvent, EventType
from ..common.landmark_scanner import scan_visible_landmarks
from ..common.runtime_artifacts import runtime_artifact_path
from ..common.combat_action import exclusive_client_action

logger = logging.getLogger(__name__)

class BackgroundSystem(ABC):
    """
    Abstract base class for parallel background systems.
    Runs in a separate thread to monitor state or perform tasks concurrently
    with the main phase execution loop.
    """
    
    
    def __init__(self, client, coordination_hub: CoordinationHub, name: str, interval: float = 1.0, resources=None):
        self.client = client
        self.coordination = coordination_hub
        self.name = name
        self.interval = interval
        self.resources = resources
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start the background system loop."""
        if self._running:
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._loop, name=f"System-{self.name}", daemon=True)
        self._thread.start()
        logger.info(f"Started system: {self.name}")

    def stop(self):
        """Stop the background system loop."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info(f"Stopped system: {self.name}")

    def _loop(self):
        while self._running:
            try:
                self.tick()
            except Exception as e:
                logger.error(f"Error in {self.name}: {e}")
            
            # Simple sleep for interval (subtracting execution time could be better but this is fine)
            time.sleep(self.interval)

    @abstractmethod
    def tick(self):
        """Perform one iteration of the system logic."""
        pass


class SafetySystem(BackgroundSystem):
    """
    Monitors player vitals and safety.
    - Checks health
    - Equips armor (todo)
    - Emits critical events
    """
    
    def __init__(
        self,
        client,
        coordination_hub: CoordinationHub,
        resources=None,
        state_manager=None,
    ):
        super().__init__(client, coordination_hub, "SafetySystem", interval=1.0, resources=resources)
        self.state_manager = state_manager
        self.low_health_threshold = 8.0  # 4 hearts
        self._last_health = 20.0

    def tick(self):
        try:
            # Poll player state - transport is thread-safe via RLock
            state = self.client.transport.dispatch("get_state", {}, timeout=1.0)

            if self.state_manager is not None:
                # Refresh the protected surface anchor from checkpointed
                # state before ambient defense can select a flee destination.
                from ..common.home_surface import bind_home_surface

                bind_home_surface(self.client, self.state_manager)
            
            health = state.get("health", 20.0)
            
            # Check for death
            if health <= 0:
                # Foreground recovery owns respawn so it can retain grave
                # evidence and coordinate item recovery.
                logger.warning("Player is dead. Signaling foreground recovery.")
                self.coordination.broadcast(SystemEvent(
                    event_type=EventType.PLAYER_DEATH,
                    source=self.name,
                    data={"death": True}
                ))
                self._last_health = health
                return
            
            # Broadcast critical event if health is low
            elif health < self.low_health_threshold and self._last_health >= self.low_health_threshold:
                logger.warning(f"Health critical: {health}")
                self.coordination.broadcast(SystemEvent(
                    event_type=EventType.HEALTH_CRITICAL,
                    source=self.name,
                    data={"health": health, "threshold": self.low_health_threshold}
                ))
            
            self._last_health = health

            # Check for physical entanglements (vines, webs)
            self._clear_entanglements(state)

            # Ambient defense: only goto()'s own periodic tick calls
            # defend_or_flee today, so mining, harvesting, and other
            # stationary work had zero threat-checking of their own -- a
            # hostile could close in and land several free hits before any
            # other system reacted. Live A1 2026-09-06: 6 deaths in under two
            # hours, every one with a shield equipped but never raised and a
            # tool (not a weapon) in hand, always mid stationary work rather
            # than inside a supervised goto or combat loop.
            self.try_defend()

        except Exception as e:
            logger.error(f"Safety Check Failed: {e}")

    @exclusive_client_action
    def try_defend(self):
        """Advance the shared defense state machine once, if nothing else
        currently owns it.

        Skips while a supervised combat loop (combat_melee.execute_safe_combat)
        is already handling a fight, so this never issues a second,
        uncoordinated evade/attack decision against the same target.
        exclusive_client_action's non-blocking acquire additionally defers
        to any in-progress item-use action (e.g. eating, raising a shield).
        """
        from ..common.combat import defend_or_flee
        from ..common.combat_melee import SUPERVISED_COMBAT_FLAG
        from ..common.navigation import _DEFENSE_GUARD

        if getattr(self.client, SUPERVISED_COMBAT_FLAG, False):
            return
        if getattr(self.client, _DEFENSE_GUARD, False):
            return
        try:
            defend_or_flee(self.client)
        except Exception as e:
            logger.error(f"Defense Check Failed: {e}")

    def _clear_entanglements(self, state: dict):
        """Check if stuck in vines/webs and clear them."""
        try:
            # Only run if not pathing (or pathing very slowly)
            # Actually, if we are in vines, we WANT to break them even if moving.
            
            pos = state.get("block_position", {})
            px, py, pz = int(pos.get("x", 0)), int(pos.get("y", 0)), int(pos.get("z", 0))
            
            # Check feet and head
            # We need to use 'get_block' command from bridge
            # Note: This increases bridge traffic. Maybe throttle?
            # Interval is 1.0s, acceptable.
            
            # We can't synchronously get_block here easily without blocking tick?
            # BackgroundSystem handles blocking fine (threaded).
            
            # Check 2 blocks
            to_check = [(px, py, pz), (px, py+1, pz)]
            
            vine_types = {"minecraft:vine", "minecraft:cave_vines", "minecraft:twisting_vines", "minecraft:weeping_vines", "minecraft:cobweb"}
            
            for bx, by, bz in to_check:
                # Dispatch get_block
                # We can batch or just do one by one.
                # Since we are in threading, we can use client.transport
                resp = self.client.transport.dispatch("get_block", {"x": bx, "y": by, "z": bz})
                if resp and resp.get("id") in vine_types:
                    logger.warning(f"Entangled in {resp.get('id')} at {bx},{by},{bz}. Cutting free!")
                    # Attack block
                    self.client.transport.dispatch("attack_block", {"x": bx, "y": by, "z": bz})
                    # Also swing hand for visual
                    self.client.transport.dispatch("swing_hand", {})
                    # Break only one per tick to avoid spam?
                    # No, break both if needed.
        except Exception as e:
            logger.warning(f"Entanglement check error: {e}")


class HungerSystem(BackgroundSystem):
    """
    Monitors food level and manages eating.
    """
    def __init__(self, client, coordination_hub: CoordinationHub, resources=None):
        super().__init__(client, coordination_hub, "HungerSystem", interval=2.0, resources=resources)
        self.min_food_level = 18  # Threshold to start eating
        self._last_food = 20
        self.food_priority = [
            "minecraft:golden_apple",
            "minecraft:enchanted_golden_apple",
            "minecraft:cooked_beef",
            "minecraft:cooked_porkchop",
            "minecraft:steak",
            "minecraft:cooked_mutton",
            "minecraft:cooked_chicken",
            "minecraft:cooked_rabbit",
            "minecraft:cooked_salmon",
            "minecraft:cooked_cod",
            "minecraft:bread",
            "minecraft:baked_potato",
            "minecraft:pumpkin_pie",
            "minecraft:mushroom_stew",
            "minecraft:beetroot_soup",
            "minecraft:rabbit_stew",
            "minecraft:apple",
            "minecraft:carrot",
            "minecraft:potato",  # Raw potato - low hunger restore
            "minecraft:melon_slice",
            "minecraft:sweet_berries",
            "minecraft:glow_berries",
            "minecraft:dried_kelp",
            # Raw meats - not ideal but better than starving
            "minecraft:beef",
            "minecraft:porkchop", 
            "minecraft:mutton",
            "minecraft:chicken",  # 30% chance of hunger effect
            "minecraft:rabbit",
            "minecraft:salmon",
            "minecraft:cod",
            # Absolute last resort - has negative effects
            "minecraft:rotten_flesh",  # 80% chance of hunger effect
            "minecraft:spider_eye",    # Poison - avoid if possible
        ]
        # Foods to avoid eating unless starving (food_level <= 2)
        self.desperate_only_foods = {
            "minecraft:rotten_flesh",
            "minecraft:spider_eye",
            "minecraft:poisonous_potato",
            "minecraft:pufferfish",
            "minecraft:chicken",  # Raw chicken has hunger chance
        }
        # Raw meat the livestock cycle is about to cook: eaten raw only when
        # genuinely hungry. Live A1 2026-09-28 ate each hunted beef raw at
        # food 14-17 (3 food instead of 8), so the cycle banked nothing and
        # the prepared reserve sat at 0/32 beside a visible herd.
        self.cook_first_foods = {"minecraft:beef", "minecraft:porkchop", "minecraft:mutton", "minecraft:rabbit"}
        self.raw_meat_food_floor = 12

    #: Below this the bot eats even if it must close a foreground screen.
    HUNGER_URGENT = 6
    _PLAYER_SCREENS = ("", "InventoryMenu", "PlayerScreenHandler")

    def _container_open(self) -> bool:
        """A chest, furnace or crafting screen is open: the foreground is mid-transaction."""
        try:
            screen = self.client.transport.dispatch("get_screen", {})
        except Exception:
            return False
        data = screen.get("data", screen) if isinstance(screen, dict) else {}
        kind = str(data.get("type") or "") if isinstance(data, dict) else ""
        return kind not in self._PLAYER_SCREENS

    def tick(self):
        try:
            state = self.client.transport.dispatch("get_state", {}, timeout=1.0)
            
            food_level = state.get("food_level", state.get("food", 20))
            
            # Check if we need to eat. Eating starts by closing any open screen
            # to get a stable inventory layout, which slams shut the chest a
            # foreground deposit or withdrawal is using (live A1 2026-10-01:
            # "no verified transfer" on every stack, then a cave chest and a
            # death). So wait a few seconds while a container is open, unless
            # the bot is actually starving.
            if food_level < self.min_food_level and not (
                food_level > self.HUNGER_URGENT and self._container_open()
            ):
                self.try_eat(food_level, health=state.get("health"))

            # Broadcast critical event if food is VERY low
            crit_threshold = 6
            if food_level < crit_threshold and self._last_food >= crit_threshold:
                logger.warning(f"Hunger critical: {food_level}")
                self.coordination.broadcast(SystemEvent(
                    event_type=EventType.HUNGER_CRITICAL,
                    source=self.name,
                    data={"food_level": food_level, "threshold": crit_threshold}
                ))
            
            self._last_food = food_level
            
        except Exception as e:
            logger.error(f"Hunger Check Failed: {e}")

    @exclusive_client_action
    def try_eat(self, current_food: int, *, health=None):
        """Attempt to find food and eat it."""
        from .actions import EatAction
        
        # We need to find the best food available
        # Refresh inventory cache if possible or just fetch it
        # Since we're in a background thread, strict cache sync with main thread isn't guaranteed
        # But we can query the bridge directly.
        
        try:
            inventory_data = self.client.transport.dispatch("get_inventory", {})
            from .hunger_food import choose_food

            best_food = choose_food(
                inventory_data, self.food_priority, self.desperate_only_foods,
                self.cook_first_foods, current_food, health,
                raw_meat_food_floor=self.raw_meat_food_floor,
            )
            
            if best_food:
                if best_food in self.desperate_only_foods:
                    logger.warning(f"HungerSystem: Desperately eating {best_food} (Food: {current_food})")
                else:
                    logger.info(f"HungerSystem: Eating {best_food} (Food: {current_food})")
                action = EatAction(best_food)
                result = action.execute(self.client)
                
                if result.success:
                    logger.info(f"HungerSystem: Finished eating {best_food}")
                else:
                    logger.warning(f"HungerSystem: Failed to eat {best_food}: {result.message}")
            else:
                # No food found in priority list
                logger.debug("HungerSystem: No suitable food found in inventory")
                pass

        except Exception as e:
            logger.error(f"Error in try_eat: {e}")

class MappingSystem(BackgroundSystem):
    """
    Passively scans environment for POIs and generates a world map report.
    """
    def __init__(
        self,
        client,
        coordination_hub: CoordinationHub,
        resources=None,
        state_manager=None,
        landmark_scan_interval: float = 60.0,
    ):
        super().__init__(client, coordination_hub, "MappingSystem", interval=5.0, resources=resources)
        self.state_manager = state_manager
        if self.state_manager:
            # Load visited
            data = self.state_manager.custom_data.get("mapping", {})
            self.visited_chunks = {tuple(x) for x in data.get("visited", [])}
        else:
            self.visited_chunks = set()
            
        self.path_history = []  # List of (x, z)
        self.last_update = 0
        self.landmark_scan_interval = max(5.0, float(landmark_scan_interval))
        # Landmark discovery is passive, expensive work.  Delay the first scan
        # so controller startup and resumed phase work own the bridge first.
        self.last_landmark_scan = time.time()
        self.last_landmark_chunk: Optional[tuple[int, int]] = None
        self.map_file = runtime_artifact_path("world_map.md", state_manager)

    def tick(self):
        try:
            state = self.client.transport.dispatch("get_state", {})
            
            # 1. Update Position
            pos = state.get("block_position", state.get("position", {}))
            px = int(pos.get("x", state.get("x", 0)))
            py = int(pos.get("y", state.get("y", 64)))
            pz = int(pos.get("z", state.get("z", 0)))
            
            chunk_x, chunk_z = px // 16, pz // 16
            
            # Record visitation
            if (chunk_x, chunk_z) not in self.visited_chunks:
                self.visited_chunks.add((chunk_x, chunk_z))
                if self.state_manager:
                     # Persist periodically (or on prompt, but here lazy save)
                     self.state_manager.custom_data.setdefault("mapping", {})["visited"] = list(self.visited_chunks)
            
            # Record path (sparse: only if moved > 5 blocks)
            if not self.path_history:
                self.path_history.append((px, pz))
            else:
                lx, lz = self.path_history[-1]
                dist = ((px - lx)**2 + (pz - lz)**2)**0.5
                if dist > 5:
                    self.path_history.append((px, pz))
                    
            # Update Map File every 10s
            if time.time() - self.last_update > 10.0:
                 self._write_map_file(px, py, pz, state.get("dimension", "Overworld"))
                 self.last_update = time.time()

            now = time.time()
            current_chunk = (chunk_x, chunk_z)
            if (
                current_chunk != self.last_landmark_chunk
                and now - self.last_landmark_scan >= self.landmark_scan_interval
            ):
                # Record the attempted chunk before dispatch.  A transient or
                # bounded scan failure must not repeatedly monopolize the
                # bridge while the foreground controller is stationary.
                self.last_landmark_scan = now
                self.last_landmark_chunk = current_chunk
                found = scan_visible_landmarks(self.client, self.state_manager)
                if found:
                    logger.info("MappingSystem recorded %d visible landmark blocks", found)
                 
        except Exception as e:
            logger.error(f"Mapping Failed: {e}")

    def _write_map_file(self, px, py, pz, dimension):
        """Generate formatted Markdown map report."""
        try:
            with open(self.map_file, "w", encoding='utf-8') as f:
                f.write(f"# 🗺️ World Map Report\n")
                f.write(f"**Timestamp:** {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"**Current Position:** X={px}, Y={py}, Z={pz} ({dimension})\n\n")
                
                # Known Locations
                f.write("## 📍 Points of Interest\n")
                locations = self.state_manager.get_locations() if self.state_manager else {}
                
                if not locations:
                    f.write("*No confirmed POIs yet.*\n")
                else:
                    for category, locs in locations.items():
                        f.write(f"### {category.title()}\n")
                        for loc in locs:
                            f.write(f"- ({loc['x']}, {loc['y']}, {loc['z']}) in {loc['dimension']} - {time.strftime('%H:%M', time.localtime(loc['timestamp']))}\n")
                
                f.write("\n## 🧭 Exploration Log\n")
                f.write(f"- **Visited Chunks:** {len(self.visited_chunks)}\n")
                f.write(f"- **Path Length:** {len(self.path_history)} points\n\n")
                
                # Recent Path
                f.write("### Recent Path (Last 10 points)\n")
                for x, z in self.path_history[-10:]:
                    f.write(f"- ({x}, {z})\n")
                    
        except Exception as e:
            logger.error(f"Failed to write map file: {e}")
