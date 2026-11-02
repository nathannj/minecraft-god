"""Tests must never read or mutate the developer's live game profiles."""
import pytest


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("MINECRAFT_GOD_HOME", str(tmp_path))
