"""Smoke-test the built Paper plugin in a separate, disposable world."""
from __future__ import annotations

import json
import argparse
import gzip
import secrets
import shutil
import socket
import struct
import subprocess
import tempfile
import time
from pathlib import Path

from .rcon import RconClient
from .runtime import workspace_root


ROOT = workspace_root()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def rotation_fixture() -> bytes:
    def string(value):
        raw = value.encode()
        return struct.pack(">H", len(raw)) + raw
    def named(kind, name, raw):
        return bytes([kind]) + string(name) + raw
    def integers(name, values):
        return named(9, name, bytes([3]) + struct.pack(">i", len(values)) + b"".join(struct.pack(">i", value) for value in values))
    palette = named(9, "palette", bytes([10]) + struct.pack(">i", 1) + named(8, "Name", string("minecraft:gold_block")) + b"\x00")
    block = integers("pos", [1, 2, 3]) + named(3, "state", struct.pack(">i", 0)) + b"\x00"
    blocks = named(9, "blocks", bytes([10]) + struct.pack(">i", 1) + block)
    return named(10, "", integers("size", [4, 5, 6]) + palette + blocks + b"\x00")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", action="store_true", help="Also install and exercise the private camera renderer")
    parser.add_argument("--multiplayer", action="store_true", help="Load and check voice chat, AI hearing and map tracking")
    args = parser.parse_args()
    eula = ROOT / "eula.txt"
    if not eula.exists() or "eula=true" not in eula.read_text().lower():
        raise RuntimeError("the existing setup must have an accepted Minecraft EULA")
    directory = Path(tempfile.mkdtemp(prefix="paper-verification-", dir=ROOT / ".tools"))
    shutil.copy2(eula, directory / "eula.txt")
    for folder in ["cache", "libraries", "versions"]:
        if (ROOT / folder).exists():
            shutil.copytree(ROOT / folder, directory / folder)
    (directory / "plugins").mkdir()
    plugin = ROOT / "paper-plugin" / "build" / "libs" / "minecraft-god-paper-plugin-0.1.0.jar"
    shutil.copy2(plugin, directory / "plugins" / plugin.name)
    if args.multiplayer:
        for name in ("voicechat.jar", "XaeroTracker.jar"):
            shutil.copy2(ROOT / "plugins" / name, directory / "plugins" / name)
        voice_directory = directory / "plugins" / "voicechat"
        voice_directory.mkdir()
        # An OS-assigned UDP port keeps verification separate from the real server.
        (voice_directory / "voicechat-server.properties").write_text("port=0\nbind_address=127.0.0.1\n")
    if args.camera:
        shutil.copy2(ROOT / "plugins" / "SpectatorCamera.jar", directory / "plugins" / "SpectatorCamera.jar")
        camera_directory = directory / "plugins" / "SpectatorCamera"
        (camera_directory / "renders").mkdir(parents=True)
        shutil.copy2(ROOT / "templates" / "SpectatorCamera.yml", camera_directory / "config.yml")
    password = secrets.token_hex(16)
    server_port, rcon_port = free_port(), free_port()
    while rcon_port == server_port:
        rcon_port = free_port()
    properties = f"server-ip=127.0.0.1\nserver-port={server_port}\nenable-rcon=true\nrcon.port={rcon_port}\nrcon.password={password}\nonline-mode=true\nlevel-type=minecraft:flat\ngenerate-structures=false\nview-distance=2\nsimulation-distance=2\nspawn-protection=0\n"
    (directory / "server.properties").write_text(properties)
    (directory / "plugins" / "MinecraftGodBridge").mkdir()
    (directory / "plugins" / "MinecraftGodBridge" / "config.yml").write_text("bridge-url: http://127.0.0.1:1/events\n")
    template = directory / "world" / "generated" / "god_test" / "structure" / "rotation.nbt"
    template.parent.mkdir(parents=True)
    template.write_bytes(gzip.compress(rotation_fixture()))
    java = (ROOT / "server" / "java-path.txt").read_text().strip()
    rcon = RconClient("127.0.0.1", rcon_port, password, timeout=3)
    report = {"world": str(directory), "checks": []}
    with (directory / "verification-console.log").open("w", encoding="utf-8") as output:
        process = subprocess.Popen([java, "-Xms512M", "-Xmx1536M", "-jar", str(ROOT / "server" / "paper.jar"), "--nogui"], cwd=directory, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT, text=True)
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("verification server exited; inspect its console log")
                try:
                    rcon.command("list")
                    break
                except (OSError, ConnectionError):
                    time.sleep(1)
            else:
                raise TimeoutError("verification server startup exceeded 90 seconds")
            def check(label, command, assertion):
                start = time.monotonic()
                result = json.loads(rcon.command(command).body)
                if not assertion(result):
                    raise AssertionError(f"{label} failed; inspect the verification server log")
                report["checks"].append({"name": label, "ok": True, "seconds": round(time.monotonic() - start, 3)})
            rcon.command("godvalidate Nobody minecraft:overworld 0 64 0 0 64 0")
            time.sleep(0.3)
            placement = rcon.command("setblock 0 64 0 minecraft:gold_block").body
            if "not loaded" in placement.lower():
                raise AssertionError("verification setup did not load its test chunk")
            check("batch_128_exact_blocks", "godcheckbatch minecraft:overworld " + " ".join(f"{x},64,0" for x in range(128)), lambda r: r["ok"] and len(r["results"]) == 128 and r["results"][0]["block"]["type"] == "minecraft:gold_block")
            check("footprint_materials", "godfootprint minecraft:overworld 0 64 0 2 66 2", lambda r: r["ok"] and r["volume"] == 27 and r["materials"]["minecraft:gold_block"] == 1)
            check("live_collision_check", "godvalidate Nobody minecraft:overworld -10 60 -10 10 80 10", lambda r: r["ok"] and not r["other_players"])
            check("observation_missing_player", "godobserve Nobody", lambda r: not r["ok"] and r["error"] == "player_not_found")
            check("surface_missing_player", "godsurface Nobody", lambda r: not r["ok"] and r["error"] == "player_not_found")
            check("reject_overlarge_footprint", "godfootprint minecraft:overworld 0 64 0 100 64 0", lambda r: not r["ok"])
            for rotation, point in [("clockwise_90", "-3,92,1"), ("clockwise_180", "-1,102,-3"), ("counterclockwise_90", "3,112,-1")]:
                height = int(point.split(",")[1]) - 2
                rcon.command(f"godvalidate Nobody minecraft:overworld -8 {height} -8 8 {height+5} 8")
                command_rotation = "180" if rotation == "clockwise_180" else rotation
                placed = rcon.command(f"place template god_test:rotation 0 {height} 0 {command_rotation}").body
                if not any(marker in placed for marker in ["Successfully", "Placed", "placed", "Loaded template"]):
                    raise AssertionError("rotation fixture placement failed: " + placed)
                check("rotation_" + rotation, "godcheckbatch minecraft:overworld " + point, lambda r: r["ok"] and r["results"][0]["block"]["type"] == "minecraft:gold_block")
            check("smite_missing_player", "godsmite Nobody", lambda r: not r["ok"] and r["error"] == "player_not_found")
            if args.multiplayer:
                output.flush()
                console = (directory / "verification-console.log").read_text(encoding="utf-8", errors="replace")
                for marker in ("AI proximity hearing registered", "Registering events for 'minecraft_god_hearing'", "Voice chat server started", "Enabling XaeroTracker"):
                    if marker not in console:
                        raise AssertionError("Multiplayer startup marker missing: " + marker)
                if "M = world map" not in rcon.command("map").body:
                    raise AssertionError("Map shortcut command did not register")
                if "Use this command in game" not in rcon.command("godvoice status").body:
                    raise AssertionError("Voice preferences command did not register")
                report["checks"].append({"name": "multiplayer_plugins_voice_udp_and_commands", "ok": True})
            if args.camera:
                from .bridge import BridgeConfig, GodBridge
                camera_bridge = GodBridge(BridgeConfig(
                    state_file=str(directory / "state/god_state.json"),
                    profile_dir=str(directory / "state/players"),
                    camera_output_dir=str(directory / "state/captures"),
                    camera_plugin_render_dir=str(directory / "plugins/SpectatorCamera/renders"),
                ), rcon=rcon)
                began = time.monotonic()
                capture = camera_bridge.capture_scene_at("0 64 0", "minecraft:overworld", 4, 4)
                if not capture.get("ok"):
                    raise AssertionError("Camera integration failed: " + str(capture.get("error")))
                if len(capture.get("images", [])) != 2 or not all(Path(image["path"]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n") for image in capture["images"]):
                    raise AssertionError("Camera did not produce two PNG views")
                report["checks"].append({"name": "private_camera_two_views", "ok": True, "seconds": round(time.monotonic() - began, 3)})
            report["ok"] = True
        finally:
            if process.poll() is None:
                try:
                    rcon.command("stop")
                except (OSError, ConnectionError):
                    if process.stdin:
                        process.stdin.write("stop\n")
                        process.stdin.flush()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=10)
            (directory / "verification-report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
