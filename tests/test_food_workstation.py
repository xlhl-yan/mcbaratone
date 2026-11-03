from types import SimpleNamespace

import pytest

from baritone_client.common import food_workstation


HOME = (10, 65, 10)
HOUSE = (9, 65, 9)
TABLE = (12, 66, 12)


class _Client:
    def __init__(self, position=(80, 72, 80)):
        self.live = {
            "block_position": dict(zip(("x", "y", "z"), position)),
            "health": 20, "is_dead": False, "is_pathing": False,
            "is_on_ground": True,
            "dimension": "minecraft:overworld", "game_mode": "survival",
        }
        self.blocks = {}
        self.routes = []
        self.transport = SimpleNamespace(dispatch=self.dispatch)

    def dispatch(self, route, _payload=None):
        self.routes.append((route, _payload))
        if route == "get_state":
            return dict(self.live)
        if route == "get_block":
            position = tuple(_payload[key] for key in ("x", "y", "z"))
            return self.blocks.get(position, {"id": "minecraft:air"})
        if route == "interact_block":
            for position, block in list(self.blocks.items()):
                if block.get("id") == "minecraft:oak_door":
                    block["state"]["open"] = "false"
            return {"accepted": True}
        raise AssertionError(f"unexpected route {route}")


@pytest.fixture
def state():
    return SimpleNamespace(custom_data={
        "base_location": list(HOME),
        "structures": {"starter_house": {"origin": list(HOUSE)}},
    })


def _set_home(client):
    client.live["block_position"] = {"x": HOME[0] + 1, "y": HOME[1], "z": HOME[2]}


def _call(client, state, *, survival_ready, return_home, craft):
    return food_workstation.craft_bread_at_saved_home(
        client, state, HOME, 2,
        survival_ready=survival_ready,
        return_home=return_home,
        craft=craft,
    )


def _mock_known_house(monkeypatch, client, *, table=TABLE):
    from baritone_client.common import farming

    def block(_client, x, y, z):
        if (x, y, z) == table:
            return {"id": "minecraft:crafting_table"}
        if y == HOUSE[1]:
            return {"id": "minecraft:stone"}
        return {"id": "minecraft:air"}

    def goto(_client, x, y, z, **_kwargs):
        client.live["block_position"] = {"x": x + 0.5, "y": y, "z": z + 0.5}
        return True

    monkeypatch.setattr(farming, "_block_data", block)
    monkeypatch.setattr("baritone_client.common.navigation.goto", goto)
    monkeypatch.setattr(
        "baritone_client.common.house_door_travel.prepare_house_door_for_entry",
        lambda *_a: True,
    )


def test_reachable_saved_home_uses_only_its_enclosed_table(monkeypatch, state):
    client = _Client()
    returns, crafts, table_checks = [], [], []
    survival_checks = []

    def return_home(_client, anchor):
        returns.append(tuple(anchor))
        _set_home(client)
        return True

    def observed_table(_client):
        table_checks.append(tuple(client.live["block_position"].values()))
        return True, TABLE

    _mock_known_house(monkeypatch, client)
    monkeypatch.setattr(
        "baritone_client.common.enclosed_workstation.sheltered_bread_table",
        observed_table,
    )

    assert _call(
        client, state,
        survival_ready=lambda _c: survival_checks.append(True) or True,
        return_home=return_home,
        craft=lambda _c, item, count: crafts.append((item, count)) or True,
    )
    assert returns == [HOME]
    assert table_checks
    assert crafts == [(food_workstation.BREAD, 2)]
    assert len(survival_checks) >= 3
    assert client._protected_home_anchor == tuple(float(value) for value in HOME)


def test_blocked_home_travel_preserves_wheat_and_skips_crafting(monkeypatch, state):
    client = _Client()
    wheat = {"count": 7}
    crafts = []
    monkeypatch.setattr(
        "baritone_client.common.enclosed_workstation.sheltered_bread_table",
        lambda _c: pytest.fail("cannot inspect a workstation before reaching home"),
    )

    assert not _call(
        client, state, survival_ready=lambda _c: True,
        return_home=lambda *_a: False,
        craft=lambda *_a: crafts.append(True) or wheat.update(count=0) or True,
    )
    assert wheat["count"] == 7
    assert crafts == []


