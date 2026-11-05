from types import SimpleNamespace

import pytest

from baritone_client.common import house_door_travel


HOME = (10, 65, 10)
HOUSE = (9, 65, 9)
LOWER = (12, 66, 9)
UPPER = (12, 67, 9)


class _DoorClient:
    def __init__(self, position):
        self.position = position
        self.selected = 0
        self.open = False
        self.facing = "south"
        self.use_applies = True
        self.routes = []
        self.transport = SimpleNamespace(dispatch=self.dispatch)

    def dispatch(self, route, payload=None):
        self.routes.append((route, payload))
        if route == "get_state":
            x, y, z = self.position
            return {
                "block_position": {"x": x, "y": y, "z": z},
                "health": 20, "is_dead": False, "is_pathing": False,
                "is_on_ground": True, "dimension": "minecraft:overworld",
                "game_mode": "survival",
            }
        if route == "get_block":
            position = tuple(payload[key] for key in ("x", "y", "z"))
            if position == LOWER:
                return {"id": "minecraft:oak_door", "state": {"half": "lower", "open": self.open, "facing": self.facing}}
            if position == UPPER:
                return {"id": "minecraft:oak_door", "state": {"half": "upper", "open": self.open, "facing": self.facing}}
            return {"id": "minecraft:air"}
        if route == "get_inventory":
            return {
                "selected_slot": self.selected,
                "inventory": [
                    {"slot": 0, "id": "minecraft:wheat_seeds", "count": 4},
                    {"slot": 1, "id": "minecraft:stone_sword", "count": 1},
                ],
            }
        if route == "select_slot":
            self.selected = payload["slot"]
            return {"accepted": True}
        if route == "look_at":
            return {"accepted": True}
        if route == "use_item":
            if self.use_applies:
                self.open = True
            return {"accepted": True}
        raise AssertionError(f"unexpected route {route}")


@pytest.fixture
def state():
    return SimpleNamespace(custom_data={
        "base_location": list(HOME),
        "structures": {"starter_house": {"origin": list(HOUSE)}},
    })


def _world(monkeypatch, client):
    from baritone_client.common import farming, navigation, survival_farm

    monkeypatch.setattr(
        farming, "_block_data",
        lambda _c, _x, y, _z: {"id": "minecraft:stone" if y == HOUSE[1] else "minecraft:air"},
    )
    def goto(_client, x, y, z, **_kwargs):
        client.position = (x + 0.25, y, z + 0.25)
        return True
    monkeypatch.setattr(navigation, "goto", goto)
    monkeypatch.setattr(survival_farm, "_hostile_close", lambda *_a: False)
    monkeypatch.setattr(house_door_travel.time, "sleep", lambda _seconds: None)


def test_entry_opens_only_recorded_door_with_observed_safe_tool(monkeypatch, state):
    client = _DoorClient((HOME[0] + 0.25, HOME[1], HOME[2] + 0.25))
    _world(monkeypatch, client)

    assert house_door_travel.prepare_house_door_for_entry(client, state, HOME)
    uses = [payload for route, payload in client.routes if route == "use_item"]
    looks = [payload for route, payload in client.routes if route == "look_at"]
    assert uses == [{"duration_ms": 0}]
    assert looks == [{"x": HOUSE[0] + 3.5, "y": HOUSE[1] + 2.5, "z": HOUSE[2] + 0.05}]
    assert client.open is True and client.selected == 0
    assert not any(route == "interact_block" for route, _ in client.routes)


@pytest.mark.parametrize("position", [
    (HOUSE[0] + 4.25, HOUSE[1] + 1, HOUSE[2] + 1.25),
    (HOUSE[0] + 3.25, HOUSE[1] + 1, HOUSE[2] + 0.25),
])
def test_departure_opens_door_from_saved_interior_or_doorway(monkeypatch, state, position):
    client = _DoorClient(position)
    _world(monkeypatch, client)

    assert house_door_travel.prepare_house_door_for_departure(client, state, HOME)
    assert client.open is True
    assert client.position == (HOUSE[0] + 3.25, HOUSE[1] + 1, HOUSE[2] + 1.25)
    assert [route for route, _ in client.routes].count("use_item") == 1


