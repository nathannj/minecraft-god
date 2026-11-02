# Architecture

Minecraft God separates world facts, narrative decisions, and permission checks. The model is a character and planner; it is not the authoritative database or event detector.

## A prayer becomes a decision

1. Paper captures a prayer or gameplay event. `EventPayload` adds schema version, source, a delivery UUID, and a timestamp. Spatial events include the dimension and position.
2. `EventOutbox` persists pending events and retries delivery. Snapshots may coalesce; prayers and objective-bearing events remain durable. Invalid payloads are archived for inspection so they cannot permanently block new delivery.
3. The authenticated HTTP endpoint validates the payload before accepting it into SQLite and the append-only audit log. Delivery UUIDs prevent a retry from creating a second event.
4. The controller routes events to the relevant UUID-owned session. Most activity stays in an accumulation buffer. A prayer, emergency, or deterministic story watch can trigger a model turn.
5. The model requests a supported MCP action. The bridge checks ownership, action shape, player/region constraints, and turn validity. RCON and the plugin perform observations and mutations.
6. Results, story state, objective receipts, permissions, and metrics survive independently of the model thread. Rotation does not erase a god's durable memory.

## Observable objectives

A story objective can register an `all` condition combining an inventory item and a remembered region. The current dimension is part of that region check. A matching event produces a `story_milestone` and marks the objective `achieved_pending_judgment`; the model still decides how the god responds.

[The offline fixture](../god_bridge/demo_scenario.json) demonstrates why another player's inventory, the same coordinates in the Nether, and a duplicate snapshot must not complete the objective again.

## Ownership and shared space

The ledger associates player UUIDs with names and aliases. Scoped bridges validate supported commands and track owned entities and protected regions. Interventions need the target god's permission and are single-use with expiry. Shared agreements have explicit participants and acceptance state; another god's message is not itself authority.

These rules limit supported bridge actions. They do not isolate Minecraft physics, a malicious server plugin, administrative RCON access, or every indirect consequence of changing a shared world. Read [security](../SECURITY.md) before operating beyond a trusted local group.

## Concurrency and durability

- SQLite uses WAL mode and indexed event history. Frequent polling reads bounded queries rather than rescanning the audit file.
- Profile state uses a cross-process lock and temporary-file replacement.
- The global SpectatorCamera is locked across positioning, rendering, waiting, and copying both views. Competing gods receive a retryable busy result.
- Pending decisions and objective receipts persist across controller restarts. Delivery is retryable; action idempotency keys suppress known repeats. A crash between a Minecraft mutation and its recorded result can still be ambiguous. Re-observation remains necessary.
- The controller uses an isolated Codex data directory, disables unrelated integrations, requests a read-only sandbox, and verifies the MCP server/tool inventory before starting model work.

## Source map

| File | Responsibility |
| --- | --- |
| [bridge.py](../god_bridge/bridge.py) | Game operations, observation, persistent character state, and action orchestration. |
| [authority.py](../god_bridge/authority.py) | Supported command parsing and ownership/region enforcement. |
| [controller.py](../god_bridge/controller.py) | Per-player sessions, event routing, scheduling, model lifecycle, and metrics. |
| [codex_runtime.py](../god_bridge/codex_runtime.py) | Version pin, private runtime config, login helper, and MCP inventory gate. |
| [event_http.py](../god_bridge/event_http.py) / [events.py](../god_bridge/events.py) | Authenticated transport and external event validation. |
| [ledger.py](../god_bridge/ledger.py) / [pantheon.py](../god_bridge/pantheon.py) | Durable identities, history, agreements, and permissions. |
| [story.py](../god_bridge/story.py) | Deterministic event classification, coalescing, and objective matching. |
| [structures.py](../god_bridge/structures.py) | Bounded structure downloads, NBT inspection, and template-only extraction. |
| [mcp_server.py](../god_bridge/mcp_server.py) | Stdio MCP schemas and dispatch. |
| [god_system.md](../god_bridge/god_system.md) | Packaged character and tool-use instructions. |
| [Paper sources](../paper-plugin/src/main/java/com/minecraftgod/bridge) | Live telemetry, exact observations, bounded world checks, and durable event delivery. |

`bridge.py` and `controller.py` still contain substantial orchestration. Prefer extracting cohesive responsibilities with regression coverage when changing them; avoid a broad rewrite that obscures gameplay behavior.
