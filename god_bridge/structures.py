"""Search and safely import Java structure templates for the God bridge."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import struct
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


VANILLA_STRUCTURES = [
    {"id": "minecraft:desert_pyramid", "name": "Desert Pyramid", "source": "vanilla", "tags": ["desert", "dungeon"]},
    {"id": "minecraft:jungle_pyramid", "name": "Jungle Pyramid", "source": "vanilla", "tags": ["jungle", "dungeon"]},
    {"id": "minecraft:igloo", "name": "Igloo", "source": "vanilla", "tags": ["snow", "shelter"]},
    {"id": "minecraft:swamp_hut", "name": "Swamp Hut", "source": "vanilla", "tags": ["swamp", "witch"]},
    {"id": "minecraft:trail_ruins/tower/tower_top", "name": "Trail Ruins Tower", "source": "vanilla", "tags": ["ruins", "tower"]},
    {"id": "minecraft:village/plains/houses/plains_small_house_1", "name": "Plains Small House", "source": "vanilla", "tags": ["village", "house"]},
]


class StructureLibrary:
    """A small, auditable catalog backed by safe datapack extraction."""

    def __init__(
        self,
        workspace: Path,
        catalog_file: str = "state/structure_catalog.json",
        datapacks_dir: str = "world/datapacks",
        download_dir: str = "state/structure_downloads",
        max_download_mb: int = 64,
        game_version: str = "26.2",
    ) -> None:
        self.workspace = workspace
        self.catalog_path = self._absolute(catalog_file)
        self.datapacks_path = self._absolute(datapacks_dir)
        self.download_path = self._absolute(download_dir)
        self.max_download_bytes = max(1, int(max_download_mb)) * 1024 * 1024
        self.game_version = game_version

    def _absolute(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.workspace / path

    def catalog(self) -> list[dict[str, Any]]:
        if not self.catalog_path.exists():
            return []
        try:
            payload = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        entries = payload.get("entries", []) if isinstance(payload, dict) else []
        return [entry for entry in entries if isinstance(entry, dict) and isinstance(entry.get("id"), str)]

    def list(self, query: str = "") -> list[dict[str, Any]]:
        needle = query.strip().lower()
        entries = VANILLA_STRUCTURES + self.catalog()
        if not needle:
            return entries
        return [entry for entry in entries if needle in json.dumps(entry, ensure_ascii=False).lower()]

    def search_online(self, query: str, limit: int = 8) -> dict[str, Any]:
        query = str(query).strip()
        if not query:
            raise ValueError("query must not be empty")
        limit = max(1, min(int(limit), 20))
        facets = json.dumps([["project_type:datapack"], [f"versions:{self.game_version}"]], separators=(",", ":"))
        url = "https://api.modrinth.com/v2/search?" + urllib.parse.urlencode({"query": query, "facets": facets, "limit": limit})
        payload = self._get_json(url)
        hits = []
        for hit in payload.get("hits", []) if isinstance(payload, dict) else []:
            if not isinstance(hit, dict):
                continue
            hits.append({
                "project_id": hit.get("project_id", ""),
                "slug": hit.get("slug", ""),
                "title": hit.get("title", ""),
                "description": hit.get("description", ""),
                "downloads": hit.get("downloads", 0),
                "page": f"https://modrinth.com/datapack/{hit.get('slug', '')}",
            })
        return {"ok": True, "source": "modrinth", "game_version": self.game_version, "query": query, "results": hits}

    def import_modrinth(self, project_id: str, version_id: str = "", namespace: str = "") -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9]{5,20}", str(project_id)):
            raise ValueError("project_id must be a Modrinth project identifier")
        project = self._get_json(f"https://api.modrinth.com/v2/project/{project_id}")
        versions = self._get_json(f"https://api.modrinth.com/v2/project/{project_id}/version")
        if not isinstance(versions, list):
            raise ValueError("Modrinth returned no versions")
        selected = next((item for item in versions if isinstance(item, dict) and item.get("id") == version_id), None) if version_id else None
        if selected is None:
            compatible = [item for item in versions if isinstance(item, dict) and self.game_version in item.get("game_versions", [])]
            selected = next((item for item in compatible if item.get("version_type") == "release"), None) or (compatible[0] if compatible else None)
        if not isinstance(selected, dict):
            raise ValueError(f"No {self.game_version}-compatible datapack version was found")
        files = selected.get("files", [])
        file_info = next((item for item in files if isinstance(item, dict) and item.get("primary")), None)
        file_info = file_info or next((item for item in files if isinstance(item, dict) and str(item.get("filename", "")).lower().endswith((".zip", ".nbt"))), None)
        if not isinstance(file_info, dict) or not file_info.get("url"):
            raise ValueError("The selected Modrinth version has no downloadable zip or nbt file")

        filename = Path(str(file_info.get("filename", "structure-pack.zip"))).name
        if not filename.lower().endswith((".zip", ".nbt")):
            raise ValueError("The selected download is not a Java datapack zip or structure NBT")
        self.download_path.mkdir(parents=True, exist_ok=True)
        download_file = self.download_path / f"{project_id}-{selected.get('id', 'latest')}-{filename}"
        if not download_file.exists():
            self._download(file_info["url"], download_file)
        self._verify_hash(download_file, file_info.get("hashes", {}))

        slug = _safe_name(str(project.get("slug", project_id)))
        pack_namespace = _safe_name(namespace or f"god_{slug}")
        pack_name = f"god_{slug}_{_safe_name(str(selected.get('id', 'latest')))}"
        pack_dir = self.datapacks_path / pack_name
        if pack_dir.exists():
            raise ValueError(f"This structure version is already imported as {pack_name}")
        pack_dir.mkdir(parents=True)
        try:
            structures, _source_pack_meta = self._extract_structures(download_file, pack_dir, pack_namespace)
            if not structures:
                raise ValueError("The download contains no Java structure NBT files")
            # Rebuild metadata for the running server instead of copying an
            # old pack format from an arbitrary download.
            (pack_dir / "pack.mcmeta").write_bytes(_default_pack_meta())
        except Exception:
            _remove_tree(pack_dir)
            raise

        entries = self.catalog()
        entries.extend(structures)
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        self.catalog_path.write_text(json.dumps({"entries": entries}, indent=2) + "\n", encoding="utf-8")
        return {
            "ok": True,
            "project": {"id": project_id, "slug": project.get("slug", ""), "title": project.get("title", "")},
            "version": {"id": selected.get("id"), "name": selected.get("name"), "game_versions": selected.get("game_versions", [])},
            "pack_name": pack_name,
            "pack_path": str(pack_dir),
            "structures": structures,
            "download": str(download_file),
        }

    def get(self, structure_id: str) -> dict[str, Any] | None:
        return next((entry for entry in self.catalog() if entry.get("id") == structure_id), None)

    def _get_json(self, url: str) -> Any:
        request = urllib.request.Request(url, headers={"User-Agent": "MinecraftGodBridge/0.1.0", "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))

    def _download(self, url: str, destination: Path) -> None:
        request = urllib.request.Request(url, headers={"User-Agent": "MinecraftGodBridge/0.1.0"})
        with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
            length = int(response.headers.get("Content-Length", "0") or 0)
            if length > self.max_download_bytes:
                raise ValueError("structure download exceeds the configured size limit")
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > self.max_download_bytes:
                    raise ValueError("structure download exceeds the configured size limit")
                output.write(chunk)

    @staticmethod
    def _verify_hash(path: Path, hashes: Any) -> None:
        if not isinstance(hashes, dict):
            return
        algorithm, expected = next(((name, value) for name, value in (("sha512", hashes.get("sha512")), ("sha1", hashes.get("sha1"))) if value), (None, None))
        if not algorithm:
            return
        digest = hashlib.new(algorithm, path.read_bytes()).hexdigest()
        if digest.lower() != str(expected).lower():
            raise ValueError(f"download {algorithm} checksum did not match Modrinth metadata")

    def _extract_structures(self, source: Path, pack_dir: Path, namespace: str) -> tuple[list[dict[str, Any]], bytes | None]:
        if not re.fullmatch(r"[a-z0-9_-]+", namespace):
            raise ValueError("invalid structure namespace")
        if source.suffix.lower() == ".nbt":
            name = _safe_name(source.stem)
            target = pack_dir / "data" / namespace / "structure" / (name + ".nbt")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            return [_entry_from_nbt(target, f"{namespace}:{name}", source.name)], None

        entries: list[dict[str, Any]] = []
        pack_meta = None
        with zipfile.ZipFile(source) as archive:
            if sum(max(0, info.file_size) for info in archive.infolist()) > self.max_download_bytes * 8:
                raise ValueError("structure archive expands beyond the configured safety limit")
            for info in archive.infolist():
                if info.is_dir():
                    continue
                safe_parts = PurePosixPath(info.filename.replace("\\", "/")).parts
                if any(part in {"", ".", ".."} for part in safe_parts) or PurePosixPath(info.filename).is_absolute():
                    continue
                lowered = [part.lower() for part in safe_parts]
                if lowered[-1:] == ["pack.mcmeta"] and pack_meta is None:
                    pack_meta = archive.read(info)
                if "data" not in lowered:
                    continue
                data_index = lowered.index("data")
                if len(safe_parts) <= data_index + 3 or lowered[data_index + 2] not in {"structures", "structure"} or lowered[-1] == "pack.mcmeta":
                    continue
                if not lowered[-1].endswith(".nbt"):
                    continue
                relative = PurePosixPath(*safe_parts[data_index + 3:])
                if not re.fullmatch(r"[a-z0-9_./-]+\.nbt", relative.as_posix()):
                    continue
                if any(part in {"", ".", ".."} for part in relative.parts):
                    continue
                target = pack_dir / "data" / namespace / "structure" / relative
                target = target.with_suffix(".nbt")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(info))
                structure_id = f"{namespace}:{relative.with_suffix('').as_posix()}"
                entries.append(_entry_from_nbt(target, structure_id, info.filename))
        return entries, pack_meta


def _entry_from_nbt(path: Path, structure_id: str, source_name: str) -> dict[str, Any]:
    metadata = read_structure_metadata(path)
    return {"id": structure_id, "name": structure_id.split(":", 1)[-1].replace("/", " / ").replace("_", " "), "source": source_name, **metadata}


def read_structure_metadata(path: Path) -> dict[str, Any]:
    """Read only safe descriptive data; never edits the structure NBT."""
    maximum = 32 * 1024 * 1024
    if path.stat().st_size > maximum:
        raise ValueError("structure NBT exceeds 32 MiB")
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("decompressed structure NBT exceeds 32 MiB")
    try:
        root = _read_nbt(raw)
    except (IndexError, struct.error, UnicodeError) as error:
        raise ValueError("structure NBT is truncated or malformed") from error
    size = root.get("size", []) if isinstance(root, dict) else []
    blocks = root.get("blocks", []) if isinstance(root, dict) else []
    palette = root.get("palette", []) if isinstance(root, dict) else []
    samples = []
    entities = root.get("entities", [])
    unsafe_blocks = {"minecraft:command_block", "minecraft:chain_command_block", "minecraft:repeating_command_block", "minecraft:structure_block", "minecraft:jigsaw"}
    scoped_safe = not entities and not any(item.get("Name") in unsafe_blocks for item in palette if isinstance(item, dict))
    if re.search(r'"(?:Command|SpawnData|SpawnPotentials|EntityTag|BlockEntityTag)"\s*:', json.dumps(root)):
        scoped_safe = False
    if isinstance(blocks, list) and isinstance(palette, list):
        for block in blocks:
            if not isinstance(block, dict) or not isinstance(block.get("pos"), list):
                continue
            state = block.get("state", 0)
            try:
                block_name = palette[int(state)].get("Name", "")
            except (IndexError, TypeError, ValueError, AttributeError):
                block_name = ""
            if block_name and block_name != "minecraft:air" and len(samples) < 12:
                samples.append({"position": block["pos"], "block": block_name})
    return {"size": size if isinstance(size, list) and len(size) == 3 else [], "samples": samples, "scoped_safe": scoped_safe, "entity_count": len(entities)}


def _read_nbt(raw: bytes) -> dict[str, Any]:
    position = 0
    tag_type = raw[position]
    position += 1
    if tag_type != 10:
        raise ValueError("structure file is not an NBT compound")
    name_length = struct.unpack_from(">H", raw, position)[0]
    position += 2 + name_length
    value, position = _read_payload(raw, position, tag_type)
    if not isinstance(value, dict):
        raise ValueError("structure NBT root is invalid")
    return value


def _read_payload(raw: bytes, position: int, tag_type: int, depth: int = 0) -> tuple[Any, int]:
    if depth > 64:
        raise ValueError("structure NBT nesting exceeds 64 levels")
    if tag_type == 1:
        return struct.unpack_from(">b", raw, position)[0], position + 1
    if tag_type == 2:
        return struct.unpack_from(">h", raw, position)[0], position + 2
    if tag_type == 3:
        return struct.unpack_from(">i", raw, position)[0], position + 4
    if tag_type == 4:
        return struct.unpack_from(">q", raw, position)[0], position + 8
    if tag_type == 5:
        return struct.unpack_from(">f", raw, position)[0], position + 4
    if tag_type == 6:
        return struct.unpack_from(">d", raw, position)[0], position + 8
    if tag_type == 7:
        length = struct.unpack_from(">i", raw, position)[0]
        if length < 0 or position + 4 + length > len(raw):
            raise ValueError("invalid NBT byte array length")
        return raw[position + 4:position + 4 + length], position + 4 + length
    if tag_type == 8:
        length = struct.unpack_from(">H", raw, position)[0]
        end = position + 2 + length
        if end > len(raw):
            raise ValueError("invalid NBT string length")
        return raw[position + 2:end].decode("utf-8"), end
    if tag_type == 9:
        child_type = raw[position]
        length = struct.unpack_from(">i", raw, position + 1)[0]
        if not 0 <= length <= min(262144, len(raw) - position - 5):
            raise ValueError("invalid or excessive NBT list length")
        position += 5
        values = []
        for _ in range(max(0, length)):
            value, position = _read_payload(raw, position, child_type, depth + 1)
            values.append(value)
        return values, position
    if tag_type == 10:
        result: dict[str, Any] = {}
        while raw[position] != 0:
            child_type = raw[position]
            position += 1
            name_length = struct.unpack_from(">H", raw, position)[0]
            position += 2
            name = raw[position:position + name_length].decode("utf-8")
            position += name_length
            result[name], position = _read_payload(raw, position, child_type, depth + 1)
        return result, position + 1
    if tag_type in {11, 12}:
        length = struct.unpack_from(">i", raw, position)[0]
        width = 4 if tag_type == 11 else 8
        end = position + 4 + max(0, length) * width
        if length < 0 or end > len(raw):
            raise ValueError("invalid NBT array length")
        return [], end
    raise ValueError(f"unsupported NBT tag type {tag_type}")


def _safe_name(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("_.-").lower()
    return value[:48] or "structure"


def _default_pack_meta() -> bytes:
    return json.dumps({"pack": {"description": "Minecraft God structure templates", "min_format": [107, 1], "max_format": [107, 1]}}, separators=(",", ":")).encode("utf-8")


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for child in sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if child.is_file() or child.is_symlink():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    path.rmdir()
