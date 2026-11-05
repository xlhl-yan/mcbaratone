"""Inventory management - Item counting, crafting, and organization."""

from typing import Dict, Optional, List, Tuple
import logging
import time
from .storage_safety import remember_unreachable_storage, storage_retry_ready

logger = logging.getLogger(__name__)

_last_inventory: Dict[str, int] = {}


def reset_inventory_cache() -> None:
    """Discard the last-known inventory snapshot.

    Inventory reads fail closed when the bridge cannot provide a current
    snapshot.  Clearing the prior observation on respawn prevents later code
    from accidentally treating that historical state as live evidence.
    """
    global _last_inventory
    _last_inventory = {}


def get_inventory(client) -> Dict[str, int]:
    """
    Get aggregated inventory counts.
    
    Returns:
        Dict of item_id -> count
    """
    global _last_inventory
    for attempt in range(3):
        try:
            response = client.transport.dispatch("get_inventory", {})
        except Exception:
            response = None
        from ..inventory_evidence import inventory_counts, unwrap_inventory, valid_inventory
        data = unwrap_inventory(response)
        if not valid_inventory(data):
            if attempt < 2:
                time.sleep(0.3)
            continue
        counts = inventory_counts(data)
        _last_inventory = dict(counts)
        return counts
    raise RuntimeError("Current inventory unavailable after bounded reads; last-known state is not current evidence")



def count_item(client, item_id: str) -> int:
    """
    Count specific item in inventory.
    
    Args:
        item_id: Full item ID (e.g., "minecraft:diamond")
        
    Returns:
        Total count of item
    """
    inventory = get_inventory(client)
    return inventory.get(item_id, 0)


def _safe_close_screen(client, label: str = "") -> None:
    """Best-effort close_screen for stale/laggy bridge responses."""
    try:
        client.transport.dispatch("close_screen", {})
    except Exception as exc:
        detail = f" ({label})" if label else ""
        logger.debug("close_screen no-op%s: %s", detail, exc)


def has_items(client, requirements: Dict[str, int]) -> bool:
    """
    Check if inventory has all required items.
    
    Args:
        requirements: Dict of item_id -> minimum count
        
    Returns:
        True if all requirements met
    """
    inventory = get_inventory(client)
    
    for item_id, required in requirements.items():
        if inventory.get(item_id, 0) < required:
            return False
    return True


def find_item_slot(client, item_id: str) -> Optional[int]:
    """
    Find slot containing specified item.
    
    Args:
        item_id: Item ID to find
        
    Returns:
        Slot index or None
    """
    try:
        response = client.transport.dispatch("get_inventory", {})
        data = response.get("data", response)
        
        for item in data.get("inventory", []):
            if item.get("id") == item_id and item.get("count", 0) > 0:
                return item.get("slot")
        
        return None
        
    except Exception:
        return None


def select_item(client, item_id: str, allow_swap: bool = False) -> bool:
    """
    Select item in hotbar.
    
    Args:
        item_id: Item to select
        allow_swap: If True, swap item from inventory to hotbar if needed
        
    Returns:
        True if item found and selected
    """
    response = client.transport.dispatch("get_inventory", {})
    data = response.get("data", response) if isinstance(response, dict) else {}
    inventory = data.get("inventory", []) if isinstance(data, dict) else []
    carried = [
        entry
        for entry in inventory
        if entry.get("id") == item_id and int(entry.get("count", 0)) > 0
    ]
    if not carried:
        return False
    hotbar_entry = next(
        (entry for entry in carried if 0 <= int(entry.get("slot", -1)) <= 8),
        None,
    )
    slot = int((hotbar_entry or carried[0]).get("slot", -1))

    def select_and_verify(target_slot: int) -> bool:
        client.transport.dispatch("select_slot", {"slot": target_slot})
        verified_response = client.transport.dispatch("get_inventory", {})
        verified = (
            verified_response.get("data", verified_response)
            if isinstance(verified_response, dict)
            else {}
        )
        target = next(
            (
                entry
                for entry in verified.get("inventory", [])
                if int(entry.get("slot", -1)) == target_slot
            ),
            {},
        )
        return (
            verified.get("selected_slot") == target_slot
            and target.get("id") == item_id
            and int(target.get("count", 0)) > 0
        )

    # Hotbar is slots 0-8
    if 0 <= slot <= 8:
        return select_and_verify(slot)
    
    if allow_swap:
        occupied = {
            int(entry.get("slot", -1))
            for entry in inventory
            if entry.get("id") not in (None, "minecraft:air")
            and int(entry.get("count", 0)) > 0
        }
        target_slot = next(
            (candidate for candidate in range(9) if candidate not in occupied),
            0,
        )
        # get_inventory uses logical player slots, while inventory_click uses
        # the current container menu. Close other screens, then use vanilla's
        # atomic SWAP action: the clicked main-inventory slot is 9-35 and the
        # button is the logical hotbar index. This cannot leave an item on the
        # cursor after a partial three-click PICKUP sequence.
        client.transport.dispatch("close_screen", {})
        client.transport.dispatch(
            "inventory_click",
            {"slot": slot, "type": "SWAP", "button": target_slot},
        )
        swapped_response = client.transport.dispatch("get_inventory", {})
        swapped = (
            swapped_response.get("data", swapped_response)
            if isinstance(swapped_response, dict)
            else {}
        )
        target = next(
            (
                entry
                for entry in swapped.get("inventory", [])
                if int(entry.get("slot", -1)) == target_slot
            ),
            {},
        )
        if target.get("id") != item_id or int(target.get("count", 0)) <= 0:
            return False
        return select_and_verify(target_slot)

    return False


def equip_best_weapon(client, target_type: object = "") -> bool:
    """Equip the best fresh, durable, target-aware melee weapon."""
    from .combat_loadout import equip_best_weapon as select_combat_weapon

    return select_combat_weapon(client, target_type)


_ARMOR_RANK = {
    "leather": 1,
    "golden": 2,
    "chainmail": 3,
    "iron": 4,
    "diamond": 5,
    "netherite": 6,
}
_ARMOR_PIECES = ("helmet", "chestplate", "leggings", "boots")
MIN_COMBAT_ARMOR_DURABILITY = 64
_PLAYER_ARMOR_CONTAINER_SLOTS = {
    "helmet": 5,
    "chestplate": 6,
    "leggings": 7,
    "boots": 8,
}


def _armor_identity(item_id: str):
    name = item_id.split(":", 1)[-1]
    for material, rank in _ARMOR_RANK.items():
        for piece in _ARMOR_PIECES:
            if name == f"{material}_{piece}":
                return material, piece, rank
    return None


def get_equipped_armor(client) -> Dict[str, str]:
    """Return equipped armor as ``piece -> item id`` from bridge truth."""
    try:
        response = client.transport.dispatch("get_inventory", {})
        data = response.get("data", response)
        equipped: Dict[str, str] = {}
        for item in data.get("armor", []):
            item_id = item.get("id", "")
            identity = _armor_identity(item_id)
            if identity and item.get("count", 0) > 0:
                equipped[identity[1]] = item_id
        return equipped
    except Exception as exc:
        logger.warning("Could not inspect equipped armor: %s", exc)
        return {}


def get_equipped_armor_details(client) -> Dict[str, Dict]:
    """Return raw equipped armor records keyed by armor piece."""
    try:
        response = client.transport.dispatch("get_inventory", {})
        data = response.get("data", response)
        equipped = {}
        for item in data.get("armor", []):
            identity = _armor_identity(item.get("id", ""))
            if identity and item.get("count", 0) > 0:
                equipped[identity[1]] = dict(item)
        return equipped
    except Exception as exc:
        logger.warning("Could not inspect armor durability: %s", exc)
        return {}


def _remaining_durability(item: Dict) -> Optional[int]:
    maximum = int(item.get("max_damage", 0) or 0)
    if maximum <= 0:
        return None
    return max(0, maximum - max(0, int(item.get("damage", 0) or 0)))


def armor_piece_is_durable(
    client,
    item_id: str,
    minimum_remaining: int = MIN_COMBAT_ARMOR_DURABILITY,
) -> bool:
    """Verify one exact equipped armor item has a safe durability reserve."""
    identity = _armor_identity(item_id)
    if identity is None:
        return False
    equipped = get_equipped_armor_details(client).get(identity[1])
    if not equipped or equipped.get("id") != item_id:
        return False
    remaining = _remaining_durability(equipped)
    return remaining is None or remaining >= minimum_remaining


def has_full_armor(client, minimum_material: str = "iron") -> bool:
    """Verify all four equipped pieces meet a minimum material tier."""
    minimum_rank = _ARMOR_RANK.get(minimum_material, _ARMOR_RANK["iron"])
    equipped = get_equipped_armor(client)
    for piece in _ARMOR_PIECES:
        identity = _armor_identity(equipped.get(piece, ""))
        if not identity or identity[2] < minimum_rank:
            return False
    return True


