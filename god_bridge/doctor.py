"""Check a local installation without starting Minecraft or making model calls."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from .codex_runtime import check_version, codex_binary, runtime_environment
from .runtime import workspace_root


def diagnose(root: Path, *, offline: bool = False) -> list[dict]:
    results = []
    def check(name, condition, hint=""):
        results.append({"check": name, "ok": bool(condition), "hint": "" if condition else hint})
    def digest(path, algorithm):
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, algorithm).hexdigest()
    check("Python 3.11+", sys.version_info >= (3, 11), "Install Python 3.11 or newer.")
    from .demo import run_demo
    try:
        check("Offline objective replay", run_demo()["milestones"] == 1)
    except Exception as error:
        check("Offline objective replay", False, str(error))
    if offline:
        return results
    check("Minecraft EULA", (root / "eula.txt").exists() and any(line.strip() == "eula=true" for line in (root / "eula.txt").read_text().splitlines()), "Read the EULA and run setup with -AcceptEula if you accept it.")
    for name in ["rcon-password.txt", "event-token.txt"]:
        path = root / "server" / name
        check(name, path.is_file() and len(path.read_text().strip()) >= 32, "Run setup to generate this local credential.")
    try:
        lock = json.loads((root / "versions.lock.json").read_text())
        jar = root / "server" / "paper.jar"
        check("Pinned Paper server", jar.is_file() and digest(jar, "sha256") == lock["paper"]["sha256"], "Run setup to install the pinned server build.")
        camera = root / "plugins" / "SpectatorCamera.jar"
        check("Camera plugin", camera.is_file() and digest(camera, "sha512") == lock["camera"]["sha512"], "Run setup to install the camera plugin.")
        for key, filename, algorithm in [("voicechat", "voicechat.jar", "sha512"), ("xaero_tracker", "XaeroTracker.jar", "sha256")]:
            path = root / "plugins" / filename
            check(filename, path.is_file() and digest(path, algorithm) == lock[key][algorithm], "Run setup-multiplayer.ps1.")
    except (OSError, ValueError, KeyError) as error:
        check("Release manifest", False, str(error))
    built = root / "paper-plugin" / "build" / "libs" / "minecraft-god-paper-plugin-0.1.0.jar"
    deployed = root / "plugins" / "MinecraftGodBridge.jar"
    check("Built bridge installed", built.is_file() and deployed.is_file() and built.read_bytes() == deployed.read_bytes(), "Run setup to build, test, and install the current plugin.")
    voice_python = root / ".tools/voice-venv/Scripts/python.exe"
    if voice_python.is_file():
        result = subprocess.run([str(voice_python), "-c", "import faster_whisper,av; assert av.__version__ == '16.0.1'"], capture_output=True, timeout=30)
        check("Local voice runtime", result.returncode == 0, "Run setup-multiplayer.ps1 to install compatible audio libraries.")
    else:
        check("Local voice runtime", False, "Run setup-multiplayer.ps1.")
    try:
        java = (root / "server" / "java-path.txt").read_text().strip()
        result = subprocess.run([java, "--version"], capture_output=True, text=True, timeout=10)
        import re
        match = re.search(r"\b(\d+)", result.stdout)
        check("Java 25+", result.returncode == 0 and match and int(match[1]) >= 25, "Run setup to select Java 25 or newer.")
    except (OSError, subprocess.SubprocessError) as error:
        check("Java 25+", False, str(error))
    try:
        binary = codex_binary()
        check("Pinned Codex CLI", bool(check_version(binary)))
        result = subprocess.run([binary, "login", "status"], env=runtime_environment(root), capture_output=True, text=True, timeout=15)
        check("Game Codex login", result.returncode == 0, "Run: py -3 -m god_bridge.codex_runtime login")
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        check("Codex runtime", False, str(error))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Check the portable Python install only")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    results = diagnose(workspace_root(), offline=args.offline)
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for result in results:
            print(f"{'PASS' if result['ok'] else 'FIX '} {result['check']}" + (f" - {result['hint']}" if result['hint'] else ""))
    raise SystemExit(0 if all(item["ok"] for item in results) else 1)


if __name__ == "__main__":
    main()
