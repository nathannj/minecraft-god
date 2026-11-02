import hashlib
import json
import sqlite3
import zipfile

import pytest

from god_bridge.maintenance import create_backup, restore_backup


def test_backup_restore_preserves_world_story_and_sqlite(tmp_path):
    source = tmp_path / "game"
    (source / "world").mkdir(parents=True)
    (source / "world/level.dat").write_bytes(b"example world")
    (source / "state").mkdir()
    (source / "state/story.json").write_text('{"vow":"remember"}')
    connection = sqlite3.connect(source / "state/ledger.sqlite3")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE example(value TEXT)")
    connection.execute("INSERT INTO example VALUES ('committed')")
    connection.commit()
    output = tmp_path / "backup.zip"
    try:
        manifest = create_backup(source, output)
    finally:
        connection.close()
    restored = tmp_path / "restored"
    assert restore_backup(output, restored) == len(manifest["files"])
    assert (restored / "world/level.dat").read_bytes() == b"example world"
    connection = sqlite3.connect(restored / "state/ledger.sqlite3")
    try:
        assert connection.execute("SELECT value FROM example").fetchone()[0] == "committed"
    finally:
        connection.close()
    with pytest.raises(ValueError, match="new directory"):
        restore_backup(output, restored)


@pytest.mark.parametrize("name", ["state/../../escape", "state/C:/escape", "state/..\\escape", "server/password"])
def test_restore_rejects_unsafe_paths_before_creating_destination(tmp_path, name):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(name, b"payload")
        output.writestr("backup-manifest.json", json.dumps({"format": 1, "files": {name: hashlib.sha256(b"payload").hexdigest()}}))
    destination = tmp_path / "destination"
    with pytest.raises(ValueError, match="unsafe|inventory"):
        restore_backup(archive, destination)
    assert not destination.exists()


def test_restore_rejects_corruption_before_writing(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("state/story.json", b"changed")
        output.writestr("backup-manifest.json", json.dumps({"format": 1, "files": {"state/story.json": "0" * 64}}))
    with pytest.raises(ValueError, match="checksum"):
        restore_backup(archive, tmp_path / "destination")
    assert not (tmp_path / "destination").exists()
