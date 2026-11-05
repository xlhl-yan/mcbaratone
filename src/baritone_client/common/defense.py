"""Pure threat assessment and self-defense policy.

This module deliberately contains no transport calls.  The combat module owns
Minecraft I/O while this module turns snapshots into deterministic decisions.
"""

from dataclasses import dataclass, field
from enum import Enum
import math
import time
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


class DefenseMode(str, Enum):
    """Persistent self-defense states."""

    CLEAR = "clear"
    ALERT = "alert"
    EVADE = "evade"
    ENGAGE = "engage"
    RECOVER = "recover"


class AttackStyle(str, Enum):
    """Coarse attack style used by the Milestone A policy."""

    MELEE = "melee"
    RANGED = "ranged"
    EXPLOSIVE = "explosive"
    BOSS = "boss"


@dataclass(frozen=True)
class ThreatProfile:
    """Static defaults for one hostile entity type."""

    severity: float
    style: AttackStyle
    always_evade: bool = False
    conditional: bool = False


@dataclass(frozen=True)
class ThreatAssessment:
    """Scored view of a nearby hostile entity."""

    entity: Dict
    entity_type: str
    distance: float
    closing_speed: float
    score: float
    style: AttackStyle
    always_evade: bool
    reasons: Tuple[str, ...] = ()


@dataclass(frozen=True)
class DefenseDecision:
    """One policy decision and the evidence used to make it."""

    mode: DefenseMode
    reason: str
    primary: Optional[ThreatAssessment] = None


@dataclass
class DefenseRuntime:
    """Small persistent state used to prevent flee/re-engage oscillation."""

    mode: DefenseMode = DefenseMode.CLEAR
    entered_at: float = field(default_factory=time.monotonic)
    recovery_until: float = 0.0
    last_threat_at: float = 0.0
    last_reason: str = "initial"
    # Consecutive failed run_away() attempts against the SAME entity id.
    # Confirmed live: an unarmored bot in mountainous terrain (windswept
    # forest -- ledges everywhere) got a zombie stuck in melee range for
    # dozens of consecutive cycles, "no terrain-safe escape endpoint found"
    # every time, and slowly died. armor_count<3 forces EVADE unconditionally
    # with no escalation, so pure evasion had a 0% success rate against this
    # specific threat/terrain combination for the entire encounter -- worse
    # than even an unfavorable fight. See evade_escalation_threshold below.
    evade_failures: int = 0
    evade_failure_entity: Optional[int] = None
    # A crowded cave can rotate the highest-scored entity every tick. Keep an
    # aggregate streak as well so switching between two attackers does not
    # reset a demonstrably failed escape strategy forever.
    consecutive_evade_failures: int = 0
    # Highest protected escape floor during the current threat episode. Keep
    # repeated failed routes from ratcheting the next floor downward.
    escape_floor: Optional[float] = None

    def record_evade_result(self, entity_id: Optional[int], escaped: bool) -> None:
        """Track a run_away() outcome against one specific threat."""
        self.consecutive_evade_failures = (
            0 if escaped else self.consecutive_evade_failures + 1
        )
        if escaped or entity_id != self.evade_failure_entity:
            self.evade_failures = 0 if escaped else 1
            self.evade_failure_entity = None if escaped else entity_id
        else:
            self.evade_failures += 1

    def should_escalate_to_combat(
        self, entity_id: Optional[int], *, threshold: int = 2
    ) -> bool:
        """True once evasion has demonstrably failed repeatedly for this threat."""
        return (
            self.consecutive_evade_failures >= threshold
            or (
                entity_id is not None
                and entity_id == self.evade_failure_entity
                and self.evade_failures >= threshold
            )
        )

    def transition(
        self,
        mode: DefenseMode,
        reason: str,
        *,
        now: Optional[float] = None,
    ) -> None:
        """Move to a state without resetting its timer on identical ticks."""
        now = time.monotonic() if now is None else now
        if mode != self.mode:
            self.mode = mode
            self.entered_at = now
        if mode is DefenseMode.CLEAR:
            self.escape_floor = None
        self.last_reason = reason
        if mode in (DefenseMode.ALERT, DefenseMode.EVADE, DefenseMode.ENGAGE):
            self.last_threat_at = now

    def hold_recovery(
        self,
        seconds: float = 6.0,
        *,
        now: Optional[float] = None,
    ) -> None:
        """Require a quiet recovery interval after an escape or fight."""
        now = time.monotonic() if now is None else now
        self.recovery_until = max(self.recovery_until, now + max(0.0, seconds))
        self.transition(DefenseMode.RECOVER, "post-encounter recovery", now=now)


