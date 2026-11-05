"""The standing iron job: when it runs, where it digs, and what it banks."""

from types import SimpleNamespace

import pytest

from baritone_client.automator import armor_upkeep, iron_stockpile
from baritone_client.common import tunnel_miner as tm
from tests.test_tunnel_miner import ENTRANCE, IRON, FakeWorld


class Bot(FakeWorld):
    """FakeWorld plus an inventory, a furnace and a chest."""

    def __init__(self):
        super().__init__()
        self.ingots = 0
        self.banked = 0
        self.deposits = []
        self.food = 20

    def dispatch(self, route, payload):
        data = super().dispatch(route, payload)
        if route == "get_state":
            data["food_level"] = self.food
        return data


@pytest.fixture
def bot(monkeypatch):
    w = Bot()
    monkeypatch.setattr("baritone_client.common.navigation.goto", w.goto)
    monkeypatch.setattr("baritone_client.common.harness_ops.place_block_exact", w.place)
    monkeypatch.setattr("baritone_client.common.resources.equip_best_pickaxe", lambda _c, **_k: True)
    monkeypatch.setattr(tm.time, "sleep", lambda _s: None)
    counts = {
        "minecraft:torch": lambda: w.torches,
        "minecraft:raw_iron": lambda: w.raw_iron,
        "minecraft:iron_ingot": lambda: w.ingots,
    }
    monkeypatch.setattr(
        "baritone_client.common.inventory.count_item",
        lambda _c, item: counts.get(item, lambda: 0)(),
    )
    monkeypatch.setattr(iron_stockpile, "banked_iron", lambda _c, _s: w.banked)
    monkeypatch.setattr("baritone_client.automator.iron_preparation.prepare_iron_inventory", lambda *_a: "")

    def smelt(_client, _state):
        made, w.raw_iron, w.ingots = w.raw_iron, 0, w.ingots + w.raw_iron
        return made

    monkeypatch.setattr("baritone_client.automator.armor_recovery._smelt_raw_iron", smelt)

    def deposit(_client, chest, deposit_items=None, retain_counts=None, **_k):
        if "minecraft:iron_ingot" not in (deposit_items or set()):  # a clutter run
            w.deposits.append((chest, "clutter", len(deposit_items or ())))
            w.used_slots = max(1, w.used_slots - 8)
            return 8
        keep = (retain_counts or {}).get("minecraft:iron_ingot", 0)
        moved = max(0, w.ingots - keep)
        w.ingots -= moved
        w.banked += moved
        w.deposits.append((chest, keep, moved))
        return 1

    monkeypatch.setattr("baritone_client.common.inventory.deposit_excess_to_chest", deposit)
    monkeypatch.setattr(
        "baritone_client.automator.weapon_upkeep.kit_gaps", lambda _c: []
    )
    return w


def state(extra=None):
    custom = {
        "base_location": [0, 70, 0],
        "structures": {"starter_house": {"origin": [-3, 70, -3], "supply_chest": [2, 70, 2]}},
    }
    custom.update(extra or {})
    return SimpleNamespace(custom_data=custom)


