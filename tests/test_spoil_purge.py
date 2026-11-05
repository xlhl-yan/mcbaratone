"""Spoil leaves full home chests by container THROW, never below a floor."""

from types import SimpleNamespace

import pytest

from baritone_client.automator import spoil_purge

HOME = (0, 70, 0)


class Catalog:
    def __init__(self, chests, totals):
        self.chests = chests  # position -> {"capacity", "occupied", "items"}
        self.totals = totals

    def inventory_totals(self):
        return dict(self.totals)

    def list_containers(self):
        return [{"x": p[0], "y": p[1], "z": p[2], "dimension": "minecraft:overworld",
                 "capacity_slots": c["capacity"], "occupied_slots": c["occupied"]}
                for p, c in self.chests.items()]

    def container_inventory(self, position, *, dimension):
        totals = {}
        for item, count in self.chests[position]["items"]:
            totals[item] = totals.get(item, 0) + count
        return totals


class Chest:
    """An open chest screen whose THROW empties a slot unless refused."""

    def __init__(self, items, refuse=False):
        self.slots = [{"slot": i, "id": item, "count": count} for i, (item, count) in enumerate(items)]
        self.slots += [{"slot": i, "id": "minecraft:air", "count": 0} for i in range(len(items), 63)]
        self.refuse = refuse
        self.clicks = []

    def dispatch(self, route, payload=None):
        if route == "get_screen":
            return {"slots": [dict(s) for s in self.slots], "total_slots": 63, "sync_id": 4}
        if route == "inventory_click":
            self.clicks.append(payload)
            if not self.refuse:
                self.slots[payload["slot"]] = {"slot": payload["slot"], "id": "minecraft:air", "count": 0}
        return {}


@pytest.fixture
def world(monkeypatch):
    def make(chests, totals, refuse=False):
        screens = {position: Chest(spec["items"], refuse) for position, spec in chests.items()}
        current = {"chest": None}
        client = SimpleNamespace(transport=SimpleNamespace(
            dispatch=lambda route, payload=None: screens[current["chest"]].dispatch(route, payload)))
        catalog = Catalog(chests, totals)
        refreshed = []
        monkeypatch.setattr("baritone_client.common.storage_catalog.catalog_for", lambda _c, _s=None: catalog)
        monkeypatch.setattr("baritone_client.common.storage_catalog.observe_open_container",
                            lambda _c, position, screen, state=None: refreshed.append(position))
        monkeypatch.setattr("baritone_client.common.harness_ops.open_container",
                            lambda _c, position, timeout=4.0: current.update(chest=position) or True)
        monkeypatch.setattr("baritone_client.common.harness_ops.close_container", lambda _c: None)
        monkeypatch.setattr(spoil_purge.time, "sleep", lambda _s: None)
        return client, screens, refreshed
    return make


def _full(items):
    return {"capacity": 27, "occupied": 27, "items": items}


def test_spoil_above_the_floor_is_thrown_out_of_a_full_home_chest(world):
    """Live A1 2026-10-21: every home chest 27/27, mostly with spoil."""
    chest = (2, 70, 2)
    client, screens, refreshed = world(
        {chest: _full([("minecraft:granite", 64), ("minecraft:carrot", 64), ("minecraft:cobblestone", 64)])},
        {"minecraft:granite": 64, "minecraft:cobblestone": 576, "minecraft:carrot": 64},
    )
    assert spoil_purge.purge_home_spoil(client, None, HOME, 5) == [chest]
    thrown = {click["slot"] for click in screens[chest].clicks}
    assert thrown == {0, 2}, "granite, and cobblestone above 512; never the carrots"
    assert all(click["type"] == "THROW" and click["sync_id"] == 4 for click in screens[chest].clicks)
    assert refreshed == [chest]


def test_storage_floors_are_never_crossed(world):
    chest = (2, 70, 2)
    client, screens, _ = world(
        {chest: _full([("minecraft:cobblestone", 64), ("minecraft:dirt", 64)])},
        {"minecraft:cobblestone": 540, "minecraft:dirt": 150},  # 28 and 22 spare: no whole stack
    )
    assert spoil_purge.purge_home_spoil(client, None, HOME, 5) == []
    assert screens[chest].clicks == []


def test_only_full_chests_near_home_are_touched(world):
    roomy, far = (2, 70, 2), (100, 70, 0)
    client, screens, _ = world(
        {roomy: {"capacity": 27, "occupied": 10, "items": [("minecraft:tuff", 64)]},
         far: _full([("minecraft:tuff", 64)])},
        {"minecraft:tuff": 128},
    )
    assert spoil_purge.purge_home_spoil(client, None, HOME, 5) == []


def test_the_stack_budget_is_respected(world):
    chest = (2, 70, 2)
    client, screens, _ = world({chest: _full([("minecraft:diorite", 64)] * 6)}, {"minecraft:diorite": 384})
    spoil_purge.purge_home_spoil(client, None, HOME, 2)
    assert len(screens[chest].clicks) == 2


def test_an_unverified_throw_stops_the_purge(world):
    chest = (2, 70, 2)
    client, screens, _ = world({chest: _full([("minecraft:andesite", 64)] * 3)}, {"minecraft:andesite": 192},
                               refuse=True)
    assert spoil_purge.purge_home_spoil(client, None, HOME, 5) == []
    assert len(screens[chest].clicks) == 1
