"""Voice ingress, local recognition queue, privacy routing, and protocol contracts."""
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import struct
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid
import wave

import pytest

from god_bridge.bridge import BridgeConfig, GodBridge
from god_bridge.event_http import start_event_server
from god_bridge.events import validate_event
from god_bridge.story import classify_event, compact_event, event_targets
from god_bridge.voice import VoiceService, decode_upload, transcript_event

ALICE, BOB, CHARLIE = [str(uuid.uuid5(uuid.NAMESPACE_DNS, name)) for name in ("Alice", "Bob", "Charlie")]


def metadata():
    now = datetime.now(timezone.utc).isoformat()
    person = {"player": "Bob", "player_uuid": BOB, "dimension": "minecraft:overworld", "position": "3 64 0", "distance": 3.0}
    return {"type": "voice_transcript", "event_uuid": str(uuid.uuid4()), "schema_version": 1,
            "player": "Alice", "player_uuid": ALICE, "position": "0 64 0", "dimension": "minecraft:overworld",
            "started_at": now, "ended_at": now, "voice_mode": "proximity", "message": "",
            "nearby_players": [person, {**person, "player": "Charlie", "player_uuid": CHARLIE}],
            "routed_listeners": [person]}


def wav_bytes(*, rate=48000, frames=24000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        wav.writeframes(b"\0\0" * frames)
    return buffer.getvalue()


def upload(event=None, audio=None):
    raw = json.dumps(event if event is not None else metadata()).encode()
    return struct.pack(">I", len(raw)) + raw + (audio if audio is not None else wav_bytes())


def wait_until(predicate):
    deadline = time.monotonic()+5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("timed out")


def test_voice_context_routes_to_speaker_and_recipients_not_everyone_nearby(tmp_path):
    event = transcript_event(decode_upload(upload())[0], {"text": "Bob, God told me to meet you here."})
    assert event_targets(event, ALICE, "Alice")
    assert event_targets(event, BOB, "Bob")
    assert not event_targets(event, CHARLIE, "Charlie")
    assert classify_event(event, player_id=ALICE).action == "wake"
    assert classify_event(event, player_id=BOB).action == "accumulate"
    assert event["possible_recipients"] == [{"player_uuid": BOB, "player": "Bob", "basis": "name_mentioned_in_transcript"}]
    assert compact_event(event)["routed_listeners"] == event["routed_listeners"]
    assert compact_event(event)["transcription"]["verified"] is False
    bridge = GodBridge(BridgeConfig(state_file=str(tmp_path / "state.json"), dry_run=True))
    bridge.record_event(event)
    assert "conduct" not in bridge.recent_events(1)[0]  # No punishment from uncertain speech.
    bridge._owner_id = BOB
    assert bridge.event_matches_owner(event)
    bridge._owner_id = CHARLIE
    assert not bridge.event_matches_owner(event)


@pytest.mark.parametrize("change", [
    {"voice_mode": "group"}, {"player_uuid": "../../escape"}, {"position": "nan 0 0"},
    {"started_at": "2000-01-01T00:00:00Z"}, {"routed_listeners": [None]},
    {"routed_listeners": [{"player": "Bob", "player_uuid": BOB, "dimension": "minecraft:the_nether", "position": "3 64 0", "distance": 3}]},
])
def test_reject_invalid_private_stale_or_cross_dimension_uploads(change):
    with pytest.raises((ValueError, TypeError)):
        decode_upload(upload({**metadata(), **change}))


@pytest.mark.parametrize("audio", [b"not a wav", wav_bytes(rate=16000), wav_bytes(frames=480001), wav_bytes()[:-8]],
                         ids=["not-wav", "wrong-rate", "too-long", "truncated"])
def test_reject_bad_audio(audio):
    with pytest.raises(ValueError):
        decode_upload(upload(audio=audio))


def test_authenticated_endpoint_transcribes_once_without_saving_audio(tmp_path):
    bridge = GodBridge(BridgeConfig(state_file=str(tmp_path / "state.json"), event_port=0, event_token="voice-test", dry_run=True))
    calls = []
    def recognize(audio):
        calls.append(audio)
        return {"text": "Bob, let's meet at the temple.", "language": "en", "model": "fixture"}
    service = VoiceService(bridge, transcriber_factory=lambda: recognize)
    wait_until(lambda: service.state == "ready")
    server = start_event_server(bridge, voice_service=service)
    data = upload()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with pytest.raises(HTTPError) as error:
            urlopen(Request(url + "/voice", data=data))
        assert error.value.code == 401
        headers = {"Authorization": "Bearer voice-test", "Content-Type": "application/x-minecraft-god-voice"}
        for _ in range(2):
            with urlopen(Request(url + "/voice", data=data, headers=headers)) as response:
                assert response.status == 202
        wait_until(lambda: service.processed == 1)
        assert len(calls) == 1 and bridge.latest_event_id() == 1
        event = bridge.events_since(0)[0]
        assert event["message"] == "Bob, let's meet at the temple."
        assert event["source"] == "voice"
        with urlopen(Request(url + "/voice/healthz", headers=headers)) as response:
            assert json.load(response)["processed"] == 1
        assert not list(tmp_path.rglob("*.wav"))
        assert "audio" not in event
    finally:
        service.close()
        server.shutdown()
        server.server_close()


def test_bounded_queue_and_failed_recognizer_do_not_block_gameplay(tmp_path):
    bridge = GodBridge(BridgeConfig(state_file=str(tmp_path / "state.json"), dry_run=True))
    entered, release = threading.Event(), threading.Event()
    def recognize(audio):
        entered.set()
        release.wait(3)
        return {"text": ""}
    service = VoiceService(bridge, transcriber_factory=lambda: recognize, queue_size=1)
    try:
        wait_until(lambda: service.state == "ready")
        assert service.submit(*decode_upload(upload()))
        assert entered.wait(2)
        assert service.submit(*decode_upload(upload()))
        assert not service.submit(*decode_upload(upload()))
        assert service.dropped == 1
    finally:
        release.set()
        service.close()
    def fail():
        raise ImportError("not installed")
    failed = VoiceService(bridge, transcriber_factory=fail)
    wait_until(lambda: failed.state == "unavailable")
    assert not failed.submit(*decode_upload(upload()))
    failed.close()


def test_map_waypoint_spatial_context_survives_compaction():
    event = validate_event({"type": "map_waypoint", "player": "Alice", "player_uuid": ALICE,
                            "dimension": "minecraft:the_nether", "position": "10 64 20", "label": "Portal"})
    assert compact_event(event)["label"] == "Portal"
    assert classify_event(event).action == "accumulate"


def test_delayed_transcription_does_not_mark_departed_player_online(tmp_path):
    bridge = GodBridge(BridgeConfig(state_file=str(tmp_path / "state.json"), dry_run=True))
    bridge.record_event({"type": "player_quit", "player": "Alice", "player_uuid": ALICE})
    bridge.record_event(transcript_event(metadata(), {"text": "See you later"}))
    assert not bridge._ledger.player(ALICE)["online"]


def test_java_wav_crosses_python_decoder():
    fixture = Path(__file__).resolve().parents[1] / "paper-plugin/build/contract/voice.wav"
    if not fixture.exists():
        pytest.skip("Build the Java voice contract fixture first")
    event, audio = decode_upload(upload(audio=fixture.read_bytes()))
    assert event["duration_seconds"] == 0.5
    with wave.open(io.BytesIO(audio)) as wav:
        assert struct.unpack("<hhh", wav.readframes(3)) == (-32768, 0, 32767)