def test_unobserved_toggle_is_not_retried(monkeypatch, state):
    client = _DoorClient((HOME[0] + 0.25, HOME[1], HOME[2] + 0.25))
    client.use_applies = False
    _world(monkeypatch, client)

    assert not house_door_travel.prepare_house_door_for_entry(client, state, HOME)
    assert [route for route, _ in client.routes].count("use_item") == 1
    assert client.open is False


def test_no_nonplaceable_tool_means_no_door_pulse(monkeypatch, state):
    client = _DoorClient((HOME[0] + 0.25, HOME[1], HOME[2] + 0.25))
    _world(monkeypatch, client)
    original = client.dispatch

    def dispatch(route, payload=None):
        result = original(route, payload)
        if route == "get_inventory":
            result["inventory"] = [
                {"slot": 0, "id": "minecraft:wheat_seeds", "count": 4}
            ]
        return result
    client.transport.dispatch = dispatch

    assert not house_door_travel.prepare_house_door_for_entry(client, state, HOME)
    assert not any(route == "use_item" for route, _ in client.routes)


@pytest.mark.parametrize("invalid", ["snapshot", "selected_slot", "duplicate_slot", "count"])
def test_invalid_post_selection_inventory_restores_prior_slot(monkeypatch, state, invalid):
    client = _DoorClient((HOME[0] + 0.25, HOME[1], HOME[2] + 0.25))
    _world(monkeypatch, client)
    original = client.dispatch
    reads = 0

    def dispatch(route, payload=None):
        nonlocal reads
        result = original(route, payload)
        if route == "get_inventory":
            reads += 1
            if reads == 2:
                if invalid == "snapshot":
                    result["snapshot_valid"] = False
                elif invalid == "selected_slot":
                    result["selected_slot"] = True
                elif invalid == "duplicate_slot":
                    result["inventory"][1]["slot"] = 0
                else:
                    result["inventory"][1]["count"] = True
        return result
    client.transport.dispatch = dispatch

    assert not house_door_travel.prepare_house_door_for_entry(client, state, HOME)
    selected = [payload["slot"] for route, payload in client.routes if route == "select_slot"]
    assert selected == [1, 0]
    assert client.selected == 0
    assert not any(route == "use_item" for route, _ in client.routes)


def test_missing_post_selection_held_item_restores_prior_slot(monkeypatch, state):
    client = _DoorClient((HOME[0] + 0.25, HOME[1], HOME[2] + 0.25))
    _world(monkeypatch, client)
    original = client.dispatch
    reads = 0

    def dispatch(route, payload=None):
        nonlocal reads
        result = original(route, payload)
        if route == "get_inventory":
            reads += 1
            if reads == 2:
                result["inventory"] = [entry for entry in result["inventory"] if entry["slot"] != 1]
        return result
    client.transport.dispatch = dispatch

    assert not house_door_travel.prepare_house_door_for_entry(client, state, HOME)
    selected = [payload["slot"] for route, payload in client.routes if route == "select_slot"]
    assert selected == [1, 0]
    assert client.selected == 0
    assert not any(route == "use_item" for route, _ in client.routes)


def test_unexpected_door_facing_fails_closed(monkeypatch, state):
    client = _DoorClient((HOME[0] + 0.25, HOME[1], HOME[2] + 0.25))
    client.facing = "west"
    _world(monkeypatch, client)

    assert not house_door_travel.prepare_house_door_for_entry(client, state, HOME)
    assert not any(route == "use_item" for route, _ in client.routes)


@pytest.mark.parametrize("invalid", ["snapshot", "nested_snapshot", "slot", "count"])
def test_invalid_inventory_evidence_never_selects_or_uses_item(monkeypatch, state, invalid):
    client = _DoorClient((HOME[0] + 0.25, HOME[1], HOME[2] + 0.25))
    _world(monkeypatch, client)
    original = client.dispatch

    def dispatch(route, payload=None):
        result = original(route, payload)
        if route == "get_inventory":
            if invalid == "snapshot":
                result["snapshot_valid"] = False
            elif invalid == "nested_snapshot":
                result = {"success": True, "data": {**result, "snapshot_valid": False}}
            elif invalid == "slot":
                result["inventory"][1]["slot"] = True
            else:
                result["inventory"][1]["count"] = True
        return result
    client.transport.dispatch = dispatch

    assert not house_door_travel.prepare_house_door_for_entry(client, state, HOME)
    assert not any(route == "use_item" for route, _ in client.routes)


