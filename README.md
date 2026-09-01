# Minecraft God

This is the first playable bridge for **“I let an AI be GOD in my Minecraft world.”** It gives Codex an MCP tool surface backed by a local Minecraft server's RCON port, plus a small HTTP endpoint where a Paper plugin can send chat and gameplay events.

## What exists now

- Dependency-free MCP JSON-RPC server over stdio.
- RCON transport for Java Edition servers.
- Persistent god narrative state in `state/god_state.json`.
- Tools for observation, exact local scene inspection, private visual scene capture, chat/events, arbitrary commands, speech, entities, safe entity-on-block placement, verified signs, items, weather, time, teleportation, effects, blocks, fills, structure search/import, and verified placement of Java structure templates.
- `POST /events` intake for the Paper event plugin.
- Dry-run mode and unit tests, so the agent loop can be exercised without Minecraft running.
- A Paper plugin under `paper-plugin/` that emits chat, lifecycle, death, advancement, and nearby explosion events.

The bridge deliberately does not poll every game tick or emit a periodic snapshot heartbeat. The intended rhythm is an observation at the start of a scene, immediate event delivery for chat/interesting events, and long stretches where God may choose to do nothing. Location-sensitive actions re-observe the live world immediately before acting.

## One-time setup and launch on Windows

This is the intended path for the video:

1. Double-click `setup-minecraft-god.cmd`.
2. Read and accept Minecraft's EULA, then run `setup-minecraft-god.cmd -AcceptEula`.
3. Double-click `launch-minecraft-god.cmd`.
4. In Minecraft Java Edition 26.2, connect to `localhost:25565`.

The launcher starts the event hub, then the Codex controller, then Paper. The controller deliberately ignores old events and waits for the first `player_join` event from any player. That player becomes the active player for the session and creates the first Codex turn. The fallback configured player is `IAmBoreder`; the God prompt tells the AI to call you Boreder or choose another in-world name.

The setup downloads the latest stable Paper build for 26.2, installs and SHA-512 verifies the private `SpectatorCamera` Paper plugin, builds and installs the bridge plugin into the active server's `plugins` directory, downloads a private Java 25 runtime if necessary, generates a private RCON password, and registers the `minecraft_god` MCP server with the local Codex CLI. It may download a private Gradle copy into `.tools` if Gradle is not already installed.

The launcher uses the npm-installed Codex CLI when available. This keeps the controller compatible with newer models such as `gpt-5.6-luna` even if the desktop-bundled CLI has not updated yet.

The dedicated God thread starts with `approvalPolicy = never` and full local access, and the controller automatically accepts approval requests from the `minecraft_god` MCP server. This is intentionally autonomous for this project; do not reuse the God thread for unrelated computer tasks.

## Track the live God session

The readable live transcript is [`logs/god-session.log`](logs/god-session.log). Double-click `watch-god-session.cmd`, or run:

```powershell
Get-Content .\logs\god-session.log -Wait
```

The detailed machine-readable transcript is [`logs/god-session.jsonl`](logs/god-session.jsonl). It records visible God messages, tool calls and results, Minecraft trigger events, and turn completion status. The Codex task is also named **Minecraft God - Live Session** in the Codex app. Private hidden chain-of-thought is not exposed; the transcript contains the observable reasoning/action surface instead.

## Run the bridge manually

From PowerShell, either use environment variables:

```powershell
$env:MINECRAFT_RCON_PASSWORD = "your-rcon-password"
$env:MINECRAFT_PLAYER = "IAmBoreder"
py -3 -m god_bridge.mcp_server
```

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

Register the bridge as a local stdio MCP server in the Codex MCP configuration. The exact file is installation-specific; the command entry is:

```toml
[mcp_servers.minecraft_god]
command = "py"
args = ["-3", "-m", "god_bridge.mcp_server"]
cwd = "E:\\MinecraftAiGod"

[mcp_servers.minecraft_god.env]
MINECRAFT_RCON_PASSWORD = "your-rcon-password"
MINECRAFT_PLAYER = "IAmBoreder"
```

If `py -3` is not accepted by the host, use the absolute Python executable path instead. Keep the password in a local environment/config mechanism rather than committing it.

Use [`prompts/god_system.md`](prompts/god_system.md) as the developer/system instruction for the Codex god session. It is intentionally story-oriented: the bridge provides power, while the prompt supplies restraint, continuity, and character.

## Sending events

The Paper plugin POSTs small JSON objects such as:

```json
{
  "type": "chat",
  "player": "IAmBoreder",
  "message": "PLEASE DON'T FUCK WITH ME RIGHT NOW"
}
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

## Current limitations

- The AI can act autonomously after join, chat, deaths, advancements, dimension changes, and nearby explosions, with a quiet control tick between events. The god remains silent while reasoning unless there is a deliberate story reason to speak.
- RCON gives the god command authority. The Paper plugin also exposes `/godscan`, which returns exact nearby non-air blocks, block states, sign text, entity positions, names, tags, and placement flags so the god can reason about a scene instead of guessing from entity counts.
- `capture_scene` points an invisible, server-side `SpectatorCamera` virtual camera at the observed build and returns two private, textured PNG views. It does not create a second account, move Boreder, interrupt the client, or notify the player. The camera is switched to manual mode for the request; the structured `/godscan` data remains authoritative for exact block identity and coordinates.
- `search_structures` searches Modrinth for datapacks compatible with the configured Minecraft version. `import_structure` downloads a selected release, verifies its published hash, and extracts only `.nbt` structure templates into a generated datapack. It deliberately excludes arbitrary functions, loot, recipes, and world-generation content. `list_structures` exposes the resulting exact template IDs.
- `place_structure` accepts vanilla or imported template IDs, supports rotation, and checks representative imported blocks after placement. Large or entity-heavy structures should still be inspected with `capture_scene` afterward.
- Relative coordinates are anchored to the active player by the convenience tools. Raw `minecraft_command` calls remain server-console commands unless `relative_to_player` is set.
- Action results classify common Minecraft failures (`Expected literal`, `No entity was found`, and similar) as `ok: false`; a successful RCON round trip is not treated as proof that the requested action happened.
- The first setup must be run once because Minecraft's EULA requires an explicit user decision. After that, the `.cmd` launcher is the one-click flow.

## Camera notes

The public SpectatorCamera release currently targets Paper 1.21.x while the project server is Paper 26.2. Setup installs the pinned, checksum-verified release and the first restart is the compatibility check; if Paper rejects it, the bridge reports that clearly and does not fall back to synthetic voxel images. The tool contract remains stable so a 26.2-native renderer can replace the plugin later.