@dataclass(frozen=True)
class EscapeCandidate:
    """Potential flee destination before terrain inspection."""

    x: int
    y: int
    z: int
    direction_score: float


_PROFILES = {
    # Loaded hostile projectiles are immediate movement hazards. Their bridge
    # metadata includes owner and velocity, so ignoring them made the policy
    # react only after the damage landed.
    "arrow": ThreatProfile(96, AttackStyle.RANGED, always_evade=True),
    "spectral_arrow": ThreatProfile(96, AttackStyle.RANGED, always_evade=True),
    "trident": ThreatProfile(100, AttackStyle.RANGED, always_evade=True),
    "small_fireball": ThreatProfile(105, AttackStyle.EXPLOSIVE, always_evade=True),
    "fireball": ThreatProfile(110, AttackStyle.EXPLOSIVE, always_evade=True),
    "dragon_fireball": ThreatProfile(118, AttackStyle.EXPLOSIVE, always_evade=True),
    "wither_skull": ThreatProfile(118, AttackStyle.EXPLOSIVE, always_evade=True),
    "shulker_bullet": ThreatProfile(98, AttackStyle.RANGED, always_evade=True),
    "wind_charge": ThreatProfile(92, AttackStyle.RANGED, always_evade=True),
    "breeze_wind_charge": ThreatProfile(94, AttackStyle.RANGED, always_evade=True),
    "creeper": ThreatProfile(100, AttackStyle.EXPLOSIVE, always_evade=True),
    "warden": ThreatProfile(120, AttackStyle.BOSS, always_evade=True),
    "wither": ThreatProfile(120, AttackStyle.BOSS, always_evade=True),
    "ender_dragon": ThreatProfile(120, AttackStyle.BOSS, always_evade=True),
    "skeleton": ThreatProfile(78, AttackStyle.RANGED, always_evade=True),
    "stray": ThreatProfile(82, AttackStyle.RANGED, always_evade=True),
    "bogged": ThreatProfile(84, AttackStyle.RANGED, always_evade=True),
    "pillager": ThreatProfile(82, AttackStyle.RANGED, always_evade=True),
    "witch": ThreatProfile(95, AttackStyle.RANGED, always_evade=True),
    "blaze": ThreatProfile(92, AttackStyle.RANGED, always_evade=True),
    "ghast": ThreatProfile(90, AttackStyle.RANGED, always_evade=True),
    "guardian": ThreatProfile(88, AttackStyle.RANGED, always_evade=True),
    "elder_guardian": ThreatProfile(105, AttackStyle.RANGED, always_evade=True),
    "shulker": ThreatProfile(86, AttackStyle.RANGED, always_evade=True),
    "breeze": ThreatProfile(82, AttackStyle.RANGED, always_evade=True),
    "drowned": ThreatProfile(68, AttackStyle.MELEE),
    "zombie": ThreatProfile(55, AttackStyle.MELEE),
    "zombie_villager": ThreatProfile(57, AttackStyle.MELEE),
    "husk": ThreatProfile(62, AttackStyle.MELEE),
    "cave_spider": ThreatProfile(75, AttackStyle.MELEE),
    "spider": ThreatProfile(52, AttackStyle.MELEE, conditional=True),
    "enderman": ThreatProfile(80, AttackStyle.MELEE, conditional=True),
    "slime": ThreatProfile(48, AttackStyle.MELEE),
    "magma_cube": ThreatProfile(65, AttackStyle.MELEE),
    "vindicator": ThreatProfile(85, AttackStyle.MELEE),
    "evoker": ThreatProfile(100, AttackStyle.RANGED, always_evade=True),
    "ravager": ThreatProfile(105, AttackStyle.MELEE, always_evade=True),
    "hoglin": ThreatProfile(78, AttackStyle.MELEE),
    "zoglin": ThreatProfile(88, AttackStyle.MELEE, always_evade=True),
    "piglin_brute": ThreatProfile(92, AttackStyle.MELEE, always_evade=True),
    "piglin": ThreatProfile(65, AttackStyle.RANGED, conditional=True),
    "zombified_piglin": ThreatProfile(72, AttackStyle.MELEE, conditional=True),
    "polar_bear": ThreatProfile(72, AttackStyle.MELEE, conditional=True),
    "bee": ThreatProfile(45, AttackStyle.MELEE, conditional=True),
    "wolf": ThreatProfile(48, AttackStyle.MELEE, conditional=True),
    "phantom": ThreatProfile(76, AttackStyle.MELEE, always_evade=True),
    "silverfish": ThreatProfile(42, AttackStyle.MELEE),
    "endermite": ThreatProfile(42, AttackStyle.MELEE),
    "vex": ThreatProfile(82, AttackStyle.MELEE, always_evade=True),
}

