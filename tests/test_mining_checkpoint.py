from types import SimpleNamespace

from baritone_client.automator.mining_checkpoint import checkpoint_progress
from baritone_client.automator.work_progress import productive_snapshot


def test_long_trip_saves_current_route_and_inventory_without_crediting_production(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("baritone_client.automator.mining_checkpoint.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("baritone_client.common.inventory.get_inventory", lambda _c: {"minecraft:raw_iron": 3})
    saves = []
    state = SimpleNamespace(custom_data={"iron_stockpile": {}}, save_checkpoint=saves.append)
    rec = state.custom_data["iron_stockpile"]
    save = checkpoint_progress(None, state, rec)
    trail = [(0, 70, 0), (1, 69, 0)]
    save(trail)
    assert not saves
    clock[0] = 60
    save(trail)
    assert rec["spine"] == [[0, 70, 0], [1, 69, 0]]
    assert saves == [{"minecraft:raw_iron": 3}]
    assert productive_snapshot(state) == {}