def with_ore(w, count=10, origin=(6, 52, 3)):
    """A compact deposit: 4x4 ore per layer, layers going down from ``origin``."""
    ox, oy, oz = origin
    for i in range(count):
        w.blocks[(ox + i % 4, oy - i // 16, oz + (i // 4) % 4)] = IRON


# -- when it runs -----------------------------------------------------------
def test_due_when_stock_is_low_and_the_bot_is_healthy(bot):
    signals = SimpleNamespace(health=20.0, nearby_hostiles=0)
    assert iron_stockpile.supply_due(bot, state(), signals, now=1000.0)


@pytest.mark.parametrize(
    "case",
    ["stock_ok_with_bank", "low_health", "hungry", "hostiles", "cooldown", "no_state", "kit_gap", "dead"],
)
def test_not_due_when_unsafe_or_unneeded(bot, monkeypatch, case):
    signals = SimpleNamespace(health=20.0, nearby_hostiles=0)
    st = state()
    if case == "stock_ok_with_bank":
        bot.banked, bot.ingots = 20, 14  # 34 held and banked
    elif case == "low_health":
        signals.health = 12.0
    elif case == "hungry":
        bot.food = 8
    elif case == "hostiles":
        signals.nearby_hostiles = 1
    elif case == "cooldown":
        st.custom_data[iron_stockpile.KEY] = {"next_trip": 5000.0}
    elif case == "no_state":
        st = None
    elif case == "kit_gap":
        monkeypatch.setattr("baritone_client.automator.weapon_upkeep.kit_gaps", lambda _c: ["x"])
    elif case == "dead":
        bot.health = 0.0
    assert not iron_stockpile.supply_due(bot, st, signals, now=1000.0)


def test_stock_counts_carried_raw_and_banked_iron(bot):
    bot.ingots, bot.raw_iron, bot.banked = 5, 7, 20
    assert iron_stockpile.iron_stock(bot, state()) == 32
    assert not iron_stockpile.supply_due(bot, state(), None, now=1.0)  # exactly LOW_STOCK


# -- where it digs ----------------------------------------------------------
def test_entrance_is_plain_ground_outside_the_built_zone(bot):
    x, y, z = iron_stockpile.choose_entrance(bot, state())
    assert y == 70 and bot.blocks[(x, 69, z)] == "minecraft:grass_block"
    assert max(abs(x), abs(z)) >= 8  # beyond the house/farm zone and its margin


def test_below_home_requires_exact_upward_recovery_before_inventory_work(bot, monkeypatch):
    bot.pos = (0, 64, 0)
    entrance = [16, 70, 0]
    st = state({iron_stockpile.KEY: {"entrance": entrance, "spine": [entrance], "failures": 18}})
    calls = []
    defended = []
    monkeypatch.setattr("baritone_client.common.combat.defend_or_flee",
                        lambda client, **kwargs: defended.append((client, kwargs)) or False)
    def blocked(_c, x, y, z, **kwargs):
        calls.append(((x, y, z), kwargs))
        assert kwargs["on_defense"]() is False
        return False
    monkeypatch.setattr("baritone_client.common.navigation.goto", blocked)
    monkeypatch.setattr("baritone_client.automator.iron_preparation.prepare_iron_inventory",
                        lambda *_a: pytest.fail("inventory preparation before upward recovery"))
    ok, detail, *_ = iron_stockpile.run_supply_trip(bot, st, now=1000)
    assert not ok and "could not get home" in detail
    target, kwargs = calls[0]
    assert target == (0, 70, 0) and kwargs["tolerance"] == 2.0
    assert "radius" not in kwargs and callable(kwargs["on_defense"])
    assert defended == [(bot, {"allow_safe_recovery_movement": True})]
    assert st.custom_data[iron_stockpile.KEY]["spine"] == [entrance]
    assert st.custom_data[iron_stockpile.KEY]["next_trip"] == 1600


def test_a_bad_entrance_is_never_chosen_again(bot):
    first = iron_stockpile.choose_entrance(bot, state())
    st = state({iron_stockpile.KEY: {"bad_entrances": [list(first)]}})
    assert iron_stockpile.choose_entrance(bot, st) not in (None, first)


@pytest.mark.parametrize("arrived", [True, False])
def test_nearby_surface_position_must_reach_home_before_storage(bot, monkeypatch, arrived):
    bot.pos = (20, 70, 0)
    calls = []
    def go_home(_client, x, y, z, **_kwargs):
        calls.append("home")
        assert (x, y, z) == (0, 70, 0)
        if arrived:
            bot.pos = (0, 70, 0)
        return True  # a navigation acknowledgement alone is insufficient
    def prepare(*_args):
        calls.append("prepare")
        assert bot.pos == (0, 70, 0)
        return "preparation deferred"
    monkeypatch.setattr("baritone_client.common.navigation.goto", go_home)
    monkeypatch.setattr("baritone_client.automator.iron_preparation.prepare_iron_inventory", prepare)
    ok, detail, *_ = iron_stockpile.run_supply_trip(bot, state(), now=1000)
    assert not ok
    assert calls == (["home", "prepare"] if arrived else ["home"])
    assert ("preparation deferred" if arrived else "could not get home") in detail


@pytest.mark.parametrize("lit", [True, False])
def test_saved_route_travel_does_not_consume_mining_work_time(bot, monkeypatch, lit):
    clock = [100.0]
    monkeypatch.setattr(iron_stockpile.time, "monotonic", lambda: clock[0])
    bot.torches = 12 if lit else 1
    budget = iron_stockpile.TRIP_SECONDS if lit else iron_stockpile.DARK_TRIP_SECONDS
    spine = [[16 + n, 70, 0] for n in range(151)]
    st = state({iron_stockpile.KEY: {"entrance": spine[0], "spine": spine, "torch_retry": 9000}})
    def descend(miner, _spine):
        clock[0] += budget + 1
        miner.check()
    def mine(miner, _goal, _count):
        clock[0] += budget - 1
        miner.check()
        bot.raw_iron += 2
        return "quota"
    monkeypatch.setattr(tm.TunnelMiner, "descend", descend)
    monkeypatch.setattr(tm.TunnelMiner, "mine", mine)
    monkeypatch.setattr(tm.TunnelMiner, "retreat", lambda _miner: True)
    ok, detail, *_ = iron_stockpile.run_supply_trip(bot, st, now=1000)
    assert ok and "mined 2" in detail and bot.ingots == 2
    assert st.custom_data[iron_stockpile.KEY]["spine"] == spine


def test_saved_route_travel_timeout_retraces_without_starting_work(bot, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(iron_stockpile.time, "monotonic", lambda: clock[0])
    spine = [[16 + n, 70, 0] for n in range(151)]
    st = state({iron_stockpile.KEY: {"entrance": spine[0], "spine": spine}})
    def descend(miner, _spine):
        clock[0] += 2401
        miner.check()
    retreated = []
    monkeypatch.setattr(tm.TunnelMiner, "descend", descend)
    monkeypatch.setattr(tm.TunnelMiner, "mine", lambda *_a: pytest.fail("expired travel must not start work"))
    monkeypatch.setattr(tm.TunnelMiner, "retreat", lambda _miner: retreated.append(True) or True)
    ok, detail, *_ = iron_stockpile.run_supply_trip(bot, st, now=1000)
    assert not ok and "time" in detail and retreated == [True]
    assert st.custom_data[iron_stockpile.KEY]["spine"] == spine
    assert st.custom_data[iron_stockpile.KEY].get("dry_trips", 0) == 0


def test_interrupted_approaches_preserve_the_route_and_retry_without_four_hour_delay(bot, monkeypatch):
    entrance = [16, 70, 0]
    st = state({iron_stockpile.KEY: {"entrance": entrance, "spine": [entrance], "failures": 18}})
    monkeypatch.setattr("baritone_client.common.navigation.goto", lambda *_a, **_k: False)
    for attempt in range(3):
        ok, detail, *_ = iron_stockpile.run_supply_trip(bot, st, now=1000 + attempt)
        assert not ok and "could not reach" in detail
        assert st.custom_data[iron_stockpile.KEY]["next_trip"] == 1600 + attempt
    rec = st.custom_data[iron_stockpile.KEY]
    assert entrance not in rec.get("bad_entrances", [])
    assert rec["entrance"] == entrance and rec["spine"] == [entrance]
    assert not bot.dug


def test_transient_failure_on_a_proven_spine_does_not_blacklist_entrance_or_erase_route(bot, monkeypatch):
    entrance = (16, 70, 0)
    spine = [entrance, (17, 69, 0), (18, 68, 0)]
    st = state({
        iron_stockpile.KEY: {
            "entrance": list(entrance), "spine": [list(c) for c in spine],
            "dry_trips": 2,
        }
    })
    for frm, to in zip(spine, spine[1:]):
        for cell in tm.required_cells(frm, to):
            bot.blocks.pop(cell, None)
    calls = []

    def fail_recorded_waypoints(_client, x, y, z, **_kw):
        target = (x, y, z)
        calls.append(target)
        if target == entrance:
            bot.pos = entrance
            return True
        return False

    monkeypatch.setattr("baritone_client.common.navigation.goto", fail_recorded_waypoints)

    ok, detail, *_ = iron_stockpile.run_supply_trip(bot, st, now=1000.0)

    rec = st.custom_data[iron_stockpile.KEY]
    assert not ok and "blocked while following the tunnel" in detail
    assert rec["entrance"] == list(entrance)
    assert rec["spine"] == [list(c) for c in spine]
    assert rec["dry_trips"] == 2
    assert rec["next_trip"] == 1600.0
    assert entrance not in rec.get("bad_entrances", [])
    assert calls.count(entrance) == 1  # no broad navigation fallback after retreat


def test_no_entrance_when_the_ground_is_water_or_trees(bot):
    for (x, y, z), block in list(bot.blocks.items()):
        if y == 69:
            bot.blocks[(x, y, z)] = "minecraft:water"
    assert iron_stockpile.choose_entrance(bot, state()) is None


# -- a whole trip -----------------------------------------------------------
def test_a_trip_mines_smelts_banks_the_surplus_and_remembers_the_tunnel(bot):
    with_ore(bot, 64)
    st = state()

    ok, detail, before, after = iron_stockpile.run_supply_trip(bot, st, now=1000.0)

    rec = st.custom_data[iron_stockpile.KEY]
    assert ok and before == 0 and after > before
    assert "mined" in detail and "smelted" in detail
    # Only an armour set stays on the bot; the rest goes in the home chest.
    assert bot.ingots <= iron_stockpile.CARRY_RESERVE and bot.banked > 0
    assert bot.deposits and bot.deposits[0][0] == (2, 70, 2) and bot.deposits[0][1] == iron_stockpile.CARRY_RESERVE
    # The tunnel is remembered, and the bot is back at the surface.
    assert rec["entrance"] and len(rec["spine"]) > 10 and bot.pos[1] >= 68
    assert rec["next_trip"] == 1000.0 + iron_stockpile.SUCCESS_REST and rec["failures"] == 0


def test_failed_inventory_preparation_never_enters_the_mine(bot, monkeypatch):
    monkeypatch.setattr("baritone_client.automator.iron_preparation.prepare_iron_inventory", lambda *_a: "not enough verified inventory space for iron")
    with_ore(bot, 64)
    ok, detail, *_ = iron_stockpile.run_supply_trip(bot, state(), now=1000)
    assert not ok and "inventory space" in detail
    assert bot.dug == [] and bot.pos == ENTRANCE


def test_closed_saved_house_door_blocks_mining_until_departure_is_safe(bot, monkeypatch):
    with_ore(bot, 64)
    called = []
    monkeypatch.setattr(
        "baritone_client.common.house_door_travel.prepare_house_door_for_departure",
        lambda *_a: called.append(True) or False,
    )

    ok, detail, *_ = iron_stockpile.run_supply_trip(bot, state(), now=1000)

    assert not ok and "doorway" in detail
    assert called == [True] and bot.dug == []


def test_the_next_trip_walks_the_recorded_tunnel_instead_of_digging_it_again(bot):
    with_ore(bot, 64)
    st = state()
    iron_stockpile.run_supply_trip(bot, st, now=1000.0)
    first_digs = len(bot.dug)
    old_spine = [tuple(c) for c in st.custom_data[iron_stockpile.KEY]["spine"]]

    bot.pos = ENTRANCE
    bot.raw_iron = bot.ingots = bot.banked = 0
    with_ore(bot, 64, origin=(-9, 52, -6))  # fresh ore beyond the old workings
    ok, *_ = iron_stockpile.run_supply_trip(bot, st, now=5000.0)

    assert ok
    # The old staircase was walked, not dug again: every cell it implies was
    # already open, so none of them show up among the second trip's digs.
    old_cells = tm.own_cells_of(old_spine)
    assert not [cell for cell, _block in bot.dug[first_digs:] if cell in old_cells]
    assert len(bot.dug) > first_digs  # and the new deposit really was mined


def test_a_trip_that_finds_no_iron_backs_off_and_eventually_picks_a_new_entrance(bot):
    st = state()
    for n in range(3):
        ok, *_ = iron_stockpile.run_supply_trip(bot, st, now=1000.0 * (n + 1))
        assert not ok
    rec = st.custom_data[iron_stockpile.KEY]
    assert rec["failures"] == 3
    assert rec["next_trip"] == 3000.0 + iron_stockpile.FAIL_BACKOFF[2]
    assert "entrance" not in rec and rec["bad_entrances"]  # rotated to a new mine


def test_a_trip_without_torches_runs_a_shorter_dark_trip_and_asks_for_torches_only_hourly(bot, monkeypatch):
    bot.torches = 1
    with_ore(bot, 64)
    asked = []
    monkeypatch.setattr(
        "baritone_client.automator.base_lighting.ensure_torches",
        lambda *_a, **_k: asked.append(1) or bot.torches,
    )
    st = state()

    ok, detail, *_ = iron_stockpile.run_supply_trip(bot, st, now=1000.0)

    assert ok and asked == [1]
    assert len(bot.placed) <= 1  # it uses the one torch it has, and nothing more
    # A second trip within the hour does not try to craft torches again.
    bot.pos = ENTRANCE
    iron_stockpile.run_supply_trip(bot, st, now=2000.0)
    assert asked == [1]
    bot.pos = ENTRANCE
    iron_stockpile.run_supply_trip(bot, st, now=1000.0 + iron_stockpile.TORCH_RETRY_SECONDS + 1)
    assert asked == [1, 1]


def test_missing_torches_are_repaired_before_entering_a_deep_saved_mine(bot, monkeypatch):
    spine = [[16, 70, 0], [17, 69, 0], [18, 50, 0]]
    st = state({iron_stockpile.KEY: {"entrance": spine[0], "spine": spine}})
    bot.torches = 0
    asked = []
    monkeypatch.setattr("baritone_client.automator.base_lighting.ensure_torches", lambda *_a: asked.append(1) or 0)
    monkeypatch.setattr(tm.TunnelMiner, "descend", lambda *_a: pytest.fail("unlit deep route must not start"))
    ok, detail, *_ = iron_stockpile.run_supply_trip(bot, st, now=5000)
    assert not ok and "needs torches before descent" in detail
    assert asked == [1] and not bot.dug
    assert st.custom_data[iron_stockpile.KEY]["spine"] == spine
    assert st.custom_data[iron_stockpile.KEY].get("bad_entrances", []) == []
    assert st.custom_data[iron_stockpile.KEY]["next_trip"] == 5600
    assert st.custom_data[iron_stockpile.KEY]["torch_retry"] <= 5600


def test_clutter_is_banked_before_a_trip_when_the_pack_is_nearly_full(bot):
    with_ore(bot, 64)
    bot.used_slots = 33
    st = state()

    ok, *_ = iron_stockpile.run_supply_trip(bot, st, now=1000.0)

    clutter_runs = [d for d in bot.deposits if d[1] == "clutter"]
    assert ok and len(clutter_runs) == 1 and bot.used_slots < 33
    assert "minecraft:iron_pickaxe" not in iron_stockpile.CLUTTER
    assert not any(n.endswith(("_sword", "_pickaxe", "torch", "ingot", "bread")) for n in iron_stockpile.CLUTTER)


def test_a_mob_during_the_trip_still_brings_the_bot_home_and_keeps_what_it_mined(bot):
    with_ore(bot, 64)
    bot.hostile_after_digs = 40
    st = state()

    ok, detail, *_ = iron_stockpile.run_supply_trip(bot, st, now=1000.0)

    assert not ok and "hostile mob" in detail and "return route not verified" in detail
    assert bot.pos[1] < 68  # persistent threats block tunnel travel
    assert st.custom_data[iron_stockpile.KEY]["spine"]  # progress survives


# -- the scheduler hook -----------------------------------------------------
def test_armour_upkeep_offers_and_runs_the_iron_trip(bot, monkeypatch):
    monkeypatch.setattr(armor_upkeep, "equipped_pieces", lambda _c: 4)
    monkeypatch.setattr(armor_upkeep, "needs_freeze_boots", lambda _c: False)
    monkeypatch.setattr("baritone_client.automator.armor_upkeep.worn_out_pieces", lambda _c: [])
    signals = SimpleNamespace(health=20.0, nearby_hostiles=0)

    offer = armor_upkeep.select_armor_opportunity(bot, signals, True, state())
    assert offer is not None and offer.score == 185 and "iron stock 0" in offer.reason

    ran = []
    monkeypatch.setattr(
        iron_stockpile, "run_supply_trip", lambda _c, _s: ran.append(1) or (True, "mined 9", 0, 9)
    )
    assert armor_upkeep.run_armor_upkeep(bot, state()) == (True, "mined 9", 0, 9)
    assert ran == [1]


def test_a_trip_starts_by_walking_home_when_the_bot_is_far_away(bot):
    with_ore(bot, 64)
    bot.pos = (500, 70, 500)  # nowhere near the base, so its chunks are unreadable
    visited = []
    real_goto = bot.goto

    def spy(client, x, y, z, **kw):
        visited.append((x, y, z))
        if (x, y, z) == (0, 70, 0):  # the base anchor itself is a house tile
            bot.pos = (x, y, z)
            return True
        return real_goto(client, x, y, z, **kw)

    import baritone_client.common.navigation as nav

    nav.goto = spy
    try:
        ok, *_ = iron_stockpile.run_supply_trip(bot, state(), now=1000.0)
    finally:
        nav.goto = real_goto
    assert ok and visited[0] == (0, 70, 0)



def test_a_route_that_lost_its_first_steps_is_entered_at_the_first_intact_step(bot, monkeypatch):
    """Live A1 2026-10-16..20: a pit took the floor from the first four steps
    of a 283-step tunnel; 63 trips refused step 1 and mined nothing."""
    bot.torches = 12
    spine = [[16 + n, 70, 0] for n in range(12)]
    floors = {(16 + n, 69, 0): "minecraft:stone" for n in range(12)}
    for lost in (1, 2, 3, 4):
        floors[(16 + lost, 69, 0)] = "minecraft:air"
    monkeypatch.setattr(iron_stockpile, "_block", lambda _c, x, y, z: floors.get((x, y, z), "minecraft:air"))
    st = state({iron_stockpile.KEY: {"entrance": spine[0], "spine": spine, "torch_retry": 9000}})
    arrivals, walked = [], []
    def goto(client, x, y, z, **kwargs):
        arrivals.append((x, y, z))
        return bot.goto(client, x, y, z, **kwargs)

    monkeypatch.setattr("baritone_client.common.navigation.goto", goto)
    monkeypatch.setattr(iron_stockpile, "_at_cell", lambda _c, cell: tuple(cell) == arrivals[-1])
    monkeypatch.setattr(tm.TunnelMiner, "descend", lambda miner, route: walked.append(list(route)))
    monkeypatch.setattr(tm.TunnelMiner, "mine", lambda *_a: "no reachable iron")
    monkeypatch.setattr(tm.TunnelMiner, "retreat", lambda _miner: True)
    iron_stockpile.run_supply_trip(bot, st, now=1000)
    assert (21, 70, 0) in arrivals and (16, 70, 0) not in arrivals, "walk to step 5, the first supported step"
    assert walked[0][0] == (21, 70, 0) and len(walked[0]) == 7
    assert st.custom_data[iron_stockpile.KEY]["entrance"] == spine[0], "the surface entrance still anchors the miner"
