#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Send one raw command to a running Baritone bridge, bypassing MCP.

Useful when you need to tell whether a problem lives in the MCP tool layer or
in the game/bridge itself: this speaks the bridge protocol directly.

Usage
-----
::

    python scripts/mc.py <port> <command> [params...]
    python scripts/mc.py 5555 get_state
    python scripts/mc.py 5555 get_entities radius=64
    python scripts/mc.py 5555 select_slot '{"slot":0}'
    python scripts/mc.py 5555 --list

``params`` are either a single JSON object or ``key=value`` tokens (values are
coerced to int/bool where they look like one). The ``key=value`` form exists
because PowerShell strips the double quotes out of inline JSON.

Shorthands fill in the parameters you would otherwise have to remember::

    python scripts/mc.py 5555 goto 100 64 200      -> goto {x,y,z}
    python scripts/mc.py 5555 follow <player>      -> follow {action, entity}
    python scripts/mc.py 5555 say hello there      -> chat {message}
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from baritone_client.transport.transport import TcpTransport

# Shorthand -> (real command, params builder)
ALIASES = {
    "follow": lambda a: ("follow", {"action": "follow", "entity": a[0] if a else "player"}),
    "goto": lambda a: ("goto", {"x": int(a[0]), "y": int(a[1]), "z": int(a[2])}),
    "say": lambda a: ("chat", {"message": " ".join(a)}),
    "drop": lambda a: ("throw_item", {"slot": int(a[0])} if a else {}),
    "hold": lambda a: ("select_slot", {"slot": int(a[0])}),
}

COMMANDS = (
    "goto come follow mine get_inventory inventory_click select_trade",
    "get_state get_entities get_combat_snapshot get_player_pos scan_biomes",
    "build sel inspect_build_site mission place_block use_bucket get_block",
    "chat select_slot respawn get_recipes craft find_blocks interact_block",
    "look_at look cancel use_item attack_entity attack_block swing_hand",
    "get_view close_screen equip get_version explore stop pause goal farm",
    "tunnel get_events get_screen settings break_block dig_block",
    "set_fast_break get_dimension get_death_location throw_item axis strip",
    "quarry path tunnel_wide place_torches screenshot auto_craft smelt_items",
    "click_recipe place_fire place_recipe entity_interact entity_transport",
    "advanced_goal craft_advanced sequence",
)


def _coerce(value):
    """Turn a command-line string into int / bool / str."""
    try:
        return int(value)
    except ValueError:
        pass
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    return value


def usage():
    print(__doc__)
    print("=== bridge handlers ===")
    for line in COMMANDS:
        print("  " + line)


def main():
    if len(sys.argv) < 3 or sys.argv[1] in ("-h", "--help", "--list"):
        usage()
        return 0

    port = int(sys.argv[1])
    raw = sys.argv[2]

    if raw in ALIASES:
        cmd, params = ALIASES[raw](sys.argv[3:])
    else:
        cmd = raw
        if len(sys.argv) > 3 and sys.argv[3].strip().startswith("{"):
            params = json.loads(sys.argv[3])
        elif len(sys.argv) > 3:
            params = {}
            for token in sys.argv[3:]:
                if "=" not in token:
                    print(f"[warn] dropping unparseable argument: {token}")
                    continue
                key, value = token.split("=", 1)
                params[key] = _coerce(value)
        else:
            params = {}

    transport = TcpTransport(host="127.0.0.1", port=port, timeout=15.0)
    print(f">>> {cmd} {json.dumps(params, ensure_ascii=False)}")
    try:
        result = transport.dispatch(cmd, params)
        print(json.dumps(result, ensure_ascii=False, indent=2)[:1500])
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI boundary, report and exit non-zero
        print(f"[FAIL] {type(exc).__name__}: {str(exc)[:400]}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
