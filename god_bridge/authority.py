"""Parse the supported console grammar and enforce resource ownership."""
from __future__ import annotations

import math
import re
from typing import Any

from .pantheon import bounds_overlap


TARGET = r"(?:@[a-z](?:\[[^\]]*\])?|[A-Za-z0-9_-]+)"
COORD = r"(?:~?-?\d*(?:\.\d+)?|~)"
XYZ = rf"({COORD})\s+({COORD})\s+({COORD})"


def authorize(bridge: Any, command: str, target_player_id: str = "") -> None:
    owner = bridge.owner_id
    if target_player_id and target_player_id != owner:
        raise PermissionError("cross-player actions require a one-use intervention")
    player = bridge.current_player_name()
    executor_owned = False
    anchored = False
    dimension = "minecraft:overworld"  # Unwrapped commands execute at the console.
    text = command.strip()

    def target(value: str) -> None:
        if value == "@s" and executor_owned:
            return
        if value.casefold() in {owner.casefold(), player.casefold()}:
            return
        selector = re.fullmatch(r"@e\[tag=([A-Za-z0-9_.-]+),limit=1\]", value)
        resource = bridge._ledger.resource("entity:" + (selector[1] if selector else value))
        if resource and resource["owner_id"] == owner:
            return
        raise PermissionError("target is not the owned player or an explicitly registered owned entity")

    while text.startswith("execute "):
        wrapper, separator, text = text[8:].partition(" run ")
        if not separator:
            raise PermissionError("execute must use supported clauses and a run command")
        while wrapper:
            match = re.match(rf"(as|at)\s+({TARGET})(?:\s+|$)", wrapper)
            if match:
                target(match[2])
                if match[1] == "as":
                    executor_owned = match[2].casefold() in {player.casefold(), owner.casefold()}
                else:
                    # Position anchoring must be to the player, not an arbitrary entity.
                    if match[2] not in {player, owner, "@s"}:
                        raise PermissionError("use absolute coordinates for entity-positioned world actions")
                    anchored = True
                    dimension = ""  # `at` adopts the owned player's dimension.
                wrapper = wrapper[match.end():]
                continue
            match = re.match(r"in\s+(minecraft:[a-z0-9_./-]+)(?:\s+|$)", wrapper)
            if match:
                dimension = match[1]
                wrapper = wrapper[match.end():]
                continue
            match = re.match(rf"(?:if|unless) block {XYZ}\s+\S+(?:\s+|$)", wrapper)
            if match:
                wrapper = wrapper[match.end():]
                continue
            raise PermissionError("unsupported execute clause; use the purpose-built God tools")

    operation = text.split(" ", 1)[0]
    if operation in {"setblock", "fill", "summon", "data", "give"} and re.search(r"\b(?:command_block|chain_command_block|repeating_command_block|command_block_minecart)\b|\b(?:Command|SpawnData|SpawnPotentials|EntityTag|BlockEntityTag)\b", text):
        raise PermissionError("command-bearing blocks, entities and data are outside scoped powers")
    if operation in {"godscan", "godcheck", "godsurface", "godobserve"}:
        target(text.split()[1])
        return
    if operation in {"godscanat", "godcheckat", "godcheckbatch", "godfootprint", "godvalidate", "camera"}:
        return
    if operation in {"weather", "time"}:
        return
    if operation in {"give", "clear", "kill", "tellraw", "tp", "teleport"}:
        match = re.match(rf"\S+\s+({TARGET})(?:\s+|$)", text)
        if not match:
            raise PermissionError("explicit target is required")
        target(match[1])
        if operation in {"tp", "teleport"}:
            rest = text[match.end():].split()
            if len(rest) < 3 or not all(re.fullmatch(COORD, token) and token for token in rest[:3]):
                raise PermissionError("teleport destinations must be coordinates")
            if match[1].casefold() not in {player.casefold(), owner.casefold(), "@s"}:
                check_world_bounds(bridge, rest[:3], None, dimension, anchored, margin=16)
        return
    if operation == "effect":
        match = re.match(rf"effect (?:give|clear)\s+({TARGET})(?:\s+|$)", text)
        if not match:
            raise PermissionError("explicit effect target is required")
        target(match[1])
        return
    if operation == "data":
        match = re.match(rf"data (get|merge|modify|remove) (entity|block)\s+({TARGET}|{XYZ})(?:\s+|$)", text)
        if not match:
            raise PermissionError("only owned entity and block data operations are supported")
        if match[2] == "entity":
            target(match[3])
            # Entity data can transfer targeting/ownership or introduce passengers.
            if match[1] != "get" and re.search(r"\b(?:Tags|Passengers|Owner|UUID|Pos|Motion|ExplosionPower|Fuse)\b", text):
                raise PermissionError("entity identity and movement require scoped tools")
        elif match[1] != "get":
            check_world_bounds(bridge, text.split()[3:6], None, dimension, anchored)
        return
    if operation == "setblock":
        check_world_bounds(bridge, text.split()[1:4], None, dimension, anchored, margin=8 if any(h in text for h in ("lava", "fire", "tnt")) else 0)
        return
    if operation == "fill":
        tokens = text.split()
        check_world_bounds(bridge, tokens[1:4], tokens[4:7], dimension, anchored, margin=8 if any(h in text for h in ("lava", "fire", "tnt")) else 0)
        return
    if operation == "summon":
        tokens = text.split()
        check_world_bounds(bridge, tokens[2:5], None, dimension, anchored, margin=16)
        if re.search(r"\b(?:Passengers|Owner|UUID|ExplosionPower|Motion)\b", text):
            raise PermissionError("unsafe entity data is outside scoped summoning")
        for tag_list in re.findall(r"Tags\s*:\s*\[([^\]]*)\]", text):
            tags = re.findall(r"['\"]([^'\"]+)['\"]", tag_list)
            if re.sub(r"['\"][^'\"]+['\"]|[\s,]", "", tag_list):
                raise PermissionError("summon tags must be quoted, registered identifiers")
            for tag in tags:
                resource = bridge._ledger.resource("entity:" + tag)
                if not resource or resource["owner_id"] != owner:
                    raise PermissionError("summoned entity tags must be reserved by the owning god")
        return
    if operation == "place" and text.startswith("place template "):
        check_world_bounds(bridge, text.split()[3:6], None, dimension, anchored)
        return
    raise PermissionError(f"'{operation}' is outside the player-god command grammar")


