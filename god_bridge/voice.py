"""Bounded, local speech recognition. Audio exists only in the in-memory queue.

Wire format: 4-byte big-endian JSON length, UTF-8 metadata, 48 kHz mono PCM WAV.
The authenticated Paper bridge supplies identities and spatial/recipient evidence;
recognition supplies text only and can never alter those identities.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
import io
import json
import os
import queue
import re
import struct
import threading
import time
import wave

from .events import validate_event
from .runtime import workspace_root

MAX_VOICE_BYTES = 1_030_000


def decode_upload(body: bytes) -> tuple[dict, bytes]:
    if not 48 <= len(body) <= MAX_VOICE_BYTES:
        raise ValueError("invalid voice upload size")
    length = struct.unpack(">I", body[:4])[0]
    if not 2 <= length <= 65536 or length + 48 > len(body):
        raise ValueError("invalid voice metadata length")
    supplied = json.loads(body[4:4+length])
    if not isinstance(supplied, dict) or supplied.get("type") != "voice_transcript":
        raise ValueError("voice metadata must be a voice_transcript object")
    keys = {"schema_version", "type", "event_uuid", "player", "player_uuid", "dimension", "position",
            "started_at", "ended_at", "voice_mode", "nearby_players", "routed_listeners"}
    event = validate_event({**{k: v for k, v in supplied.items() if k in keys}, "message": ""})
    if not event.get("event_uuid"):
        raise ValueError("voice requires a unique utterance ID")
    for key in ("started_at", "ended_at"):
        value = event.get(key)
        if not isinstance(value, str) or len(value) > 40:
            raise ValueError("voice requires capture timestamps")
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or abs((datetime.now(timezone.utc)-stamp).total_seconds()) > 120:
            raise ValueError("voice capture expired or timestamp is invalid")
    audio = body[4+length:]
    try:
        with wave.open(io.BytesIO(audio)) as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 48000, "NONE"):
                raise ValueError("voice must be 48 kHz mono PCM16 WAV")
            frames = wav.getnframes()
            if not 9600 <= frames <= 480000 or len(wav.readframes(frames)) != frames*2:
                raise ValueError("voice must contain 0.2 to 10 seconds of complete audio")
    except (wave.Error, EOFError) as error:
        raise ValueError("invalid voice WAV") from error
    event["duration_seconds"] = frames / 48000
    return event, audio


class LocalTranscriber:
    def __init__(self):
        from faster_whisper import WhisperModel
        self.name = os.getenv("MINECRAFT_VOICE_MODEL", "base.en")
        self.model = WhisperModel(self.name, device="cpu", compute_type="int8",
                                  cpu_threads=2, num_workers=1,
                                  download_root=str(workspace_root() / ".tools" / "whisper-models"))

    def __call__(self, audio: bytes) -> dict:
        segments, info = self.model.transcribe(io.BytesIO(audio), beam_size=3, vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 350}, condition_on_previous_text=False,
            language=os.getenv("MINECRAFT_VOICE_LANGUAGE", "en") or None)
        parts = [s for s in segments if s.no_speech_prob < 0.6 and s.avg_logprob > -1.0]
        return {"text": " ".join(s.text.strip() for s in parts).strip()[:4096],
                "model": self.name, "language": info.language,
                "average_log_probability": round(sum(s.avg_logprob for s in parts)/len(parts), 3) if parts else None}


def transcript_event(metadata: dict, result: dict) -> dict | None:
    text = str(result.get("text", "")).strip()[:4096]
    if not text:
        return None
    event = {**metadata, "message": text, "source": "voice", "at": metadata["started_at"],
             "transcribed_at": datetime.now(timezone.utc).isoformat(), "transcription": {
                 "engine": "faster-whisper", "model": result.get("model", "unknown"),
                 "language": result.get("language"), "average_log_probability": result.get("average_log_probability"),
                 "verified": False},
             "listener_evidence": "server_routed_in_range; client mute/volume and actual hearing unknown"}
    # A named listener is only a possible addressee, never proof of intent.
    event["possible_recipients"] = [
        {"player_uuid": p["player_uuid"], "player": p["player"], "basis": "name_mentioned_in_transcript"}
        for p in event["routed_listeners"]
        if re.search(r"(?<!\w)" + re.escape(p["player"]) + r"(?!\w)", text, re.I)
    ]
    return event


class VoiceService:
    def __init__(self, bridge, *, transcriber_factory=LocalTranscriber, queue_size=16):
        self.bridge = bridge
        self.factory = transcriber_factory
        self.queue = queue.Queue(maxsize=queue_size)
        self.lock = threading.Lock()
        self.seen = OrderedDict()
        self.stop = threading.Event()
        self.state = "loading"
        self.processed = self.dropped = self.failed = 0
        self.thread = threading.Thread(target=self._run, name="god-local-transcription", daemon=True)
        self.thread.start()

    def status(self) -> dict:
        return {"state": self.state, "queued": self.queue.qsize(), "processed": self.processed,
                "dropped": self.dropped, "failed": self.failed, "raw_audio_saved": False}

    def submit(self, event: dict, audio: bytes) -> bool:
        with self.lock:
            if event["event_uuid"] in self.seen:
                return True
            if self.state != "ready" or self.stop.is_set():
                return False
            try:
                self.queue.put_nowait((time.monotonic(), event, audio))
            except queue.Full:
                self.dropped += 1
                return False
            self.seen[event["event_uuid"]] = None
            while len(self.seen) > 512:
                self.seen.popitem(last=False)
            return True

    def _run(self):
        try:
            transcribe = self.factory()
        except Exception as error:
            self.state = "unavailable"
            # No audio, transcript, credentials or exception payload in logs.
            print(f"[voice] Local recognizer unavailable ({type(error).__name__}); run setup-multiplayer.ps1.", flush=True)
            return
        self.state = "ready"
        while not self.stop.is_set():
            try:
                received, metadata, audio = self.queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                if time.monotonic() - received > 60:
                    self.dropped += 1
                    continue
                event = transcript_event(metadata, transcribe(audio))
                if event and not self.stop.is_set():
                    self.bridge.record_event(event)
                self.processed += 1
            except Exception as error:
                self.failed += 1
                print(f"[voice] Utterance failed ({type(error).__name__}); proximity chat is unaffected.", flush=True)
            finally:
                audio = b""
                self.queue.task_done()

    def close(self):
        self.stop.set()
        self.state = "stopped"
        while True:
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except queue.Empty:
                break


if __name__ == "__main__":
    LocalTranscriber()
    print("Local voice recognition model is ready.")
