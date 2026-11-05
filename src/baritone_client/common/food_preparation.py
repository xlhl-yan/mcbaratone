"""Prepare and bank carried grain using fresh output-capacity evidence."""

def _bread_output_capacity(client):
    """Use one fresh carried-slot snapshot, excluding offhand food stacks."""
    from . import food_supply as api
    from ..inventory_evidence import carried_output_capacity
    from .tasks import PlayerDeathDetected, SurvivalRecoveryRequired

    try:
        payload = client.transport.dispatch("get_inventory", {})
        return carried_output_capacity(payload, api.BREAD, max_count=64)
    except (PlayerDeathDetected, SurvivalRecoveryRequired):
        raise
    except Exception:
        return 0


def _prepare_bread(client, state, personal_food_reserve, seed_reserve):
    from . import food_supply as api

    before = api.get_inventory(client)
    # Keep room for the output before opening a crafting grid. A full pack of
    # ingredients is not proof the finished bread can be collected safely.
    capacity = api._bread_output_capacity(client)
    requested = min(api._count(before, api.WHEAT) // 3, capacity, api.MAX_BREAD_PER_CYCLE)
    if requested:
        api.craft_bread_at_saved_home(client, state, api._base_anchor(state), requested, survival_ready=api._survival_ready, return_home=api._return_to_anchor, craft=api.craft)
    after = api.get_inventory(client)
    crafted = max(0, api._count(after, api.BREAD) - api._count(before, api.BREAD))
    banked = 0
    chest = api.resolve_storage_location(client, state=state, verify=False)
    if chest is not None and api._count(after, api.BREAD) > max(0, int(personal_food_reserve)):
        deposited = api.deposit_excess_to_chest(
            client, chest, deposit_items={api.BREAD},
            retain_counts={api.BREAD: max(0, int(personal_food_reserve)), api.SEEDS: max(0, int(seed_reserve))},
            state=state,
        )
        if deposited >= 0:
            banked = max(0, api._count(after, api.BREAD) - api._count(api.get_inventory(client), api.BREAD))
    return crafted, banked