def check_world_bounds(bridge: Any, start: list[str], end: list[str] | None, dimension: str, anchored: bool, margin: int = 0) -> None:
    if not bridge.owner_id:
        return
    if len(start) != 3 or end is not None and len(end) != 3:
        raise ValueError("world actions require three coordinates")
    relative = any(token.startswith("~") for token in start + (end or []))
    if relative and not anchored:
        raise PermissionError("relative world actions must be anchored to the owned player")
    if relative and dimension:
        raise PermissionError("explicit-dimension world actions require absolute coordinates")
    # Resolve a live base only when needed. This read does not recurse into terrain checks.
    base = bridge._player_position() if relative else (0, 0, 0)
    def resolve(tokens: list[str]) -> list[int]:
        return [math.floor(base[i] + float(token[1:] or 0)) if token.startswith("~") else math.floor(float(token)) for i, token in enumerate(tokens)]
    first, last = resolve(start), resolve(end or start)
    if math.prod(abs(a - b) + 1 for a, b in zip(first, last)) > 32768 or any(abs(a-b) > 127 for a,b in zip(first,last)):
        raise PermissionError("world action must fit 32768 blocks and 128 blocks per axis")
    bounds = {"min": [min(a, b) - margin for a, b in zip(first, last)], "max": [max(a, b) + margin for a, b in zip(first, last)]}
    if not dimension:
        raw = bridge.execute(f"data get entity {bridge.current_player_name()} Dimension", record=False)["output"] if not bridge.config.dry_run else "minecraft:overworld"
        found = re.search(r"minecraft:[a-z0-9_./-]+", raw)
        if not found:
            raise RuntimeError("could not establish the live dimension for a world action")
        dimension = found[0]
    regions = bridge._ledger.resources("region")
    shared_authority = any(region["payload"].get("agreement_id") and dimension == region["payload"]["dimension"]
        and bridge.owner_id in region["payload"].get("participants", []) and contains(region["payload"]["bounds"], bounds) for region in regions)
    for region in regions:
        payload = region["payload"]
        if not shared_authority and dimension == payload["dimension"] and bridge.owner_id not in payload.get("participants", [region["owner_id"]]) and bounds_overlap(payload["bounds"], bounds):
            raise PermissionError("world action intersects another god's protected location")
    if not bridge.config.dry_run:
        coordinates = " ".join(str(v) for v in bounds["min"] + bounds["max"])
        output = bridge.execute(f"godvalidate {bridge.current_player_name()} {dimension} {coordinates}", record=False)["output"]
        import json
        value = json.loads(output)
        if not value.get("ok"):
            raise PermissionError("another player is inside the world action's affected area")


def contains(outer: dict[str, Any], inner: dict[str, Any]) -> bool:
    def coords(v: Any) -> list[float]:
        return [float(x) for x in v.split()] if isinstance(v, str) else v
    return all(a <= b and c <= d for a,b,c,d in zip(coords(outer["min"]),coords(inner["min"]),coords(inner["max"]),coords(outer["max"])))
