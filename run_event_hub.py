"""Project-specific entry point for the authenticated local event hub."""
import os
from pathlib import Path

os.environ["MINECRAFT_GOD_HOME"] = str(Path(__file__).resolve().parent)

from god_bridge.event_hub import main

if __name__ == "__main__":
    main()
