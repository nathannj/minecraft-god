"""Standalone HTTP event hub for the Paper plugin and god controller."""

from __future__ import annotations

import argparse
import os
import threading

from .bridge import GodBridge
from .mcp_server import config_from_env, start_event_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Minecraft God event hub")
    parser.add_argument("--config", help="Optional JSON config file; environment variables override it")
    args = parser.parse_args()
    bridge = GodBridge(config_from_env(args.config))
    voice = None
    if os.getenv("MINECRAFT_VOICE_ENABLED", "0") == "1":
        from .voice import VoiceService
        voice = VoiceService(bridge)
    server = start_event_server(bridge, voice_service=voice)
    print(f"Minecraft God event hub listening on http://{server.server_address[0]}:{server.server_address[1]}", flush=True)
    try:
        threading.Event().wait()
    finally:
        if voice:
            voice.close()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()