def test_unknown_fresh_state_fails_before_travel_or_crafting(state):
    client = _Client()
    client.live = {"success": False, "error": "state unavailable", "data": {
        "block_position": {"x": 80, "y": 72, "z": 80}, "health": 20,
        "is_dead": False, "dimension": "minecraft:overworld",
    }}
    returns, crafts = [], []

    assert not _call(
        client, state, survival_ready=lambda _c: True,
        return_home=lambda *a: returns.append(a) or True,
        craft=lambda *a: crafts.append(a) or True,
    )
    assert returns == [] and crafts == []


@pytest.mark.parametrize("field,value", [("is_pathing", True), ("game_mode", "creative")])
def test_moving_or_non_survival_state_fails_before_travel(state, field, value):
    client = _Client()
    client.live[field] = value
    returns, crafts = [], []

    assert not _call(
        client, state, survival_ready=lambda _c: True,
        return_home=lambda *a: returns.append(a) or True,
        craft=lambda *a: crafts.append(a) or True,
    )
    assert returns == [] and crafts == []


def test_missing_saved_home_never_invents_a_return_target():
    client = _Client()
    state_without_home = SimpleNamespace(custom_data={})
    returns, crafts = [], []

    assert not food_workstation.craft_bread_at_saved_home(
        client, state_without_home, None, 2,
        survival_ready=lambda _c: True,
        return_home=lambda *a: returns.append(a) or True,
        craft=lambda *a: crafts.append(a) or True,
    )
    assert returns == [] and crafts == []


def test_missing_home_table_fails_without_crafting_or_material_prep(monkeypatch, state):
    client = _Client()
    returns, crafts = [], []

    def return_home(_client, _anchor):
        returns.append(True)
        _set_home(client)
        return True

    _mock_known_house(monkeypatch, client, table=None)
    monkeypatch.setattr(
        "baritone_client.common.enclosed_workstation.sheltered_bread_table",
        lambda _c: (True, None),
    )
    assert not _call(
        client, state, survival_ready=lambda _c: True,
        return_home=return_home,
        craft=lambda *a: crafts.append(a) or True,
    )
    assert returns == [True]
    assert crafts == []


def test_saved_anchor_outside_house_routes_to_known_interior_table(monkeypatch, state):
    client = _Client()
    routes, crafts, returns, travel_order = [], [], [], []

    def goto(_client, x, y, z, **kwargs):
        travel_order.append("interior_goto")
        routes.append(((x, y, z), kwargs))
        client.live["block_position"] = {"x": x + 0.5, "y": y, "z": z + 0.5}
        return True

    from baritone_client.common import farming
    monkeypatch.setattr(farming, "_block_data", lambda _c, x, y, z: (
        {"id": "minecraft:crafting_table"} if (x, y, z) == TABLE
        else {"id": "minecraft:stone"} if y == HOUSE[1]
        else {"id": "minecraft:air"}
    ))
    monkeypatch.setattr("baritone_client.common.navigation.goto", goto)
    def prepare_entry(*_args):
        travel_order.append("door_check")
        return True

    monkeypatch.setattr(
        "baritone_client.common.house_door_travel.prepare_house_door_for_entry",
        prepare_entry,
    )
    monkeypatch.setattr(
        "baritone_client.common.enclosed_workstation.sheltered_bread_table",
        lambda _c: (True, TABLE),
    )
    # The saved return stops at the anchor, which is outside the house.
    def return_home(_client, anchor):
        assert tuple(anchor) == HOME
        returns.append(True)
        client.live["block_position"] = {"x": HOME[0] + 0.5, "y": HOME[1], "z": HOME[2] + 0.5}
        return True

    assert _call(
        client, state, survival_ready=lambda _c: True,
        return_home=return_home,
        craft=lambda _c, item, count: crafts.append((item, count)) or True,
    )
    assert returns == [True]
    assert travel_order[:2] == ["door_check", "interior_goto"]
    assert routes and routes[0][1] == {"timeout": 20, "tolerance": 0.5, "radius": 0}
    assert crafts == [(food_workstation.BREAD, 2)]


