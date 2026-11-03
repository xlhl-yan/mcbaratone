"""
EndGame Automator - Main orchestrator for end-game automation.
"""

import time
from typing import Optional, Callable, Any
from .state_manager import StateManager, Phase
from .resource_manager import ResourceManager
from .phase_executor import PhaseExecutor, PhaseHandler
from .phase_verifier import PhaseVerifier
from .objective import ObjectivePlanner, default_objectives
from .adaptive_scheduler import AdaptiveScheduler
from .postgame_lifecycle import PersistentPostgameLifecycle
from .objective_survival import recover_survival_before_objective
from .pacing import hold_between_turns
from .progress_control import progression_fingerprint
from .coordination_hub import CoordinationHub, SystemEvent, EventType
from .systems import SafetySystem, HungerSystem, MappingSystem
from .telemetry import TelemetrySystem
from .strategic_state import StrategicState, strategy_for
from ..common.nether import find_nearest_portal
from ..common.tasks import PlayerDeathDetected, TaskResult
from ..actions.death_recovery_action import DeathRecoveryAction
from ..core.interfaces import ActionContext, ActionResult
from ..actions.base import BaseAction
from ..actions.suites import SUITES
from ..world_identity import WorldIdentity

class EndGameAutomator:
    """
    Main orchestrator for end-game automation.
    
    Coordinates:
    - State management (phases, checkpoints)
    - Resource tracking (inventory, requirements)
    - Phase execution (handlers, retries)
    
    Example:
        ```python
        from baritone_client import Client, TcpTransport
        from baritone_client.automator import EndGameAutomator
        
        transport = TcpTransport()
        client = Client(transport)
        
        automator = EndGameAutomator(client)
        automator.register_default_handlers()
        automator.run()
        ```
    """
    
    def __init__(
        self,
        client,
        checkpoint_dir: Optional[str] = None,
        auto_checkpoint: bool = True,
        checkpoint_interval: float = 60.0,
        screenshot_enabled: bool = True,
        world_seed_override: Optional[int] = None,
    ):
        """
        Initialize the automator.
        
        Args:
            client: Baritone client instance
            checkpoint_dir: Directory for checkpoint files
            auto_checkpoint: Whether to auto-save checkpoints
            checkpoint_interval: Seconds between auto-checkpoints
            screenshot_enabled: Capture screenshots on phase transitions
            world_seed_override: Authoritative seed when a dedicated server
                cannot expose it through the client bridge
        """
        self.client = client
        self.world_seed_override = world_seed_override
        self.state = StateManager(checkpoint_dir)
        self.resources = ResourceManager(client)
        self.resources.inventory_observer = self._observe_inventory
        self.coordination = CoordinationHub()
        self.systems = [
            SafetySystem(client, self.coordination, resources=self.resources, state_manager=self.state),
            HungerSystem(client, self.coordination, resources=self.resources),
            MappingSystem(client, self.coordination, resources=self.resources, state_manager=self.state)
        ]
        self.phase_verifier = PhaseVerifier(client, self.resources, self.state)
        self.executor = PhaseExecutor(
            client,
            self.resources,
            self.state,
            self.coordination,
            screenshot_enabled=screenshot_enabled,
            verifier=self.phase_verifier,
        )
        self.telemetry = TelemetrySystem(checkpoint_dir)

        # Goal-graph scheduler.  Replaces the linear phase walk; reconstructed
        # from the checkpoint in run().
        self.planner = ObjectivePlanner(default_objectives())
        self.scheduler = AdaptiveScheduler(client, self.resources, self.state)
        self.postgame = PersistentPostgameLifecycle(self.state, self.scheduler, client)
        self.strategy = StrategicState(self.state)
        self.auto_checkpoint = auto_checkpoint
        self.checkpoint_interval = checkpoint_interval
        self._last_checkpoint = 0.0
        self._running = False
        self._stall_reported = False
        
        # Callbacks
        self.on_phase_start: Optional[Callable[[Phase], None]] = None
        self.on_phase_complete: Optional[Callable[[Phase], None]] = None
        self.on_phase_fail: Optional[Callable[[Phase], None]] = None
        self.on_complete: Optional[Callable[[], None]] = None

    def _observe_inventory(self, inventory: dict[str, int]) -> None:
        """Retain inventory high-water marks until the next checkpoint write."""
        for item_id, count in inventory.items():
            self.state.inventory_observations[item_id] = max(
                self.state.inventory_observations.get(item_id, 0),
                int(count or 0),
            )
    
    def register_handler(self, phase: Phase, handler: PhaseHandler) -> None:
        """Register a phase handler."""
        self.executor.register_handler(phase, handler)
    
    def register_default_handlers(self) -> None:
        """Register all default phase handlers."""
        from .phases import (
            BridgeCheckHandler,
            SpawnBootstrapHandler,
            InitialGatheringHandler,
            BaseConstructionHandler,
            BootSequenceHandler,
            FoodAndIronHandler,
            EnchantingPipelineHandler,
            NetherAndBlazeHandler,
            VillagerInfraHandler,
            XpEngineHandler,
            IronFarmHandler,
            ToolPerfectionHandler,
            WorldUnlockHandler,
            MegabaseInitHandler,
            TerraformingHandler,
            CityBuildingHandler,
        )
        
        self.register_handler(Phase.BRIDGE_CHECK, BridgeCheckHandler())
        self.register_handler(Phase.SPAWN_BOOTSTRAP, SpawnBootstrapHandler())
        self.register_handler(Phase.INITIAL_GATHERING, InitialGatheringHandler())
        self.register_handler(Phase.BASE_CONSTRUCTION, BaseConstructionHandler())
        self.register_handler(Phase.BOOT_SEQUENCE, BootSequenceHandler())
        self.register_handler(Phase.FOOD_AND_IRON, FoodAndIronHandler())
        self.register_handler(Phase.ENCHANTING_PIPELINE, EnchantingPipelineHandler())
        self.register_handler(Phase.NETHER_AND_BLAZE, NetherAndBlazeHandler())
        self.register_handler(Phase.VILLAGER_INFRA, VillagerInfraHandler())
        self.register_handler(Phase.XP_ENGINE, XpEngineHandler())
        self.register_handler(Phase.IRON_FARM, IronFarmHandler())
        self.register_handler(Phase.TOOL_PERFECTION, ToolPerfectionHandler())
        self.register_handler(Phase.WORLD_UNLOCK, WorldUnlockHandler())
        self.register_handler(Phase.MEGABASE_INIT, MegabaseInitHandler())
        self.register_handler(Phase.TERRAFORM, TerraformingHandler())
        self.register_handler(Phase.CITY_BUILD, CityBuildingHandler())
        
    def _get_current_world_identity(self) -> Optional[WorldIdentity]:
        """Fetch stable world identity from bridge state telemetry."""
        try:
            state = self.client.transport.dispatch("get_state", {})
            transport = self.client.transport
            server_address = None
            host = getattr(transport, "host", None)
            port = getattr(transport, "port", None)
            if host:
                server_address = f"{host}:{port}" if port is not None else str(host)
            identity = WorldIdentity.from_state(state, server_address=server_address)
            if self.world_seed_override is not None and identity.seed is None:
                identity = WorldIdentity(
                    seed=int(self.world_seed_override),
                    world_name=identity.world_name,
                    server_address=identity.server_address,
                )
            return identity if identity.available else None
        except Exception:
            return None

    def _get_current_seed(self) -> Optional[int]:
        """Backward-compatible seed accessor."""
        identity = self._get_current_world_identity()
        return identity.seed if identity else None
    
    
    def configure_baritone(self):
        """Configure Baritone settings for safety and performance."""
        print("Configuring Baritone settings...")
        settings = [
            "assumeWalkOnLava false",
            "assumeWalkOnWater true",
            "avoidWater true", # Prevent pathing into deep water bodies
            "costLava 200", # Extremely high cost to avoid lava
            "allowParkour true",
            "allowSprint true",
            "maxFallHeightNoWater 3",
            "chatDebug false", # Reduce spam
            "freeLook true", # Allow looking around while pathing
            "allowVines false" # Prevent getting stuck in vines
        ]
        for setting in settings:
            self.client.transport.dispatch("chat", {"message": f"#set {setting}"})
            time.sleep(0.1)
            
        # Note: Lava avoidance handled by costLava setting above


    def load_or_start(self) -> Phase:
        """
        Load checkpoint or start fresh.
        
        Returns:
            Starting phase
        """
        identity = self._get_current_world_identity()
        self.state.bind_world_identity(identity)
        seed = identity.seed if identity else None
        if self.state.load_checkpoint(
            current_seed=seed,
            current_world_identity=identity,
        ):
            print(f"Resumed from checkpoint: {self.state.get_current_phase().name}")
            if seed:
                print(f"  World seed: {seed}")
            self._restore_planner()
            self._revalidate_completed_objectives()
        else:
            print("Starting fresh automation")
        self.strategy.refresh(current=self.state.get_current_phase(), completed=self.planner.completed_phases())

        try:
            from ..common.storage_catalog import seed_from_state

            seeded = seed_from_state(self.client, self.state)
            if seeded:
                print(f"Storage catalog loaded {seeded} checkpointed container landmark(s)")
        except Exception as exc:
            print(f"Storage catalog seed deferred: {exc}")

        try:
            from ..common.landmark_scanner import import_shared_landmarks

            imported = import_shared_landmarks(self.client, self.state)
            if imported:
                print(f"Imported {imported} shared fleet landmark(s) into locations")
        except Exception as exc:
            print(f"Shared landmark import deferred: {exc}")

        from ..common.home_surface import bind_home_surface
        bind_home_surface(self.client, self.state, controller_start=True)
        return self.state.get_current_phase()


    def _maintain_stalled_objective_graph(self) -> None:
        """Rearm a recoverable graph or perform one bounded survival pass."""
        from .stall_recovery import (
            maintain_stalled_survival,
            rearm_any_abandoned_objectives,
            rearm_recovered_survival_objectives,
            report_stall,
        )

        reopened = rearm_recovered_survival_objectives(
            self.planner,
            self.client,
        )
        if reopened:
            print(
                "SURVIVAL REPAIR: re-opened safe objectives: "
                + ", ".join(reopened)
            )
            self._persist_objective_progress()
            self._save_checkpoint()
            self._stall_reported = False
            return

        forced = rearm_any_abandoned_objectives(
            self.planner,
            self.state.custom_data,
            now=time.time(),
        )
        if forced:
            print(
                "OBJECTIVE REPAIR: nothing runnable; re-opened "
                "abandoned objectives: " + ", ".join(forced)
            )
            self._persist_objective_progress()
            self._save_checkpoint()
            self._stall_reported = False
            return

        # Only report a *terminal-looking* stall when the graph genuinely has
        # nothing left to re-open. rearm_any_abandoned_objectives is rate
        # limited, so during its cooldown it returns nothing even though the
        # graph will recover on the next pass. An external supervisor treats
        # the exact string "Automation stalled: no runnable objective remains."
        # as a terminal safety stop and refuses to relaunch, so printing it
        # during a cooldown permanently stops an agent for a condition that
        # self-heals in five minutes.
        from .objective import ObjStatus

        recoverable = any(
            objective.status in {ObjStatus.BLOCKED, ObjStatus.ABANDONED}
            for objective in self.planner.objectives
        )
        if recoverable:
            if not self._stall_reported:
                print(
                    "  Objective graph is stalled but recoverable; waiting for "
                    "the re-arm cooldown to expire."
                )
                self._stall_reported = True
        elif not self._stall_reported:
            report_stall(self.planner)
            self._stall_reported = True
        try:
            activity = maintain_stalled_survival(self.client)
            print(f"  Stalled survival hold: {activity}")
        except PlayerDeathDetected:
            # The outer loop owns death recovery so the exact grave and
            # pre-death inventory remain available.
            return
        time.sleep(5.0)


    def run(self, resume: bool = True, *, max_postgame_iterations: Optional[int] = None) -> bool:
        """
        Run the automation loop.
        
        Args:
            resume: Whether to attempt checkpoint resumption
            
        Returns:
            True if the terminal city objective is verified, False if stopped/failed.
            ``max_postgame_iterations`` is a test/one-shot seam; normal runs
            remain in postgame until ``stop()`` is called externally.
        """
        self._running = True
        
        if resume:
            self.load_or_start()
            
        # Baritone settings are applied by the spawn-bootstrap phase before
        # pathing starts.  Reapplying them here from the bridge worker can race
        # Baritone's tick thread (and has caused ConcurrentModificationException
        # crashes), especially when resuming directly into a later phase.
            
        # Initialize dynamic resources
        self.resources.initialize_recipes()

        from .stall_recovery import rearm_abandoned_objectives

        reopened = rearm_abandoned_objectives(
            self.planner,
            self.state.custom_data,
        )
        if reopened:
            print(
                "OBJECTIVE REPAIR: re-opened abandoned objectives for "
                f"runtime revision: {', '.join(reopened)}"
            )
            self._persist_objective_progress()
            self._save_checkpoint()
        
        # Record Spawn Location
        try:
             state = self.client.transport.dispatch("get_state", {})
             pos = state.get("block_position", {})
             if pos:
                 self.state.add_location("spawn", int(pos.get("x")), int(pos.get("y")), int(pos.get("z")), tags=["start"], client=self.client)
        except:
             pass
        # Start background systems
        for system in self.systems:
            system.start()
        print(f"\n{'#'*60}")
        print(f"#  EndGame Automator Started")
        print(f"#  Current Phase: {self.state.get_current_phase().name}")
        print(f"#  Overall Progress: {self.state.get_overall_progress()*100:.1f}%")
        print(f"{'#'*60}\n")
        print("\n".join(strategy_for(self).status_lines()) + "\n")
        try:
            postgame_iterations = 0
            while self._running:
                if self.planner.is_complete():
                    finished, postgame_iterations = (
                        self.postgame.run_completed_controller_turn(
                            self,
                            postgame_iterations,
                            max_postgame_iterations,
                        )
                    )
                    if finished:
                        return True
                    continue

                # Check for death and handle recovery before choosing a goal.
                if self._handle_death_recovery():
                    continue

                # Recover before an ordinary failure can turn into unsafe,
                # unrelated objective work.
                if not recover_survival_before_objective(self.client, self.state, self.strategy):
                    self._save_checkpoint()
                    hold_between_turns(self.client, duration=5.0)
                    continue

                # The adaptive scheduler may run one cooldown-protected local
                # farm action, or rank the dependency-ready objective frontier.
                decision = self.scheduler.next_step(self.planner)
                if decision.local_work:
                    print(decision.summary)
                    self._persist_objective_progress()
                    self._save_checkpoint()
                    continue

                obj = decision.objective
                if obj is None:
                    if decision.role_hold:
                        print(decision.summary)
                        hold_between_turns(self.client, duration=30.0)
                        continue
                    self._maintain_stalled_objective_graph()
                    continue

                phase = obj.phase
                self._stall_reported = False
                print(decision.summary)
                self._activate_objective(obj)

                if self.on_phase_start:
                    self.on_phase_start(phase)

                self.telemetry.start_timer(f"phase_{phase.name}")
                success = self.executor.execute_phase(phase)
                self.telemetry.stop_timer(f"phase_{phase.name}", success=success)

                if (
                    not success
                    and self.executor.interruption_reason
                    in {
                        "player_death",
                        "progress_recovery",
                        "survival_recovery",
                        "incremental_progress",
                        "pacing_hold",
                    }
                ):
                    interruption = self.executor.interruption_reason
                    if interruption != "pacing_hold":
                        self.planner.record_evidence(
                            obj, progression_fingerprint(self.state)
                        )
                    if interruption == "incremental_progress":
                        requeued = self.planner.mark_incremental_yield(
                            obj,
                            interruption,
                        )
                    elif interruption == "pacing_hold":
                        requeued = self.planner.mark_pacing_hold(
                            obj,
                            interruption,
                        )
                    else:
                        requeued = self.planner.mark_yielded(obj, interruption)
                    self._persist_objective_progress()
                    self._save_checkpoint()
                    if requeued:
                        print(
                            f"Phase {phase.name} yielded to {interruption}; "
                            f"recovery budget {obj.interruptions}/"
                            f"{obj.max_interruptions}."
                        )
                    else:
                        print(
                            f"Phase {phase.name} exhausted its recovery/no-progress "
                            "budget and was abandoned for this checkpoint."
                        )
                    if interruption in {"incremental_progress", "pacing_hold"}:
                        hold_between_turns(self.client, duration=30.0)
                    elif interruption == "survival_recovery":
                        time.sleep(max(1.0, min(5.0, self.executor.retry_delay)))
                    continue

                if success:
                    self.planner.record_evidence(
                        obj, progression_fingerprint(self.state)
                    )
                    self.planner.mark_done(obj)
                    self._record_verified_objective_completion(phase)
                    if self.on_phase_complete:
                        self.on_phase_complete(phase)

                    payload = self.state.get_phase_payload(phase)
                    if payload:
                        print(f"Phase {phase.name} data: {payload}")

                    self._persist_objective_progress()
                    self._maybe_checkpoint()
                else:
                    if self.on_phase_fail:
                        self.on_phase_fail(phase)

                    # Non-fatal: re-queue for a later pass, or abandon and move on to
                    # other objectives instead of killing the whole run.
                    self.planner.record_evidence(
                        obj, progression_fingerprint(self.state)
                    )
                    obj.last_failure = "phase_failed"
                    requeued = self.planner.mark_failed(obj)
                    if requeued:
                        print(f"\nPhase {phase.name} failed (attempt {obj.attempts}/"
                              f"{obj.max_attempts}); will retry after other objectives.")
                    else:
                        print(f"\nPhase {phase.name} abandoned after {obj.attempts} "
                              f"attempts; continuing with remaining objectives.")
                    self._persist_objective_progress()
                    self._save_checkpoint()

            return False
        finally:
             self.stop()
    def run_suite(self, suite_name: str) -> bool:
        """
        Run a specific test suite/mission action.
        
        Args:
            suite_name: Name of the suite (e.g. "T900")
            
        Returns:
            True if success
        """
        if suite_name not in SUITES:
            print(f"Error: Unknown suite '{suite_name}'")
            return False
            
        action_class = SUITES[suite_name]
        action = action_class()
        
        print(f"\n>>> Starting Suite: {suite_name} ({action.__class__.__name__})")
        
        self._running = True
        # Suites that need non-default Baritone settings should apply them as a
        # phase action before beginning pathing, rather than mutating settings
        # here while the Baritone tick thread may already be active.
        self.resources.initialize_recipes()
        
        # Start background systems
        for system in self.systems:
            system.start()
            
        try:
            context = ActionContext(
                client=self.client,
                state=self.state,
                resources=self.resources
            )
            
            result = action.execute(context)
            
            if result.success:
                print(f"\n>>> Suite {suite_name} COMPLETED SUCCESSFULLY!")
                return True
            else:
                reason = getattr(result, "reason", getattr(result, "message", ""))
                print(f"\n!!! Suite {suite_name} FAILED: {reason}")
                return False
                
        except KeyboardInterrupt:
            print("\nSuite execution interrupted.")
            return False
        except Exception as e:
            print(f"\nError executing suite: {e}")
            import traceback
            traceback.print_exc()
            return False
        finally:
            self.stop()
    
    def stop(self) -> None:
        """Stop the automation loop."""
        self._running = False
        for system in self.systems:
            system.stop()
        self._save_checkpoint()
        self.telemetry.write_report()
        self.telemetry.write_log()
    
    def _maybe_checkpoint(self) -> None:
        """Save checkpoint if interval elapsed."""
        if not self.auto_checkpoint:
            return
        
        now = time.time()
        if now - self._last_checkpoint >= self.checkpoint_interval:
            self._save_checkpoint()
            self._last_checkpoint = now
    
    def _save_checkpoint(self) -> None:
        """Save current state to checkpoint."""
        self.resources.refresh_inventory()
        inventory = self.resources.cached_inventory
        identity = self._get_current_world_identity()
        seed = identity.seed if identity else None
        path = self.state.save_checkpoint(
            inventory, world_seed=seed, world_identity=identity
        )
        print(f"Checkpoint saved: {path}")

    _COMPLETION_ATTESTATION_VERSION = 1

    @staticmethod
    def _attested_completed_phases(state: StateManager) -> set[Phase]:
        """Return objectives that already passed the live phase verifier.

        Re-querying a remote or unloaded homestead reports air/void. Treating
        that as failed evidence reopened BOOT and BASE after every restart and
        made bots build another house. Only unattested legacy completions need
        startup live revalidation.
        """
        raw = state.custom_data.get("verified_objective_completions", {})
        if not isinstance(raw, dict):
            return set()
        attested = set()
        for name, evidence in raw.items():
            if not isinstance(evidence, dict):
                continue
            try:
                phase = Phase[name]
                version = int(evidence.get("version", 0) or 0)
            except (KeyError, TypeError, ValueError):
                continue
            if version >= EndGameAutomator._COMPLETION_ATTESTATION_VERSION:
                attested.add(phase)
        return attested

    def _record_verified_objective_completion(self, phase: Phase) -> None:
        """Persist proof that the executor's postcondition gate passed."""
        records = self.state.custom_data.setdefault(
            "verified_objective_completions", {}
        )
        if not isinstance(records, dict):
            records = {}
            self.state.custom_data["verified_objective_completions"] = records
        records[phase.name] = {
            "version": self._COMPLETION_ATTESTATION_VERSION,
        }

    def _restore_planner(self) -> None:
        """Rebuild objective statuses from the resumed checkpoint.

        Prefers the explicit completed-objective set (correct even when objectives
        finished out of enum order); falls back to a linear reconstruction for old
        checkpoints that only persisted ``current_phase``.
        """
        names = self.state.custom_data.get("completed_objectives")
        if names:
            completed = []
            for name in names:
                try:
                    completed.append(Phase[name])
                except KeyError:
                    continue  # phase renamed/removed across versions -> ignore
            self.planner.restore(
                completed,
                runtime=self.state.custom_data.get("objective_runtime"),
            )
        else:
            self.planner.restore_linear(self.state.get_current_phase())

    def _revalidate_completed_objectives(self) -> None:
        """Drop stale pre-verifier completions that no longer have evidence."""
        if not self.state.has_durable_inventory_observations:
            print(
                "CHECKPOINT AUDIT: preserving legacy objective completion "
                "until durable inventory observations are available"
            )
            return
        completed = self.planner.completed_phases()
        attested = EndGameAutomator._attested_completed_phases(self.state)
        valid = {
            phase
            for phase in (Phase.BRIDGE_CHECK, Phase.SPAWN_BOOTSTRAP)
            if phase in completed
        }
        removed = []
        retained = []
        for objective in self.planner.objectives:
            phase = objective.phase
            if phase not in completed or phase in valid:
                continue
            if not set(objective.requires).issubset(valid):
                removed.append((phase, "prerequisite evidence is missing"))
                continue
            if phase in attested:
                valid.add(phase)
                continue
            verification = self.phase_verifier.verify(
                phase,
                TaskResult.ok("checkpoint revalidation"),
            )
            if verification.success:
                valid.add(phase)
            elif verification.evidence_unreadable:
                # The world could not be read, so nothing was actually
                # observed. Revoking a finished objective on a coordinate in
                # an unloaded chunk is how a bot that logged out away from
                # base wiped its own progression graph and rebuilt work it had
                # already done. Keep the credit and re-check when it is nearer.
                valid.add(phase)
                retained.append((phase, verification.reason))
            else:
                removed.append((phase, verification.reason))
        if retained:
            print("CHECKPOINT AUDIT: kept objectives whose evidence was unreadable:")
            for phase, reason in retained:
                print(f"  - {phase.name}: {reason}")
        if not removed:
            return
        self.planner.restore(
            valid,
            runtime=self.state.custom_data.get("objective_runtime"),
        )
        for phase, _reason in removed:
            self.state.update_progress(0.0, phase=phase)
        self.state.custom_data["completed_objectives"] = [
            phase.name for phase in valid
        ]
        runnable = self.planner.select(self.planner.runnable())
        if runnable is not None:
            self.state.set_phase(runnable.phase)
        print("CHECKPOINT AUDIT: removed unverified objective completion:")
        for phase, reason in removed:
            print(f"  - {phase.name}: {reason}")

    def _persist_objective_progress(self) -> None:
        """Record completed objectives into custom_data so they survive a restart."""
        self.state.custom_data["completed_objectives"] = [
            p.name for p in self.planner.completed_phases()
        ]
        self.state.custom_data["objective_runtime"] = self.planner.runtime_state()
        strategy_for(self).refresh(current=self.state.get_current_phase(), completed=self.planner.completed_phases())

    def _activate_objective(self, objective) -> None:
        """Persist ownership and its consumed attempt before gameplay starts."""
        self.planner.mark_active(objective)
        self.state.set_phase(objective.phase)
        self._persist_objective_progress()
        self._save_checkpoint()

    def _handle_death_recovery(self) -> bool:
        """Handle death and stop the run if recovery cannot be proven safe."""
        # Use modular death recovery action
        context = ActionContext(
            client=self.client,
            state=self.state,  # StateManager instance
            resources=self.resources
        )

        action = DeathRecoveryAction()
        result = action.execute(context)

        if result.success and result.message == "No death detected":
            return False

        if not result.success:
            # Previously this returned True, causing one loop iteration to be
            # skipped; on the following iteration the alive player resumed the
            # phase with a partial inventory.  A failed recovery is a hard
            # terminal condition for this supervised run.
            print(f"Death recovery failed: {result.message}. Stopping automation.")
            try:
                summary = self.resources.get_summary()
                self.state.save_checkpoint(summary.get("inventory", {}))
            except Exception as exc:
                print(f"Warning: Could not persist death recovery state: {exc}")
            self._running = False
            return True

        # A successful recovery still skips the interrupted phase iteration so
        # every phase starts from a fresh state/inventory snapshot.
        return True

    def get_status(self) -> dict:
        """Get current automation status."""
        return {
            "running": self._running,
            "current_phase": self.state.get_current_phase().name,
            "phase_index": self.state.get_phase_index(),
            "total_phases": self.state.get_total_phases(),
            "phase_progress": self.state.get_progress(),
            "overall_progress": self.state.get_overall_progress(),
            "resources": self.resources.get_summary(),
            "adaptive_scheduler": self.state.custom_data.get("adaptive_scheduler", {}),
            "strategic_state": dict(strategy_for(self).data),
        }
