# Minecraft God

This is the first playable bridge for **“I let an AI be GOD in my Minecraft world.”** It gives Codex an MCP tool surface backed by a local Minecraft server's RCON port, plus a small HTTP endpoint where a Paper plugin can send chat and gameplay events.

## What exists now

- Dependency-free MCP JSON-RPC server over stdio.
- RCON transport for Java Edition servers.
- Persistent per-player god narrative state in `state/players/<uuid>/god_state.json`, with a shared SQLite pantheon ledger in `state/god_ledger.sqlite3`.
- Tools for player-centred and remote observation, exact block inspection, private visual scene capture, chat/events, arbitrary commands, speech, entities, safe entity-on-block placement, verified signs, items, weather, time, teleportation, effects, dimension-aware blocks and fills, structure search/import, and verified placement of Java structure templates.
- `POST /events` intake for the Paper event plugin.
- Dry-run mode and unit tests, so the agent loop can be exercised without Minecraft running.
- A Paper plugin under `paper-plugin/` that emits UUID-tagged chat, lifecycle, death (including killer), advancement, nearby explosion, block, item, health, mob-kill, and changed snapshot signals.
- A multi-god controller that creates one persistent Codex god/thread per joining player and routes each event only to the relevant god(s).
- A deterministic story director that suppresses self-action echoes, accumulates low-salience events, coalesces bursts, and wakes the model only for direct, urgent, or active-story decisions.
- Persistent landmarks, story phases, explicit objectives, and deterministic watches for events, regions, inventory goals, block actions, health thresholds, mob kills, deadlines, and composite conditions.
- Private player whispers, god-to-god diplomacy, and expiring one-use intervention permissions.
- Private player-to-god input through `/pray <message>` or `/whisper god <message>`; these do not appear as public chat messages.

The bridge deliberately does not invoke a model every game tick. The Paper plugin emits a changed snapshot at most every 15 seconds as cheap deterministic telemetry. Routine activity accumulates, but a sufficiently active online player now prompts an ambient "presence pulse" after two quiet minutes so the god can notice progress without being summoned; the model may still choose silence. Dangerously low health, direct address, urgent events, objectives, and registered watches wake it immediately. The intended rhythm is attentive and proactive without turning every block placement into speech. Location-sensitive actions re-observe the live world immediately before acting.

## One-time setup and launch on Windows

This is the intended path for the video:

1. Double-click `setup-minecraft-god.cmd`.
2. Read and accept Minecraft's EULA, then run `setup-minecraft-god.cmd -AcceptEula`.
3. Double-click `launch-minecraft-god.cmd`.
4. In Minecraft Java Edition 26.2, connect to `localhost:25565`.

The launcher starts the event hub, then the Codex controller, then Paper. The controller deliberately ignores old events and waits for new `player_join` events. Every joining player gets a persistent personal god; reconnecting with the same UUID resumes that god's identity and state. `MINECRAFT_MAX_GODS` limits resource usage (32 by default).

The setup downloads the latest stable Paper build for 26.2, installs and SHA-512 verifies the private `SpectatorCamera` Paper plugin, builds and installs the bridge plugin into the active server's `plugins` directory, downloads a private Java 25 runtime if necessary, generates a private RCON password, keeps the bridge project-scoped, and removes any legacy user-wide MCP registration. It may download a private Gradle copy into `.tools` if Gradle is not already installed.

The launcher uses the npm-installed Codex CLI when available. This keeps the controller compatible with newer models such as `gpt-5.6-luna` even if the desktop-bundled CLI has not updated yet.

Each dedicated God thread starts with `approvalPolicy = never` and full local access, and the controller automatically accepts approval requests from the `minecraft_god` MCP server. This is intentionally autonomous for this project; do not reuse these God threads for unrelated computer tasks.

To reload the controller and its Codex god sessions without stopping Paper or disconnecting players, run `.\restart-god-controller.ps1` from this directory. It resumes profiles that the ledger still marks online and leaves the Minecraft server and event hub processes untouched.

