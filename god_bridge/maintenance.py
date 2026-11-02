"""Back up a stopped game, restore into a new directory, or preview old captures."""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import socket
import time
import zipfile

from .runtime import workspace_root

DATA_DIRECTORIES = ("state", "world", "world_nether", "world_the_end")


@contextmanager
def stopped_game(root: Path):
    from .controller import ControllerLock
    properties = {}
    path = root / "server.properties"
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                properties[key] = value
    for port in {int(properties.get("server-port", "25565")), int(properties.get("rcon.port", "25575")), 8765}:
        with socket.socket() as connection:
            connection.settimeout(0.2)
            if connection.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError("Stop Paper, the controller, and the event hub before backing up or pruning data")
    lock = ControllerLock(root / "logs" / "god-controller.lock")
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def create_backup(root: Path, output: Path) -> dict:
    root, output = root.resolve(), output.resolve()
    sources = [root / name for name in DATA_DIRECTORIES]
    if any(output.is_relative_to(source) for source in sources):
        raise ValueError("backup output must be outside the data being archived")
    if output.exists():
        raise FileExistsError("backup already exists; choose a new filename")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"format": 1, "files": {}}
    # Caller must hold stopped_game for live workspaces. Tests use isolated data.
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for source in sources:
            if not source.exists():
                continue
            if source.is_symlink() or not source.resolve().is_relative_to(root):
                raise ValueError("backup data directories must remain inside the game workspace")
            for path in sorted(source.rglob("*")):
                if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
                    continue
                if path.suffix in {".lock", ".tmp"} or path.name.endswith(("-wal", "-shm")):
                    continue
                name = path.relative_to(root).as_posix()
                digest = hashlib.sha256()
                if path.name.endswith((".sqlite3", ".db")):
                    # A clean SQLite snapshot includes committed WAL data even
                    # after an unclean shutdown left a WAL beside the database.
                    import sqlite3
                    import tempfile
                    with tempfile.TemporaryDirectory() as directory:
                        snapshot = Path(directory) / "snapshot.db"
                        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as original, closing(sqlite3.connect(snapshot)) as copy:
                            original.backup(copy)
                        with snapshot.open("rb") as stream, archive.open(name, "w") as target:
                            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                                digest.update(chunk)
                                target.write(chunk)
                else:
                    with path.open("rb") as stream, archive.open(name, "w") as target:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                            target.write(chunk)
                manifest["files"][name] = digest.hexdigest()
        archive.writestr("backup-manifest.json", json.dumps(manifest, indent=2))
    return manifest


def restore_backup(source: Path, destination: Path) -> int:
    destination = destination.resolve()
    if destination.exists():
        raise ValueError("restore destination must be a new directory; existing games are never overwritten")
    with zipfile.ZipFile(source) as archive:
        manifest = json.loads(archive.read("backup-manifest.json"))
        if manifest.get("format") != 1 or not isinstance(manifest.get("files"), dict):
            raise ValueError("unsupported backup format")
        names = [info.filename for info in archive.infolist()]
        if len(names) != len(set(names)) or set(names) != set(manifest["files"]) | {"backup-manifest.json"}:
            raise ValueError("backup file inventory does not match its manifest")
        for name in manifest["files"]:
            parts = PurePosixPath(name).parts
            if not parts or parts[0] not in DATA_DIRECTORIES or ".." in parts or "\\" in name or ":" in name or PureWindowsPath(name).drive:
                raise ValueError("unsafe backup member path")
            if not (destination / name).resolve().is_relative_to(destination):
                raise ValueError("backup path escapes the restore destination")
            with archive.open(name) as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != manifest["files"][name]:
                    raise ValueError("backup checksum mismatch; no data restored")
        destination.mkdir(parents=True)
        for name in manifest["files"]:
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as stream, target.open("xb") as output:
                shutil.copyfileobj(stream, output)
    return len(manifest["files"])


def cleanup_candidates(root: Path, days: int) -> list[Path]:
    cutoff = time.time() - max(1, days) * 86400
    candidates = list((root / "state/captures").glob("*.png"))
    for pattern in ["god-session-*.log", "god-session-*.jsonl"]:
        candidates.extend((root / "logs").glob(pattern))
    return sorted(path for path in candidates if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve()) and path.stat().st_mtime < cutoff)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    backup = sub.add_parser("backup", help="Archive world and story data after stopping the game")
    backup.add_argument("output", type=Path)
    restore = sub.add_parser("restore", help="Restore verified data into a new, empty location")
    restore.add_argument("archive", type=Path)
    restore.add_argument("destination", type=Path)
    cleanup = sub.add_parser("cleanup", help="Preview old captures and rotated logs; durable story data is excluded")
    cleanup.add_argument("--older-than-days", type=int, default=30)
    cleanup.add_argument("--apply", action="store_true", help="Delete the listed files after stopping the game")
    args = parser.parse_args()
    root = workspace_root()
    if args.action == "restore":
        print(f"Restored {restore_backup(args.archive, args.destination)} files. Copy source files into that directory, run setup, then doctor before starting.")
    elif args.action == "backup":
        with stopped_game(root):
            manifest = create_backup(root, args.output)
        print(f"Backed up {len(manifest['files'])} files to {args.output}. Keep this archive private.")
    else:
        paths = cleanup_candidates(root, args.older_than_days)
        for path in paths:
            print(path.relative_to(root))
        if args.apply:
            with stopped_game(root):
                for path in paths:
                    path.unlink()
        print(f"{'Removed' if args.apply else 'Would remove'} {len(paths)} files. Current logs, event history, profiles, worlds, and credentials are excluded.")


if __name__ == "__main__":
    main()