def test_open_saved_house_door_is_closed_before_strict_table_proof(monkeypatch, state):
    client = _Client(position=(HOME[0], HOME[1], HOME[2]))
    lower = (HOUSE[0] + 3, HOUSE[1] + 1, HOUSE[2])
    upper = (HOUSE[0] + 3, HOUSE[1] + 2, HOUSE[2])
    for position, half in ((lower, "lower"), (upper, "upper")):
        client.blocks[position] = {
            "id": "minecraft:oak_door",
            "state": {"half": half, "open": "true", "facing": "south"},
        }
    routes, crafts, sheltered_checks = [], [], []

    def goto(_client, x, y, z, **kwargs):
        routes.append(((x, y, z), kwargs))
        client.live["block_position"] = {"x": x + 0.5, "y": y, "z": z + 0.5}
        return True

    from baritone_client.common import farming, survival_farm
    monkeypatch.setattr(farming, "_block_data", lambda _c, x, y, z: (
        {"id": "minecraft:crafting_table"} if (x, y, z) == TABLE
        else {"id": "minecraft:stone"} if y == HOUSE[1]
        else {"id": "minecraft:air"}
    ))
    monkeypatch.setattr(survival_farm, "_hostile_close", lambda *_a: False)
    monkeypatch.setattr("baritone_client.common.navigation.goto", goto)
    monkeypatch.setattr(
        "baritone_client.common.enclosed_workstation.sheltered_bread_table",
        lambda _c: (
            sheltered_checks.append(True) or len(sheltered_checks) > 1,
            TABLE if len(sheltered_checks) > 1 else None,
        ),
    )

    assert _call(
        client, state, survival_ready=lambda _c: True,
        return_home=lambda *_a: pytest.fail("already near saved home"),
        craft=lambda _c, item, count: crafts.append((item, count)) or True,
    )
    interactions = [payload for route, payload in client.routes if route == "interact_block"]
    assert interactions == [{"x": upper[0], "y": upper[1], "z": upper[2]}]
    assert client.blocks[lower]["state"]["open"] == "false"
    assert client.blocks[upper]["state"]["open"] == "false"
    assert len(sheltered_checks) == 2
    assert any(point == (HOUSE[0] + 4, HOUSE[1] + 1, HOUSE[2] + 1) for point, _ in routes)
    assert crafts == [(food_workstation.BREAD, 2)]


def test_timed_out_door_toggle_is_not_retried_while_still_open(monkeypatch, state):
    client = _Client(position=(HOME[0], HOME[1], HOME[2]))
    lower = (HOUSE[0] + 3, HOUSE[1] + 1, HOUSE[2])
    upper = (HOUSE[0] + 3, HOUSE[1] + 2, HOUSE[2])
    for position, half in ((lower, "lower"), (upper, "upper")):
        client.blocks[position] = {
            "id": "minecraft:oak_door",
            "state": {"half": half, "open": "true", "facing": "south"},
        }
    from baritone_client.common import farming, survival_farm
    monkeypatch.setattr(farming, "_block_data", lambda _c, x, y, z: (
        {"id": "minecraft:crafting_table"} if (x, y, z) == TABLE
        else {"id": "minecraft:stone"} if y == HOUSE[1]
        else {"id": "minecraft:air"}
    ))
    monkeypatch.setattr(survival_farm, "_hostile_close", lambda *_a: False)
    monkeypatch.setattr(
        "baritone_client.common.navigation.goto",
        lambda _c, x, y, z, **_kwargs: (
            client.live.update(block_position={"x": x + 0.5, "y": y, "z": z + 0.5})
            or True
        ),
    )
    interactions = []

    def dispatch(route, payload=None):
        if route == "interact_block":
            interactions.append(payload)
            raise TimeoutError("uncertain toggle")
        return client.dispatch(route, payload)

    client.transport.dispatch = dispatch
    monkeypatch.setattr(
        "baritone_client.common.enclosed_workstation.sheltered_bread_table",
        lambda _c: (False, None),
    )
    crafts = []

    assert not _call(
        client, state, survival_ready=lambda _c: True,
        return_home=lambda *_a: pytest.fail("already near saved home"),
        craft=lambda *args: crafts.append(args) or True,
    )
    assert len(interactions) == 1
    assert client.blocks[lower]["state"]["open"] == "true"
    assert crafts == []


@pytest.mark.parametrize(
    "position,grounded",
    [
        ((HOUSE[0] + 4.5, HOUSE[1] + 1, HOUSE[2] + 1.5), False),
        ((HOUSE[0] + 4.5, HOUSE[1] + 2, HOUSE[2] + 1.5), True),
    ],
)
def test_final_craft_pose_requires_grounded_exact_interior_y(
    monkeypatch, state, position, grounded
):
    client = _Client(position=(HOME[0], HOME[1], HOME[2]))
    from baritone_client.common import farming
    monkeypatch.setattr(farming, "_block_data", lambda _c, x, y, z: (
        {"id": "minecraft:crafting_table"} if (x, y, z) == TABLE
        else {"id": "minecraft:stone"} if y == HOUSE[1]
        else {"id": "minecraft:air"}
    ))

    def goto(_client, x, y, z, **_kwargs):
        client.live["block_position"] = dict(zip(("x", "y", "z"), position))
        client.live["is_on_ground"] = grounded
        return True

    monkeypatch.setattr("baritone_client.common.navigation.goto", goto)
    monkeypatch.setattr(
        "baritone_client.common.enclosed_workstation.sheltered_bread_table",
        lambda _c: (True, TABLE),
    )
    crafts = []

    assert not _call(
        client, state, survival_ready=lambda _c: True,
        return_home=lambda *_a: True,
        craft=lambda *args: crafts.append(args) or True,
    )
    assert crafts == []