To shut down everything, double-click `stop-minecraft-god.cmd`. It asks Paper to save and stop cleanly, then forcibly removes any surviving project launcher, server, event hub, God controller, Codex God session, MCP bridge, or log watcher. It does not target unrelated Codex, Python, Java, or Minecraft processes. Running it again is safe and confirms that nothing remains.

## Track the live God session

The readable live transcript is [`logs/god-session.log`](logs/god-session.log). Double-click `watch-god-session.cmd`, or run:

```powershell
Get-Content .\logs\god-session.log -Wait
```

The detailed machine-readable transcript is [`logs/god-session.jsonl`](logs/god-session.jsonl). It records visible God messages, tool calls and results, Minecraft trigger events, and turn completion status. The Codex task is also named **Minecraft God - Live Session** in the Codex app. Private hidden chain-of-thought is not exposed; the transcript contains the observable reasoning/action surface instead.

Transcripts are bounded to 64 KiB per record and rotate at 25 MiB. Rotated files remain in `logs` with timestamped names. Token-counter/status spam is not persisted. To produce a compact streaming report without loading a transcript into memory, run:

```powershell
.\summarize-god-session.ps1
.\summarize-god-session.ps1 --all
```

`watch-god-session.cmd` automatically follows the active readable log across rotations.

## Run the bridge manually

From PowerShell, either use environment variables:

```powershell
$env:MINECRAFT_RCON_PASSWORD = "your-rcon-password"
$env:MINECRAFT_PLAYER = "IAmBoreder"
py -3 -m god_bridge.mcp_server
```

For a manually scoped god, also set `MINECRAFT_GOD_PLAYER_ID` to the player's UUID and `MINECRAFT_GOD_PLAYER` to their current name. The controller injects those values automatically for live sessions.

or copy `config.example.json`, edit it, and pass it explicitly:

```powershell
py -3 -m god_bridge.mcp_server --config .\config.json
```

For protocol testing without a Minecraft server:

```powershell
py -3 -m god_bridge.mcp_server --dry-run
```

Minecraft `server.properties` needs a local RCON listener:

```properties
enable-rcon=true
rcon.port=25575
rcon.password=your-rcon-password
```

Keep RCON bound to localhost. Do not expose it to the internet.

## Add it to Codex

The bridge is project-scoped in `.codex/config.toml`; do not register it in the user-wide Codex configuration. The setup script removes any legacy global registration. The controller also supplies per-player overrides when it launches each dedicated Codex AppServer.

```toml
[mcp_servers.minecraft_god]
command = "C:\\Windows\\py.exe"
args = ["-3", "E:\\MinecraftAiGod\\run_bridge.py", "--no-events"]

[mcp_servers.minecraft_god.env]
MINECRAFT_RCON_PASSWORD_FILE = "E:\\MinecraftAiGod\\server\\rcon-password.txt"
MINECRAFT_EVENTS_FILE = "E:\\MinecraftAiGod\\state\\events.jsonl"
MINECRAFT_LEDGER_FILE = "E:\\MinecraftAiGod\\state\\god_ledger.sqlite3"
MINECRAFT_PROFILE_DIR = "E:\\MinecraftAiGod\\state\\players"
```

Keep the password in the local password file; do not commit it.

Use [`prompts/god_system.md`](prompts/god_system.md) as the developer/system instruction for the Codex god session. It is intentionally story-oriented: the bridge provides power, while the prompt supplies restraint, continuity, and character.

## Sending events

The Paper plugin POSTs small JSON objects such as:

```json
{
  "type": "chat",
  "player": "IAmBoreder",
  "player_uuid": "player-uuid",
  "message": "PLEASE DON'T FUCK WITH ME RIGHT NOW"
}
```

Player deaths include `victim_uuid` and, when Minecraft can identify a direct player killer, `killer_uuid`. Both gods receive that death event, allowing the victim's god to open a diplomatic intervention request with the killer's god.

Players can address only their own god privately in-game:

```text
/pray I have found the shrine.
/whisper god What do you want from me?
```

to `http://127.0.0.1:8765/events`. The bridge also accepts arbitrary event types such as `player_death`, `diamond_found`, `creeper_explosion`, `sleep`, and `dimension_change`. Set `MINECRAFT_EVENT_TOKEN` and send `Authorization: Bearer <token>` if the event endpoint is ever moved off localhost.

