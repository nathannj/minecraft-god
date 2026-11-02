# Configuration

Set environment variables in the terminal before starting the launcher. The launcher supplies defaults for resource limits and preserves your overrides. Mutable paths belong to the checkout (`MINECRAFT_GOD_HOME`), never the installed Python package.

```powershell
$env:MINECRAFT_GOD_MODEL = 'gpt-6-luna'
$env:MINECRAFT_MAX_GODS = '2'
$env:MINECRAFT_GOD_MAX_TURNS_PER_HOUR = '40'
$env:MINECRAFT_JAVA_MAX_MEMORY = '3G'
.\start-minecraft-god.ps1
```

Model availability depends on the account. The default is a configuration choice, not a promise of access or a quoted price. Monitor the account's usage separately.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MINECRAFT_GOD_MODEL` | `gpt-6-luna` | Model requested for each player god. |
| `MINECRAFT_GOD_REASONING_EFFORT` | `low` | Explicit effort on new threads and turns. |
| `MINECRAFT_MAX_GODS` | `2` | Maximum concurrent player sessions. Additional players do not receive a new session while the cap is full. |
| `MINECRAFT_GOD_MAX_TURNS_PER_HOUR` | `60` | Rolling decision-start limit per player, persisted across restarts. Pending work waits when exhausted. |
| `MINECRAFT_GOD_MAX_TURN_SECONDS` | `90` | Time before requesting interruption of a long decision; interruption is cooperative. |
| `MINECRAFT_GOD_IDLE_SECONDS` | `120` | Minimum spacing for accumulated ambient activity. Zero disables these ambient turns. |
| `MINECRAFT_GOD_STORY_INTERVAL_SECONDS` | `180` | Activity accumulation interval for an active story. |
| `MINECRAFT_GOD_MAX_THREAD_TURNS` | `12` | Rotate model context while preserving durable story state. |
| `MINECRAFT_JAVA_MIN_MEMORY` / `MAX_MEMORY` | `1G` / `4G` | Initial and maximum Paper heap in the Windows launcher. |
| `MINECRAFT_CAMERA_TIMEOUT` | `120` | Per-view render timeout, clamped to 10–300 seconds. Shared camera requests wait up to 30 seconds for a lock. |
| `MINECRAFT_GOD_LOG_MAX_BYTES` | `26214400` | Per-transcript rotation threshold. Archives require explicit cleanup. |
| `MINECRAFT_CODEX_CLI_PATH` | CLI on PATH | An explicit path to the tested Codex CLI installation. |

These are usage controls, not a monetary spending limit. A turn can contain several model requests. Token totals reported by Codex are recorded in turn metrics; they are not a billing reconciliation.

## Credentials and paths

Setup generates `server/rcon-password.txt` and `server/event-token.txt`. Launchers pass their paths through `MINECRAFT_RCON_PASSWORD_FILE` and `MINECRAFT_EVENT_TOKEN_FILE`. The token is also written into the ignored Paper plugin configuration. Keep these files local.

Each controller-created MCP connection has an explicit player UUID, player name, profile directory, ledger path, and `MINECRAFT_REQUIRE_GOD_SCOPE=true`. Do not hand-configure an unscoped bridge as a player god: unscoped mode is administrative access.

[config.example.json](../config.example.json) documents the standalone bridge fields. `minecraft-god --config config.json` reads it, with environment variables taking precedence. It is not the Windows launcher's configuration file. Prefer environment variables for the standard full-stack workflow.

The event hub accepts only loopback bindings and requires a token outside dry-run mode. Live requests must contain version-1 Paper event payloads, canonicalizable UUIDs, valid player names, and finite coordinates. Bodies are limited to 64 KiB.

## Pin updates

[versions.lock.json](../versions.lock.json) contains exact download URLs and hashes. The Paper API coordinate in the Gradle build must match it. The Codex version is checked before a game session starts because its app-server interface can change. Update the pins, tests, and release evidence together; do not replace a checksum merely to make a failed download pass.
