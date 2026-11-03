"""Choose routine meals without consuming a scarce expedition reserve."""

import math

from ..inventory_evidence import inventory_counts, unwrap_inventory, valid_inventory
from .end_readiness import PREPARED_FOOD_ITEMS


def choose_food(snapshot, priority, desperate_only, cook_first, current_food, health,
                *, raw_meat_food_floor=12):
    """Use abundant carrots only for healthy, nonurgent routine meals.

    Scarce crops, wounds, urgent hunger and unknown health retain ordinary
    nutrition priority. Inventory uncertainty never authorizes an eating action.
    """
    data = unwrap_inventory(snapshot)
    if (not valid_inventory(snapshot) or not isinstance(data, dict)
            or snapshot.get("success") is False or data.get("success") is False
            or data.get("error") or data.get("status") == "error"
            or type(current_food) is not int or not 0 <= current_food <= 20):
        return None
    counts = inventory_counts({
        "inventory": data.get("inventory", []),
        "offhand": data.get("offhand", []),
    })
    best = next((item for item in priority if counts.get(item, 0) > 0
                 and (item not in desperate_only or current_food <= 2)
                 and (item not in cook_first or current_food <= raw_meat_food_floor)), None)
    from .food_opportunity import BALANCED_PREPARED_FOOD_TARGET

    healthy = (not isinstance(health, bool) and isinstance(health, (int, float))
               and math.isfinite(health) and health >= 18)
    if (healthy and current_food > 6 and counts.get("minecraft:carrot", 0) >= 16
            and best in PREPARED_FOOD_ITEMS and best != "minecraft:golden_apple"
            and sum(counts.get(item, 0) for item in PREPARED_FOOD_ITEMS)
            < BALANCED_PREPARED_FOOD_TARGET):
        return "minecraft:carrot"
    return best
