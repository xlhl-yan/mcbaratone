"""State-driven scheduling and bounded local farming opportunities.

The objective graph remains authoritative. This module supplies preference,
never completion: every phase must still pass its handler and verifier.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from ..common.combat import _threat_can_reach_player, get_nearby_entities
from ..common.defense import assess_threats
from ..common.husbandry import BREEDING_FOOD, breed_pair
from ..common.forestry import run_wood_cycle
from ..common.inventory import get_inventory
from ..common.navigation import find_nearby_block, goto
from ..common.storage_organizer import run_quartermaster_cycle
from .end_readiness import (
    FleetRole, allows_local_work, fleet_role, record_readiness,
    role_focused_candidates,
)
from .fleet_coverage import borrowed_specialty_roles, configured_specialty_roles
from . import aid_response, armor_upkeep, camp_breaker, food_opportunity, house_upkeep, mutual_aid
from .common.role_opportunities import run_role_opportunity
from .common.crop_opportunity import run_crop_opportunity
from .iron_scheduler import run_scheduled_iron_cycle
from .local_opportunity import (
    LocalOpportunity,
    OpportunityKind,
    local_work_blockers,
    local_work_hold_reason,
    record_opportunity_result,
)
from .safety_recovery import run_self_defense
from .objective import Objective
from .state_manager import Phase
from .specialty_scheduler import select_profile_specialty_opportunities, select_specialty_opportunity
from .work_progress import productive_snapshot, record_productive_attempt
from .work_retry import retry_cooldown
CROP_BLOCKS = (
    "minecraft:wheat",
    "minecraft:carrots",
    "minecraft:potatoes",
    "minecraft:beetroots",
)
CROP_ITEMS = (
    "minecraft:wheat",
    "minecraft:carrot",
    "minecraft:potato",
    "minecraft:beetroot",
)
PLANTABLE_ITEMS = (
    "minecraft:wheat_seeds",
    "minecraft:carrot",
    "minecraft:potato",
    "minecraft:beetroot_seeds",
)
_ROLE_OPPORTUNITY_KINDS = frozenset({OpportunityKind.END_SUPPLY, OpportunityKind.NETHER_SUPPLY,
    OpportunityKind.ENCHANTING_XP, OpportunityKind.DIMENSION_ENTRY, OpportunityKind.ENCHANTING_MATERIAL,
    OpportunityKind.END_FRONTIER, OpportunityKind.END_CITY_ROUTE})
LEATHER_ANIMALS = {
    "cow", "mooshroom", "horse", "donkey", "mule", "llama",
}
@dataclass(frozen=True)
class GameSignals:
    """One best-effort snapshot used for a single scheduling decision."""

    observed: bool = False
    entities_observed: bool = False
    dimension: str = ""
    difficulty: str = ""
    biome: str = ""
    health: float = 0.0
    food: int = 0
    experience_level: int = 0
    world_time: int = 0
    position: Tuple[int, int, int] = (0, 64, 0)
    inventory: Mapping[str, int] = field(default_factory=dict)
    animals: Mapping[str, int] = field(default_factory=dict)
    adult_animals: Mapping[str, int] = field(default_factory=dict)
    adult_villagers: int = 0
    nearby_hostiles: int = 0
    crop_location: Optional[Tuple[int, int, int]] = None
    crop_block: str = ""
    known_farm_location: Optional[Tuple[int, int, int]] = None
    known_portal: bool = False

    def count(self, item_id: str) -> int:
        """Return an inventory count without trusting value types."""
        try:
            return max(0, int(self.inventory.get(item_id, 0) or 0))
        except (TypeError, ValueError):
            return 0

    def animal_pairs(self) -> Dict[str, int]:
        """Breedable adult pairs whose food is currently carried."""
        pairs: Dict[str, int] = {}
        for family, count in self.adult_animals.items():
            food = BREEDING_FOOD.get(family)
            if food and int(count) >= 2 and self.count(food) >= 2:
                pairs[family] = int(count) // 2
        return pairs

    def local_work_blockers(self) -> Tuple[str, ...]:
        """Name every condition currently preventing local side work."""
        return local_work_blockers(self)

    @property
    def safe_for_local_work(self) -> bool:
        """Local side work is allowed only with a comfortable safety margin."""
        return not self.local_work_blockers()


@dataclass(frozen=True)
class PhaseScore:
    """Explainable signal contribution to one runnable phase."""

    value: float
    reasons: Tuple[str, ...] = ()


@dataclass(frozen=True)
class OpportunityResult:
    """Evidence recorded after a local opportunity attempt."""

    opportunity: LocalOpportunity
    success: bool
    detail: str
    before: int = 0
    after: int = 0


@dataclass(frozen=True)
class SchedulingDecision:
    """One scheduler turn: either local work or a runnable objective."""

    objective: Optional[Objective] = None
    opportunity_result: Optional[OpportunityResult] = None
    score: float = 0.0
    reasons: Tuple[str, ...] = ()
    role_hold: bool = False

    @property
    def local_work(self) -> bool:
        return self.opportunity_result is not None

    @property
    def summary(self) -> str:
        if self.opportunity_result is not None:
            result = self.opportunity_result
            status = "verified" if result.success else "deferred"
            return (
                f"ADAPTIVE WORK: {result.opportunity.kind.value} {status}: "
                f"{result.opportunity.reason} ({result.detail})"
            )
        if self.objective is None:
            if self.reasons:
                return f"END READINESS HOLD: {'; '.join(self.reasons)}"
            return "ADAPTIVE PHASE: no runnable objective"
        explanation = "; ".join(self.reasons) or "stable graph fallback"
        return (
            f"ADAPTIVE PHASE: selected {self.objective.phase.name} "
            f"(score={self.score:.1f}; {explanation})"
        )


def _unwrap(response: Any) -> Mapping[str, Any]:
    if not isinstance(response, Mapping):
        return {}
    data = response.get("data", response)
    return data if isinstance(data, Mapping) else {}


def _position(snapshot: Mapping[str, Any]) -> Tuple[int, int, int]:
    raw = snapshot.get("block_position", snapshot.get("position", {}))
    raw = raw if isinstance(raw, Mapping) else {}
    try:
        return (
            int(float(raw.get("x", 0) or 0)),
            int(float(raw.get("y", 64) or 64)),
            int(float(raw.get("z", 0) or 0)),
        )
    except (TypeError, ValueError):
        return (0, 64, 0)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coordinate(value: Any) -> Optional[Tuple[int, int, int]]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    try:
        return tuple(int(float(part)) for part in value)  # type: ignore[return-value]
    except (TypeError, ValueError):
        return None


def _known_farm(state: Any) -> Optional[Tuple[int, int, int]]:
    custom = getattr(state, "custom_data", {}) or {}
    direct = _coordinate(custom.get("farm_location"))
    if direct is not None:
        return direct
    wheat_farm = custom.get("wheat_farm", {})
    if isinstance(wheat_farm, Mapping):
        origin = _coordinate(wheat_farm.get("origin"))
        if origin is not None:
            return origin
    structures = custom.get("structures", {})
    if isinstance(structures, Mapping):
        source = structures.get("food_source", {})
        if isinstance(source, Mapping):
            return _coordinate(source.get("location"))
    return None


def _known_portal(state: Any) -> bool:
    custom = getattr(state, "custom_data", {}) or {}
    if custom.get("nether_portal"):
        return True
    locations = custom.get("locations", {})
    return isinstance(locations, Mapping) and bool(locations.get("nether_portal"))


def _family(entity_type: object) -> str:
    path = str(entity_type or "").lower().split(":", 1)[-1]
    for family in BREEDING_FOOD:
        if path == family or path.endswith(f"_{family}"):
            return family
    return ""


def collect_game_signals(client: Any, resources: Any, state: Any) -> GameSignals:
    """Collect live scheduling inputs, falling back to fixed priorities on error."""
    try:
        snapshot = _unwrap(client.transport.dispatch("get_state", {}))
    except Exception:
        return GameSignals()
    if not snapshot or snapshot.get("error"):
        return GameSignals()

    try:
        inventory = resources.refresh_inventory()
    except Exception:
        inventory = getattr(resources, "cached_inventory", {}) or {}
    inventory = inventory if isinstance(inventory, Mapping) else {}

    try:
        entities = get_nearby_entities(client, radius=24, raise_on_error=True)
        entities_observed = True
    except Exception:
        entities = []
        entities_observed = False

    animals: Dict[str, int] = {}
    adult_animals: Dict[str, int] = {}
    villagers = 0
    for entity in entities:
        entity_type = str(entity.get("type", "")).lower()
        if entity_type == "minecraft:villager" and not bool(entity.get("is_baby")):
            villagers += 1
        family = _family(entity_type)
        if family:
            animals[family] = animals.get(family, 0) + 1
            if not bool(entity.get("is_baby")):
                adult_animals[family] = adult_animals.get(family, 0) + 1

    try:
        threats = [threat for threat in assess_threats(entities, dict(snapshot)) if _threat_can_reach_player(threat.entity, dict(snapshot))]
    except Exception:
        threats = []

    dimension = str(snapshot.get("dimension", ""))
    crop_location = None
    if "overworld" in dimension:
        crop_location = find_nearby_block(client, list(CROP_BLOCKS), radius=24)
    crop_block = ""
    if crop_location is not None:
        try:
            crop_block = str(
                _unwrap(
                    client.transport.dispatch(
                        "get_block",
                        {
                            "x": crop_location[0],
                            "y": crop_location[1],
                            "z": crop_location[2],
                        },
                    )
                ).get("id", "")
            )
        except Exception:
            crop_block = ""

    return GameSignals(
        observed=True,
        entities_observed=entities_observed,
        dimension=dimension,
        difficulty=str(snapshot.get("difficulty", "")).lower(),
        biome=str(snapshot.get("biome", "")),
        health=_as_float(snapshot.get("health", 0) or 0),
        food=_as_int(snapshot.get("food_level", snapshot.get("food", 0)) or 0),
        experience_level=_as_int(snapshot.get("experience_level", 0) or 0),
        world_time=_as_int(snapshot.get("world_time", 0) or 0),
        position=_position(snapshot),
        inventory=dict(inventory),
        animals=animals,
        adult_animals=adult_animals,
        adult_villagers=villagers,
        nearby_hostiles=len(threats),
        crop_location=crop_location,
        crop_block=crop_block,
        known_farm_location=_known_farm(state),
        known_portal=_known_portal(state),
    )


def score_phase(phase: Phase, signals: GameSignals) -> PhaseScore:
    """Return live utility for a runnable phase without changing eligibility."""
    if not signals.observed:
        return PhaseScore(0.0, ())

    score = 0.0
    reasons = []
    inventory = signals.inventory

    def add(points: float, reason: str) -> None:
        nonlocal score
        score += points
        reasons.append(reason)

    if phase is Phase.NETHER_AND_BLAZE:
        if "the_nether" in signals.dimension or signals.dimension.endswith(":nether"):
            add(120, "already in the Nether")
        if signals.known_portal:
            add(22, "verified portal is known")
        if signals.count("minecraft:obsidian") >= 10:
            add(20, "portal obsidian is carried")
        if signals.count("minecraft:flint_and_steel") >= 1:
            add(12, "portal ignition is carried")
        rods = signals.count("minecraft:blaze_rod")
        if 0 < rods < 6:
            add(30, "blaze-rod objective is already in progress")

    elif phase is Phase.ENCHANTING_PIPELINE:
        leather_adults = sum(
            count
            for family, count in signals.adult_animals.items()
            if family in LEATHER_ANIMALS
        )
        if leather_adults:
            add(35, "leather animals are nearby")
        if any(family in LEATHER_ANIMALS for family in signals.animal_pairs()):
            add(45, "a renewable leather pair can be bred now")
        if signals.count("minecraft:leather") or signals.count("minecraft:paper"):
            add(12, "book materials are already carried")
        if signals.count("minecraft:diamond") >= 2:
            add(18, "enchanting-table diamonds are carried")
        if signals.experience_level >= 20:
            add(12, "experience is already accumulated")

    elif phase is Phase.VILLAGER_INFRA:
        if signals.adult_villagers >= 2:
            add(110, "two adult villagers are nearby")
            if signals.crop_location or signals.known_farm_location:
                add(20, "a crop supply is available for breeding")
            if signals.count("minecraft:bread") >= 6:
                add(25, "breeding bread is carried")
            elif signals.count("minecraft:wheat") >= 18:
                add(18, "breeding wheat is carried")

    elif phase is Phase.XP_ENGINE:
        if signals.experience_level >= 20:
            add(20, "high experience makes XP infrastructure timely")
        if signals.count("minecraft:lapis_lazuli"):
            add(8, "lapis is ready for the XP loop")

    elif phase is Phase.IRON_FARM:
        if signals.adult_villagers >= 3:
            add(70, "an iron-farm villager population is nearby")
        if signals.count("minecraft:iron_ingot") < 16:
            add(8, "iron reserves are low")

    elif phase is Phase.TOOL_PERFECTION:
        if signals.experience_level >= 30:
            add(35, "level 30 is available for tool work")
        if signals.count("minecraft:emerald") >= 16:
            add(20, "trading emeralds are carried")

    elif phase is Phase.WORLD_UNLOCK:
        eyes = signals.count("minecraft:ender_eye")
        pearls = signals.count("minecraft:ender_pearl")
        if eyes:
            add(min(48, eyes * 4), "Eyes of Ender are already carried")
        elif pearls:
            add(min(24, pearls * 2), "ender pearls are already carried")
        if "the_end" in signals.dimension or signals.dimension.endswith(":end"):
            add(120, "already in the End")

    elif phase is Phase.MEGABASE_INIT:
        if signals.count("minecraft:beacon"):
            add(45, "a beacon is available")
        if signals.count("minecraft:shulker_box"):
            add(20, "bulk storage is available")

    # A modest inventory-density signal prefers work whose ingredients are
    # already in hand without overpowering the explicit opportunity rules.
    if phase in {Phase.TERRAFORM, Phase.CITY_BUILD} and len(inventory) >= 20:
        add(5, "inventory is stocked for construction")

    return PhaseScore(score, tuple(reasons))


class AdaptiveScheduler:
    """Select explainable live objectives and recurring local farm work."""

    _COOLDOWNS = {
        OpportunityKind.ANIMAL_FARM: 300.0,
        OpportunityKind.CROP_FARM: 180.0,
        OpportunityKind.WOOD_FARM: 60.0,
        OpportunityKind.IRON_MINE: 120.0,
        OpportunityKind.FOOD_RECOVERY: 60.0,
        OpportunityKind.FOOD_PRODUCTION: 120.0,
        OpportunityKind.END_SUPPLY: 600.0,
        OpportunityKind.NETHER_SUPPLY: 300.0,
        OpportunityKind.ENCHANTING_XP: 300.0,
        OpportunityKind.DIMENSION_ENTRY: 120.0,
        OpportunityKind.ENCHANTING_MATERIAL: 180.0,
        OpportunityKind.END_FRONTIER: 600.0,
        OpportunityKind.END_CITY_ROUTE: 300.0,
        OpportunityKind.STORAGE_MAINTENANCE: 120.0,
        OpportunityKind.ARMOR_UPKEEP: 120.0,
        # A full survey is 168 get_block round-trips; unlike armour this is
        # not urgent survival work, so it can wait far longer between checks.
        OpportunityKind.HOUSE_UPKEEP: 600.0,
    }
    _BORROWED_SPECIALTY_COOLDOWN = 300.0

    def __init__(self, client: Any, resources: Any, state: Any):
        self.client = client
        self.resources = resources
        self.state = state

    def observe(self) -> GameSignals:
        signals = collect_game_signals(self.client, self.resources, self.state)
        mutual_aid.publish_requests(self.state, signals)
        return signals

    def next_step(self, planner: Any) -> SchedulingDecision:
        """Observe once, run useful local work, or choose a runnable objective."""
        signals = self.observe()
        completed = tuple(planner.completed_phases())
        role = fleet_role(self.state)
        if signals.observed:
            record_readiness(
                self.state,
                signals.inventory,
                completed=completed,
                known_portal=signals.known_portal,
            )
        opportunity = None
        if allows_local_work(role):
            opportunity = self.select_local_opportunity(
                signals,
                completed,
                role=role,
            )
        if opportunity is not None:
            return SchedulingDecision(opportunity_result=self.run_local_opportunity(opportunity))

        candidates, focus_reason, role_complete = role_focused_candidates(
            planner.runnable(),
            planner.objectives,
            completed,
            role,
        )
        objective = planner.select(
            candidates,
            utility=lambda candidate: self.objective_score(candidate, signals),
        )
        if objective is not None and role is FleetRole.BALANCED and objective.phase in {Phase.NETHER_AND_BLAZE, Phase.WORLD_UNLOCK}:
            food_hold = food_opportunity.balanced_prepared_food_hold_reason(signals)
            if food_hold:
                camp_breaker.clear_holds(self.state)
                return SchedulingDecision(reasons=(food_hold,), role_hold=True)
        if objective is None:
            # A hold is a decision to do nothing; say why, and bound repeats.
            hold_reason = local_work_hold_reason(signals)
            reasons = ((focus_reason,) if focus_reason else ()) + (hold_reason,)
            (camp_breaker.note_hold(self.client, self.state, hold_reason) if local_work_blockers(signals) else camp_breaker.clear_holds(self.state))
            return SchedulingDecision(reasons=reasons, role_hold=role_complete)
        camp_breaker.clear_holds(self.state)
        self.record_decision(objective, signals)
        contribution = score_phase(objective.phase, signals)
        reasons = contribution.reasons or ("stable graph fallback",)
        if focus_reason:
            reasons = (focus_reason, *reasons)
        return SchedulingDecision(
            objective=objective,
            score=self.objective_score(objective, signals),
            reasons=reasons,
        )

    @staticmethod
    def objective_score(objective: Objective, signals: GameSignals) -> float:
        """Blend stable graph priority with a stronger live opportunity score."""
        return float(objective.priority * 10) + score_phase(objective.phase, signals).value

    def record_decision(self, objective: Objective, signals: GameSignals) -> None:
        contribution = score_phase(objective.phase, signals)
        runtime = self._runtime()
        runtime["last_decision"] = {
            "phase": objective.phase.name,
            "fleet_role": fleet_role(self.state).value,
            "score": self.objective_score(objective, signals),
            "signals": list(contribution.reasons) or ["stable graph fallback"],
            "timestamp": time.time(),
        }

    def select_local_opportunity(
        self,
        signals: GameSignals,
        completed: Sequence[Phase],
        *,
        now: Optional[float] = None,
        role: Optional[FleetRole] = None,
    ) -> Optional[LocalOpportunity]:
        """Choose one safe renewable-resource action, if any is useful now."""
        completed_set = set(completed)
        if Phase.SPAWN_BOOTSTRAP not in completed_set:
            return None
        current_time = time.time() if now is None else float(now)
        role = FleetRole.BALANCED if role is None else role
        primary = select_specialty_opportunity(
            client=self.client,
            state=self.state,
            role=role,
            signals=signals,
            completed=completed,
            current_time=current_time,
            cooldown_ready=self._cooldown_ready,
            allow_recovery=True,
        )
        specialty_candidates = []
        if primary is not None and primary.kind in (OpportunityKind.SELF_DEFENSE, OpportunityKind.FOOD_RECOVERY):
            return primary
        if primary is not None:
            specialty_candidates.append(replace(primary, assigned_role=role.value))
        specialty_candidates.extend(
            select_profile_specialty_opportunities(
                client=self.client, state=self.state, primary_role=role, signals=signals,
                completed=completed, current_time=current_time,
                cooldown_ready=self._cooldown_ready,
                primary_has_work=primary is not None,
            )
        )

        runtime = self._runtime()
        last_borrowed = float(runtime.get("last_borrowed_specialty_at", 0) or 0)
        if current_time - last_borrowed >= self._BORROWED_SPECIALTY_COOLDOWN:
            for borrowed in borrowed_specialty_roles(
                self.state, role, now=current_time
            ):
                candidate = select_specialty_opportunity(
                    client=self.client,
                    state=self.state,
                    role=borrowed,
                    signals=signals,
                    completed=completed,
                    current_time=current_time,
                    cooldown_ready=self._cooldown_ready,
                    allow_recovery=False,
                )
                if candidate is not None:
                    specialty_candidates.append(
                        replace(candidate, assigned_role=borrowed.value)
                    )
        # Armour is survival work, offered in every role and phase.
        if (armor := armor_upkeep.select_armor_opportunity(self.client, signals, self._cooldown_ready(OpportunityKind.ARMOR_UPKEEP, current_time), self.state)) is not None:
            specialty_candidates.append(armor)
        # Same reasoning as armour: the starter house is a one-time build
        # with no other owner, so it is offered in every role and phase too.
        if (
            house := house_upkeep.select_house_upkeep_opportunity(
                self.state, signals, self._cooldown_ready(OpportunityKind.HOUSE_UPKEEP, current_time)
            )
        ) is not None:
            specialty_candidates.append(house)
        if specialty_candidates and role is not FleetRole.BALANCED:
            return max(specialty_candidates, key=lambda candidate: candidate.score)
        if role is not FleetRole.BALANCED:
            return None
        if not signals.safe_for_local_work:  # self-gated safety work still runs
            return max(specialty_candidates, key=lambda c: c.score) if specialty_candidates else None
        candidates = specialty_candidates
        pairs = signals.animal_pairs()
        for family, pair_count in pairs.items():
            if signals.adult_animals.get(family, 0) >= 8:
                continue
            if self._cooldown_ready(OpportunityKind.ANIMAL_FARM, current_time):
                leather_bonus = 20 if family in LEATHER_ANIMALS else 0
                candidates.append(
                    LocalOpportunity(
                        OpportunityKind.ANIMAL_FARM,
                        100 + pair_count * 5 + leather_bonus,
                        f"{signals.adult_animals[family]} adult {family} and breeding food nearby",
                        animal_type=family,
                    )
                )
        crop_location = food_opportunity.reachable_farm_location(signals)
        crop_reserve = sum(signals.count(item) for item in CROP_ITEMS)
        plantable = sum(signals.count(item) for item in PLANTABLE_ITEMS)
        crop_useful = crop_reserve < 32 or signals.count("minecraft:wheat") < 18
        if (
            crop_location is not None
            and crop_useful
            and (signals.crop_location is not None or plantable > 0)
            and self._cooldown_ready(OpportunityKind.CROP_FARM, current_time)
        ):
            candidates.append(
                LocalOpportunity(
                    OpportunityKind.CROP_FARM,
                    80 + max(0, 32 - crop_reserve),
                    "a nearby crop patch can replenish renewable food",
                    location=crop_location,
                )
            )
        if not candidates:
            return None
        return max(candidates, key=lambda candidate: candidate.score)

    def run_local_opportunity(
        self,
        opportunity: LocalOpportunity,
        *,
        crop_timeout: float = 45.0,
    ) -> OpportunityResult:
        """Execute and record exactly one bounded local opportunity."""
        productive_before = productive_snapshot(self.state)
        try:
            if opportunity.kind is OpportunityKind.ANIMAL_FARM:
                before_signals = self.observe()
                before = int(
                    before_signals.animals.get(
                        opportunity.animal_type,
                        before_signals.adult_animals.get(opportunity.animal_type, 0),
                    )
                )
                success = breed_pair(
                    self.client,
                    opportunity.animal_type,
                    radius=16,
                )
                after_signals = self.observe()
                after = int(
                    after_signals.animals.get(
                        opportunity.animal_type,
                        after_signals.adult_animals.get(opportunity.animal_type, 0),
                    )
                )
                result = OpportunityResult(
                    opportunity,
                    bool(success and after > before),
                    "new offspring observed" if success and after > before else "herd did not grow",
                    before,
                    after,
                )
            elif opportunity.kind is OpportunityKind.CROP_FARM:
                result = OpportunityResult(
                    opportunity, *run_crop_opportunity(
                        self.client, opportunity, crop_timeout,
                        inventory_reader=get_inventory, traveler=goto,
                        block_finder=find_nearby_block, sleeper=time.sleep,
                        state=self.state,
                    )
                )
            elif opportunity.kind is OpportunityKind.WOOD_FARM:
                before_total = int(
                    self._runtime().get("wood_logs_banked", 0) or 0
                )
                cycle = run_wood_cycle(self.client, self.state)
                after_total = max(before_total, int(cycle.total_logs_banked or 0))
                self._runtime()["wood_logs_banked"] = after_total
                result = OpportunityResult(
                    opportunity,
                    cycle.success,
                    cycle.detail,
                    before_total,
                    after_total,
                )
            elif opportunity.kind is OpportunityKind.FOOD_RECOVERY:
                result = OpportunityResult(opportunity, *food_opportunity.run_scheduled_food_recovery(self.client, self.state))
            elif opportunity.kind is OpportunityKind.FOOD_PRODUCTION:
                result = OpportunityResult(
                    opportunity,
                    *food_opportunity.run_food_production(
                        self.client, self.state, self._runtime(), opportunity.assigned_role
                    ),
                )
                if opportunity.assigned_role != FleetRole.BALANCED.value:
                    self._runtime()["food_banked"] = result.after
            elif opportunity.kind is OpportunityKind.STORAGE_MAINTENANCE:
                before_total = int(
                    self._runtime().get("quartermaster_items_moved", 0) or 0
                )
                cycle = run_quartermaster_cycle(self.client, self.state)
                after_total = max(before_total, int(cycle.total_items_moved or 0))
                self._runtime()["quartermaster_items_moved"] = after_total
                result = OpportunityResult(
                    opportunity,
                    cycle.success,
                    cycle.detail,
                    before_total,
                    after_total,
                )
            elif opportunity.kind is OpportunityKind.ARMOR_UPKEEP:
                result = OpportunityResult(opportunity, *armor_upkeep.run_armor_upkeep(self.client, self.state))
            elif opportunity.kind is OpportunityKind.HOUSE_UPKEEP:
                result = OpportunityResult(opportunity, *house_upkeep.run_house_upkeep(self.client, self.state))
            elif opportunity.kind is OpportunityKind.SELF_DEFENSE:
                result = OpportunityResult(opportunity, *run_self_defense(self.client, self.observe))
            elif opportunity.kind is OpportunityKind.CLEAR_HOSTILES_AID:
                result = OpportunityResult(opportunity, *aid_response.run_scheduled_clear_hostiles(self.client, self.state, self.observe(), opportunity))
            elif opportunity.kind in _ROLE_OPPORTUNITY_KINDS:
                result = OpportunityResult(opportunity, *run_role_opportunity(self.client, self.state, opportunity))
            else:
                success, detail, before_total, after_total = run_scheduled_iron_cycle(
                    self.client, self.state, self._runtime()
                )
                result = OpportunityResult(
                    opportunity,
                    success,
                    detail,
                    before_total,
                    after_total,
                )
        except Exception as exc:
            result = OpportunityResult(
                opportunity,
                False,
                f"{type(exc).__name__}: {exc}",
            )
        self._record_opportunity_result(result)
        if (
            opportunity.assigned_role
            and opportunity.assigned_role != fleet_role(self.state).value
            and opportunity.assigned_role not in {role.value for role in configured_specialty_roles(self.state, fleet_role(self.state))}
        ):
            runtime = self._runtime()
            runtime["last_borrowed_specialty_at"] = time.time()
            runtime["last_borrowed_specialty_role"] = opportunity.assigned_role
        attempt = record_productive_attempt(
            self.state,
            opportunity.kind.value,
            productive_before,
            productive_snapshot(self.state),
            detail=result.detail,
        )
        camp_breaker.self_rescue_if_escalated(self.client, self.state, attempt)
        return result

    def _runtime(self) -> Dict[str, Any]:
        custom = getattr(self.state, "custom_data", None)
        if not isinstance(custom, dict):
            custom = {}
            self.state.custom_data = custom
        runtime = custom.setdefault("adaptive_scheduler", {})
        if not isinstance(runtime, dict):
            runtime = {}
            custom["adaptive_scheduler"] = runtime
        return runtime
    def _cooldown_ready(self, kind: OpportunityKind, now: float) -> bool:
        return food_opportunity.cooldown_ready(self._runtime(), kind, now, {kind: retry_cooldown(self.state, kind.value, self._COOLDOWNS[kind])})

    def _record_opportunity_result(self, result: OpportunityResult) -> None:
        record_opportunity_result(self._runtime(), result, time.time())

__all__ = [
    "AdaptiveScheduler",
    "GameSignals",
    "LocalOpportunity", "OpportunityKind",
    "OpportunityResult",
    "PhaseScore",
    "SchedulingDecision",
    "collect_game_signals",
    "score_phase",
]
