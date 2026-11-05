"""Keep a usable sword and pickaxe, in any phase, from cheap local materials.

Armour upkeep dressed the bot, but nothing ever asked "can you still fight?".
Live A1 2026-09-28: its only sword was an iron one at 247/250 damage, which
`combat_loadout.weapon_score` rightly refuses (three uses left), so every
defence ended in `attack_declined:weapon_unavailable`. It carried 73
cobblestone and sticks -- a stone sword is one craft away -- and died to a
single zombie beside its own house. It had no pickaxe at all either, so the
armour-recovery mining stage could never produce iron.

Stone is the default: cobblestone is plentiful at any base and the bot's few
ingots are reserved for armour. Wood is the fallback when no cobblestone is
carried.
"""

from __future__ import annotations

from typing import Any, List, Optional

#: Remaining uses at or below which a tool counts as already gone. Well above
#: `weapon_score`'s own floor of 3 so a replacement exists *before* the fight
#: that breaks the old one.
WORN_OUT_USES = 16
#: (role, stone recipe, cobblestone cost, wooden recipe, plank cost)
KIT = (
    ("weapon", "minecraft:stone_sword", 2, "minecraft:wooden_sword", 2),
    ("pickaxe", "minecraft:stone_pickaxe", 3, "minecraft:wooden_pickaxe", 3),
)
#: Sticks are made from planks by `craft`, so reserve two for them.
STICK_PLANKS = 2


def _entries(client: Any) -> list:
    try:
        response = client.transport.dispatch("get_inventory", {})
    except Exception:
        return []
    data = response.get("data", response) if isinstance(response, dict) else {}
    items = data.get("inventory", []) if isinstance(data, dict) else []
    return [i for i in items if isinstance(i, dict) and int(i.get("count", 0) or 0) > 0]


def _durable(item: dict, minimum_uses: int = WORN_OUT_USES) -> bool:
    from ..common.combat_loadout import remaining_durability

    remaining = remaining_durability(item)
    return remaining is None or remaining > minimum_uses


def has_usable(entries: list, role: str) -> bool:
    """A carried weapon or pickaxe with real life left in it."""
    from ..common.tunnel_miner import PICKAXE_RETURN_RESERVE

    # A pickaxe scores as a weapon too, but at 3 damage it is not a defence.
    suffixes = ("_sword", "_axe") if role == "weapon" else ("_pickaxe",)
    minimum = WORN_OUT_USES if role == "weapon" else PICKAXE_RETURN_RESERVE
    return any(str(i.get("id", "")).endswith(suffixes) and _durable(i, minimum) for i in entries)


def has_mining_pickaxe(client: Any) -> bool:
    """Iron requires a durable stone-or-better pickaxe, not wood or gold."""
    from ..common.tunnel_miner import PICKAXE_RETURN_RESERVE

    return any(
        item.get("id") in {f"minecraft:{tier}_pickaxe" for tier in ("stone", "iron", "diamond", "netherite")}
        and _durable(item, PICKAXE_RETURN_RESERVE) for item in _entries(client)
    )


def _count(entries: list, predicate) -> int:
    return sum(int(i.get("count", 0) or 0) for i in entries if predicate(str(i.get("id", ""))))


def _recipe(entries: list, stone: str, cobble: int, wood: str, planks: int) -> Optional[str]:
    """The affordable recipe for one gap, stone first, or None."""
    if _count(entries, lambda i: i == "minecraft:cobblestone") >= cobble:
        return stone
    wood_planks = _count(entries, lambda i: i.endswith("_planks")) + 4 * _count(
        entries, lambda i: i.endswith("_log") and "stripped" not in i
    )
    return wood if wood_planks >= planks + STICK_PLANKS else None


def kit_gaps(client: Any) -> List[str]:
    """Affordable recipes that would restore a missing weapon or pickaxe."""
    entries = _entries(client)
    if not entries:
        return []  # unknown inventory: never offer work on a guess
    return [
        recipe
        for role, stone, cobble, wood, planks in KIT
        if (not has_usable(entries, role) or (
            role == "pickaxe" and not has_mining_pickaxe(client)
            and _count(entries, lambda i: i == "minecraft:cobblestone") >= cobble
        ))
        and (recipe := _recipe(entries, stone, cobble, wood, planks))
    ]


def restore_kit(client: Any) -> List[str]:
    """Craft what `kit_gaps` names; return the short names actually made."""
    from ..common.inventory import craft
    from ..common.tasks import PlayerDeathDetected, SurvivalRecoveryRequired

    made = []
    for recipe in kit_gaps(client):
        try:
            if craft(client, recipe, 1):
                made.append(recipe.split(":")[1])
        except (PlayerDeathDetected, SurvivalRecoveryRequired):
            raise
        except Exception:
            continue
    return made


__all__ = ["KIT", "WORN_OUT_USES", "has_usable", "kit_gaps", "restore_kit"]
