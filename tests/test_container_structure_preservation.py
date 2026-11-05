from types import SimpleNamespace

import pytest

from baritone_client.common.container_reads import clear_occluded_container_face


@pytest.mark.parametrize("material", [
    "minecraft:cobblestone", "minecraft:stone", "minecraft:dirt",
    "minecraft:oak_planks", "minecraft:crafting_table", "minecraft:gravel",
    "minecraft:snow_block", "minecraft:oak_leaves", "minecraft:void_air", "",
])
def test_storage_access_never_demolishes_walls_floors_or_workstations(material):
    mutations = []
    def dispatch(route, payload):
        if route == "get_state":
            return {"block_position": {"x": 0, "y": 70, "z": 0}}
        if route == "get_block":
            return {"id": material}
        mutations.append((route, payload))
        pytest.fail("structural or unknown container surrounds must not be mutated")
    clear_occluded_container_face(SimpleNamespace(transport=SimpleNamespace(dispatch=dispatch)), (0, 70, 2))
    assert mutations == []
