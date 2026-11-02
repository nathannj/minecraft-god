"""Install the wheel in an empty environment and exercise it outside the repo."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import venv

ROOT = Path(__file__).resolve().parents[1]


def main():
    wheels = sorted((ROOT / "dist").glob("minecraft_god_bridge-*.whl"))
    if len(wheels) != 1:
        raise SystemExit("Build exactly one release wheel in dist first")
    with tempfile.TemporaryDirectory(prefix="minecraft-god-wheel-") as directory:
        root = Path(directory)
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        clean_env = {key: value for key, value in os.environ.items() if not key.startswith("MINECRAFT_") and key != "PYTHONPATH"}
        def run(*arguments, stdin=None):
            result = subprocess.run([str(python), "-I", *arguments], cwd=root, env=clean_env, input=stdin, capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise RuntimeError(result.stderr or result.stdout)
            return result.stdout
        run("-m", "pip", "install", "--no-index", "--no-deps", str(wheels[0]))
        demo = json.loads(run("-m", "god_bridge.demo", "--json"))
        assert demo["milestones"] == 1 and demo["model_calls"] == 0
        checks = json.loads(run("-m", "god_bridge.doctor", "--offline", "--json"))
        assert all(check["ok"] for check in checks)
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        replies = run("-m", "god_bridge.mcp_server", "--dry-run", "--no-events", stdin="".join(json.dumps(request) + "\n" for request in requests))
        decoded = [json.loads(line) for line in replies.splitlines()]
        assert decoded[0]["result"]["serverInfo"]["version"] == "0.1.0"
        assert "observe_world" in {tool["name"] for tool in decoded[1]["result"]["tools"]}
    print("Installed wheel passed: offline demo, diagnostics, packaged prompt, and MCP handshake outside the source tree.")


if __name__ == "__main__":
    main()
