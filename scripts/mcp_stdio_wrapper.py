#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""MCP server wrapper for mcbaratone: force the plain-TCP transport.

Why this exists
---------------
``BridgeConfig`` in :mod:`baritone_client.mcp.mcp_server` defaults to
``transport_type="websocket"``, and the ``main()`` of that module builds its
config without overriding the field (the CLI exposes no flag for it either).
The bridge's command port is **plain TCP**, while the WebSocket event service
listens on a different port. Starting the upstream entrypoint directly
therefore tries ``ws://<host>:<port>`` and never connects.

This wrapper sets ``transport_type = "tcp"`` and nothing else.

Usage
-----
::

    <venv>\\Scripts\\python.exe scripts\\mcp_stdio_wrapper.py --host localhost --port 5555

Point the MCP client's ``command`` / ``args`` at this file. For a full DSH
walkthrough see ``DSH-READHERE.md`` at the repository root.
"""
import argparse
import logging
import sys
from pathlib import Path

# Fallback for running straight from a checkout (an editable install already
# makes the package importable, so this is belt-and-braces).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from baritone_client.mcp.mcp_server import BridgeConfig, create_mcp_server  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="mcbaratone MCP server (TCP transport wrapper)"
    )
    parser.add_argument("--host", default="localhost", help="Baritone bridge host")
    parser.add_argument("--port", type=int, default=5555, help="Baritone bridge TCP port")
    parser.add_argument(
        "--timeout", type=float, default=15.0, help="Per-request timeout in seconds"
    )
    parser.add_argument("--log-level", default="INFO", help="DEBUG/INFO/WARNING/ERROR")
    parser.add_argument(
        "--mcp-transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="How the MCP client talks to this server (default: stdio)",
    )
    parser.add_argument("--http-host", default="127.0.0.1")
    parser.add_argument("--http-port", type=int, default=8000)
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = BridgeConfig(host=args.host, port=args.port, timeout=args.timeout)
    config.transport_type = "tcp"  # upstream defaults to websocket

    server = create_mcp_server(config)
    if args.mcp_transport == "stdio":
        server.run(transport="stdio")
    else:
        server.settings.host = args.http_host
        server.settings.port = args.http_port
        server.run(transport="streamable-http")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