def test_recorded_house_door_stand_rejects_non_support_floor(monkeypatch):
    from baritone_client.common import farming

    monkeypatch.setattr(
        farming,
        "_block_data",
        lambda _c, _x, y, _z: {"id": "minecraft:air" if y >= 66 else "minecraft:torch"},
    )
    assert not food_workstation._safe_stand_block(_Client(), (13, 66, 10))


def test_unverified_saved_house_door_close_fails_closed(monkeypatch, state):
    client = _Client(position=(HOME[0], HOME[1], HOME[2]))
    lower = (HOUSE[0] + 3, HOUSE[1] + 1, HOUSE[2])
    upper = (HOUSE[0] + 3, HOUSE[1] + 2, HOUSE[2])
    for position, half in ((lower, "lower"), (upper, "upper")):
        client.blocks[position] = {
            "id": "minecraft:oak_door",
            "state": {"half": half, "open": "unknown"},
        }
    crafts = []
    from baritone_client.common import farming
    monkeypatch.setattr(farming, "_block_data", lambda _c, x, y, z: (
        {"id": "minecraft:crafting_table"} if (x, y, z) == TABLE
        else {"id": "minecraft:stone"} if y == HOUSE[1]
        else {"id": "minecraft:air"}
    ))
    monkeypatch.setattr(
        "baritone_client.common.enclosed_workstation.sheltered_bread_table",
        lambda _c: (False, None),
    )

    assert not _call(
        client, state, survival_ready=lambda _c: True,
        return_home=lambda *_a: pytest.fail("already near saved home"),
        craft=lambda *a: crafts.append(a) or True,
    )
    assert not any(route == "interact_block" for route, _ in client.routes)
    assert crafts == []


def test_unreachable_known_house_table_never_spends_wheat(monkeypatch, state):
    client = _Client(position=(HOME[0], HOME[1], HOME[2]))
    client._protected_home_anchor = HOME
    from baritone_client.common import farming
    monkeypatch.setattr(farming, "_block_data", lambda _c, x, y, z: (
        {"id": "minecraft:crafting_table"} if (x, y, z) == TABLE
        else {"id": "minecraft:stone"} if y == HOUSE[1]
        else {"id": "minecraft:air"}
    ))
    attempts, wheat, crafts = [], {"count": 7}, []
    monkeypatch.setattr(
        "baritone_client.common.navigation.goto",
        lambda _c, *args, **_kwargs: attempts.append(args) or False,
    )
    monkeypatch.setattr(
        "baritone_client.common.house_door_travel.prepare_house_door_for_entry",
        lambda *_a: True,
    )

    assert not _call(
        client, state, survival_ready=lambda _c: True,
        return_home=lambda *_a: pytest.fail("already within saved-home bounds"),
        craft=lambda *_a: crafts.append(True) or wheat.update(count=0) or True,
    )
    assert attempts and len(attempts) == 4
    assert wheat["count"] == 7 and crafts == []


@pytest.mark.parametrize("failure", ["survival", "height"])
def test_failed_posttravel_survival_or_height_proof_fails_closed(state, failure):
    client = _Client()
    returns, crafts = [], []

    def return_home(_client, _anchor):
        returns.append(True)
        client.live["block_position"] = {
            "x": HOME[0], "y": HOME[1] + (12 if failure == "height" else 0),
            "z": HOME[2],
        }
        return True

    survival_checks = []

    def survival_ready(_client):
        survival_checks.append(True)
        return not (failure == "survival" and len(survival_checks) == 2)

    assert not _call(
        client, state, survival_ready=survival_ready,
        return_home=return_home,
        craft=lambda *a: crafts.append(a) or True,
    )
    assert returns == [True]
    assert len(survival_checks) == 2
    assert crafts == []
