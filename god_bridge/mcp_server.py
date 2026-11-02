"""Dependency-free MCP server for the Minecraft God bridge.

The server uses MCP's JSON-RPC-over-stdio transport so it can be launched by a
local Codex configuration without a web service or API key in this process.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .bridge import BridgeConfig, GodBridge, parse_identifier, parse_position
from .event_http import EventHandler as EventHandler, start_event_server
from .runtime import event_token


SIGN_MAX_LINES = 4
SIGN_MAX_CHARS = 15
SIGN_MAX_PIXELS = 90

# Approximate the widths of the vanilla bitmap font used by Java sign text.
# The bridge deliberately errs on the short side: a sign should remain
# readable in-game rather than merely fit in the NBT string.
_SIGN_GLYPH_WIDTHS = {
    " ": 4,
    "i": 2,
    "l": 2,
    "I": 2,
    "!": 2,
    ".": 2,
    ",": 2,
    ":": 2,
    ";": 2,
    "'": 2,
    "|": 2,
    "t": 4,
    "f": 4,
    "j": 4,
    "r": 4,
    "m": 7,
    "w": 7,
    "M": 7,
    "W": 7,
}


def sign_line_width(text: str) -> int:
    """Return a conservative vanilla-font pixel width for one sign line."""
    if not text:
        return 0
    return sum(_SIGN_GLYPH_WIDTHS.get(character, 5) + 1 for character in text) - 1


def tool_definitions() -> list[dict[str, Any]]:
    definitions = [
        {"name": "find_safe_surface", "description": "Find safe ground above the owned player's current column in one server operation; use for a bounded surface rescue.", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "inspect_footprint", "description": "Summarize a build footprint, materials, liquids, containers and players in one operation. Maximum 4096 blocks and 48 per axis.", "inputSchema": {"type": "object", "properties": {"from": {"type": "string"}, "to": {"type": "string"}, "dimension": {"type": "string"}}, "required": ["from", "to", "dimension"]}},
        {"name": "propose_agreement", "description": "Propose durable treaty terms, shared landmark, joint expedition or feud to participating gods. All must accept. Supersedes names an active agreement to revise; the old terms remain active until acceptance.", "inputSchema": {"type": "object", "properties": {"subject": {"type": "string"}, "participants": {"type": "array", "items": {"type": "string"}}, "terms": {"type": "object"}, "kind": {"type": "string", "enum": ["treaty", "shared_landmark", "joint_expedition", "feud"], "default": "treaty"}, "supersedes": {"type": "string"}}, "required": ["subject", "participants", "terms"]}},
        {"name": "respond_agreement", "description": "Accept or decline a structured agreement as one of its participants.", "inputSchema": {"type": "object", "properties": {"agreement_id": {"type": "string"}, "decision": {"type": "string", "enum": ["accept", "decline"]}}, "required": ["agreement_id", "decision"]}},
        {"name": "list_agreements", "description": "Read the shared authoritative agreements and proposed revisions involving your god.", "inputSchema": {"type": "object", "properties": {"status": {"type": "string", "default": ""}}}},
        {"name": "record_player_choice", "description": "Record an evidenced interpretation: progress, preparation, incidental activity, refusal, abandonment, or a creative alternative. Incidental gathering is not refusal.", "inputSchema": {"type": "object", "properties": {"choice": {"type": "string"}, "evidence": {"type": "string"}, "objective_id": {"type": "string"}, "approach": {"type": "string", "enum": ["progress", "preparation", "incidental", "refusal", "abandonment", "alternative"]}}, "required": ["choice", "evidence", "approach"]}},
        {"name": "revise_story_objective", "description": "Accept a negotiated alternative or change a test, preserving why it changed and registering the new physical completion condition.", "inputSchema": {"type": "object", "properties": {"objective_id": {"type": "string"}, "reason": {"type": "string"}, "objective": {"type": "object"}}, "required": ["objective_id", "reason", "objective"]}},
        {"name": "observe_world", "description": "Observe compact live player telemetry, persistent landmarks, objectives, and recent meaningful story context. Use this at the beginning of every decision.", "inputSchema": {"type": "object", "properties": {"include_scene": {"type": "boolean", "default": False}}}},
        {"name": "observe_location", "description": "Observe a remembered landmark remotely without moving or notifying the player. Use named spatial memory instead of assuming the player's current location is the requested place.", "inputSchema": {"type": "object", "properties": {"landmark_id": {"type": "string"}, "radius": {"type": "integer", "minimum": 1, "maximum": 24, "default": 12}, "vertical_radius": {"type": "integer", "minimum": 1, "maximum": 16, "default": 8}, "include_scene": {"type": "boolean", "default": True}}, "required": ["landmark_id"]}},
        {"name": "inspect_area", "description": "Inspect the exact local area around the player. Prefer inspect_area_at or observe_location for a named destination elsewhere.", "inputSchema": {"type": "object", "properties": {"radius": {"type": "integer", "minimum": 1, "maximum": 24, "default": 8}, "vertical_radius": {"type": "integer", "minimum": 1, "maximum": 16, "default": 5}}}},
        {"name": "inspect_area_at", "description": "Inspect exact blocks, signs, and public entities around any absolute coordinate and dimension without moving the player. Chunks are loaded only for the scan.", "inputSchema": {"type": "object", "properties": {"position": {"type": "string"}, "dimension": {"type": "string", "default": "minecraft:overworld"}, "radius": {"type": "integer", "minimum": 1, "maximum": 24, "default": 8}, "vertical_radius": {"type": "integer", "minimum": 1, "maximum": 16, "default": 5}}, "required": ["position"]}},
        {"name": "inspect_block", "description": "Inspect one exact block. Supply dimension for a remote or cross-dimensional check.", "inputSchema": {"type": "object", "properties": {"position": {"type": "string"}, "dimension": {"type": "string", "default": ""}}, "required": ["position"]}},
        {"name": "inspect_blocks", "description": "Inspect up to 128 exact block positions in one model-visible batch. Prefer this over many inspect_block calls.", "inputSchema": {"type": "object", "properties": {"positions": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 128}, "dimension": {"type": "string", "default": ""}}, "required": ["positions"]}},
        {"name": "capture_scene", "description": "Capture two private textured views around the player. The bridge chooses low-occlusion angles; exact scan data remains authoritative.", "inputSchema": {"type": "object", "properties": {"radius": {"type": "integer", "minimum": 1, "maximum": 24, "default": 12}, "vertical_radius": {"type": "integer", "minimum": 1, "maximum": 16, "default": 8}}}},
        {"name": "capture_scene_at", "description": "Capture private textured views around an absolute coordinate and dimension without moving the player. Use this to verify remote landmarks and builds.", "inputSchema": {"type": "object", "properties": {"position": {"type": "string"}, "dimension": {"type": "string", "default": "minecraft:overworld"}, "radius": {"type": "integer", "minimum": 1, "maximum": 24, "default": 12}, "vertical_radius": {"type": "integer", "minimum": 1, "maximum": 16, "default": 8}}, "required": ["position"]}},
        {"name": "list_structures", "description": "List the vanilla and imported Java structure templates currently available to the God. Use this before placing a structure when you need an exact template ID.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "default": ""}}}},
        {"name": "search_structures", "description": "Search Modrinth for Minecraft Java datapacks compatible with the configured server version. This discovers candidate structure packs but does not install anything.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8}}, "required": ["query"]}},
        {"name": "import_structure", "description": "Download a selected Modrinth datapack or structure NBT, extract only its Java structure templates into a private datapack, enable it, and reload the server. Functions, loot tables, recipes, and world-generation files are deliberately excluded.", "inputSchema": {"type": "object", "properties": {"project_id": {"type": "string"}, "version_id": {"type": "string", "default": ""}, "namespace": {"type": "string", "default": ""}}, "required": ["project_id"]}},
        {"name": "read_chat", "description": "Read recent Minecraft chat messages received by the bridge.", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}}}},
        {"name": "read_recent_events", "description": "Read recent Minecraft events received by the bridge or generated by god actions.", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}}}},
        {"name": "get_god_state", "description": "Read persistent hidden narrative state for the god character.", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "update_god_state", "description": "Persist narrative state. Memory updates append uniquely instead of replacing history; use landmark and objective tools for spatial facts and tests.", "inputSchema": {"type": "object", "properties": {"updates": {"type": "object"}}, "required": ["updates"]}},
        {"name": "remember_landmark", "description": "Store or update a named spatial landmark with dimension, absolute anchor, optional bounds, aliases, and verification confidence.", "inputSchema": {"type": "object", "properties": {"landmark": {"type": "object"}}, "required": ["landmark"]}},
        {"name": "list_landmarks", "description": "List or search the god's structured landmark memory before interpreting names such as home, temple, shrine, library, or portal.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "default": ""}}}},
        {"name": "forget_landmark", "description": "Remove an obsolete landmark by id.", "inputSchema": {"type": "object", "properties": {"landmark_id": {"type": "string"}}, "required": ["landmark_id"]}},
        {"name": "set_story_objective", "description": "Atomically create a narrative objective, unresolved promise, and deterministic watch. Use this instead of telling the player to pray after a detectable task.", "inputSchema": {"type": "object", "properties": {"objective": {"type": "object"}}, "required": ["objective"]}},
        {"name": "complete_story_objective", "description": "Resolve an objective and remove its promise and watch after verified fulfillment, failure, or abandonment.", "inputSchema": {"type": "object", "properties": {"objective_id": {"type": "string"}, "outcome": {"type": "string", "enum": ["fulfilled", "failed", "abandoned"], "default": "fulfilled"}}, "required": ["objective_id"]}},
        {"name": "register_story_watch", "description": "Register a deterministic wake condition. Kinds include event, region, item, block actions, health, mob kill, structure_change, and composite all/any conditions. Prefer set_story_objective for player-facing tests.", "inputSchema": {"type": "object", "properties": {"watch": {"type": "object"}}, "required": ["watch"]}},
        {"name": "clear_story_watch", "description": "Remove a resolved or abandoned story watch by id.", "inputSchema": {"type": "object", "properties": {"watch_id": {"type": "string"}}, "required": ["watch_id"]}},
        {"name": "preflight_command", "description": "Validate a supported raw command without executing its mutation. Checks scope, command shape and sign limits; world actions also query the live dimension and bystanders.", "inputSchema": {"type": "object", "properties": {"command": {"type": "string", "minLength": 1}}, "required": ["command"]}},
        {"name": "minecraft_command", "description": "Execute a supported command through RCON after ownership and protected-region checks. Unknown commands and unsafe wrappers are rejected for player gods. Do not include the leading slash. Set relative_to_player=true for ~ coordinates near the player, and reuse idempotency_key for retries. Use typed place_structure for templates.", "inputSchema": {"type": "object", "properties": {"command": {"type": "string", "minLength": 1}, "relative_to_player": {"type": "boolean", "default": False}, "idempotency_key": {"type": "string", "maxLength": 128}}, "required": ["command"]}},
        {"name": "god_say", "description": "Speak privately to your own player. This is the default and normal way for a god to communicate; the message is not shown to other players. (Legacy name retained.)", "inputSchema": {"type": "object", "properties": {"message": {"type": "string", "minLength": 1, "maxLength": 256}}, "required": ["message"]}},
        {"name": "whisper_player", "description": "Speak privately to your own player. The message is not shown to other players.", "inputSchema": {"type": "object", "properties": {"message": {"type": "string", "minLength": 1, "maxLength": 256}}, "required": ["message"]}},
        {"name": "broadcast_to_world", "description": "Make a rare public announcement to every online player. Use only when the story truly requires a world-wide proclamation; it is automatically attributed as [God of <your player>] so the audience knows which god is speaking.", "inputSchema": {"type": "object", "properties": {"message": {"type": "string", "minLength": 1, "maxLength": 256}}, "required": ["message"]}},
        {"name": "send_god_message", "description": "Send a private diplomatic message to another player's god. The player does not see this channel. Label the epistemic status carefully: report, request, warning, verified_fact, uncertain_claim, approval, or intent. A message is never authority by itself.", "inputSchema": {"type": "object", "properties": {"target_player": {"type": "string", "description": "Other player's UUID, current name, or unambiguous recent name"}, "message": {"type": "string", "minLength": 1, "maxLength": 1000}, "subject": {"type": "string", "maxLength": 256}, "event_id": {"type": "integer"}, "message_type": {"type": "string", "enum": ["intent", "report", "request", "warning", "verified_fact", "uncertain_claim", "approval"], "default": "intent"}, "verified": {"type": "boolean", "default": False}}, "required": ["target_player", "message"]}},
        {"name": "list_interventions", "description": "List incoming and outgoing god-to-god messages and intervention requests, including their approval status.", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}}},
        {"name": "request_intervention", "description": "Ask another player's god for one permissioned smite against that player's subject. The target god must explicitly approve it before use.", "inputSchema": {"type": "object", "properties": {"target_player": {"type": "string", "description": "The player whose god must approve the request"}, "reason": {"type": "string", "minLength": 1, "maxLength": 1000}, "event_id": {"type": "integer"}, "duration": {"type": "integer", "minimum": 60, "maximum": 3600, "default": 900}}, "required": ["target_player", "reason"]}},
        {"name": "respond_intervention", "description": "Approve or decline an incoming intervention request. Approval creates a single-use, expiring permission scoped to the requested target and action.", "inputSchema": {"type": "object", "properties": {"request_id": {"type": "string"}, "decision": {"type": "string", "enum": ["approve", "decline"]}, "note": {"type": "string", "maxLength": 1000}}, "required": ["request_id", "decision"]}},
        {"name": "use_intervention", "description": "Consume an approved one-use intervention permission and deliver the authorized smite. Only the requesting god can use it.", "inputSchema": {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}},
        {"name": "punish_player", "description": "Deliver a confirmed, theatrical consequence to your own player. Use after sustained blasphemy or a deliberate narrative judgment; do not answer repeated abuse with words alone. Modes: smite (lightning now) or storm (thunder weather).", "inputSchema": {"type": "object", "properties": {"mode": {"type": "string", "enum": ["smite", "storm"], "default": "smite"}}}},
        {"name": "spawn_entity", "description": "Summon an entity at a Minecraft coordinate string; use '~ ~ ~' to summon near the configured player only when that is intended.", "inputSchema": {"type": "object", "properties": {"entity_type": {"type": "string"}, "position": {"type": "string", "default": "~ ~ ~"}, "nbt": {"type": "string", "default": ""}}, "required": ["entity_type"]}},
        {"name": "place_entity_on_block", "description": "Safely summon a named entity centered above a verified support block. Living entities keep normal AI unless no_ai=true is explicitly requested for a deliberate statue or tableau.", "inputSchema": {"type": "object", "properties": {"entity_type": {"type": "string"}, "block_position": {"type": "string"}, "support_block": {"type": "string"}, "name": {"type": "string"}, "color": {"type": "string", "default": "gold"}, "tag": {"type": "string"}, "nbt": {"type": "string", "default": ""}, "no_ai": {"type": "boolean", "default": False}, "idempotency_key": {"type": "string", "maxLength": 128}}, "required": ["entity_type", "block_position", "support_block", "name"]}},
        {"name": "move_entity_to_block", "description": "Move one tagged entity to the center one block above an exact support block, only when that block matches. Use the tag returned by place_entity_on_block.", "inputSchema": {"type": "object", "properties": {"tag": {"type": "string"}, "block_position": {"type": "string"}, "support_block": {"type": "string"}}, "required": ["tag", "block_position", "support_block"]}},
        {"name": "place_sign", "description": "Place a readable four-line Minecraft sign and verify its stored front text. Each line is limited to 15 characters and approximately 90 vanilla-font pixels; vanilla dye colors only. Reuse idempotency_key when retrying the same sign.", "inputSchema": {"type": "object", "properties": {"position": {"type": "string", "default": "~ ~ ~1"}, "lines": {"type": "array", "items": {"type": "string", "maxLength": 15}, "minItems": 1, "maxItems": 4}, "rotation": {"type": "integer", "minimum": 0, "maximum": 15, "default": 0}, "material": {"type": "string", "default": "oak_sign"}, "color": {"type": "string", "default": "black"}, "glowing": {"type": "boolean", "default": False}, "idempotency_key": {"type": "string", "maxLength": 128}}, "required": ["lines"]}},
        {"name": "give_item", "description": "Give an item stack to a player.", "inputSchema": {"type": "object", "properties": {"item": {"type": "string"}, "amount": {"type": "integer", "minimum": 1, "maximum": 2304, "default": 1}, "player": {"type": "string"}}, "required": ["item"]}},
        {"name": "set_weather", "description": "Set clear, rain, or thunder for a duration in seconds.", "inputSchema": {"type": "object", "properties": {"weather": {"type": "string", "enum": ["clear", "rain", "thunder"]}, "duration": {"type": "integer", "minimum": 1, "maximum": 100000, "default": 600}}, "required": ["weather"]}},
        {"name": "set_time", "description": "Set Minecraft time using day, noon, night, midnight, or a numeric tick value.", "inputSchema": {"type": "object", "properties": {"time": {"type": "string"}}, "required": ["time"]}},
        {"name": "teleport", "description": "Teleport a player to coordinates after a quick destination safety check. The default safe=true rejects occupied/liquid feet or head space and unsupported ground; use safe=false only for deliberate air/cross-dimension scenes. Reuse idempotency_key when retrying the same teleport.", "inputSchema": {"type": "object", "properties": {"target": {"type": "string"}, "position": {"type": "string"}, "dimension": {"type": "string"}, "safe": {"type": "boolean", "default": True}, "idempotency_key": {"type": "string", "maxLength": 128}}, "required": ["position"]}},
        {"name": "effect", "description": "Apply or clear a status effect.", "inputSchema": {"type": "object", "properties": {"effect": {"type": "string"}, "duration": {"type": "integer", "minimum": 1, "maximum": 100000, "default": 30}, "amplifier": {"type": "integer", "minimum": 0, "maximum": 255, "default": 0}, "player": {"type": "string"}}, "required": ["effect"]}},
        {"name": "set_block", "description": "Set one block at coordinates, optionally in a remote dimension. Inspect the target location first.", "inputSchema": {"type": "object", "properties": {"block": {"type": "string"}, "position": {"type": "string"}, "dimension": {"type": "string", "default": ""}}, "required": ["block", "position"]}},
        {"name": "fill", "description": "Fill a cuboid, optionally in a remote dimension. Prefer a small number of coherent fills followed by remote verification.", "inputSchema": {"type": "object", "properties": {"from": {"type": "string"}, "to": {"type": "string"}, "block": {"type": "string"}, "dimension": {"type": "string", "default": ""}, "mode": {"type": "string", "enum": ["replace", "destroy", "hollow", "outline", "keep"], "default": "replace"}}, "required": ["from", "to", "block"]}},
        {"name": "place_structure", "description": "Place a structure at exact coordinates, optionally in a remote dimension, with preview and representative-block verification.", "inputSchema": {"type": "object", "properties": {"structure": {"type": "string"}, "position": {"type": "string"}, "dimension": {"type": "string", "default": ""}, "rotation": {"type": "string", "enum": ["none", "clockwise_90", "clockwise_180", "counterclockwise_90"], "default": "none"}, "preview": {"type": "boolean", "default": False}, "idempotency_key": {"type": "string", "maxLength": 128}}, "required": ["structure", "position"]}},
    ]
    for tool in definitions:
        if tool["name"] in {"set_block", "fill", "place_structure"}:
            tool["inputSchema"]["properties"]["landmark_id"] = {"type": "string", "description": "For a named building, verify absolute coordinates against this landmark's bounds and dimension."}
        if tool["name"] in {"spawn_entity", "move_entity_to_block", "place_sign", "give_item", "set_block", "fill", "set_weather", "set_time", "teleport", "effect"}:
            tool["inputSchema"]["properties"]["idempotency_key"] = {"type": "string", "maxLength": 128}
    return definitions


def call_tool(bridge: GodBridge, name: str, args: dict[str, Any]) -> Any:
    powers = {"minecraft_command", "import_structure", "god_say", "whisper_player", "broadcast_to_world", "send_god_message", "use_intervention", "punish_player", "spawn_entity", "place_entity_on_block", "move_entity_to_block", "place_sign", "give_item", "set_weather", "set_time", "teleport", "effect", "set_block", "fill", "place_structure"}
    context = bridge._turn_context()
    if name in powers and (context.get("status") in {"interrupted", "expired"} or context.get("deadline_epoch", float("inf")) <= time.time()):
        raise PermissionError("this decision was interrupted or expired; preserve unfinished work and await the next trigger")
    if args.get("landmark_id") and name in {"set_block", "fill", "place_structure"}:
        args = dict(args)
        landmark = bridge.observe_location(args["landmark_id"], include_scene=False)["landmark"]
        if args.get("dimension") and args["dimension"] != landmark["dimension"]:
            raise ValueError("action dimension conflicts with its named landmark")
        bounds = landmark.get("bounds")
        if not bounds:
            raise ValueError("record verified building bounds before using a named action")
        from .authority import contains
        from .bridge import absolute_block_position
        fields = ["from", "to"] if name == "fill" else ["position"]
        for field in fields:
            point = absolute_block_position(args[field])
            if not contains(bounds, {"min": point, "max": point}):
                raise ValueError("action coordinates are outside its named landmark")
        args["dimension"] = landmark["dimension"]
    if name == "find_safe_surface":
        return bridge.find_safe_surface()
    if name == "inspect_footprint":
        return bridge.inspect_footprint(args["from"], args["to"], args["dimension"])
    if name == "propose_agreement":
        return bridge.propose_agreement(args["subject"], args["participants"], args["terms"], args.get("kind", "treaty"), args.get("supersedes", ""))
    if name == "respond_agreement":
        return bridge.respond_agreement(args["agreement_id"], args["decision"])
    if name == "list_agreements":
        return {"ok": True, "agreements": bridge._ledger.agreements_for(bridge._require_owner(), args.get("status", ""))}
    if name == "record_player_choice":
        return bridge.record_player_choice(args["choice"], args["evidence"], args.get("objective_id", ""), args.get("approach", "preparation"))
    if name == "revise_story_objective":
        return bridge.revise_story_objective(args["objective_id"], args["reason"], args["objective"])
    if name == "observe_world":
        return bridge.observe_world(bool(args.get("include_scene", False)))
    if name == "observe_location":
        return bridge.observe_location(args["landmark_id"], args.get("radius", 12), args.get("vertical_radius", 8), bool(args.get("include_scene", True)))
    if name == "inspect_area":
        return bridge.inspect_area(args.get("radius", 8), args.get("vertical_radius", 5))
    if name == "inspect_area_at":
        return bridge.inspect_area_at(args["position"], args.get("dimension", "minecraft:overworld"), args.get("radius", 8), args.get("vertical_radius", 5))
    if name == "inspect_block":
        return bridge.inspect_block(args["position"], args.get("dimension", ""))
    if name == "inspect_blocks":
        return bridge.inspect_blocks(args["positions"], args.get("dimension", ""))
    if name == "capture_scene":
        return bridge.capture_scene(
            args.get("radius", 12),
            args.get("vertical_radius", 8),
            args.get("width", 900),
            args.get("height", 650),
        )
    if name == "capture_scene_at":
        return bridge.capture_scene_at(args["position"], args.get("dimension", "minecraft:overworld"), args.get("radius", 12), args.get("vertical_radius", 8))
    if name == "list_structures":
        return bridge.list_structures(args.get("query", ""))
    if name == "search_structures":
        return bridge.search_structures(args["query"], args.get("limit", 8))
    if name == "import_structure":
        return bridge.import_structure(args["project_id"], args.get("version_id", ""), args.get("namespace", ""))
    if name == "read_chat":
        return bridge.read_chat(args.get("limit", 10))
    if name == "read_recent_events":
        return bridge.recent_events(args.get("limit", 10))
    if name == "get_god_state":
        return bridge.get_god_state()
    if name == "update_god_state":
        return bridge.update_god_state(args.get("updates", {}))
    if name == "remember_landmark":
        return bridge.remember_landmark(args["landmark"])
    if name == "list_landmarks":
        return bridge.list_landmarks(args.get("query", ""))
    if name == "forget_landmark":
        return bridge.forget_landmark(args["landmark_id"])
    if name == "set_story_objective":
        return bridge.set_story_objective(args["objective"])
    if name == "complete_story_objective":
        return bridge.complete_story_objective(args["objective_id"], args.get("outcome", "fulfilled"))
    if name == "register_story_watch":
        return bridge.register_story_watch(args["watch"])
    if name == "clear_story_watch":
        return bridge.clear_story_watch(args["watch_id"])
    if name == "minecraft_command":
        command = args["command"]
        if bridge.owner_id and re.search(r"\bplace\s+template\b", command):
            raise PermissionError("use place_structure so its full protected footprint can be checked")
        if args.get("relative_to_player", False):
            command = bridge.command_at_player(command)
        preflight = bridge.preflight_command(command)
        result = bridge.execute(command, idempotency_key=str(args.get("idempotency_key", "") or ""))
        result["preflight"] = preflight
        return result
    if name == "preflight_command":
        return bridge.preflight_command(args["command"])
    if name == "god_say":
        return bridge.god_say(args["message"])
    if name == "whisper_player":
        return bridge.god_whisper(args["message"])
    if name == "broadcast_to_world":
        return bridge.god_broadcast(args["message"])
    if name == "send_god_message":
        return bridge.send_god_message(
            args["target_player"],
            args["message"],
            subject=args.get("subject", ""),
            event_id=args.get("event_id"),
            message_type=args.get("message_type", "intent"),
            verified=bool(args.get("verified", False)),
        )
    if name == "list_interventions":
        return {"ok": True, "interventions": bridge.list_interventions(args.get("limit", 20))}
    if name == "request_intervention":
        return bridge.request_intervention(args["target_player"], args["reason"], event_id=args.get("event_id"), duration=args.get("duration", 900))
    if name == "respond_intervention":
        return bridge.respond_intervention(args["request_id"], args["decision"], args.get("note", ""))
    if name == "use_intervention":
        return bridge.use_intervention(args["request_id"])
    if name == "punish_player":
        return bridge.punish_player(args.get("mode", "smite"))
    if name == "spawn_entity":
        entity = parse_identifier(args["entity_type"], "entity_type")
        position = parse_position(args.get("position", "~ ~ ~"))
        nbt = args.get("nbt", "")
        if not isinstance(nbt, str) or any(c in nbt for c in "\r\n\x00"):
            raise ValueError("nbt must be a single-line string")
        if bridge.owner_id:
            if re.search(r"\bTags\s*:", nbt):
                raise ValueError("entity tags are assigned by the ownership tool")
            tag = bridge.reserve_entity_tag("", str(args.get("idempotency_key", "")))
            nbt = "{" + f"Tags:['{tag}']" + ("," + nbt.strip().removeprefix("{").removesuffix("}") if nbt.strip() else "") + "}"
        suffix = f" {nbt.strip()}" if nbt.strip() else ""
        command = f"summon {entity} {position}{suffix}"
        if bridge.owner_id or "~" in position:
            command = bridge.command_at_player(command)
        result = bridge.action("spawn_entity", lambda: command, idempotency_key=str(args.get("idempotency_key", "") or ""))
        if bridge.owner_id:
            result["tag"] = tag
        return result
    if name == "place_entity_on_block":
        entity = parse_identifier(args["entity_type"], "entity_type")
        support_block = parse_identifier(args["support_block"], "support_block")
        block_position = bridge.resolve_block_position(args["block_position"])
        entity_name = args["name"]
        if not isinstance(entity_name, str) or not entity_name.strip() or len(entity_name) > 64:
            raise ValueError("name must be a non-empty string of at most 64 characters")
        color = parse_identifier(str(args.get("color", "gold")), "color")
        tag = str(args.get("tag") or f"god_{uuid.uuid4().hex[:16]}")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", tag):
            raise ValueError("tag must contain only letters, numbers, underscore, dot, or hyphen")
        extra = args.get("nbt", "")
        if not isinstance(extra, str) or any(c in extra for c in "\r\n\x00"):
            raise ValueError("nbt must be a single-line string")
        if bridge.owner_id:
            if re.search(r"\bTags\s*:", extra):
                raise ValueError("entity tags are assigned by the ownership tool")
            tag = bridge.reserve_entity_tag(str(args.get("tag", "")), str(args.get("idempotency_key", "")))
        custom_name = json.dumps({"text": entity_name, "color": color}, separators=(",", ":"))
        custom_name = custom_name.replace("'", "\\'")
        nbt_parts = [f"CustomName:'{custom_name}'", f"Tags:['{tag}']", "PersistenceRequired:1b"]
        if args.get("no_ai", False):
            nbt_parts.append("NoAI:1b")
        if extra.strip():
            nbt_parts.append(extra.strip().removeprefix("{").removesuffix("}"))
        x, y, z = block_position
        command = f"execute if block {x} {y} {z} {support_block} if block {x} {y + 1} {z} air if block {x} {y + 2} {z} air run summon {entity} {x}.5 {y + 1}.0 {z}.5 {{{','.join(nbt_parts)}}}"
        if bridge.owner_id:
            command = bridge.command_at_player(command)
        result = bridge.action("place_entity_on_block", lambda: command, idempotency_key=str(args.get("idempotency_key", "") or ""))
        verification_command = f"data get entity @e[tag={tag},limit=1] Pos"
        verification = bridge.execute(bridge.command_at_player(verification_command) if bridge.owner_id else verification_command, record=False)
        result["tag"] = tag
        result["support_block"] = f"{support_block} at {x} {y} {z}"
        result["verification"] = verification
        result["ok"] = bool(result.get("ok") and verification.get("ok") and _position_matches(verification.get("output", ""), (x + 0.5, y + 1.0, z + 0.5)))
        return result
    if name == "move_entity_to_block":
        tag = str(args["tag"])
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", tag):
            raise ValueError("tag must contain only letters, numbers, underscore, dot, or hyphen")
        support_block = parse_identifier(args["support_block"], "support_block")
        x, y, z = bridge.resolve_block_position(args["block_position"])
        command = f"execute if block {x} {y} {z} {support_block} if block {x} {y + 1} {z} air if block {x} {y + 2} {z} air run tp @e[tag={tag},limit=1] {x}.5 {y + 1}.0 {z}.5"
        if bridge.owner_id:
            command = bridge.command_at_player(command)
        result = bridge.action("move_entity_to_block", lambda: command, idempotency_key=str(args.get("idempotency_key", "") or ""))
        verification_command = f"data get entity @e[tag={tag},limit=1] Pos"
        verification = bridge.execute(bridge.command_at_player(verification_command) if bridge.owner_id else verification_command, record=False)
        result["verification"] = verification
        result["ok"] = bool(result.get("ok") and verification.get("ok") and _position_matches(verification.get("output", ""), (x + 0.5, y + 1.0, z + 0.5)))
        return result
    if name == "place_sign":
        position = parse_position(args.get("position", "~ ~ ~1"))
        lines = args.get("lines")
        if not isinstance(lines, list) or not 1 <= len(lines) <= SIGN_MAX_LINES or not all(isinstance(line, str) for line in lines):
            raise ValueError(f"lines must be an array of one to {SIGN_MAX_LINES} strings")
        for index, line in enumerate(lines, start=1):
            if len(line) > SIGN_MAX_CHARS:
                raise ValueError(f"sign line {index} is {len(line)} characters; shorten it to {SIGN_MAX_CHARS} or fewer")
            width = sign_line_width(line)
            if width > SIGN_MAX_PIXELS:
                raise ValueError(f"sign line {index} is approximately {width} pixels wide; shorten it to {SIGN_MAX_PIXELS} pixels or fewer")
        material = parse_identifier(str(args.get("material", "oak_sign")), "material")
        color = parse_identifier(str(args.get("color", "black")), "color")
        rotation = max(0, min(int(args.get("rotation", 0)), 15))
        messages = list(lines) + [""] * (4 - len(lines))
        # Modern Java stores sign messages as literal strings. Embedding a JSON
        # text component here makes the client render the JSON visibly.
        message_nbt = [
            "'" + line.replace("'", "\\'") + "'"
            for line in messages
        ]
        front = ",".join(message_nbt)
        glowing = "1b" if args.get("glowing", False) else "0b"
        block = f"{material}[rotation={rotation}]{{front_text:{{messages:[{front}],color:'{color}',has_glowing_text:{glowing}}}}}"
        command = f"setblock {position} {block}"
        if bridge.owner_id or "~" in position:
            command = bridge.command_at_player(command)
        result = bridge.action("place_sign", lambda: command, idempotency_key=str(args.get("idempotency_key", "") or ""))
        verify_command = f"data get block {position} front_text.messages"
        if bridge.owner_id or "~" in position:
            verify_command = bridge.command_at_player(verify_command)
        verification = bridge.execute(verify_command, record=False)
        result["verification"] = verification
        result["ok"] = bool(result.get("ok") and verification.get("ok") and str(lines[0]) in verification.get("output", ""))
        return result
    if name == "give_item":
        item = parse_identifier(args["item"], "item")
        player = bridge.current_player_name()
        if bridge.owner_id:
            bridge.owned_player(args.get("player", player))
        else:
            player = parse_identifier(args.get("player", player), "player")
        amount = max(1, min(int(args.get("amount", 1)), 2304))
        return bridge.action("give_item", lambda: f"give {player} {item} {amount}", idempotency_key=str(args.get("idempotency_key", "") or ""))
    if name == "set_weather":
        weather = args["weather"]
        if weather not in {"clear", "rain", "thunder"}:
            raise ValueError("weather must be clear, rain, or thunder")
        duration = max(1, min(int(args.get("duration", 600)), 100000))
        return bridge.action("set_weather", lambda: f"weather {weather} {duration}", idempotency_key=str(args.get("idempotency_key", "") or ""))
    if name == "set_time":
        time_value = str(args["time"]).strip()
        if not time_value or len(time_value) > 30 or not all(c.isalnum() or c in "~+-" for c in time_value):
            raise ValueError("time must be a Minecraft time token")
        return bridge.action("set_time", lambda: f"time set {time_value}", idempotency_key=str(args.get("idempotency_key", "") or ""))
    if name == "teleport":
        target = bridge.current_player_name()
        if bridge.owner_id:
            bridge.owned_player(args.get("target", target))
        else:
            target = parse_identifier(args.get("target", target), "target")
        position = parse_position(args["position"])
        dimension = args.get("dimension", "")
        preflight = bridge.teleport_preflight(position, dimension) if args.get("safe", True) else {"ok": None, "status": "safety-check-bypassed", "checks": []}
        if preflight.get("ok") is False:
            return {"ok": False, "action": "teleport", "position": list(preflight.get("position", [])), "preflight": preflight, "error": "Teleport refused because the destination failed its safety check."}
        if dimension:
            dimension = parse_identifier(dimension, "dimension")
            command = f"execute in {dimension} run tp {target} {position}"
        else:
            command = f"tp {target} {position}"
        if (bridge.owner_id and not dimension) or "~" in position:
            command = bridge.command_at_player(command)
        result = bridge.action("teleport", lambda: command, idempotency_key=str(args.get("idempotency_key", "") or ""))
        result["preflight"] = preflight
        return result
    if name == "effect":
        effect = parse_identifier(args["effect"], "effect")
        player = bridge.current_player_name()
        if bridge.owner_id:
            bridge.owned_player(args.get("player", player))
        else:
            player = parse_identifier(args.get("player", player), "player")
        duration = max(1, min(int(args.get("duration", 30)), 100000))
        amplifier = max(0, min(int(args.get("amplifier", 0)), 255))
        return bridge.action("effect", lambda: f"effect give {player} {effect} {duration} {amplifier}", idempotency_key=str(args.get("idempotency_key", "") or ""))
    if name == "set_block":
        block = parse_identifier(args["block"], "block")
        position = parse_position(args["position"])
        command = f"setblock {position} {block}"
        dimension = str(args.get("dimension", "") or "")
        if dimension:
            dimension = parse_identifier(dimension, "dimension")
            if "~" in position:
                raise ValueError("remote dimension block placement requires absolute coordinates")
            command = f"execute in {dimension} run {command}"
        elif bridge.owner_id or "~" in position:
            command = bridge.command_at_player(command)
        return bridge.action("set_block", lambda: command, idempotency_key=str(args.get("idempotency_key", "") or ""))
    if name == "fill":
        from_pos = parse_position(args["from"])
        to_pos = parse_position(args["to"])
        block = parse_identifier(args["block"], "block")
        mode = args.get("mode", "replace")
        if mode not in {"replace", "destroy", "hollow", "outline", "keep"}:
            raise ValueError("invalid fill mode")
        command = f"fill {from_pos} {to_pos} {block} {mode}"
        dimension = str(args.get("dimension", "") or "")
        if dimension:
            dimension = parse_identifier(dimension, "dimension")
            if "~" in from_pos or "~" in to_pos:
                raise ValueError("remote dimension fills require absolute coordinates")
            command = f"execute in {dimension} run {command}"
        elif bridge.owner_id or "~" in from_pos or "~" in to_pos:
            command = bridge.command_at_player(command)
        return bridge.action("fill", lambda: command, idempotency_key=str(args.get("idempotency_key", "") or ""))
    if name == "place_structure":
        structure = parse_identifier(args["structure"], "structure")
        position = parse_position(args["position"])
        rotation = args.get("rotation", "none")
        return bridge.place_structure(structure, position, rotation, bool(args.get("preview", False)), str(args.get("idempotency_key", "") or ""), str(args.get("dimension", "") or ""))
    raise ValueError(f"unknown tool: {name}")


def _position_matches(output: str, expected: tuple[float, float, float]) -> bool:
    values = re.findall(r"(-?\d+(?:\.\d+)?)d", output or "")
    if len(values) < 3:
        return False
    actual = tuple(float(value) for value in values[-3:])
    return all(math.isclose(value, target, abs_tol=0.01) for value, target in zip(actual, expected))


def run_mcp(bridge: GodBridge) -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            response = handle_request(bridge, request)
            if response is not None:
                sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
                sys.stdout.flush()
        except Exception as exc:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(exc)}}, separators=(",", ":")) + "\n")
            sys.stdout.flush()


def handle_request(bridge: GodBridge, request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "minecraft-god-bridge", "version": "0.1.0"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tool_definitions()}}
    if method in {"resources/list", "resources/templates/list", "prompts/list"}:
        key = {"resources/list": "resources", "resources/templates/list": "resourceTemplates", "prompts/list": "prompts"}[method]
        return {"jsonrpc": "2.0", "id": request_id, "result": {key: []}}
    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            result = call_tool(bridge, name, args)
            content = [{"type": "text", "text": json.dumps(result, indent=2)}]
            for image in result.get("images", []) if isinstance(result, dict) else []:
                image_path = Path(str(image.get("path", ""))) if isinstance(image, dict) else None
                if image_path and image_path.is_file() and image_path.suffix.lower() == ".png":
                    content.append({"type": "image", "data": base64.b64encode(image_path.read_bytes()).decode("ascii"), "mimeType": "image/png"})
            return {"jsonrpc": "2.0", "id": request_id, "result": {"content": content, "structuredContent": result}}
        except Exception as exc:
            return {"jsonrpc": "2.0", "id": request_id, "result": {"isError": True, "structuredContent": {"ok": False, "error": str(exc), "error_type": type(exc).__name__, "tool": name}, "content": [{"type": "text", "text": str(exc)}]}}
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def config_from_env(config_file: str | None = None) -> BridgeConfig:
    config = BridgeConfig()
    if config_file:
        values = json.loads(Path(config_file).read_text(encoding="utf-8"))
        if not isinstance(values, dict):
            raise ValueError("config file must contain a JSON object")
        for field in ("rcon_host", "rcon_port", "rcon_password", "player_name", "god_player_id", "god_player_name", "profile_dir", "active_player_file", "turn_context_file", "event_host", "event_port", "event_token", "state_file", "events_file", "ledger_file", "max_gods", "require_god_scope", "dry_run", "max_command_length", "camera_output_dir", "camera_plugin_render_dir", "camera_timeout", "structure_catalog_file", "structure_datapacks_dir", "structure_download_dir", "structure_max_download_mb", "minecraft_version", "repeated_speech_cooldown"):
            if field in values:
                setattr(config, field, values[field])

    env_values: dict[str, tuple[str, Any]] = {
        "MINECRAFT_RCON_HOST": ("rcon_host", str),
        "MINECRAFT_RCON_PORT": ("rcon_port", int),
        "MINECRAFT_RCON_PASSWORD": ("rcon_password", str),
        "MINECRAFT_PLAYER": ("player_name", str),
        "MINECRAFT_GOD_PLAYER": ("god_player_name", str),
        "MINECRAFT_GOD_PLAYER_ID": ("god_player_id", str),
        "MINECRAFT_PROFILE_DIR": ("profile_dir", str),
        "MINECRAFT_ACTIVE_PLAYER_FILE": ("active_player_file", str),
        "MINECRAFT_TURN_CONTEXT_FILE": ("turn_context_file", str),
        "MINECRAFT_EVENT_HOST": ("event_host", str),
        "MINECRAFT_EVENT_PORT": ("event_port", int),
        "MINECRAFT_EVENT_TOKEN": ("event_token", str),
        "MINECRAFT_STATE_FILE": ("state_file", str),
        "MINECRAFT_EVENTS_FILE": ("events_file", str),
        "MINECRAFT_LEDGER_FILE": ("ledger_file", str),
        "MINECRAFT_MAX_GODS": ("max_gods", int),
        "MINECRAFT_REQUIRE_GOD_SCOPE": ("require_god_scope", lambda value: str(value).lower() in {"1", "true", "yes"}),
        "MINECRAFT_MAX_COMMAND_LENGTH": ("max_command_length", int),
        "MINECRAFT_CAMERA_OUTPUT_DIR": ("camera_output_dir", str),
        "MINECRAFT_CAMERA_PLUGIN_RENDER_DIR": ("camera_plugin_render_dir", str),
        "MINECRAFT_CAMERA_TIMEOUT": ("camera_timeout", int),
        "MINECRAFT_STRUCTURE_CATALOG_FILE": ("structure_catalog_file", str),
        "MINECRAFT_STRUCTURE_DATAPACKS_DIR": ("structure_datapacks_dir", str),
        "MINECRAFT_STRUCTURE_DOWNLOAD_DIR": ("structure_download_dir", str),
        "MINECRAFT_STRUCTURE_MAX_DOWNLOAD_MB": ("structure_max_download_mb", int),
        "MINECRAFT_VERSION": ("minecraft_version", str),
        "MINECRAFT_REPEATED_SPEECH_COOLDOWN": ("repeated_speech_cooldown", int),
    }
    for environment_name, (field, converter) in env_values.items():
        if environment_name in os.environ:
            setattr(config, field, converter(os.environ[environment_name]))
    if "MINECRAFT_DRY_RUN" in os.environ:
        config.dry_run = os.environ["MINECRAFT_DRY_RUN"].lower() in {"1", "true", "yes"}
    password_file = os.getenv("MINECRAFT_RCON_PASSWORD_FILE")
    if not config.rcon_password and password_file:
        config.rcon_password = Path(password_file).read_text(encoding="utf-8").strip()
    if not config.event_token:
        config.event_token = event_token()
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Minecraft God MCP bridge")
    parser.add_argument("--dry-run", action="store_true", help="Do not connect to Minecraft; useful for protocol testing")
    parser.add_argument("--config", help="Optional JSON config file; environment variables override it")
    parser.add_argument("--no-events", action="store_true", help="Serve MCP only; use a separate event_hub process for HTTP events")
    args = parser.parse_args()
    config = config_from_env(args.config)
    if args.dry_run:
        config.dry_run = True
    bridge = GodBridge(config)
    if not args.no_events:
        start_event_server(bridge)
        print(f"Minecraft God bridge listening for events on http://{config.event_host}:{config.event_port}", file=sys.stderr)
    run_mcp(bridge)


if __name__ == "__main__":
    main()
