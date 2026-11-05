"""Native crop work cannot unprotect a home after its initial travel."""

from types import SimpleNamespace

import pytest

from baritone_client.automator.common.crop_opportunity import run_crop_opportunity
from baritone_client.common.tasks import PlayerDeathDetected


@pytest.mark.parametrize("dies", [False, True])
def test_native_crop_process_holds_digging_guard_and_restores_after_stop(dies):
    settings = {"value": "true"}
    farm_calls = []

    def dispatch(route, payload):
        if route == "settings":
            if "set" in payload:
                settings["value"] = payload["value"]
            return dict(settings)
        if route == "get_state":
            return {"is_pathing": False, "health": 20, "air_supply": 300,
                    "block_position": {"x": 0, "y": 65, "z": 0}}
        if route == "farm":
            farm_calls.append(settings["value"])
            if dies:
                raise PlayerDeathDetected("death during crop dispatch")
        return {}

    client = SimpleNamespace(transport=SimpleNamespace(dispatch=dispatch),
                             _protected_home_anchor=(0, 65, 0))
    reads = iter([{}, {"minecraft:wheat": 1}])
    opportunity = SimpleNamespace(location=(0, 64, 0))
    arguments = dict(inventory_reader=lambda _c: next(reads),
                     traveler=lambda *_a, **_k: True,
                     sleeper=lambda _s: None)
    if dies:
        with pytest.raises(PlayerDeathDetected):
            run_crop_opportunity(client, opportunity, 1, **arguments)
    else:
        assert run_crop_opportunity(client, opportunity, 1, **arguments)[0]
    assert farm_calls == ["false"]
    assert settings["value"] == "true"


def test_unavailable_home_setting_refuses_native_crop_dispatch():
    calls = []
    client = SimpleNamespace(
        transport=SimpleNamespace(dispatch=lambda route, payload: calls.append(route) or {}),
        _protected_home_anchor=(0, 65, 0),
    )
    result = run_crop_opportunity(client, SimpleNamespace(location=(0, 64, 0)), 1)
    assert result[0] is False
    assert "farm" not in calls