def has_durable_full_armor(
    client,
    minimum_material: str = "iron",
    minimum_remaining: int = MIN_COMBAT_ARMOR_DURABILITY,
) -> bool:
    """Verify armor tier and a remaining-durability floor on every piece."""
    minimum_rank = _ARMOR_RANK.get(minimum_material, _ARMOR_RANK["iron"])
    equipped = get_equipped_armor_details(client)
    for piece in _ARMOR_PIECES:
        item = equipped.get(piece)
        identity = _armor_identity(item.get("id", "")) if item else None
        if not identity or identity[2] < minimum_rank:
            return False
        remaining = _remaining_durability(item)
        if remaining is not None and remaining < minimum_remaining:
            return False
    return True


def equip_best_armor(client) -> int:
    """Equip the best carried armor; return the number of worn pieces."""
    try:
        client.transport.dispatch("close_screen", {})
    except Exception:
        pass

    for piece in _ARMOR_PIECES:
        response = client.transport.dispatch("get_inventory", {})
        data = response.get("data", response)
        equipped_details = get_equipped_armor_details(client)
        current_item = equipped_details.get(piece, {})
        current = current_item.get("id")
        current_identity = _armor_identity(current or "")
        current_rank = current_identity[2] if current_identity else 0
        current_remaining = _remaining_durability(current_item)
        candidates = []
        for item in data.get("inventory", []):
            identity = _armor_identity(item.get("id", ""))
            if not identity or identity[1] != piece or item.get("count", 0) <= 0:
                continue
            remaining = _remaining_durability(item)
            candidates.append((identity[2], remaining or 0, item))
        if not candidates:
            continue
        from .combat_loadout import choose_armor_replacement
        selected = choose_armor_replacement(
            candidates,
            current_rank=current_rank,
            current_remaining=current_remaining,
            minimum_rank=_ARMOR_RANK["iron"],
            minimum_remaining=MIN_COMBAT_ARMOR_DURABILITY,
        )
        if selected is None:
            continue
        _best_rank, _best_remaining, best_item = selected

        if _best_rank <= current_rank and (current_remaining or 0) >= _best_remaining:
            continue
        try:
            inventory_slot = int(best_item["slot"])
            if not 0 <= inventory_slot < 36:
                raise ValueError("Armor candidate is not in player inventory")
            player_slot = 36 + inventory_slot if 0 <= inventory_slot <= 8 else inventory_slot
            # Stage the exact stack in hotbar zero, exchange with armor,
            # restore the hotbar. SWAP needs neither cursor nor empty space.
            staged = inventory_slot != 0
            if staged:
                client.transport.dispatch("inventory_click", {
                    "slot": player_slot, "type": "SWAP", "button": 0, "sync_id": 0})
            client.transport.dispatch(
                "inventory_click",
                {"slot": _PLAYER_ARMOR_CONTAINER_SLOTS[piece], "type": "SWAP", "button": 0, "sync_id": 0},
            )
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                worn = get_equipped_armor_details(client).get(piece, {})
                if (
                    worn.get("id") == best_item["id"]
                    and worn.get("damage", 0) == best_item.get("damage", 0)
                    and worn.get("components", {}) == best_item.get("components", {})
                ):
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("Selected armor was not observed equipped")
            if staged:
                client.transport.dispatch("inventory_click", {
                    "slot": player_slot, "type": "SWAP", "button": 0, "sync_id": 0})
        except Exception as exc:
            logger.warning("Failed to equip %s: %s", best_item.get("id"), exc)
            # Unknown clicks require reconciliation, not mutation/rollback.
            break

    return len(get_equipped_armor(client))


def equip_offhand(client, item_id: str) -> bool:
    """
    Equip item to offhand slot.
    """
    try:
        slot = find_item_slot(client, item_id)
        if slot is None:
            return False
            
        # 45 is usually offhand
        # or use inventory_click with swap
        # The bridge might not have a direct 'equip_offhand' macro, so we try a click interaction or dispatch a simple 'equip' check if available.
        # Standard minecraft protocol: Swap item to slot 45 (offhand)
        
        # NOTE: Baritone generic 'click' might be needed.
        # Let's try to swap slot with offhand slot (45)
        
        client.transport.dispatch("inventory_click", {
            "slot": slot,
            "type": "SWAP",
            "button": 40, # 'F' key swap usually? Or verify slot ID 45? 
            # Actually SWAP with offhand is a specific packet action often found in 1.9+
            # If the bridge exposes standard click:
            # Slot 45 is offhand.
            
            # Simple fallback: use "equip" command if the bridge supports it? 
            # Or assume the bridge has "equip" macro.
        })
        
        # Actually, let's look at the bridge capabilities. 
        # If 'inventory_click' is raw, we need exact slot IDs.
        # Use simpler approach: Send a client-side command if possible, or try to drag-and-drop.
        
        # Attempt 1: Swap with offhand key (F)
        # client.transport.dispatch("input", {"key": "key.swapOffhand"}) 
        # But that swaps current hotbar item.
        
        # Attempt 2: Pickup item, Click offhand slot (45)
        # Click source
        client.transport.dispatch("inventory_click", {"slot": slot, "type": "PICKUP", "button": 0})
        time.sleep(0.1)
        # Click offhand (45)
        client.transport.dispatch("inventory_click", {"slot": 45, "type": "PICKUP", "button": 0})
        time.sleep(0.1)
        # If we had something in offhand, it's now on cursor, put it back in source (or first empty)
        # For simplicity, put back in source (swap)
        client.transport.dispatch("inventory_click", {"slot": slot, "type": "PICKUP", "button": 0})
        
        return True
    except Exception as e:
        print(f"Offhand equip failed: {e}")
        return False



_PLANK_WOODS = [
    "oak",
    "spruce",
    "birch",
    "dark_oak",
    "acacia",
    "jungle",
    "mangrove",
    "cherry",
    "pale_oak",
]


def _craft_family_count(client, item_id: str) -> int:
    """Plank requests count all wood types: the bridge crafts from whatever
    logs are in inventory (asking for oak_planks with birch logs yields
    birch_planks)."""
    if item_id.split(":")[-1].endswith("_planks"):
        return sum(count_item(client, f"minecraft:{w}_planks") for w in _PLANK_WOODS)
    return count_item(client, item_id)


def _craft_log_family_for_planks(log_family: str) -> str:
    family = log_family.split(":")[-1]
    if family.endswith("_log"):
        family = family[:-4]
    return f"minecraft:{family}_planks"


def _first_log_family_with_stock(client, required_logs: int = 1) -> str:
    """Return the first available log family with at least ``required_logs`` stock."""
    for wood in _PLANK_WOODS:
        log_id = f"minecraft:{wood}_log"
        if count_item(client, log_id) >= required_logs:
            return log_id
    return ""


