"""Project-specific entry point: also makes process ownership unambiguous."""
import os
from pathlib import Path

os.environ["MINECRAFT_GOD_HOME"] = str(Path(__file__).resolve().parent)

from god_bridge.controller import main

if __name__ == "__main__":
    main()