_PROJECTILE_TYPES = {
    "arrow",
    "spectral_arrow",
    "trident",
    "small_fireball",
    "fireball",
    "dragon_fireball",
    "wither_skull",
    "shulker_bullet",
    "wind_charge",
    "breeze_wind_charge",
}

def normalize_entity_type(entity_type: object) -> str:
    """Return a Minecraft entity registry path without its namespace."""
    value = str(entity_type or "").lower()
    return value.split(":", 1)[-1]


def is_projectile_threat(entity_type: object) -> bool:
    """Return whether a threat is a projectile that must never be meleed."""
    return normalize_entity_type(entity_type) in _PROJECTILE_TYPES


def _profile_for(entity_type: str) -> Optional[ThreatProfile]:
    # Exact matching avoids classifying a pig as a piglin or a harmless item
    # because one happens to contain a hostile substring.
    return _PROFILES.get(normalize_entity_type(entity_type))


def _explicit_aggression(entity: Dict, player_state: Dict) -> Optional[bool]:
    for key in ("is_attacking", "is_aggressive", "angry_at_player", "hostile"):
        if key in entity:
            return bool(entity.get(key))
    target_id = entity.get("target_id")
    player_id = player_state.get("entity_id", player_state.get("player_id"))
    if target_id is not None and player_id is not None:
        return target_id == player_id
    return None


def _conditional_is_active(
    entity_type: str,
    entity: Dict,
    player_state: Dict,
    distance: float,
) -> bool:
    explicit = _explicit_aggression(entity, player_state)
    if entity_type == "spider":
        # Mob targets can briefly clear between melee hits.  A daytime spider
        # already inside contact range is therefore unsafe even when the
        # sampled aggression flag is false.
        world_time = int(player_state.get("world_time", 13000)) % 24000
        return explicit is True or world_time >= 12000 or distance <= 5.0
    if explicit is not None:
        return explicit
    if entity_type == "enderman":
        # Without an anger signal, only treat immediate contact range as an
        # active threat.  Merely seeing an Enderman must not start a fight.
        return distance <= 3.5
    return distance <= 3.5


def _closing_speed(
    entity: Dict,
    player_state: Dict,
    *,
    ignore_grounded_vertical_velocity: bool = False,
) -> float:
    position = entity.get("position") or {}
    player = player_state.get(
        "block_position",
        player_state.get("position", {}),
    ) or {}
    velocity = entity.get("velocity") or {}
    player_velocity = player_state.get("velocity") or {}
    try:
        dx = float(position.get("x", 0)) - float(player.get("x", 0))
        dy = float(position.get("y", 0)) - float(player.get("y", 0))
        dz = float(position.get("z", 0)) - float(player.get("z", 0))
        distance = math.sqrt(dx * dx + dy * dy + dz * dz)
        if distance < 0.01:
            return 0.0
        player_vertical_velocity = (
            0.0
            if ignore_grounded_vertical_velocity
            else float(player_velocity.get("y", 0))
        )
        radial_velocity = (
            (float(velocity.get("x", 0)) - float(player_velocity.get("x", 0)))
            * dx
            + (float(velocity.get("y", 0)) - player_vertical_velocity)
            * dy
            + (float(velocity.get("z", 0)) - float(player_velocity.get("z", 0)))
            * dz
        ) / distance
        return max(0.0, -radial_velocity)
    except (TypeError, ValueError):
        return 0.0