def _ensure_raw_planks(client, required_planks: int) -> bool:
    """Ensure at least ``required_planks`` oak-family planks are on hand,
    gathering and converting logs from the world if the carried supply is
    short."""
    required_planks = max(0, required_planks)
    current_planks = _craft_family_count(client, "minecraft:oak_planks")
    if current_planks >= required_planks:
        return True

    missing_planks = required_planks - current_planks
    while missing_planks > 0:
        log_family = _first_log_family_with_stock(client, 1)
        if not log_family:
            # No carried logs: gather a small reserve instead of failing the
            # whole tool craft (same ingredient-acquisition gap as the
            # stone-material fix; live blocker "failed to prepare planks for
            # minecraft:iron_pickaxe" with zero logs in inventory).
            from .resources import gather_wood  # lazy: resources imports this module

            needed_logs = max(1, (missing_planks + 3) // 4)
            print(
                f"  [Craft Debug] no carried logs for {missing_planks} planks; "
                f"gathering {needed_logs} logs first..."
            )
            gather_wood(client, count=needed_logs, timeout=300)
            log_family = _first_log_family_with_stock(client, 1)
        if not log_family:
            return False
        plank_family = _craft_log_family_for_planks(log_family)
        planks_to_craft = ((missing_planks + 3) // 4) * 4
        logs_to_use = min(count_item(client, log_family), max(1, (planks_to_craft + 3) // 4))
        planks_to_craft = max(4, min(planks_to_craft, logs_to_use * 4))
        if not craft(client, plank_family, planks_to_craft):
            return False
        current_planks = _craft_family_count(client, "minecraft:oak_planks")
        if current_planks >= required_planks:
            return True
        missing_planks = required_planks - current_planks

    return True


def _ensure_planks_for_sticks(client, required_sticks: int) -> bool:
    """Ensure enough planks are available to craft ``required_sticks``.

    The stick recipe yields FOUR sticks from TWO planks, so the plank cost is
    ``ceil(sticks / 4) * 2`` - two planks make a full batch. The old formula
    (``sticks * 2``) demanded 6 planks for 3 sticks when 2 suffice, which
    dead-locked FOOD_AND_IRON's deep-mining prep: with 5 planks and no logs it
    thought it was one plank short and failed trying to gather a log it did
    not need.
    """
    required_sticks = max(0, required_sticks)
    batches = (required_sticks + 3) // 4  # 4 sticks per craft batch
    required_planks = batches * 2
    return _ensure_raw_planks(client, required_planks)


# Planks consumed directly as a wooden tool's head material (mirrors the
# manual-grid recipe table in tests/functional/shared/inventory_ops.py).
_WOODEN_TOOL_MAT_PLANKS = {
    "pickaxe": 3,
    "axe": 3,
    "shovel": 1,
    "sword": 2,
    "hoe": 2,
}


def _ensure_wooden_tool_ingredients(client, item_id: str, count: int) -> bool:
    """Reserve planks for a wooden tool's head material *and* its sticks in
    one gathering pass, before either consumes carried wood.

    ``ensure_tool_sticks`` only tops planks up to what the stick half of the
    recipe needs. Gathering the two halves separately can leave the tool
    head one plank short with nothing left to trigger further gathering: a
    from-scratch craft (e.g. right after death wipes the inventory) chops
    just enough wood for sticks, spends it all there, and then retries a
    failing manual craft forever. Confirmed live: Bot09 stuck looping
    "Manual minecraft:wooden_pickaxe missing #planks" after respawning with
    zero items.
    """
    from . import harness_ops

    parsed = harness_ops.parse_tool_id(item_id)
    if not parsed:
        return True
    material, tool_type = parsed
    if material != "_planks":
        return True

    count = max(1, count)
    mat_planks = _WOODEN_TOOL_MAT_PLANKS.get(tool_type, 3) * count
    sticks_per_tool = 1 if tool_type == "sword" else 2
    required_sticks = sticks_per_tool * count
    current_sticks = count_item(client, "minecraft:stick")
    missing_sticks = max(0, required_sticks - current_sticks)
    stick_batches = (missing_sticks + 3) // 4
    stick_planks = stick_batches * 2
    return _ensure_raw_planks(client, mat_planks + stick_planks)


# Progression recipes the bridge cannot craft: native `craft` depends on
# recipe listing (broken on the live 1.21.x client) and `auto_craft` only
# knows a hardcoded slice (planks, sticks, stone/wood tools, iron
# pickaxe/sword, torch, chest, furnace, crafting table). Everything else on
# the road to the Dragon must be drivable through the verified manual grid
# (slots are 1-9 row-major; "#planks" matches any plank family). The bucket
# entry replaced the first live dead-end ("Prepare deep-mining tools +
# bucket"); the rest are the recipes the progression plan is known to need
# ahead of time: shield (iron defense), enchanting line (paper, book,
# bookshelf, enchanting table), nether line (flint and steel, blaze powder,
# eye of ender), and dragon-fight ranged gear (bow, arrow) plus ladders.
_MANUAL_GRID_RECIPES: Dict[str, Dict] = {
    # Two planks stacked vertically -> four sticks. Sticks are otherwise
    # auto_craft-only, so a dirty/left-open crafting-table grid (e.g. right
    # after a manual bucket craft) made "all methods failed for minecraft:stick"
    # dead-end FOOD_AND_IRON's iron-pickaxe preparation. The manual driver
    # clears the grid before placing, so this fallback recovers cleanly.
    "minecraft:stick": {
        "placements": [
            ("#planks", 1),
            ("#planks", 4),
        ],
        "output": 4,
    },
    # Fuel above a stick -> four torches. There was no manual fallback for
    # torches at all, so when the bridge's craft reported "Missing
    # ingredients" (it wants literal coal) and auto_craft reported "Recipe
    # not found", every method failed. Live 2026-07-31: Bot16 sat on 6
    # charcoal and 4 sticks failing torch_supply 50 times, the last step
    # standing between the fleet and its first BOOT_SEQUENCE completion.
    "minecraft:torch": {
        "placements": [
            ("#coals", 1),
            ("minecraft:stick", 4),
        ],
        "output": 4,
    },
    # BOOT_SEQUENCE / BASE_CONSTRUCTION infrastructure. Planks and the
    # crafting table intentionally stay on their dedicated 2x2 bootstrap
    # drivers: requiring an open table to craft the table would deadlock a
    # fresh or post-death inventory.
    "minecraft:furnace": {
        "placements": [
            ("minecraft:cobblestone", 1),
            ("minecraft:cobblestone", 2),
            ("minecraft:cobblestone", 3),
            ("minecraft:cobblestone", 4),
            ("minecraft:cobblestone", 6),
            ("minecraft:cobblestone", 7),
            ("minecraft:cobblestone", 8),
            ("minecraft:cobblestone", 9),
        ],
    },
    "minecraft:chest": {
        "placements": [
            ("#planks", 1),
            ("#planks", 2),
            ("#planks", 3),
            ("#planks", 4),
            ("#planks", 6),
            ("#planks", 7),
            ("#planks", 8),
            ("#planks", 9),
        ],
    },
    "minecraft:white_bed": {
        "placements": [
            ("minecraft:white_wool", 1),
            ("minecraft:white_wool", 2),
            ("minecraft:white_wool", 3),
            ("#planks", 4),
            ("#planks", 5),
            ("#planks", 6),
        ],
    },
    # Villager breeding requires six bread. The live client cannot list this
    # recipe, and its three-wide shape does not fit the player 2x2 grid.
    "minecraft:bread": {
        "placements": [
            ("minecraft:wheat", 4),
            ("minecraft:wheat", 5),
            ("minecraft:wheat", 6),
        ],
    },
    # The starter-house repair path derives the door id from the carried
    # plank family. Keep these selectors literal so mixed planks cannot craft
    # a different door from the expected output.
    **{
        f"minecraft:{wood}_door": {
            "placements": [
                (f"minecraft:{wood}_planks", 1),
                (f"minecraft:{wood}_planks", 2),
                (f"minecraft:{wood}_planks", 4),
                (f"minecraft:{wood}_planks", 5),
                (f"minecraft:{wood}_planks", 7),
                (f"minecraft:{wood}_planks", 8),
            ],
            "output": 3,
        }
        for wood in (
            "oak",
            "spruce",
            "birch",
            "jungle",
            "acacia",
            "dark_oak",
            "mangrove",
            "cherry",
            "bamboo",
        )
    },
    # BOOT_SEQUENCE tools and farm equipment.
    "minecraft:wooden_hoe": {
        "placements": [
            ("#planks", 1),
            ("#planks", 2),
            ("minecraft:stick", 5),
            ("minecraft:stick", 8),
        ],
    },
    **{
        f"minecraft:stone_{tool}": {
            "placements": [
                *(("minecraft:cobblestone", slot) for slot in head_slots),
                *(("minecraft:stick", slot) for slot in stick_slots),
            ],
        }
        for tool, head_slots, stick_slots in (
            ("pickaxe", (1, 2, 3), (5, 8)),
            ("axe", (1, 2, 4), (5, 8)),
            ("shovel", (2,), (5, 8)),
            ("sword", (2, 5), (8,)),
        )
    },
    # FOOD_AND_IRON completion and deep-mining preparation can demand the
    # complete iron loadout, not only the pickaxe and sword in the verifier.
    **{
        f"minecraft:iron_{tool}": {
            "placements": [
                *(("minecraft:iron_ingot", slot) for slot in head_slots),
                *(("minecraft:stick", slot) for slot in stick_slots),
            ],
        }
        for tool, head_slots, stick_slots in (
            ("pickaxe", (1, 2, 3), (5, 8)),
            ("axe", (1, 2, 4), (5, 8)),
            ("shovel", (2,), (5, 8)),
            ("sword", (2, 5), (8,)),
        )
    },
    # Diamond tier. The harness carries craft_diamond_pickaxe_manual and
    # craft_diamond_axe_manual, but neither had a grid recipe here, so a bot
    # that mined diamonds could not turn them into tools. That is a hard stop
    # rather than an inefficiency: obsidian for the nether portal can only be
    # mined with a diamond pickaxe, so NETHER_AND_BLAZE is unreachable
    # without this.
    **{
        f"minecraft:diamond_{tool}": {
            "placements": [
                *(("minecraft:diamond", slot) for slot in head_slots),
                *(("minecraft:stick", slot) for slot in stick_slots),
            ],
        }
        for tool, head_slots, stick_slots in (
            ("pickaxe", (1, 2, 3), (5, 8)),
            ("axe", (1, 2, 4), (5, 8)),
            ("shovel", (2,), (5, 8)),
            ("sword", (2, 5), (8,)),
        )
    },
    # Wooden tier: the bootstrap loadout after a death that loses everything.
    **{
        f"minecraft:wooden_{tool}": {
            "placements": [
                *(("#planks", slot) for slot in head_slots),
                *(("minecraft:stick", slot) for slot in stick_slots),
            ],
        }
        for tool, head_slots, stick_slots in (
            ("pickaxe", (1, 2, 3), (5, 8)),
            ("axe", (1, 2, 4), (5, 8)),
            ("shovel", (2,), (5, 8)),
            ("sword", (2, 5), (8,)),
        )
    },
    # Hoe variants beyond the wooden one already present; the micro-farm and
    # the FOOD_AND_IRON farm both till with whatever hoe is carried.
    "minecraft:stone_hoe": {
        "placements": [
            ("minecraft:cobblestone", 1),
            ("minecraft:cobblestone", 2),
            ("minecraft:stick", 5),
            ("minecraft:stick", 8),
        ],
    },
    "minecraft:iron_hoe": {
        "placements": [
            ("minecraft:iron_ingot", 1),
            ("minecraft:iron_ingot", 2),
            ("minecraft:stick", 5),
            ("minecraft:stick", 8),
        ],
    },
    **{
        f"minecraft:iron_{piece}": {
            "placements": [
                ("minecraft:iron_ingot", slot) for slot in slots
            ],
        }
        for piece, slots in (
            ("helmet", (1, 2, 3, 4, 6)),
            ("chestplate", (1, 3, 4, 5, 6, 7, 8, 9)),
            ("leggings", (1, 2, 3, 4, 6, 7, 9)),
            ("boots", (4, 6, 7, 9)),
        )
    },
    "minecraft:bucket": {
        "placements": [
            ("minecraft:iron_ingot", 1),
            ("minecraft:iron_ingot", 3),
            ("minecraft:iron_ingot", 5),
        ],
    },
    "minecraft:shield": {
        "placements": [
            ("#planks", 1),
            ("minecraft:iron_ingot", 2),
            ("#planks", 3),
            ("#planks", 4),
            ("#planks", 5),
            ("#planks", 6),
            ("#planks", 8),
        ],
    },
    "minecraft:flint_and_steel": {
        "placements": [
            ("minecraft:iron_ingot", 1),
            ("minecraft:flint", 2),
        ],
    },
    "minecraft:paper": {
        "placements": [
            ("minecraft:sugar_cane", 1),
            ("minecraft:sugar_cane", 2),
            ("minecraft:sugar_cane", 3),
        ],
        "output": 3,
    },
    "minecraft:book": {
        "placements": [
            ("minecraft:paper", 1),
            ("minecraft:paper", 2),
            ("minecraft:paper", 3),
            ("minecraft:leather", 4),
        ],
    },
    "minecraft:bookshelf": {
        "placements": [
            ("#planks", 1),
            ("#planks", 2),
            ("#planks", 3),
            ("minecraft:book", 4),
            ("minecraft:book", 5),
            ("minecraft:book", 6),
            ("#planks", 7),
            ("#planks", 8),
            ("#planks", 9),
        ],
    },
    "minecraft:enchanting_table": {
        "placements": [
            ("minecraft:book", 2),
            ("minecraft:diamond", 4),
            ("minecraft:obsidian", 5),
            ("minecraft:diamond", 6),
            ("minecraft:obsidian", 7),
            ("minecraft:obsidian", 8),
            ("minecraft:obsidian", 9),
        ],
    },
    "minecraft:blaze_powder": {
        "placements": [("minecraft:blaze_rod", 1)],
        "output": 2,
    },
    "minecraft:ender_eye": {
        "placements": [
            ("minecraft:blaze_powder", 1),
            ("minecraft:ender_pearl", 2),
        ],
    },
    "minecraft:shulker_box": {
        "placements": [
            ("minecraft:shulker_shell", 2),
            ("minecraft:chest", 5),
            ("minecraft:shulker_shell", 8),
        ],
    },
    "minecraft:bow": {
        "placements": [
            ("minecraft:stick", 2),
            ("minecraft:string", 3),
            ("minecraft:stick", 4),
            ("minecraft:string", 6),
            ("minecraft:stick", 8),
            ("minecraft:string", 9),
        ],
    },
    "minecraft:arrow": {
        "placements": [
            ("minecraft:flint", 2),
            ("minecraft:stick", 5),
            ("minecraft:feather", 8),
        ],
        "output": 4,
    },
    "minecraft:ladder": {
        "placements": [
            ("minecraft:stick", 1),
            ("minecraft:stick", 3),
            ("minecraft:stick", 4),
            ("minecraft:stick", 5),
            ("minecraft:stick", 6),
            ("minecraft:stick", 7),
            ("minecraft:stick", 9),
        ],
        "output": 3,
    },
}


def _wait_craft_result(client, item_id: str, target: int, timeout: float = 6.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _craft_family_count(client, item_id) >= target:
            return True
        time.sleep(0.5)
    return False


def ensure_tool_sticks(client, item_id: str, count: int = 1) -> bool:
    """Craft the stick dependency required by a tool recipe.

    Tool crafting can be entered from several progression paths.  The bridge
    does not recursively craft recipe ingredients, so asking it for a tool
    while the player has planks but no sticks silently fails.  Prepare sticks
    in the player 2x2 grid before any code opens a crafting table.
    """
    from . import harness_ops

    parsed = harness_ops.parse_tool_id(item_id)
    if not parsed:
        return True

    _material, tool_type = parsed
    sticks_per_tool = 1 if tool_type == "sword" else 2
    required = sticks_per_tool * max(1, count)
    current = count_item(client, "minecraft:stick")
    if current >= required:
        return True

    _safe_close_screen(client, "craft sticks")

    missing = required - current
    if not _ensure_planks_for_sticks(client, missing):
        print(f"  [Craft Debug] failed to prepare planks for {item_id}")
        return False
    print(f"  [Craft Debug] preparing sticks for {item_id} ({current}/{required})...")
    if not craft(client, "minecraft:stick", max(4, missing)):
        return False
    return count_item(client, "minecraft:stick") >= required


def _nearest_local_crafting_table(client, found_tables, *, maximum_distance: float = 12.0):
    from .crafting_workstation import nearest_local_crafting_table

    return nearest_local_crafting_table(client, found_tables, maximum_distance=maximum_distance)


def craft(client, item_id: str, count: int = 1) -> bool:
    """
    Craft an item, verified by inventory delta (a status=ok reply does not
    mean anything appeared - the 1.21.4 client has no recipe listing, so
    crafting silently no-ops in several situations).

    Fallback chain: bridge "craft" -> "auto_craft" -> manual grid clicks via
    the functional-harness library for recipes it knows (tools, beds, doors,
    chests).

    Args:
        item_id: Item to craft
        count: Number to craft

    Returns:
        True if the items verifiably appeared in inventory
    """
    from . import harness_ops

    if item_id == "minecraft:bread":
        from .enclosed_workstation import prepare_sheltered_bread_craft
        if not prepare_sheltered_bread_craft(client, harness_ops.ensure_crafting_table_open):
            return False
    # A table is a four-plank player-grid bootstrap, not a 3x3-grid recipe.
    if item_id == "minecraft:crafting_table" and not _ensure_raw_planks(
        client, 4 * max(1, count)
    ):
        print("  [Craft Debug] could not prepare planks for minecraft:crafting_table")
        return False

    if not _ensure_wooden_tool_ingredients(client, item_id, count):
        print(f"  [Craft Debug] could not prepare wooden tool ingredients for {item_id}")
        return False

    if not ensure_tool_sticks(client, item_id, count):
        print(f"  [Craft Debug] could not prepare stick dependency for {item_id}")
        return False

    before = _craft_family_count(client, item_id)
    target = before + count

    # Recipe lookup is unavailable on this client, and the native recipe
    # handler can leave unrelated ingredients arranged in the player grid.
    # Planks are a one-cell 2x2 recipe, so drive that deterministic path first.
    if item_id.split(":")[-1].endswith("_planks") and harness_ops.available():
        try:
            if harness_ops.craft_planks_manual(client, item_id, count):
                return _craft_family_count(client, item_id) >= target
        except Exception as exc:
            print(f"  [Craft Debug] manual plank craft failed for {item_id}: {exc}")

    # Native 1.21.8 tool crafting can leave an invalid recipe in the 3x3
    # table.  Tool recipes have a verified manual implementation below, so
    # skip both corrupting native attempts and go straight to that path.
    manual_tool = (
        harness_ops.available()
        and harness_ops.parse_tool_id(item_id) is not None
    )
    # Sticks remain on the bridge's small reliable auto-craft slice and are
    # needed before a table is open. Other explicit progression recipes should
    # skip the known-broken native lookup and go straight to the verified grid.
    manual_recipe = manual_tool or item_id == "minecraft:crafting_table" or (
        item_id in _MANUAL_GRID_RECIPES and item_id != "minecraft:stick"
    )

    if not manual_recipe:
        try:
            client.transport.dispatch("craft", {"item": item_id, "count": count})
        except Exception as e:
            print(f"  [Craft Debug] craft dispatch failed for {item_id}: {e}")
        if _wait_craft_result(client, item_id, target):
            return True

        try:
            client.transport.dispatch("auto_craft", {"item": item_id, "quantity": count})
        except Exception as e:
            print(f"  [Craft Debug] auto_craft dispatch failed for {item_id}: {e}")
        if _wait_craft_result(client, item_id, target, timeout=8.0):
            return True

    if harness_ops.available():
        name = item_id.split(":")[-1]
        manual = None
        if item_id in _MANUAL_GRID_RECIPES and item_id != "minecraft:stick":
            spec = _MANUAL_GRID_RECIPES[item_id]
            output_per_recipe = spec.get("output", 1)
            crafts = max(1, (count + output_per_recipe - 1) // output_per_recipe)
            manual = lambda: harness_ops.craft_recipe_manual(
                client,
                item_id,
                spec["placements"],
                crafts=crafts,
                output_per_recipe=output_per_recipe,
            )
        elif harness_ops.parse_tool_id(item_id):
            manual = lambda: harness_ops.craft_tool_manual(client, item_id)
        elif harness_ops.parse_armor_id(item_id):
            manual = lambda: harness_ops.craft_armor_manual(client, item_id)
        elif name.endswith("_bed"):
            manual = lambda: harness_ops.craft_bed_manual(client, item_id)
        elif name.endswith("_door"):
            manual = lambda: harness_ops.craft_door_manual(client, item_id)
        elif name == "chest":
            manual = lambda: harness_ops.craft_chest_manual(client)
        elif name == "furnace":
            manual = lambda: harness_ops.craft_furnace_manual(client)
        elif name == "crafting_table":
            manual = lambda: harness_ops.craft_crafting_table_manual(client)
        elif item_id in _MANUAL_GRID_RECIPES:
            spec = _MANUAL_GRID_RECIPES[item_id]
            output_per_recipe = spec.get("output", 1)
            crafts = max(1, (count + output_per_recipe - 1) // output_per_recipe)
            manual = lambda: harness_ops.craft_recipe_manual(
                client,
                item_id,
                spec["placements"],
                crafts=crafts,
                output_per_recipe=output_per_recipe,
            )

        if manual is not None:
            print(f"  [Craft Debug] falling back to manual grid crafting for {item_id}...")
            try:
                if name == "crafting_table":
                    client.transport.dispatch("close_screen", {})
                else:
                    # A generic open GUI is not proof that the 3x3 crafting
                    # table is open (the player 2x2 screen also has slots).
                    # When no table is carried, make one before asking the
                    # placement harness to enumerate candidates. The old
                    # order spent tens of seconds trying to place a missing
                    # item at every candidate before reaching this recovery.
                    table_open = False
                    if count_item(client, "minecraft:crafting_table") == 0:
                        nearby = client.transport.dispatch(
                            "find_blocks",
                            {
                                "blocks": ["minecraft:crafting_table"],
                                "radius": 8,
                                "limit": 16,
                            },
                        )
                        found_tables = nearby.get("found")
                        if found_tables:
                            local_table = _nearest_local_crafting_table(
                                client,
                                found_tables,
                            )
                            if local_table is not None:
                                table_open = harness_ops.ensure_crafting_table_open(
                                    client,
                                    table_pos=local_table,
                                )
                        elif found_tables is None:
                            # Compatibility for transports without block search.
                            table_open = harness_ops.ensure_crafting_table_open(client)

                        if not table_open:
                            client.transport.dispatch("close_screen", {})
                            if craft(client, "minecraft:crafting_table", 1):
                                table_open = harness_ops.ensure_crafting_table_open(client)
                    else:
                        table_open = harness_ops.ensure_crafting_table_open(client)
                    if not table_open:
                        print(f"  [Craft Debug] no verified crafting table for {item_id}")
                        return False
                if manual():
                    return True
            except Exception as e:
                print(f"  [Craft Debug] manual grid craft failed for {item_id}: {e}")

    print(f"  [Craft Debug] all methods failed for {item_id} (have {_craft_family_count(client, item_id)}, wanted {target})")
    return False


def get_recipes_for(client, item_id: str) -> List[Dict]:
    """
    Get crafting recipes for an item.
    
    Args:
        item_id: Item to get recipes for
        
    Returns:
        List of recipe dictionaries
    """
    try:
        response = client.transport.dispatch("get_recipes", {
            "filter": item_id,
            "limit": 5,
        })
        
        if response.get("status") == "ok":
            return response.get("data", {}).get("recipes", [])
        return []
        
    except Exception:
        return []


def _storage_position_from_mapping(value) -> Optional[Tuple[int, int, int]]:
    """Normalize a storage checkpoint or StateManager location entry."""
    if not isinstance(value, dict):
        return None
    data = value.get("data", value)
    if not isinstance(data, dict):
        return None
    try:
        return (int(data["x"]), int(data["y"]), int(data["z"]))
    except (KeyError, TypeError, ValueError):
        return None


def resolve_storage_location(
    client,
    state=None,
    *,
    verify: bool = True,
) -> Optional[Tuple[int, int, int]]:
    """Resolve the newest usable chest from production or legacy state.

    ``StateManager`` stores landmarks inside
    ``spawn_to_dragon_checkpoint.json`` while ``WorldState`` stores
    ``checkpoint_storage.json``. Older code wrote one format and read the
    other. This compatibility resolver accepts both and, by default, refuses
    stale coordinates that no longer contain a chest.
    """

    candidates: List[Tuple[int, int, int]] = []

    if state is not None:
        custom_data = getattr(state, "custom_data", {})
        supply_chest = (
            custom_data.get("structures", {})
            .get("starter_house", {})
            .get("supply_chest")
        )
        if isinstance(supply_chest, (list, tuple)) and len(supply_chest) == 3:
            try:
                candidates.append(tuple(int(value) for value in supply_chest))
            except (TypeError, ValueError):
                pass
        try:
            location_map = state.get_locations("chest")
            locations = location_map.get("chest", [])
        except (AttributeError, TypeError):
            locations = (
                getattr(state, "custom_data", {})
                .get("locations", {})
                .get("chest", [])
            )
        for location in reversed(locations):
            position = _storage_position_from_mapping(location)
            if position is not None and position not in candidates:
                candidates.append(position)

    from .state import WorldState

    legacy = WorldState(client).load_checkpoint("storage")
    legacy_position = _storage_position_from_mapping(legacy)
    if legacy_position is not None and legacy_position not in candidates:
        candidates.append(legacy_position)

    unloaded_candidate = None
    for position in candidates:
        if not verify:
            return position
        x, y, z = position
        try:
            block_id = client.transport.dispatch(
                "get_block", {"x": x, "y": y, "z": z}
            ).get("id", "")
        except Exception as exc:
            print(f"STORAGE: could not verify saved chest at {position}: {exc}")
            continue
        if "chest" in block_id:
            return position
        # The bridge reports void_air for coordinates in an unloaded chunk,
        # and an empty id when the read itself yielded nothing. Neither is
        # proof that a persisted chest was removed. Return the landmark so the
        # caller can path there, load the chunk, and perform the stronger
        # open-container verification.
        if _classify_container_read(block_id) == "unknown":
            print(f"STORAGE: saved chest at {position} is in an unloaded chunk; retaining landmark")
            if unloaded_candidate is None:
                unloaded_candidate = position
            continue
        print(f"STORAGE: ignoring stale saved location {position} ({block_id or 'unknown'})")
        try:
            from .storage_catalog import catalog_for

            dimension = client.transport.dispatch("get_state", {}).get(
                "dimension", "minecraft:overworld"
            )
            catalog_for(client, state).mark_missing(
                position,
                dimension=str(dimension),
                observed_block_id=block_id,
            )
        except Exception as exc:
            print(f"STORAGE: missing-container catalog update deferred ({exc})")

    return unloaded_candidate


def persist_storage_location(client, chest_pos, state=None) -> bool:
    """Persist a verified chest to both checkpoint formats.

    The production ``StateManager`` checkpoint is authoritative. The
    ``WorldState`` checkpoint is maintained for older callers until they are
    all migrated. When a StateManager is provided, flush it immediately so a
    crash after placement cannot orphan the physical chest.
    """

    try:
        x, y, z = (int(value) for value in chest_pos)
    except (TypeError, ValueError):
        return False

    try:
        block_id = client.transport.dispatch(
            "get_block", {"x": x, "y": y, "z": z}
        ).get("id", "")
    except Exception as exc:
        print(f"STORAGE: could not verify chest at {(x, y, z)}: {exc}")
        return False
    if "chest" not in block_id:
        print(f"STORAGE: refusing to checkpoint non-chest block at {(x, y, z)}")
        return False

    saved = False
    if state is not None and hasattr(state, "add_location"):
        try:
            state.add_location(
                "chest", x, y, z, tags=["storage"], client=client
            )
            saved = True
        except Exception as exc:
            print(f"STORAGE: failed to update production location: {exc}")

    from .state import WorldState

    try:
        WorldState(client).save_checkpoint(
            "storage", {"x": x, "y": y, "z": z}
        )
        saved = True
    except Exception as exc:
        print(f"STORAGE: failed to write compatibility checkpoint: {exc}")

    if state is not None and hasattr(state, "save_checkpoint"):
        try:
            state.save_checkpoint(get_inventory(client))
        except Exception as exc:
            print(f"STORAGE: failed to flush production checkpoint: {exc}")

    # The checkpoint answers "where is home storage?"; the catalog answers
    # "what containers exist and what did they contain last time?".  Keep
    # both because the catalog is intentionally not embedded in checkpoint
    # JSON as it can grow to hundreds of containers.
    try:
        from .storage_catalog import catalog_for

        dimension = client.transport.dispatch("get_state", {}).get(
            "dimension", "minecraft:overworld"
        )
        catalog_for(client, state).register_container(
            (x, y, z),
            dimension=str(dimension),
            container_type=block_id,
            label="home_storage",
            purpose="general_storage",
            metadata={"tags": ["storage", "home"]},
        )
    except Exception as exc:
        # Cataloging must never turn a successfully placed chest into a failed
        # survival action.  The next inspection will repair the catalog.
        print(f"STORAGE: catalog registration deferred ({exc})")

    return saved


def dump_to_chest(client, keep_items: List[str], state=None) -> int:
    """
    Dump all items except specified ones to nearby chest.
    
    Args:
        keep_items: List of item IDs to keep
        
    Returns:
        Number of stacks deposited, or ``-1`` when storage was unavailable.
    """
    chest_pos = resolve_storage_location(client, state=state, verify=True)
    if chest_pos is None:
        print("  No storage location saved.")
        return -1

    return deposit_excess_to_chest(
        client,
        chest_pos,
        keep_items=set(keep_items),
        state=state,
    )


DISPOSABLE_CLUTTER_ITEMS = (
    "minecraft:acacia_sapling",
    "minecraft:birch_sapling",
    "minecraft:cherry_sapling",
    "minecraft:dark_oak_sapling",
    "minecraft:jungle_sapling",
    "minecraft:oak_sapling",
    "minecraft:spruce_sapling",
    "minecraft:mangrove_propagule",
    "minecraft:decorated_pot",
    "minecraft:tuff_bricks",
    "minecraft:waxed_copper_block",
    "minecraft:waxed_exposed_copper_bulb",
    "minecraft:waxed_oxidized_cut_copper_stairs",
)

EARLY_GAME_EXCESS_ITEMS = {
    *DISPOSABLE_CLUTTER_ITEMS,
    "minecraft:birch_door",
    "minecraft:bone",
    "minecraft:pumpkin_seeds",
    "minecraft:melon_seeds",
    "minecraft:wheat_seeds",
    "minecraft:beetroot_seeds",
    "minecraft:rotten_flesh",
    "minecraft:spider_eye",
    "minecraft:poisonous_potato",
    "minecraft:grass_block",
    "minecraft:moss_block",
    "minecraft:wildflowers",
    "minecraft:oak_leaves",
    "minecraft:birch_leaves",
    "minecraft:spruce_leaves",
    "minecraft:cobbled_deepslate",
    "minecraft:dirt",
    "minecraft:gravel",
    "minecraft:diorite",
    "minecraft:andesite",
    "minecraft:granite",
    "minecraft:tuff",
    "minecraft:dripstone_block",
    "minecraft:music_disc_cat",
    "minecraft:golden_horse_armor",
    "minecraft:gunpowder",
    "minecraft:leather",
    "minecraft:leaf_litter",
    "minecraft:name_tag",
    "minecraft:redstone",
}

# Durable progression should leave the player's inventory after each risky
# expedition. Whole stacks are deposited while ``retain_counts`` keeps one
# working loadout and a bounded food/fuel reserve carried.
PROGRESSION_BANK_ITEMS = EARLY_GAME_EXCESS_ITEMS | {
    "minecraft:raw_iron",
    "minecraft:iron_ingot",
    "minecraft:raw_gold",
    "minecraft:gold_ingot",
    "minecraft:diamond",
    "minecraft:emerald",
    "minecraft:lapis_lazuli",
    "minecraft:obsidian",
    "minecraft:ender_pearl",
    "minecraft:ender_eye",
    "minecraft:blaze_rod",
    "minecraft:blaze_powder",
    "minecraft:coal",
    "minecraft:charcoal",
    "minecraft:cooked_beef",
    "minecraft:cooked_porkchop",
    "minecraft:cooked_chicken",
    "minecraft:cooked_mutton",
    "minecraft:cooked_rabbit",
    "minecraft:baked_potato",
    "minecraft:bread",
    "minecraft:golden_apple",
    "minecraft:iron_pickaxe",
    "minecraft:iron_axe",
    "minecraft:iron_sword",
    "minecraft:iron_shovel",
    "minecraft:stone_pickaxe",
    "minecraft:stone_axe",
    "minecraft:stone_sword",
    "minecraft:stone_shovel",
}

PROGRESSION_RETAIN_COUNTS = {
    "minecraft:chest": 1,
    "minecraft:oak_planks": 8,
    "minecraft:spruce_planks": 8,
    "minecraft:birch_planks": 8,
    "minecraft:jungle_planks": 8,
    "minecraft:acacia_planks": 8,
    "minecraft:dark_oak_planks": 8,
    "minecraft:mangrove_planks": 8,
    "minecraft:cherry_planks": 8,
    "minecraft:coal": 16,
    "minecraft:charcoal": 16,
    "minecraft:cooked_beef": 16,
    "minecraft:cooked_porkchop": 16,
    "minecraft:cooked_chicken": 16,
    "minecraft:cooked_mutton": 16,
    "minecraft:cooked_rabbit": 16,
    "minecraft:baked_potato": 16,
    "minecraft:bread": 16,
    "minecraft:iron_pickaxe": 1,
    "minecraft:iron_axe": 1,
    "minecraft:iron_sword": 1,
    "minecraft:iron_shovel": 1,
    "minecraft:stone_pickaxe": 1,
    "minecraft:stone_axe": 1,
    "minecraft:stone_sword": 1,
    "minecraft:stone_shovel": 1,
}


# Container-read classification and retirement live in container_reads so the
# "void_air means unloaded, not absent" rule has one home; see that module for
# why retiring an entry on an unproven read is effectively irreversible.
from .container_reads import (  # noqa: E402
    _STORAGE_CONTAINER_TOKENS,
    _UNREADABLE_BLOCK_IDS,
    classify_container_read as _classify_container_read,
    clear_occluded_container_face as _clear_occluded_chest_face,
    resolve_container_block as _resolve_container_block,
    forget_missing_container as _forget_missing_container,
    is_storage_container as _is_storage_container,
    open_verified_container as _open_verified_container,
)


def deposit_excess_to_chest(
    client,
    chest_pos: Tuple[int, int, int],
    deposit_items=None,
    keep_items=None,
    state=None,
    retain_counts=None,
) -> int:
    """Deposit selected player stacks into a verified base chest.

    Returns the number of stacks moved, or ``-1`` when the chest could not be
    verified/opened.  The allow-list makes this safe at phase boundaries:
    ores, tools, food, fuel, and future progression materials stay carried.
    """
    from . import harness_ops
    from .navigation import goto
    from .storage_safety import load_storage_chunk, storage_travel_safe

    deposit_items = set(deposit_items or EARLY_GAME_EXCESS_ITEMS)
    keep_items = None if keep_items is None else set(keep_items)
    retain_counts = {
        item_id: max(0, int(count))
        for item_id, count in (retain_counts or {}).items()
    }
    cx, cy, cz = (int(value) for value in chest_pos)

    initial_state = client.transport.dispatch("get_state", {})
    if not storage_travel_safe(initial_state):
        print("STORAGE: refusing travel below health/hunger safety margin")
        return -1
    client._storage_survival_abort = False

    try:
        client.transport.dispatch("close_screen", {})
    except Exception:
        pass

    block = _resolve_container_block(client, (cx, cy, cz))
    if block is None:
        return -1
    if not _is_storage_container(block):
        print(f"STORAGE: expected container is missing at {(cx, cy, cz)}")
        _forget_missing_container(client, (cx, cy, cz), state, block_id=block)
        return -1

    live_state = client.transport.dispatch("get_state", {})
    position = live_state.get(
        "block_position", live_state.get("position", {})
    )
    distance = (
        (float(position.get("x", 0)) - cx) ** 2
        + (float(position.get("y", 0)) - cy) ** 2
        + (float(position.get("z", 0)) - cz) ** 2
    ) ** 0.5
    if distance > 4.5 and not harness_ops.move_near(
        client, cx, cy, cz, timeout=30.0
    ):
        print("STORAGE: could not move within interaction range")
        return -1

    # Movement/pathing must never be allowed to silently remove the target.
    block = client.transport.dispatch(
        "get_block", {"x": cx, "y": cy, "z": cz}
    ).get("id", "")
    if not _is_storage_container(block):
        print("STORAGE: container disappeared during approach")
        return -1

    # Stand at a real adjacent floor tile.  Merely being within four blocks is
    # insufficient when the crafting table/furnace blocks the ray trace from
    # the opposite side of this compact starter house.
    for sx, sy, sz in (
        (cx + 1, cy, cz),
        (cx + 1, cy, cz + 1),
        (cx, cy, cz + 1),
        (cx - 1, cy, cz),
    ):
        stand_block = client.transport.dispatch(
            "get_block", {"x": sx, "y": sy, "z": sz}
        ).get("id", "")
        floor_block = client.transport.dispatch(
            "get_block", {"x": sx, "y": sy - 1, "z": sz}
        ).get("id", "")
        if "air" not in stand_block or "air" in floor_block:
            continue
        goto(
            client,
            sx,
            sy,
            sz,
            timeout=20,
            check_interval=0.25,
            tolerance=0.5,
        )
        break

    screen = {}
    opened = False
    try:
        if harness_ops.available() and harness_ops.open_container(
            client, (cx, cy, cz), timeout=4.0
        ):
            screen = client.transport.dispatch("get_screen", {})
            data = screen.get("data", screen)
            total_slots = int(
                data.get("total_slots") or len(data.get("slots", []))
            )
            opened = total_slots in (63, 90)
    except Exception as exc:
        print(f"STORAGE: verified chest opener failed ({exc}); retrying natively")

    if not opened:
        for _attempt in range(3):
            client.transport.dispatch(
                "look_at", {"x": cx + 0.5, "y": cy + 0.5, "z": cz + 0.5}
            )
            time.sleep(0.2)
            client.transport.dispatch(
                "interact_block", {"x": cx, "y": cy, "z": cz}
            )
            for _ in range(20):
                screen = client.transport.dispatch("get_screen", {})
                data = screen.get("data", screen)
                total_slots = int(
                    data.get("total_slots") or len(data.get("slots", []))
                )
                if total_slots in (63, 90):
                    opened = True
                    break
                time.sleep(0.1)
            if opened:
                break
            client.transport.dispatch("close_screen", {})
    if not opened:
        client.transport.dispatch("close_screen", {})
        print("STORAGE: chest screen did not open")
        return -1

    data = screen.get("data", screen)
    slots = data.get("slots", [])
    total_slots = int(data.get("total_slots") or len(slots))
    container_slots = total_slots - 36
    if container_slots not in (27, 54):
        client.transport.dispatch("close_screen", {})
        print(f"STORAGE: unexpected container layout ({total_slots} slots)")
        return -1

    sync_id = data.get("sync_id", screen.get("sync_id"))
    deposited = 0
    player_totals = {}
    for item in slots:
        slot = int(item.get("slot", -1))
        item_id = item.get("id")
        count = int(item.get("count", 0) or 0)
        if slot >= container_slots and item_id and count > 0:
            player_totals[item_id] = player_totals.get(item_id, 0) + count
    for item in slots:
        slot = int(item.get("slot", -1))
        item_id = item.get("id")
        if slot < container_slots or int(item.get("count", 0)) <= 0:
            continue
        if keep_items is not None and item_id in keep_items:
            continue
        if keep_items is None and item_id not in deposit_items:
            continue
        count = int(item.get("count", 0) or 0)
        from .container_transfer import deposit_stack_respecting_reserve

        moved_count = deposit_stack_respecting_reserve(
            client, slot=slot, item_id=item_id, count=count,
            totals=player_totals, retains=retain_counts, sync_id=sync_id,
        )
        if moved_count is None:
            continue
        if moved_count <= 0:
            print(f"STORAGE: no verified transfer for {item_id}; checking other stacks")
            continue
        player_totals[item_id] = max(0, player_totals.get(item_id, 0) - moved_count)
        deposited += 1

    try:
        from .storage_catalog import catalog_for, observe_open_container

        final_screen = client.transport.dispatch("get_screen", {})
        observe_open_container(
            client,
            (cx, cy, cz),
            final_screen,
            state=state,
            container_type=block,
            label="home_storage",
            purpose="general_storage",
        )
        catalog_for(client, state).record_event(
            (cx, cy, cz),
            "deposit",
            dimension=str(
                client.transport.dispatch("get_state", {}).get(
                    "dimension", "minecraft:overworld"
                )
            ),
            details={"moved_stacks": deposited},
        )
    except Exception as exc:
        print(f"STORAGE: post-deposit catalog update deferred ({exc})")

    client.transport.dispatch("close_screen", {})
    print(f"STORAGE: deposited {deposited} excess stacks at home")
    return deposited


def deposit_progression_to_chest(
    client,
    chest_pos: Tuple[int, int, int],
    *,
    state=None,
    retain_counts=None,
    deposit_items=None,
) -> int:
    """Bank valuable surplus while retaining a bounded active loadout."""
    reserves = dict(PROGRESSION_RETAIN_COUNTS)
    reserves.update(retain_counts or {})
    return deposit_excess_to_chest(
        client,
        chest_pos,
        deposit_items=deposit_items or PROGRESSION_BANK_ITEMS,
        state=state,
        retain_counts=reserves,
    )


def withdraw_required_from_chest(
    client,
    chest_pos: Tuple[int, int, int],
    requirements: Dict[str, int],
    state=None,
    *,
    open_attempts: int = 4,
    allow_recovery_access: bool = False,
) -> int:
    """Withdraw only banked stacks needed by the current objective.

    Returns the number of chest stacks moved, ``0`` when the player already
    carries every requirement, or ``-1`` when the checkpointed chest cannot be
    verified/opened.  Shift-clicking may retrieve more than the exact shortfall;
    that is preferable to splitting stacks through bridge-specific GUI packets.
    """
    remaining = {
        item_id: max(0, int(required) - count_item(client, item_id))
        for item_id, required in requirements.items()
    }
    remaining = {item_id: count for item_id, count in remaining.items() if count}
    if not remaining:
        return 0

    cx, cy, cz = (int(value) for value in chest_pos)
    position = (cx, cy, cz)
    block = _resolve_container_block(client, (cx, cy, cz))
    if block is None:
        return -1
    if not _is_storage_container(block):
        print(f"STORAGE: expected container is missing at {(cx, cy, cz)}")
        _forget_missing_container(client, (cx, cy, cz), state, block_id=block)
        return -1
    # Only honour the "recently failed" cooldown when the container is actually
    # gone. A block that is still a real chest must always be re-approached —
    # the cooldown was meant to stop re-travelling to a *destroyed* container,
    # but it was also short-circuiting a present-but-hard-to-reach chest and
    # deadlocking the phase forever.
    if not storage_retry_ready(client, position):
        print(f"STORAGE: clearing stale cooldown for present container at {position}")
        cooldowns = getattr(client, "_unreachable_storage_until", {})
        cooldowns.pop(position, None)
        client._unreachable_storage_until = cooldowns

    # A chest can only open via the face the player interacts with. If that
    # face is occluded by a solid block (e.g. a crafting table placed in front
    # of the chest during base construction, or a cave-in), ``interact_block``
    # silently fails and the phase loops "supply chest screen did not open"
    # forever. Clear the blocking block along the player->chest axis first.
    _clear_occluded_chest_face(client, (cx, cy, cz), block)

    screen = _open_verified_container(
        client,
        position,
        attempts=open_attempts,
        allow_recovery_access=allow_recovery_access,
    )
    if screen is None:
        print("STORAGE: supply chest screen did not open")
        remember_unreachable_storage(client, position)
        return -1

    try:
        data = screen.get("data", screen)
        slots = data.get("slots", [])
        total_slots = int(data.get("total_slots") or len(slots))
        container_slots = total_slots - 36
        if container_slots not in (27, 54):
            print(f"STORAGE: unexpected container layout ({total_slots} slots)")
            remember_unreachable_storage(client, position)
            return -1

        sync_id = data.get("sync_id", screen.get("sync_id"))
        moved = 0
        for item in slots:
            slot = int(item.get("slot", -1))
            item_id = str(item.get("id", ""))
            count = int(item.get("count", 0))
            if slot < 0 or slot >= container_slots or remaining.get(item_id, 0) <= 0:
                continue
            payload = {"slot": slot, "type": "QUICK_MOVE", "button": 0}
            if sync_id is not None:
                payload["sync_id"] = sync_id
            client.transport.dispatch("inventory_click", payload)
            remaining[item_id] = max(0, remaining[item_id] - count)
            moved += 1
            time.sleep(0.05)
        try:
            from .storage_catalog import catalog_for, observe_open_container

            final_screen = client.transport.dispatch("get_screen", {})
            observe_open_container(
                client,
                (cx, cy, cz),
                final_screen,
                state=state,
                container_type=block,
                label="home_storage",
                purpose="general_storage",
            )
            catalog_for(client, state).record_event(
                (cx, cy, cz),
                "withdraw",
                dimension=str(
                    client.transport.dispatch("get_state", {}).get(
                        "dimension", "minecraft:overworld"
                    )
                ),
                details={"moved_stacks": moved, "requirements": requirements},
            )
        except Exception as exc:
            print(f"STORAGE: post-withdraw catalog update deferred ({exc})")
        print(f"STORAGE: withdrew {moved} required stacks from home")
        return moved
    finally:
        client.transport.dispatch("close_screen", {})


def withdraw_required_from_catalog(
    client,
    requirements: Dict[str, int],
    *,
    state=None,
    max_containers: int = 8,
    max_snapshot_age: float = 300.0,
    max_travel_distance: Optional[float] = None,
    max_vertical_distance: Optional[float] = None,
    allow_recovery_access: bool = False,
) -> int:
    """Find and withdraw required items across known storage containers.

    Fresh catalog hits are tried first.  Containers whose contents have not
    yet been observed are then inspected so checkpoint-imported landmarks can
    become useful without claiming that their contents are known.  Every open
    updates the catalog through :func:`withdraw_required_from_chest`.

    Returns the number of moved stacks, ``0`` when nothing needs moving, and
    ``-1`` when no known container could be reached and opened.
    """
    from . import harness_ops
    from .storage_catalog import catalog_for

    outstanding = {
        item_id: max(0, int(required) - count_item(client, item_id))
        for item_id, required in requirements.items()
    }
    outstanding = {item_id: count for item_id, count in outstanding.items() if count}
    if not outstanding:
        return 0

    try:
        catalog = catalog_for(client, state)
        live_state = client.transport.dispatch("get_state", {})
        current_dimension = str(live_state.get("dimension", "minecraft:overworld"))
        current_position = live_state.get("block_position", {})
    except Exception as exc:
        print(f"STORAGE: catalog lookup unavailable ({exc})")
        return -1

    candidates = []
    seen = set()

    def _player_distance(position) -> float:
        """Straight-line distance from the bot, or 0.0 if position is unknown."""
        if not all(axis in current_position for axis in ("x", "y", "z")):
            return 0.0
        return (
            (float(current_position["x"]) - position[0]) ** 2
            + (float(current_position["y"]) - position[1]) ** 2
            + (float(current_position["z"]) - position[2]) ** 2
        ) ** 0.5

    def add_candidate(entry, tier: int = 0) -> None:
        if str(entry.get("dimension", current_dimension)) != current_dimension:
            return
        try:
            position = (
                int(entry["x"]),
                int(entry["y"]),
                int(entry["z"]),
            )
        except (KeyError, TypeError, ValueError):
            return
        if not storage_retry_ready(client, position):
            return
        if position not in seen:
            seen.add(position)
            candidates.append((tier, _player_distance(position), position))

    # Prefer containers last observed with a requested item.  A single chest
    # may satisfy several items, hence the de-duplication above.
    for item_id in outstanding:
        for entry in catalog.find_item(item_id):
            add_candidate(entry)
    now = time.time()
    for entry in catalog.list_containers():
        last_scan = entry.get("last_inventory_scan")
        # A recent complete snapshot with no requested item is authoritative
        # negative knowledge. Reinspect unscanned/stale containers, while item
        # hits above remain eligible regardless of snapshot age.
        if last_scan is not None:
            try:
                if now - float(last_scan) <= max(0.0, float(max_snapshot_age)):
                    continue
            except (TypeError, ValueError):
                pass
        add_candidate(entry, tier=1)

    if not candidates:
        print("STORAGE: fresh catalog snapshots contain none of the requested items")
        return 0

    # Nearest first, within each tier. Without this the list stays in catalog
    # order and the `[:max_containers]` slice below keeps an arbitrary eight of
    # however many containers the fleet has catalogued. With 325 of them, the
    # useful chest almost never made the cut: Bot15 stood 19.5m from a chest
    # while logging the same distant ones hundreds of times over
    # ("305 x 217.1m exceeds the 96.0m recovery radius"), and banked 25 loads
    # all day. The tier is preserved so containers known to hold a requested
    # item still outrank speculative unscanned ones.
    candidates.sort(key=lambda candidate: (candidate[0], candidate[1]))
    candidates = [position for _tier, _distance, position in candidates]

    moved_total = 0
    opened_any = False
    for position in candidates[: max(0, int(max_containers))]:
        if max_vertical_distance is not None and "y" in current_position:
            vertical_distance = abs(float(current_position["y"]) - position[1])
            if vertical_distance > max(0.0, float(max_vertical_distance)):
                print(
                    "STORAGE: skipping cataloged container at "
                    f"{position}; {vertical_distance:.1f}m vertical separation "
                    f"exceeds the {float(max_vertical_distance):.1f}m safety limit"
                )
                continue
        if max_travel_distance is not None and all(
            axis in current_position for axis in ("x", "y", "z")
        ):
            travel_distance = (
                (float(current_position["x"]) - position[0]) ** 2
                + (float(current_position["y"]) - position[1]) ** 2
                + (float(current_position["z"]) - position[2]) ** 2
            ) ** 0.5
            if travel_distance > max(0.0, float(max_travel_distance)):
                print(
                    "STORAGE: skipping cataloged container at "
                    f"{position}; {travel_distance:.1f}m exceeds the "
                    f"{float(max_travel_distance):.1f}m recovery radius"
                )
                continue
        try:
            block_id = client.transport.dispatch(
                "get_block",
                {"x": position[0], "y": position[1], "z": position[2]},
            ).get("id", "")
        except Exception:
            block_id = ""
        if block_id == "minecraft:void_air":
            from .navigation import goto

            print(f"STORAGE: loading cataloged chest chunk at {position}")
            if not goto(
                client,
                *position,
                timeout=120.0,
                check_interval=0.5,
                tolerance=3.0,
            ):
                print(f"STORAGE: could not load cataloged container at {position}")
                remember_unreachable_storage(client, position)
                continue
        try:
            state_resp = client.transport.dispatch("get_state", {})
            live_position = state_resp.get("block_position") or state_resp.get("position") or {}
            dist_to_container = (
                (float(live_position["x"]) - position[0]) ** 2
                + (float(live_position["y"]) - position[1]) ** 2
                + (float(live_position["z"]) - position[2]) ** 2
            ) ** 0.5 if all(axis in live_position for axis in ("x", "y", "z")) else 999.0
            already_near = dist_to_container <= 4.5
        except (AttributeError, TypeError, ValueError):
            already_near = False
            dist_to_container = 999.0

        if not already_near and dist_to_container > 12.0:
            from .navigation import goto
            print(f"STORAGE: bot is {dist_to_container:.1f}m away from cataloged container at {position}; navigating closer first...")
            goto(client, *position, timeout=60.0, check_interval=1.0, tolerance=3.0)

        if not already_near and not harness_ops.move_near(
            client, *position, timeout=90.0
        ):
            print(f"STORAGE: could not reach cataloged container at {position}")
            remember_unreachable_storage(client, position)
            continue
        moved = withdraw_required_from_chest(
            client,
            position,
            outstanding,
            state=state,
            open_attempts=1,
            allow_recovery_access=allow_recovery_access,
        )
        if moved < 0:
            continue
        opened_any = True
        moved_total += moved
        outstanding = {
            item_id: max(0, required - count_item(client, item_id))
            for item_id, required in requirements.items()
        }
        outstanding = {
            item_id: count for item_id, count in outstanding.items() if count
        }
        if not outstanding:
            break

    return moved_total if opened_any else -1


def check_craft(client, output_item: str, count: int = 1) -> Dict:
    """Check if a given output_item can be crafted.

    Tries the bridge `check_craft` route if available, otherwise falls back to
    querying recipes and checking inventory locally.

    Returns:
        {"can_craft": bool, "missing": [{"item": str, "count": int}], "recipe_id": Optional[str]}
    """
    try:
        # Try bridge-supported endpoint first
        resp = client.transport.dispatch("check_craft", {"output_item": output_item, "count": count})
        if resp.get("status") == "ok":
            data = resp.get("data", {})
            return {
                "can_craft": bool(data.get("can_craft", False)),
                "missing": data.get("missing", []),
                "recipe_id": data.get("recipe_id"),
            }
    except Exception:
        # Fallback to local inference
        pass

    # Fallback: get recipes and check inventory
    recipes = get_recipes_for(client, output_item)
    inv = get_inventory(client)

    for r in recipes:
        req = r.get("requires", {}) or r.get("ingredients", {})
        missing = []
        ok = True
        for item, needed in req.items():
            if inv.get(item, 0) < needed * count:
                missing.append({"item": item, "count": needed * count - inv.get(item, 0)})
                ok = False
        if ok:
            return {"can_craft": True, "missing": [], "recipe_id": r.get("id")}

    # If none found craftable
    return {"can_craft": False, "missing": [{"item": "unknown", "count": 0}], "recipe_id": None}


def craft_item(client, recipe_id: str, count: int = 1) -> bool:
    """Invoke bridge craft by recipe_id (preferred) or fall back to generic craft(item).

    Returns True on success.
    """
    try:
        resp = client.transport.dispatch("craft", {"recipe_id": recipe_id, "count": count})
        # If bridge returns structured data, check it
        if resp.get("status") == "ok":
            data = resp.get("data", {})
            # Some bridge stubs return crafted boolean in data
            return bool(data.get("crafted", True))
        # Older craft shim returns a bare dict
        return resp.get("crafted", True)
    except Exception:
        # Fallback: attempt best-effort craft using the older 'craft' helper
        try:
            return craft(client, recipe_id, count)
        except Exception:
            return False
def is_full(client) -> bool:
    """Check if inventory is full (no empty slots)."""
    try:
        raw_inv = client.transport.dispatch("get_inventory", {})
        items = raw_inv.get("inventory", [])
        # Normal inventory has 36 slots (0-35). Count occupied ones.
        occupied = 0
        for item in items:
            if item.get("id") and item.get("count", 0) > 0:
                occupied += 1
        return occupied >= 36
    except Exception:
        return False


def free_inventory_slots(client) -> int:
    """Count capacity only from a valid current carried-slot snapshot."""
    try:
        from ..inventory_evidence import free_carried_slots

        return free_carried_slots(client.transport.dispatch("get_inventory", {}))
    except Exception:
        return 0


def _player_handler_slot(inventory_slot: int) -> int:
    """Map PlayerInventory indices to PlayerScreenHandler slot ids."""
    return 36 + inventory_slot if 0 <= inventory_slot <= 8 else inventory_slot


def drop_items(
    client,
    item_ids: List[str],
    max_stacks: Optional[int] = None,
    retain_counts: Optional[Dict[str, int]] = None,
) -> int:
    from .inventory_disposal import drop_items as implementation

    return implementation(
        client,
        item_ids,
        max_stacks=max_stacks,
        retain_counts=retain_counts,
    )