## Build and install the Paper event plugin manually

From the repository root, with Gradle installed:

```powershell
Set-Location paper-plugin
gradle build
Copy-Item build\libs\minecraft-god-paper-plugin-0.1.0.jar "$env:APPDATA\.minecraft\server\plugins\"
```

Edit `plugins/MinecraftGodBridge/config.yml` after the first server start if the player name or bridge URL differs. The plugin is intentionally read-only with respect to game powers: it only sends events to the local bridge. RCON remains the god's action channel.

## Story trigger policy

- Direct prayers, God-addressed public chat, deaths, story milestones, intervention requests/responses, and first joins wake a god immediately.
- Ordinary public conversation and routine advancements accumulate silently. Dimension changes, major advancements, explosions, and pantheon reports are promoted only when they advance an active story or cross the salience threshold.
- `god_action` and quits remain audit-only. Routine snapshots, block activity, item pickups, health telemetry, and mob kills accumulate for story context; active play can produce a presence pulse after two quiet minutes. Health at six hearts or below wakes immediately.
- God-to-god messages wake only their recipient. Intervention responses wake the requester. A god's own commands and outbound messages never echo into another turn.
- When an objective is registered, composite watches can recognize its actual world, region, inventory, or event conditions automatically. The god should not require the player to pray merely to announce completion.
- Empty idle turns are disabled. Accumulated meaningful activity may trigger a presence pulse after two minutes, active stories flush after three minutes, and registered deadlines can wake independently.
- Each model thread rotates after 12 meaningful turns; continuity comes from compact persistent God state. Retired threads are archived so their MCP subprocesses are released.

## Current limitations

- Each god can act autonomously after a story-relevant trigger. A trigger permits a decision; it does not require speech or intervention.
- Targeted powers are ownership-checked in the bridge. Cross-player effects require an approved, expiring, one-use intervention record in the SQLite ledger; raw selectors such as `@a`, `@p`, `@r`, and unscoped `@e` are rejected for player gods.
- RCON gives the god command authority. The Paper plugin exposes player-centred `/godscan` and `/godcheck`, plus remote `/godscanat` and `/godcheckat` commands. Remembered landmarks include a dimension and absolute coordinates, allowing the god to inspect, photograph, verify, and build at places such as a temple even when the player is elsewhere.
- `capture_scene` points an invisible, server-side `SpectatorCamera` virtual camera at the observed build and returns two private, textured PNG views. It does not create a second account, move the player, interrupt the client, or notify the player. The camera is switched to manual mode for the request; the structured `/godscan` data remains authoritative for exact block identity and coordinates.
- `search_structures` searches Modrinth for datapacks compatible with the configured Minecraft version. `import_structure` downloads a selected release, verifies its published hash, and extracts only `.nbt` structure templates into a generated datapack. It deliberately excludes arbitrary functions, loot, recipes, and world-generation content. `list_structures` exposes the resulting exact template IDs.
- `place_structure` accepts vanilla or imported template IDs, supports rotation, and checks representative imported blocks after placement. Large or entity-heavy structures should still be inspected with `capture_scene` afterward.
- Relative coordinates are anchored to the owning player by the convenience tools. Remote and cross-dimension actions require absolute coordinates and can preflight the destination without moving the player. Raw `minecraft_command` calls remain server-console commands unless `relative_to_player` is set, and the bridge rejects known cross-player targets.
- Action results classify common Minecraft failures (`Expected literal`, `No entity was found`, and similar) as `ok: false`; a successful RCON round trip is not treated as proof that the requested action happened.
- The first setup must be run once because Minecraft's EULA requires an explicit user decision. After that, the `.cmd` launcher is the one-click flow.

## Camera notes

The public SpectatorCamera release currently targets Paper 1.21.x while the project server is Paper 26.2. Setup installs the pinned, checksum-verified release and the first restart is the compatibility check; if Paper rejects it, the bridge reports that clearly and does not fall back to synthetic voxel images. The tool contract remains stable so a 26.2-native renderer can replace the plugin later.