@pytest.mark.parametrize("field,value", [
    ("game_mode", "creative"),
    ("is_on_ground", False),
    ("is_pathing", True),
])
def test_unsafe_or_moving_player_never_pulses_door(monkeypatch, state, field, value):
    client = _DoorClient((HOME[0] + 0.25, HOME[1], HOME[2] + 0.25))
    _world(monkeypatch, client)
    original = client.dispatch

    def dispatch(route, payload=None):
        result = original(route, payload)
        if route == "get_state":
            result[field] = value
        return result
    client.transport.dispatch = dispatch

    assert not house_door_travel.prepare_house_door_for_entry(client, state, HOME)
    assert not any(route == "use_item" for route, _ in client.routes)


def test_lit_exterior_stand_remains_usable_for_door_entry(monkeypatch, state):
    from baritone_client.common import farming
    client = _DoorClient((HOME[0], HOME[1], HOME[2]))
    _world(monkeypatch, client)
    stand = (HOUSE[0]+3, HOUSE[1]+1, HOUSE[2]-1)
    def block(_client, x, y, z):
        return {"id": "minecraft:torch" if (x,y,z) == stand else
                "minecraft:stone" if y == HOUSE[1] else "minecraft:air"}
    monkeypatch.setattr(farming, "_block_data", block)
    assert house_door_travel.prepare_house_door_for_entry(client, state, HOME)


def test_home_return_uses_exterior_doorway_then_verified_interior(monkeypatch, state):
    from baritone_client.common import navigation
    client = _DoorClient((30, 65, 30))
    _world(monkeypatch, client)
    goals = []
    def travel(_client, x, y, z, **_kwargs):
        goals.append((x,y,z))
        client.position = (x,y,z)
        return True
    monkeypatch.setattr(navigation, "goto", travel)
    assert house_door_travel.return_to_saved_house(client, state, HOME)
    assert goals == [(12,66,8), (12,66,9), (10,66,10)]
    assert client.open


@pytest.mark.parametrize("stage", [0,1,2])
def test_acknowledged_staged_return_without_exact_displacement_is_rejected(monkeypatch, state, stage):
    from baritone_client.common import navigation
    client = _DoorClient((30,65,30));_world(monkeypatch, client)
    goals = []
    def travel(_client, x,y,z,**_kwargs):
        goals.append((x,y,z))
        if len(goals)-1 != stage:client.position=(x,y,z)
        return True
    monkeypatch.setattr(navigation,"goto",travel)
    assert not house_door_travel.return_to_saved_house(client,state,HOME)
    assert len(goals)==stage+1


@pytest.mark.parametrize("feet,head,floor", [
    ("minecraft:water", "minecraft:air", "minecraft:stone"),
    ("minecraft:lava", "minecraft:air", "minecraft:stone"),
    ("minecraft:stone", "minecraft:air", "minecraft:stone"),
    ("minecraft:torch", "minecraft:stone", "minecraft:stone"),
    ("minecraft:torch", "minecraft:air", "minecraft:air"),
    ("minecraft:torch", "minecraft:air", "minecraft:gravel"),
    (None, "minecraft:air", "minecraft:stone"),
])
def test_unknown_obstructed_or_unsupported_door_stand_cannot_start_return(monkeypatch, state, feet,head,floor):
    from baritone_client.common import farming,navigation
    client=_DoorClient((30,65,30));_world(monkeypatch,client)
    exterior=(12,66,8)
    def block(_client,x,y,z):
        if (x,z)==(exterior[0],exterior[2]):
            return {"id":{65:floor,66:feet,67:head}.get(y,"minecraft:air")}
        return {"id":"minecraft:stone" if y==65 else "minecraft:air"}
    monkeypatch.setattr(farming,"_block_data",block)
    monkeypatch.setattr(navigation,"goto",lambda *_a,**_k:pytest.fail("unsafe stand"))
    assert not house_door_travel.return_to_saved_house(client,state,HOME)
    assert not any(route=="use_item" for route,_ in client.routes)
