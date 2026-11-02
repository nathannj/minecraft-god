"""Exercise real Paper callbacks, native Opus, authenticated HTTP, local STT and routing.

Uses synthetic Alice/Bob identities and a supplied 48 kHz mono PCM16 speech fixture
in a disposable world. It does not use microphones, player accounts or model turns.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time

from .bridge import BridgeConfig, GodBridge
from .event_http import start_event_server
from .runtime import workspace_root
from .story import event_targets
from .verify_server import free_port
from .voice import VoiceService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wav", required=True, type=Path)
    parser.add_argument("--expect", default="Bob temple God home", help="Words that must appear in the transcript")
    args = parser.parse_args()
    root = workspace_root()
    eula = root / "eula.txt"
    if not eula.exists() or not any(line.strip() == "eula=true" for line in eula.read_text().splitlines()):
        raise RuntimeError("The existing setup must have an accepted Minecraft EULA")
    directory = Path(tempfile.mkdtemp(prefix="voice-verification-", dir=root / ".tools"))
    for folder in ("cache", "libraries", "versions"):
        if (root / folder).exists():
            shutil.copytree(root / folder, directory / folder)
    shutil.copy2(eula, directory / "eula.txt")
    plugins = directory / "plugins"
    plugins.mkdir()
    for source, name in [(root / "paper-plugin/build/libs/minecraft-god-paper-plugin-0.1.0.jar", "MinecraftGodBridge.jar"),
                         (root / "paper-plugin/build/verification/VoiceProbe.jar", "VoiceProbe.jar"),
                         (root / "plugins/voicechat.jar", "voicechat.jar")]:
        shutil.copy2(source, plugins / name)
    token = secrets.token_hex(32)
    bridge = GodBridge(BridgeConfig(state_file=str(directory / "state/god_state.json"),
        events_file=str(directory / "state/events.jsonl"), ledger_file=str(directory / "state/ledger.sqlite3"),
        profile_dir=str(directory / "state/players"), event_port=0, event_token=token, dry_run=True))
    voice = VoiceService(bridge)
    server = start_event_server(bridge, voice_service=voice)
    process = None
    report = {"ok": False, "world": str(directory)}
    try:
        deadline = time.monotonic()+60
        while voice.state == "loading" and time.monotonic() < deadline:
            time.sleep(.1)
        if voice.state != "ready":
            raise RuntimeError("Local recognizer is unavailable")
        for name in ("MinecraftGodBridge", "VoiceProbe"):
            folder = plugins / name
            folder.mkdir()
            (folder / "config.yml").write_text(f"bridge-url: http://127.0.0.1:{server.server_port}/events\nbridge-token: '{token}'\n")
        shutil.copy2(args.wav, plugins / "VoiceProbe/fixture.wav")
        (plugins / "voicechat").mkdir()
        (plugins / "voicechat/voicechat-server.properties").write_text("port=0\nbind_address=127.0.0.1\n")
        (directory / "server.properties").write_text(f"server-ip=127.0.0.1\nserver-port={free_port()}\nonline-mode=true\nview-distance=2\nsimulation-distance=2\ngenerate-structures=false\n")
        java = (root / "server/java-path.txt").read_text().strip()
        began = time.monotonic()
        with (directory / "console.log").open("w", encoding="utf-8") as output:
            process = subprocess.Popen([java, "-Xms512M", "-Xmx1536M", "-jar", str(root / "server/paper.jar"), "--nogui"],
                cwd=directory, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT, text=True)
            deadline = time.monotonic()+90
            events = []
            while time.monotonic() < deadline and process.poll() is None:
                events = bridge.events_since(0)
                if events or voice.failed:
                    break
                time.sleep(.2)
            if not events:
                raise AssertionError(f"Voice probe did not produce a transcript: {voice.status()}; inspect {directory / 'console.log'}")
            event = events[0]
            if not all(word.lower() in event["message"].lower() for word in args.expect.split()):
                raise AssertionError("Spoken fixture was not recognized: " + event["message"])
            listener = event["routed_listeners"][0]
            assert listener["player"] == "Bob" and listener["distance"] == 3
            assert event_targets(event, event["player_uuid"], "Alice")
            assert event_targets(event, listener["player_uuid"], "Bob")
            assert not event_targets(event, "00000000-0000-0000-0000-000000000000", "Charlie")
            report.update(ok=True, seconds=round(time.monotonic()-began, 2), recognized=event["message"],
                          transcription=event["transcription"], routed_listeners=event["routed_listeners"],
                          checks=["native_opus_roundtrip", "production_capture_callbacks", "authenticated_voice_upload",
                                  "local_recognition", "listener_context", "scoped_god_routing"])
    finally:
        if process and process.poll() is None:
            process.stdin.write("stop\n")
            process.stdin.flush()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=10)
        voice.close()
        server.shutdown()
        server.server_close()
        (directory / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
