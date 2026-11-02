# Operations

## Start, stop, and restart

Use `start-minecraft-god.ps1` to start the event hub, controller, and Paper. Use `stop-minecraft-god.ps1` to request a world save and stop, then terminate this checkout's remaining runtime processes. It has `-WhatIf` support for previewing the action. Forced fallback after a shutdown failure can interrupt a save; retain backups and inspect the console when a clean stop was unavailable.

`restart-god-controller.ps1` restarts only this checkout's controller and its children, leaving Paper and the event hub running. State and pending work remain on disk. If you change the event token or plugin, stop the whole stack and rerun setup instead.

## What is stored

| Location | Contents | Publish? |
| --- | --- | --- |
| `state/players/` | Private god memories, objectives, pending work, turn budgets | No |
| `state/god_ledger.sqlite3` | Player identities, events, ownership, agreements, permissions | No |
| `state/events.jsonl` | Append-only gameplay and action audit | No |
| `state/captures/` | Private world images sent to model tools | No |
| `logs/` | Controller, server, and model activity | No |
| `world*` | Minecraft saves, player data, builds | No |
| `server/` | Downloaded server, credential files, runtime paths | No |
| `.codex-runtime/` | Game-specific Codex login, sessions, caches | No |
| `plugins/MinecraftGodBridge/` | Plugin config, pending delivery queue, rejected payload archive | No |

The gitignore excludes these paths. That does not remove previously committed files or redact an archive you attach to an issue. Use synthetic scenarios and small redacted excerpts when sharing a bug.

## Back up and restore

Stop the whole game first. The backup command refuses to run while the default runtime ports are accepting connections or the controller lock is held. Keep the game stopped until it finishes; do not start another launcher concurrently.

```powershell
.\stop-minecraft-god.ps1
py -3 -m god_bridge.maintenance backup backups/before-upgrade.zip
```

The backup contains `state`, `world`, `world_nether`, and `world_the_end`, with per-file SHA-256 checksums. SQLite databases are copied using SQLite's backup API so committed WAL data is included. It excludes credentials, model login files, transient locks, and plugin outboxes. Complete a normal shutdown/delivery before backup; if the plugin has an undelivered queue, save that queue separately and restore it only with the matching world state.

Restore into a **new directory**:

```powershell
py -3 -m god_bridge.maintenance restore backups/before-upgrade.zip E:/MinecraftGod-Restored
```

The command validates paths and checksums before creating the destination. It never overwrites an existing game. Add the matching release's source files to the restored directory, run setup to regenerate local credentials and plugins, sign in to the game runtime, and run doctor before starting. Backups contain private player/world data even though credentials are excluded.

## Storage and retention

Transcript files rotate at the configured size, but archives are retained. Captures, event history, SQLite records, and rejected deliveries can also grow. There is no silent deletion of story history or undelivered events.

Preview old captures and rotated transcripts:

```powershell
py -3 -m god_bridge.maintenance cleanup --older-than-days 30
```

After stopping the game and reviewing the list, repeat with `--apply` to remove only those files. The cleanup command excludes active logs, event history, profiles, worlds, databases, and credentials. Take backups or move an entire old game installation to private archival storage when you need to retire its durable history.

## Inspect a session

```powershell
py -3 -m god_bridge.log_report --all --json
```

Reports include turn triggers, tool failures, response timing, confirmed world changes, repeated-message candidates, objective achievements, and reported tokens. These metrics help identify weak gameplay; they do not prove a player enjoyed a session or that a model's intention was correct. Token metrics may be incomplete after an interrupted process and do not replace account billing records.

The per-player hourly turn limit survives restarts. Reaching it preserves queued work and logs `turn_budget_wait`. Adjust limits before a session based on your available account allowance, then observe actual use.
