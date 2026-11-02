# Multiplayer maps and voice

The server stays on Paper 26.2. Players use Fabric 26.2 with Xaero's Minimap, Xaero's World Map, Fabric API and Simple Voice Chat. Downloads are version-pinned and checksum-verified. The installer creates a separate **Minecraft AI God** launcher profile in `client/`, preserving existing Minecraft installations.

## Play

1. Run `start-minecraft-god.ps1`. If the dedicated game runtime needs a login, first run `py -3 -m god_bridge.codex_runtime login`.
2. Reopen Minecraft Launcher, select **Minecraft AI God** in Installations, and launch it. Connect to `localhost:25565` on the server computer, or the server's address remotely.
3. Friends can import `dist/Minecraft-AI-God-26.2.mrpack` into a launcher that supports Modrinth packs. It downloads original mods from upstream and contains no accounts, credentials, world saves or private server address. Alternatively, install the four client mods in `versions.lock.json` into Fabric Loader 0.19.5 for Minecraft 26.2.

| Shortcut | Action |
| --- | --- |
| **M** | Full world map |
| **B** | New local waypoint |
| **U** | Waypoint list |
| **Y** | Minimap settings |
| **V** | Voice setup, microphone and push-to-talk settings |
| `/map` | Shortcut help |
| `/waypoint Temple` | Share this location as an importable Xaero waypoint with players in this world; also inform AI God of its name and coordinates |
| `/xaerotracker toggleTracked` | Toggle your player marker sharing |
| `/godvoice on`, `/godvoice off`, `/godvoice status` | Control transcription of your own speech |

These are default keys; Minecraft Controls can rebind them. Configure your microphone in **V** before playing. Normal speech reaches 48 blocks and whispers 16, with distance attenuation. Change distances in `plugins/voicechat/voicechat-server.properties`.

XaeroTracker sends player positions every 250 ms, including beyond entity render distance. This installation shares markers within the same world. Invisible, spectator, vanished and opted-out players are normally excluded; upstream administrative overrides can bypass some exclusions. Each client maps terrain it explores; terrain syncing is not included. Imported waypoints persist in each client's Xaero data. Sharing a waypoint never teleports anyone.

## What AI God hears

The bridge decodes opted-in microphone packets into utterances and sends them to local `faster-whisper` through the authenticated loopback endpoint. The default English `base.en` model uses CPU int8 inference with two threads. No speech API key is needed. Recognition and uploads run outside the Minecraft tick thread and voice network thread.

Each transcript includes the authenticated speaker UUID/name, capture timestamps, dimension, location, voice mode and duration. It also includes nearby players, in-range connected voice recipients observed through outgoing packet events, and possible addressees when a recipient's name appears in the recognized text. Recognition results are marked unverified.

The speaker's god and the gods of routed listeners receive the event. Proximity alone does not make someone a recipient. Client muting, volume and attention are unknown; the AI must not assume someone heard or understood. Context is sampled every 250 ms, spans the utterance, and is capped at 32 nearby players and 32 listeners. Direct appeals to God can wake the speaker's god; ordinary/overheard conversation accumulates quietly under existing decision limits. Speech never triggers automatic conduct punishment. Gods continue responding through existing text and world actions.

Private groups, including open groups, are excluded. Players receive a join notice and can opt their own speech out with `/godvoice off`; voice chat still works. Preferences persist with player data. Opting out does not erase previously accepted transcripts. Raw production audio stays in bounded memory queues and is discarded after processing, failure or shutdown. Transcripts/context persist in the existing ledger and may be sent to the AI model. Full queues or recognition failures drop transcription without interrupting voice. Audio is not durably retried after a process restart.

## Installation and configuration

Full setup includes these features. To update only multiplayer while stopped, run `setup-multiplayer.ps1`. Use `-SkipClient` on a server computer without Minecraft Launcher. `-SkipVoiceModel` skips recognition setup; set `MINECRAFT_VOICE_ENABLED=0` until it is ready. `-SkipBuild` assumes the current bridge is already built and installed.

| Setting | Default | Purpose |
| --- | --- | --- |
| `MINECRAFT_VOICE_ENABLED` | `1` in launcher | Start local recognition |
| `MINECRAFT_VOICE_MODEL` | `base.en` | Faster Whisper model name or local directory |
| `MINECRAFT_VOICE_LANGUAGE` | `en` | Recognition language; other languages need a multilingual model |
| Plugin `voice-transcription.enabled` | `true` | Enable capture integration |
| Plugin `voice-transcription.default-enabled` | `true` | Preference for players without a saved choice |
| Plugin `voice-transcription.nearby-radius` | `64` | Context radius, capped at 128 blocks |

The speech environment is `.tools/voice-venv`. After changing models, prepare it with `.tools/voice-venv/Scripts/python.exe -m god_bridge.voice`. PyAV is pinned to 16.0.1 because version 19 removed an argument Faster Whisper 1.2.1 uses. Recognition status/counters are available at the authenticated `/voice/healthz` endpoint and failures appear in event-hub logs. Ordinary `/healthz` only confirms the HTTP service.

Remote friends need **TCP 25565** for Minecraft and **UDP 24454** for voice through the host firewall and router/tunnel. A TCP-only Minecraft tunnel cannot carry voice. Configure `voice_host` in the voice plugin if its advertised address differs from the game address. Never forward RCON or the event-hub port. Verify remote reachability from a friend's connection.

## Verification

```powershell
py -3 -m pytest -q
.\paper-plugin\gradlew.bat -p paper-plugin build voiceProbeJar --no-daemon
py -3 -m god_bridge.verify_server --multiplayer
.\.tools\voice-venv\Scripts\python.exe -m god_bridge.verify_voice --wav path\to\speech.wav --expect "words in the fixture"
```

The fixture must be 48 kHz mono PCM16, 0.2–10 seconds. The test-only voice probe uses synthetic Alice/Bob identities in a disposable world. It exercises native Opus, production capture/recipient callbacks, authenticated upload, actual local recognition, ledger persistence and god routing. It uses no microphones or model turns. Never deploy `VoiceProbe.jar` to the real server.

Tests also cover malformed audio, authentication, dimension boundaries, group rejection, deduplication, overload, listener filtering and delayed transcripts after disconnect. Two-person playtesting should check map rendering, waypoint import, voice distance/whispering, local mute, opt-out and remote UDP connectivity.

Sources: [XaeroTracker](https://github.com/inf-mc/XaeroTracker), [Xaero's Minimap](https://modrinth.com/mod/xaeros-minimap), [Xaero's World Map](https://modrinth.com/mod/xaeros-world-map), [Simple Voice Chat](https://modrepo.de/minecraft/voicechat/overview), [Voice Chat API](https://voicechat.modrepo.de/).