def _creeper_below_work_floor(entity, state, distance, closing):
    """Recognize a hidden cave creeper without lowering same-level clearance."""
    try:
        if any(key in entity and entity[key] is not False
               for key in ("is_attacking", "angry_at_player")):
            return False
        vectors = (entity["position"], state.get("position", state.get("block_position")))
        if any(
            isinstance(vector[axis], bool) or not isinstance(vector[axis], (int, float))
            or not math.isfinite(vector[axis])
            for vector in vectors for axis in ("x", "y", "z")
        ):
            return False
        mob, player = vectors
        observed = math.dist(
            [mob[axis] for axis in ("x", "y", "z")],
            [player[axis] for axis in ("x", "y", "z")],
        )
        return (player["y"] - mob["y"] >= 5.0 and distance > 5.0
                and observed > 5.0 and abs(distance - observed) <= 0.25
                and closing <= 0.05)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


#: Vanilla gravity a grounded entity reports every tick (-0.0784 Y).
_GROUNDED_FALL = 0.0785


def _settled(entity):
    """Drop a mob's grounded-gravity Y velocity, as #88 did for the player.

    Grounded gravity alone must not make a stationary mob look closer.
    """
    try:
        velocity = entity["velocity"]
        if -_GROUNDED_FALL <= float(velocity["y"]) <= 0.0:
            return {**entity, "velocity": {**velocity, "y": 0.0}}
    except (KeyError, TypeError, ValueError):
        pass
    return entity


def _sealed_above(entity, state, distance):
    """A blind, calm mob standing on a ceiling over the player's head.

    Feet three blocks over the player's leave a block between the mob and
    the player's head space; its jumps and steps stay inside its pocket,
    so only sideways approach counts. Fuse and melee range stay
    actionable, and the reported distance must match the positions. The
    separation is measured from the live position, so a player stepping
    up its staircase need not be grounded.
    """
    try:
        mob, player = entity["position"], state.get("position", state.get("block_position"))
        velocity = entity["velocity"]
        values = [velocity["x"], velocity["z"]] + [state["velocity"][axis] for axis in ("x", "y", "z")] + [
            vector[axis] for vector in (mob, player) for axis in ("x", "y", "z")]
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
               for value in values):
            return False
        observed = math.dist([mob[axis] for axis in ("x", "y", "z")],
                             [player[axis] for axis in ("x", "y", "z")])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    dx, dz = player["x"] - mob["x"], player["z"] - mob["z"]
    across = math.hypot(dx, dz)
    approach = (velocity["x"] * dx + velocity["z"] * dz) / across if across > 0 else 0.0
    return (mob["y"] - player["y"] >= 3.0 and distance > 3.0
            and abs(distance - observed) <= 0.25 and approach <= 0.1)


