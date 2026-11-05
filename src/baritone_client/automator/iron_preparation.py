"""Prepare local inventory before an iron expedition; never discard supplies."""

from math import hypot

from ..core.exceptions import CommandError
from .weapon_upkeep import has_mining_pickaxe, restore_kit

#: Worthless stacks thrown away only once banking cannot make room. Plain
#: digging spoil (cobblestone keeps its retain floor) and inedible drops.
LAST_RESORT_JUNK = frozenset({
    "minecraft:rotten_flesh", "minecraft:poisonous_potato", "minecraft:spider_eye",
    "minecraft:wildflowers", "minecraft:leaf_litter", "minecraft:feather",
})


def prepare_iron_inventory(client, state):
    """Bank surplus food, retrieve stone locally, then verify space and tool."""
    from ..common.home_respawn import WOOL_ITEMS, withdraw_from_home_containers
    from ..common.inventory import deposit_excess_to_chest, drop_items, get_inventory, resolve_storage_location
    from ..common.inventory_disposal import SEED_CARRY_CAP, shed_surplus_seeds, throw_clutter
    from ..common.resources import PLANK_ITEMS
    from ..common.tunnel_miner import DIGGING_JUNK, free_slots
    from .armor_recovery import _home
    from .iron_stockpile import CLUTTER, STORAGE_FOOD_RESERVE, ROOM_WANTED

    # Building leftovers belong at home; retain a wood reserve for field repairs.
    prep_bank_items = set(CLUTTER) | set(DIGGING_JUNK) | set(PLANK_ITEMS) | set(WOOL_ITEMS) | {
        item.replace("_planks", "_door") for item in PLANK_ITEMS
    } | {
        "minecraft:carrot", "minecraft:enchanted_book", "minecraft:lead",
        "minecraft:bell", "minecraft:leaf_litter", "minecraft:raw_copper",
    }

    anchor = _home(state)
    if anchor is None:
        return "no home anchor for inventory preparation"
    # Surplus seeds are thrown, not banked: they filled every home chest.
    # The trip leaves right after, so the thrown stacks despawn unvisited.
    if free_slots(client) < ROOM_WANTED:
        shed_surplus_seeds(client, SEED_CARRY_CAP)
    if free_slots(client) < ROOM_WANTED:
        from .mining_compaction import compact_home_stacks

        compact_home_stacks(client, anchor, ROOM_WANTED)
    chest = resolve_storage_location(client, state=state, verify=True)
    if chest is not None and (
        hypot(chest[0] - anchor[0], chest[2] - anchor[2]) > 32
        or abs(chest[1] - anchor[1]) > 8
    ):
        chest = None  # preparation is local, never a remote storage expedition
    retains = {
        "minecraft:carrot": STORAGE_FOOD_RESERVE,
        "minecraft:cobblestone": 64,
        "minecraft:wheat_seeds": SEED_CARRY_CAP,
    }
    if free_slots(client) < ROOM_WANTED:
        inventory = get_inventory(client)  # Unknown inventory must abort before banking.
        planks = [item for item in PLANK_ITEMS if inventory.get(item, 0) > 0]
        if planks:
            retains[max(planks, key=lambda item: inventory[item])] = 4
    if chest is not None and free_slots(client) < ROOM_WANTED:
        try:
            deposit_excess_to_chest(
                client, chest, deposit_items=prep_bank_items,
                retain_counts=retains, state=state,
            )
        except CommandError as exc:
            # A rejected interaction is not an inventory observation. The
            # guarded overflow path may still recover at the verified home.
            print(f"IRON PREPARATION: home chest interaction rejected ({exc})")
    if not has_mining_pickaxe(client):
        withdraw_from_home_containers(
            client, state, "minecraft:cobblestone", 3,
            origin=anchor, radius=32.0, max_vertical=8.0, recovery=True,
        )
        restore_kit(client)
    if not has_mining_pickaxe(client):
        return "no durable stone-or-better pickaxe for iron"
    if free_slots(client) < ROOM_WANTED:
        from .spoil_purge import purge_home_spoil

        # The trip is about to leave: spoil thrown out of full home chests
        # despawns while the bot is away, and the freed slots take the
        # carried surplus (live A1 2026-10-21: four stacks of carrots).
        for purged in purge_home_spoil(client, state, anchor, ROOM_WANTED - free_slots(client) + 2):
            try:
                deposit_excess_to_chest(
                    client, purged, deposit_items=prep_bank_items,
                    retain_counts=retains, state=state,
                )
            except CommandError as exc:
                print(f"IRON PREPARATION: purged chest interaction rejected ({exc})")
    if free_slots(client) < ROOM_WANTED:
        from .mining_storage import bank_mining_overflow

        bank_mining_overflow(client, state, anchor, ROOM_WANTED, prep_bank_items, retains)
    if free_slots(client) < ROOM_WANTED:
        throw_clutter(client)  # eggs may hatch chicks; snowballs just go
    missing = ROOM_WANTED - free_slots(client)
    if missing > 0:
        # Full home chests left ~350 trips refused for space; plain spoil is
        # cheaper to dig again than another day without iron. Keep the
        # biggest wool stack for a bed; odd colours are spare.
        carried = get_inventory(client)
        wool = sorted((item for item in WOOL_ITEMS if carried.get(item, 0) > 0), key=lambda item: -carried[item])
        drop_items(
            client, sorted(set(DIGGING_JUNK) | LAST_RESORT_JUNK | set(wool[1:]) | {"minecraft:wheat_seeds"}),
            max_stacks=missing, retain_counts=retains,  # seeds keep SEED_CARRY_CAP
        )
    if free_slots(client) < ROOM_WANTED:
        return "not enough verified inventory space for iron"
    return ""
