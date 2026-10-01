import json
import struct
import zipfile
from pathlib import Path

from god_bridge.bridge import BridgeConfig, GodBridge
from god_bridge.mcp_server import call_tool
from god_bridge.structures import StructureLibrary, read_structure_metadata


def _string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return struct.pack(">H", len(encoded)) + encoded


def _named(tag_type: int, name: str, payload: bytes) -> bytes:
    return bytes([tag_type]) + _string(name) + payload


def _int_list(name: str, values: list[int]) -> bytes:
    return _named(9, name, bytes([3]) + struct.pack(">i", len(values)) + b"".join(struct.pack(">i", value) for value in values))


def _structure_nbt() -> bytes:
    palette_item = _named(8, "Name", _string("minecraft:stone")) + b"\x00"
    palette = _named(9, "palette", bytes([10]) + struct.pack(">i", 1) + palette_item)
    block = _int_list("pos", [1, 2, 3]) + _named(3, "state", struct.pack(">i", 0)) + b"\x00"
    blocks = _named(9, "blocks", bytes([10]) + struct.pack(">i", 1) + block)
    root = _named(10, "", _int_list("size", [4, 5, 6]) + palette + blocks + b"\x00")
    return root


def test_structure_nbt_metadata_is_read_without_external_dependencies(tmp_path: Path):
    path = tmp_path / "altar.nbt"
    path.write_bytes(_structure_nbt())
    assert read_structure_metadata(path) == {"size": [4, 5, 6], "samples": [{"position": [1, 2, 3], "block": "minecraft:stone"}]}


def test_structure_zip_import_extracts_only_nbt_templates(tmp_path: Path):
    source = tmp_path / "pack.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("pack.mcmeta", json.dumps({"pack": {"pack_format": 1}}))
        archive.writestr("pack/data/demo/structures/altar.nbt", _structure_nbt())
        archive.writestr("pack/data/demo/function/dangerous.mcfunction", "stop")

    library = StructureLibrary(tmp_path, datapacks_dir="datapacks", download_dir="downloads")
    target = tmp_path / "installed"
    target.mkdir()
    entries, _ = library._extract_structures(source, target, "god_demo")

    assert entries[0]["id"] == "god_demo:altar"
    assert (target / "data/god_demo/structures/altar.nbt").is_file()
    assert not (target / "data/demo/function/dangerous.mcfunction").exists()


def test_imported_structure_placement_checks_a_representative_block(tmp_path: Path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"entries": [{
        "id": "god_demo:altar",
        "name": "altar",
        "size": [4, 5, 6],
        "samples": [{"position": [1, 2, 3], "block": "minecraft:stone"}],
    }]}), encoding="utf-8")

    class Rcon:
        def __init__(self):
            self.commands = []

        def command(self, command):
            self.commands.append(command)
            if command.startswith("godcheck "):
                block_type = "minecraft:stone" if command.endswith(" 11 22 33") else "minecraft:air"
                return type("Response", (), {"body": json.dumps({
                    "type": block_type,
                    "solid": block_type != "minecraft:air",
                    "passable": block_type == "minecraft:air",
                    "liquid": False,
                    "replaceable": block_type == "minecraft:air",
                })})()
            body = "Block at 11 22 33 is minecraft:stone" if command.startswith("data get block") else "Successfully placed structure"
            return type("Response", (), {"body": body})()

    bridge = GodBridge(BridgeConfig(state_file=str(tmp_path / "state.json"), structure_catalog_file=str(catalog)), rcon=Rcon())
    result = call_tool(bridge, "place_structure", {"structure": "god_demo:altar", "position": "10 20 30"})
    assert result["ok"] is True
    assert result["verification"]["ok"] is True