def _occluded_calm_mob(entity, state, entity_type, profile, distance, closing):
    """Keep shelter only on explicit, fresh evidence of a non-actionable mob.

    A nearby cave mob must not drive the player out of a safe work area.
    Unknown telemetry, contact, pursuit, other explosives, bosses, and
    wall-crossing vexes retain the conservative policy; every snapshot
    reassesses visibility.
    """
    # Creepers are normally avoid-only, but a creeper that is positively
    # observed as calm, occluded, stationary, and well outside fuse range is
    # not an actionable threat.  Without this narrow exception, an idle
    # creeper just beyond the 10m action radius can keep the controller in an
    # evade/relocate loop forever.  Explosives remain fail-closed when any of
    # those observations are missing or uncertain.
    calm_creeper = entity_type == "creeper"
    stationary_creeper = False
    vertically_separated = False
    sealed = _sealed_above(entity, state, distance)
    if calm_creeper and not sealed:
        # Fabric can report vanilla grounded gravity (-0.0784 Y velocity)
        # even while `is_on_ground` is true. That artificial vertical component
        # made a stationary creeper below the player look like it was closing.
        # Only suppress it for an explicitly grounded player with a complete,
        # finite velocity vector; airborne and unknown states stay fail-closed.
        if state.get("is_on_ground") is not True:
            return False
        try:
            player_velocity = state["velocity"]
            if not all(math.isfinite(float(player_velocity[axis])) for axis in ("x", "y", "z")):
                return False
            grounded_vertical_velocity = float(player_velocity["y"])
            if not -0.1 <= grounded_vertical_velocity <= 0.05:
                return False
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        try:
            velocity = _settled(entity)["velocity"]
            components = [float(velocity[axis]) for axis in ("x", "y", "z")]
            stationary_creeper = (
                all(math.isfinite(component) for component in components)
                and math.sqrt(sum(component * component for component in components)) <= 0.05
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        closing = _closing_speed(
            _settled(entity), state, ignore_grounded_vertical_velocity=True
        )
    elif (
        profile.style in (AttackStyle.MELEE, AttackStyle.RANGED)
        and state.get("is_on_ground") is True
    ):
        # Occluded mobs several blocks below the surface can move laterally
        # without being able to reach the player. Require complete fresh
        # player velocity here so airborne falls and unknown ground state
        # retain the normal closing-speed policy.
        try:
            mob_y = float(entity["position"]["y"])
            player_y = float(
                state.get("block_position", state.get("position"))["y"]
            )
            player_velocity = state["velocity"]
            velocity_is_finite = all(
                math.isfinite(float(player_velocity[axis]))
                for axis in ("x", "y", "z")
            )
            vertically_separated = (
                velocity_is_finite
                and -0.1 <= float(player_velocity["y"]) <= 0.05
                and math.isfinite(mob_y)
                and math.isfinite(player_y)
                and player_y - mob_y >= 6.0
            )
            if velocity_is_finite and -0.1 <= float(player_velocity["y"]) <= 0.05:
                # A grounded player is reported falling at -0.0784. With a
                # sealed mob four blocks below, that phantom fall read as
                # ~0.06 closing, over the 0.05 limit: live A1 2026-10-16 froze
                # for 35+ minutes beside a buried, blind, motionless skeleton.
                # The mob's own motion still counts, as for creepers.
                closing = _closing_speed(_settled(entity), state, ignore_grounded_vertical_velocity=True)
        except (KeyError, TypeError, ValueError, OverflowError):
            vertically_separated = False
    if not (
        (profile.style in (AttackStyle.MELEE, AttackStyle.RANGED) or calm_creeper)
        and entity_type not in _PROJECTILE_TYPES | {"vex"}
        and entity.get("can_see_player") is False
        and entity.get("is_aggressive") is False
        and not any(entity.get(key) is True for key in ("is_attacking", "angry_at_player"))
        and _explicit_aggression(entity, state) is False
        and math.isfinite(distance)
        and distance < 999.0
        and (sealed or (
            (distance > (6.0 if calm_creeper else 5.0)
             or (calm_creeper and _creeper_below_work_floor(entity, state, distance, closing)))
            and (not calm_creeper or stationary_creeper)
            and (closing <= 0.05 or vertically_separated or stationary_creeper)))
    ):
        return False
    target = entity.get("target_id")
    if target is not None and target == state.get("entity_id", state.get("player_id")):
        return False
    if calm_creeper:
        # Do not suppress a creeper with an ambiguous target.
        if target is not None:
            return False
    vectors = [entity.get("position"), entity.get("velocity"),
               state.get("block_position", state.get("position"))]
    if "velocity" in state:
        vectors.append(state["velocity"])
    try:
        return all(math.isfinite(float(vector[axis]))
                   for vector in vectors for axis in ("x", "y", "z"))
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def assess_threats(
    entities: Iterable[Dict],
    player_state: Optional[Dict] = None,
) -> List[ThreatAssessment]:
    """Classify and rank hostiles by lethality, proximity, and movement."""
    state = player_state or {}
    assessments: List[ThreatAssessment] = []
    for entity in entities:
        entity_type = normalize_entity_type(entity.get("type"))
        profile = _profile_for(entity_type)
        explicit_aggression = _explicit_aggression(entity, state)
        # Future Minecraft hostiles and modded mobs must fail safe when the
        # bridge explicitly reports that they target this player.  Unknown,
        # non-aggressive entities remain ignored.
        if profile is None and explicit_aggression is True:
            profile = ThreatProfile(72, AttackStyle.MELEE)
        if profile is None:
            continue
        try:
            distance = max(0.0, float(entity.get("distance", 999)))
        except (TypeError, ValueError):
            distance = 999.0
        if profile.conditional and not _conditional_is_active(
            entity_type, entity, state, distance
        ):
            continue
        closing = _closing_speed(entity, state)
        if _occluded_calm_mob(entity, state, entity_type, profile, distance, closing):
            continue
        if entity_type in _PROJECTILE_TYPES:
            owner_id = entity.get("owner_id")
            player_id = state.get("entity_id", state.get("player_id"))
            if owner_id is not None and owner_id == player_id:
                continue
            # Ignore spent/outbound projectiles. A projectile already at
            # contact range remains urgent even when one sample reports no
            # closing velocity.
            if closing <= 0.05 and distance > 2.0:
                continue
        proximity = max(0.0, 16.0 - distance) * 2.5
        closing_risk = min(20.0, closing * 16.0)
        aggression_risk = 18.0 if explicit_aggression is True else 0.0
        line_of_sight_risk = (
            8.0
            if profile.style == AttackStyle.RANGED
            and entity.get("can_see_player") is True
            else 0.0
        )
        score = (
            profile.severity
            + proximity
            + closing_risk
            + aggression_risk
            + line_of_sight_risk
        )
        reasons = [profile.style.value, f"{distance:.1f}m"]
        if explicit_aggression is True:
            reasons.append("targeting player")
        if line_of_sight_risk:
            reasons.append("line of sight")
        if closing > 0.1:
            reasons.append(f"closing {closing:.2f}m/tick")
        if profile.always_evade:
            reasons.append("avoid-only")
        assessments.append(
            ThreatAssessment(
                entity=entity,
                entity_type=entity_type,
                distance=distance,
                closing_speed=closing,
                score=score,
                style=profile.style,
                always_evade=profile.always_evade,
                reasons=tuple(reasons),
            )
        )
    return sorted(assessments, key=lambda item: (-item.score, item.distance))


# A threat that stays outside the 10m action radius and never closes (see
# the ALERT branch below) produces zero action every single tick it is
# re-observed in that state. Live proof from dragon-lab's dragon-a run
# (controller/monitor/bot.log): a skeleton parked at 11.2m held the bot in
# ALERT for six consecutive self-defense opportunity cycles (~60s, no
# DEFENSE: line printed at all) while the nearby-hostile count climbed
# 1->2->3. ALERT is meant to be a "keep watching" state, not a place a
# threat can sit forever -- once a standoff has already been watched for a
# full dwell window without resolving on its own, stop watching and evade.
# Moving away from a distant, non-closing threat is always a safe action
# regardless of what is making closing_speed read this low; this dwell
# timer does not change (or attempt to diagnose) that underlying reading.
_ALERT_DWELL_LIMIT_SECONDS = 15.0


def choose_defense_action(
    threats: Sequence[ThreatAssessment],
    *,
    health: float,
    armor_count: int,
    has_weapon: bool,
    runtime: DefenseRuntime,
    now: Optional[float] = None,
) -> DefenseDecision:
    """Choose a conservative action while retaining post-fight hysteresis."""
    now = time.monotonic() if now is None else now
    if not threats:
        runtime.escape_floor = None
    if health < 12.0:
        if threats and threats[0].distance <= 12.0:
            return DefenseDecision(
                DefenseMode.EVADE,
                "critical health with nearby threat",
                threats[0],
            )
        return DefenseDecision(DefenseMode.RECOVER, "critical health")

    if not threats:
        if runtime.mode in (
            DefenseMode.EVADE,
            DefenseMode.ENGAGE,
            DefenseMode.RECOVER,
        ):
            if now < runtime.recovery_until:
                return DefenseDecision(DefenseMode.RECOVER, "recovery hysteresis")
        return DefenseDecision(DefenseMode.CLEAR, "no active threats")

    urgent = [threat for threat in threats if threat.distance <= 10.0]
    if urgent and len(threats) > 1:
        return DefenseDecision(
            DefenseMode.EVADE,
            "multiple threats inside defense radius",
            urgent[0],
        )
    primary = urgent[0] if urgent else threats[0]
    if not urgent and primary.closing_speed <= 0.15:
        if (
            runtime.mode is DefenseMode.ALERT
            and now - runtime.entered_at >= _ALERT_DWELL_LIMIT_SECONDS
        ):
            return DefenseDecision(
                DefenseMode.EVADE,
                "threat stalled in alert range too long; forcing evasion",
                primary,
            )
        return DefenseDecision(
            DefenseMode.ALERT,
            "threat outside action radius",
            primary,
        )
    if primary.always_evade:
        return DefenseDecision(DefenseMode.EVADE, f"avoid {primary.style.value} threat", primary)
    if armor_count < 3:
        return DefenseDecision(DefenseMode.EVADE, f"only {armor_count}/4 armor pieces", primary)
    if health < 16.0:
        return DefenseDecision(DefenseMode.EVADE, f"health only {health:.1f}", primary)
    if not has_weapon:
        return DefenseDecision(DefenseMode.EVADE, "no usable weapon", primary)
    return DefenseDecision(DefenseMode.ENGAGE, "equipped for bounded melee", primary)


def plan_escape_candidates(
    player_position: Dict,
    threats: Sequence[ThreatAssessment],
    *,
    distance: float = 24.0,
) -> List[EscapeCandidate]:
    """Generate routes away from the combined pressure of all threats."""
    def coordinate(name: str, default: float) -> float:
        value = player_position.get(name, default)
        try:
            return float(default if value is None else value)
        except (TypeError, ValueError):
            return default

    px = coordinate("x", 0.0)
    py = int(coordinate("y", 64.0))
    pz = coordinate("z", 0.0)
    away_x = 0.0
    away_z = 0.0
    for threat in threats:
        position = threat.entity.get("position") or {}
        try:
            tx = float(px if position.get("x") is None else position.get("x"))
            tz = float(pz if position.get("z") is None else position.get("z"))
        except (TypeError, ValueError):
            tx, tz = px, pz
        dx = px - tx
        dz = pz - tz
        length = max(0.01, math.hypot(dx, dz))
        weight = max(1.0, threat.score) / max(2.0, threat.distance)
        away_x += dx / length * weight
        away_z += dz / length * weight

    length = math.hypot(away_x, away_z)
    if length < 0.01:
        away_x, away_z, length = 1.0, 0.0, 1.0
    base_angle = math.atan2(away_z / length, away_x / length)
    candidates: List[EscapeCandidate] = []
    offsets = (0.0, math.pi / 4, -math.pi / 4, math.pi / 2, -math.pi / 2)
    y_offsets = (0, 1, -1)
    for index, offset in enumerate(offsets):
        angle = base_angle + offset
        for dy in y_offsets:
            candidates.append(
                EscapeCandidate(
                    x=int(round(px + math.cos(angle) * distance)),
                    y=py + dy,
                    z=int(round(pz + math.sin(angle) * distance)),
                    direction_score=1.0 - index * 0.1 - abs(dy) * 0.05,
                )
            )
    return candidates


@dataclass(frozen=True)
class ArmorAssessment:
    """Result of armor assessment for environmental threats."""

    action: str  # "none", "swap_boots", "equip_full_leather"
    reason: str
    current_boots: Optional[str] = None
    recommended_boots: Optional[str] = None


def assess_armor_for_environment(client, environment: str) -> ArmorAssessment:
    """Assess whether current armor is appropriate for the environment.

    Args:
        client: Minecraft client with transport for inventory queries
        environment: Environmental threat type ("freezing", "fire", etc.)

    Returns:
        ArmorAssessment with recommended action and reasoning.
    """
    if environment != "freezing":
        return ArmorAssessment("none", f"no specific armor requirement for {environment}")

    # Query current armor and inventory
    try:
        response = client.transport.dispatch("get_inventory", {})
        data = response.get("data", response)
    except Exception:
        return ArmorAssessment("none", "failed to query inventory")

    armor = data.get("armor", [])
    inventory = data.get("inventory", [])

    # Find currently equipped boots
    current_boots = None
    for item in armor:
        if item.get("id", "").endswith("_boots"):
            current_boots = item.get("id")
            break

    # Check if leather boots are already equipped (optimal for freezing)
    if current_boots == "minecraft:leather_boots":
        return ArmorAssessment(
            "none",
            "leather boots already equipped for freezing protection",
            current_boots=current_boots,
            recommended_boots="minecraft:leather_boots",
        )

    # Check if leather boots are available in inventory
    has_leather_boots = any(
        item.get("id") == "minecraft:leather_boots" and item.get("count", 0) > 0
        for item in inventory
    )

    if has_leather_boots:
        return ArmorAssessment(
            "swap_boots",
            "leather boots available in inventory for freezing protection",
            current_boots=current_boots,
            recommended_boots="minecraft:leather_boots",
        )

    # No leather boots available - signal need for leather
    return ArmorAssessment(
        "gather_leather",
        "freezing biome but no leather boots; needs 4 leather",
        current_boots=current_boots,
        recommended_boots="minecraft:leather_boots",
    )


__all__ = [
    "AttackStyle",
    "ArmorAssessment",
    "DefenseDecision",
    "DefenseMode",
    "DefenseRuntime",
    "EscapeCandidate",
    "ThreatAssessment",
    "assess_armor_for_environment",
    "assess_threats",
    "choose_defense_action",
    "normalize_entity_type",
    "plan_escape_candidates",
]
