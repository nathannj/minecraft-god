"""Build a pinned Modrinth pack and install an isolated official-launcher profile."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from urllib.request import Request, urlopen
import zipfile

from .runtime import workspace_root

MODS = ("fabric_api", "minimap", "worldmap", "voicechat_client")


def fetch(url: str) -> bytes:
    with urlopen(Request(url, headers={"User-Agent": "MinecraftGodBridge/0.1.0"}), timeout=60) as response:
        return response.read()


def build_pack(root: Path, lock: dict) -> Path:
    files = []
    for key in MODS:
        item = lock[key]
        destination = root / "client" / "mods" / item["filename"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists() or hashlib.sha512(destination.read_bytes()).hexdigest() != item["sha512"]:
            content = fetch(item["url"])
            if hashlib.sha512(content).hexdigest() != item["sha512"]:
                raise ValueError(f"Checksum mismatch: {key}")
            temporary = destination.with_suffix(".download")
            temporary.write_bytes(content)
            temporary.replace(destination)
        raw = destination.read_bytes()
        files.append({"path": "mods/" + item["filename"], "hashes": {"sha512": item["sha512"], "sha1": hashlib.sha1(raw).hexdigest()},
                      "env": {"client": "required", "server": "unsupported"}, "downloads": [item["url"]], "fileSize": len(raw)})
    # Archive only mods previously managed by this installer on an upgrade.
    manifest = root / "client" / "managed-mods.json"
    previous = json.loads(manifest.read_text()) if manifest.exists() else []
    current = [lock[key]["filename"] for key in MODS]
    for name in previous:
        if name in current or Path(name).name != name:
            continue
        old = root / "client" / "mods" / name
        if old.is_file():
            archive = root / "client" / "previous-mods"
            archive.mkdir(exist_ok=True)
            shutil.move(str(old), str(archive / name))
    manifest.write_text(json.dumps(current, indent=2) + "\n")
    index = {"formatVersion": 1, "game": "minecraft", "versionId": "0.1.0", "name": "Minecraft AI God",
             "summary": "Multiplayer maps, waypoints and proximity voice for AI God.", "files": files,
             "dependencies": {"minecraft": lock["minecraft"], "fabric-loader": lock["fabric_loader"]}}
    output = root / "dist" / f'Minecraft-AI-God-{lock["minecraft"]}.mrpack'
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as pack:
        pack.writestr("modrinth.index.json", json.dumps(index, indent=2))
    return output


def install_profile(root: Path, lock: dict, minecraft: Path) -> None:
    version_id = f'fabric-loader-{lock["fabric_loader"]}-{lock["minecraft"]}'
    version = json.loads(fetch(f'https://meta.fabricmc.net/v2/versions/loader/{lock["minecraft"]}/{lock["fabric_loader"]}/profile/json'))
    if version.get("id") != version_id or version.get("inheritsFrom") != lock["minecraft"]:
        raise ValueError("Unexpected Fabric launcher metadata")
    folder = minecraft / "versions" / version_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{version_id}.json").write_text(json.dumps(version, indent=2))
    profiles_path = minecraft / "launcher_profiles.json"
    profiles = json.loads(profiles_path.read_text(encoding="utf-8")) if profiles_path.exists() else {"profiles": {}, "version": 3}
    profile_id = "minecraft-ai-god"
    existing = profiles.setdefault("profiles", {}).get(profile_id, {})
    game_dir = str((root / "client").resolve())
    if existing.get("gameDir") and Path(existing["gameDir"]).resolve() != Path(game_dir):
        raise ValueError("An AI God profile already uses another folder; import the generated mrpack instead")
    profiles["profiles"][profile_id] = {**existing, "name": "Minecraft AI God", "type": "custom", "icon": "Grass",
        "lastVersionId": version_id, "gameDir": game_dir, "javaArgs": "-Xmx3G -XX:+UseG1GC",
        "created": existing.get("created", datetime.now(timezone.utc).isoformat())}
    # Preserve existing profiles, selected installation, accounts and launch settings.
    if profiles_path.exists():
        backup = root / "backups" / ("launcher-profiles-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json")
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(profiles_path, backup)
    temporary = profiles_path.with_suffix(".ai-god.tmp")
    temporary.write_text(json.dumps(profiles, indent=2) + "\n", encoding="utf-8")
    temporary.replace(profiles_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--minecraft-dir", type=Path)
    args = parser.parse_args()
    root = workspace_root()
    lock = json.loads((root / "versions.lock.json").read_text())
    print(f"Friend install pack: {build_pack(root, lock)}")
    if args.install:
        minecraft = args.minecraft_dir or Path(os.environ["APPDATA"]) / ".minecraft"
        install_profile(root, lock, minecraft)
        print("Installed Minecraft AI God profile. Reopen Minecraft Launcher, then select it in Installations.")


if __name__ == "__main__":
    main()
