"""Surplus seeds are thrown away from the retreat, keeping about 32 carried.

Live A1 2026-10-15 carried 548 wheat seeds (nine slots) with 3307 more banked,
so every home chest was full and the iron trip's ten-slot gate refused about
350 times. Thrown stacks also came back: a bot that walks over its own drop
re-collects it once the two-second pickup delay ends.
"""

from types import SimpleNamespace

import pytest

from baritone_client.common import inventory, inventory_disposal as disposal


SEEDS = "minecraft:wheat_seeds"


class _World:
    """A solid floor with a straight corridor along x from ``low`` to ``high``."""

    def __init__(self, stacks, low=-3, high=3):
        self.slots = {slot: [SEEDS, count] for slot, count in enumerate(stacks)}
        self.low, self.high = low, high
        self.position = [0, 64, 0]
        self.calls = []

    def dispatch(self, route, payload=None):
        self.calls.append((route, payload))
        if route == "get_inventory":
            return {"inventory": [
                {"id": item, "slot": slot, "count": count}
                for slot, (item, count) in self.slots.items()
            ]}
        if route == "get_state":
            x, y, z = self.position
            return {"block_position": {"x": x, "y": y, "z": z}}
        if route == "get_block":
            x, y, z = payload["x"], payload["y"], payload["z"]
            if y == 63:
                return {"id": "minecraft:stone"}
            open_cell = z == 0 and self.low <= x <= self.high
            return {"id": "minecraft:air" if open_cell else "minecraft:stone"}
        if route == "inventory_click":
            slot = payload["slot"]
            self.slots.pop(slot - 36 if slot >= 36 else slot, None)
        return {}

    def carried(self):
        return sum(count for item, count in self.slots.values() if item == SEEDS)


@pytest.fixture
def world(monkeypatch):
    from baritone_client.common import navigation
    from baritone_client import inventory_evidence

    monkeypatch.setattr(disposal.time, "sleep", lambda _s: None)
    monkeypatch.setattr(inventory_evidence, "valid_inventory", lambda _d: True)

    def free(client):
        return 36 - len(client.transport.slots)

    monkeypatch.setattr(inventory, "free_inventory_slots", free)

    def goto(client, x, y, z, **_k):
        client.transport.position = [x, y, z]
        return True

    monkeypatch.setattr(navigation, "goto", goto)

    def make(stacks, **kwargs):
        transport = _World(stacks, **kwargs)
        return SimpleNamespace(transport=transport)

    return make


def test_the_seed_glut_is_shed_down_to_about_32(world):
    client = world([64] * 8 + [36])
    assert disposal.shed_surplus_seeds(client) >= 7
    assert 32 <= client.transport.carried() <= 64 + 32


@pytest.mark.parametrize("stacks", [[64, 26], [32], []])
def test_less_than_a_stack_over_the_cap_is_left_alone(world, stacks):
    client = world(stacks)
    assert disposal.shed_surplus_seeds(client) == 0
    assert not any(route == "inventory_click" for route, _ in client.transport.calls)


def test_each_throw_faces_away_from_its_retreat(world):
    client = world([64, 64, 64], low=0, high=3)
    disposal.shed_surplus_seeds(client)
    calls = client.transport.calls
    first_throw = next(i for i, (route, _) in enumerate(calls) if route == "inventory_click")
    looks = [payload for route, payload in calls[:first_throw] if route == "look_at"]
    # Only +x is open, so the bot retreats east and must throw west.
    assert looks and looks[-1]["x"] < 0


def test_retreat_never_walks_back_over_an_earlier_stack():
    """With +x and -x equally open, a stack already lying at +2 rules out +x."""
    world = _World([], low=-3, high=3)
    client = SimpleNamespace(transport=world)
    assert disposal._retreat_plan(client)[3:5] == (1, 0)
    assert disposal._retreat_plan(client, avoid=[(2, 0)])[3:5] == (-1, 0)


def test_a_boxed_in_bot_plans_no_retreat():
    world = _World([], low=0, high=0)
    assert disposal._retreat_plan(SimpleNamespace(transport=world)) is None


class _Thrower:
    """A pack whose held item leaves one per accepted use."""

    def __init__(self, carried, accepts=True):
        self.carried = dict(carried)
        self.accepts = accepts
        self.held = None
        self.calls = []

    def dispatch(self, route, payload=None):
        self.calls.append((route, payload))
        if route == "get_state":
            return {"block_position": {"x": 0, "y": 64, "z": 0}}
        if route == "get_block":
            return {"id": "minecraft:stone" if payload["y"] == 63 else "minecraft:air"}
        if route == "use_item" and self.accepts and self.carried.get(self.held, 0) > 0:
            self.carried[self.held] -= 1
        return {}


@pytest.fixture
def thrower(monkeypatch):
    def make(carried, accepts=True):
        transport = _Thrower(carried, accepts)
        monkeypatch.setattr(disposal.time, "sleep", lambda _s: None)
        monkeypatch.setattr(inventory, "count_item", lambda _c, item: transport.carried.get(item, 0))

        def select(_c, item, allow_swap=False):
            transport.held = item
            return transport.carried.get(item, 0) > 0

        monkeypatch.setattr(inventory, "select_item", select)
        return SimpleNamespace(transport=transport)
    return make


def test_eggs_and_snowballs_are_thrown_until_none_are_left(thrower):
    client = thrower({"minecraft:egg": 10, "minecraft:snowball": 3, "minecraft:feather": 1})
    assert disposal.throw_clutter(client) == 2
    assert client.transport.carried == {"minecraft:egg": 0, "minecraft:snowball": 0, "minecraft:feather": 1}
    looks = [p for r, p in client.transport.calls if r == "look_at"]
    assert looks, "aim at open ground, not a wall"


def test_a_refused_throw_stops_instead_of_spinning(thrower):
    client = thrower({"minecraft:egg": 10}, accepts=False)
    assert disposal.throw_clutter(client) == 0
    assert sum(1 for r, _ in client.transport.calls if r == "use_item") == 1
