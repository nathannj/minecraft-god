"""Standalone HTTP event hub for the Paper plugin and god controller."""

from __future__ import annotations

import argparse
import threading

from .bridge import GodBridge
from .mcp_server import config_from_env, start_event_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Minecraft God event hub")
    parser.add_argument("--config", help="Optional JSON config file; environment variables override it")
    args = parser.parse_args()
    bridge = GodBridge(config_from_env(args.config))
    server = start_event_server(bridge)
    print(f"Minecraft God event hub listening on http://{server.server_address[0]}:{server.server_address[1]}", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()

