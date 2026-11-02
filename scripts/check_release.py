"""Audit publishable files, version alignment, links, and common secret formats."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = [
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{40,}"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(rb"\bAKIA[A-Z0-9]{16}\b"),
]


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="")
    args = parser.parse_args()
    problems = []
    paths = {name for name in git("ls-files", "--cached", "--others", "--exclude-standard", "-z").decode().split("\0") if name}
    prohibited = {"state", "server", "world", "world_nether", "world_the_end", "logs", "plugins", "backups", ".tools", ".codex-runtime"}
    secrets = []
    for path in [ROOT / "server/rcon-password.txt", ROOT / "server/event-token.txt"]:
        if path.exists():
            value = path.read_bytes().strip()
            if len(value) >= 16:
                secrets.append(value)
    def scan(raw, label):
        if any(pattern.search(raw) for pattern in PATTERNS) or any(secret in raw for secret in secrets):
            problems.append(f"Possible credential in {label}; inspect privately")
    for name in sorted(paths):
        path = ROOT / name
        if not path.is_file():
            continue
        if Path(name).parts[0] in prohibited:
            problems.append(f"Runtime data is publishable: {name}")
        scan(path.read_bytes(), name)
        if path.suffix == ".md":
            for target in re.findall(r"\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
                target = target.split("#", 1)[0].split(' "', 1)[0]
                if not target or "://" in target or target.startswith("mailto:"):
                    continue
                if not (path.parent / target).exists():
                    problems.append(f"Broken relative link in {name}: {target}")
    # Scan reachable historical file contents too: .gitignore cannot remove an
    # already-committed credential. Print identifiers, never matched values.
    objects = git("rev-list", "--objects", "--all").decode().splitlines()
    for row in objects:
        object_id, _, name = row.partition(" ")
        if name and git("cat-file", "-t", object_id).strip() == b"blob":
            scan(git("cat-file", "blob", object_id), f"history {object_id[:12]} ({name})")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    version = project["version"]
    gradle = (ROOT / "paper-plugin/build.gradle.kts").read_text()
    lock = json.loads((ROOT / "versions.lock.json").read_text())
    if f'version = "{version}"' not in gradle or lock["paper_api"] not in gradle:
        problems.append("Python/plugin version or pinned Paper API mismatch")
    if args.tag and args.tag != "v" + version:
        problems.append("Release tag does not match package version")
    for required in ["LICENSE", "SECURITY.md", "CONTRIBUTING.md", "CHANGELOG.md", "templates/SpectatorCamera.yml", "paper-plugin/gradle/wrapper/gradle-wrapper.jar"]:
        if not (ROOT / required).is_file():
            problems.append("Missing release file: " + required)
    if problems:
        raise SystemExit("\n".join(problems))
    print(f"Release checks passed: {len(paths)} publishable paths, reachable history, local links, and version alignment.")
    print("This targeted scan is not a guarantee that every form of private data is absent.")


if __name__ == "__main__":
    main()
