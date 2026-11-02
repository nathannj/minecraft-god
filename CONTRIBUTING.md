# Contributing

Start with the [offline demo](README.md#try-it-without-minecraft) and [architecture](docs/architecture.md). Small, reproducible improvements are easiest to review. For a large feature, explain a concrete player scenario before implementing it.

## Development loop

Use Python 3.11+ and Java 25+:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install ".[dev]"
.\paper-plugin\gradlew.bat -p paper-plugin build --no-daemon
.\.venv\Scripts\python.exe -m pytest -q
```

On Linux/macOS, use `.venv/bin/python` and `bash paper-plugin/gradlew -p paper-plugin build --no-daemon`. Python-only tests work without Java; the Java/Python event contract test is skipped until Gradle generates its payload fixture. CI runs that contract after building Java.

## What makes a useful change

- Describe the trigger, old behavior, and expected new behavior.
- Add regression coverage for real defects and changed boundaries. Prefer the actual routing/HTTP/RCON paths over a test that only restates a helper implementation.
- Keep runtime data outside source and installed package directories. Tests must use temporary roots.
- Preserve UUID ownership, durable event delivery, and restart behavior when adding tools or events.
- Keep event schema changes coordinated between Java and Python. Update the producer contract test and validation together.
- Treat player text, books, signs, downloaded structures, and model output as data rather than operational instructions.
- Explain verification honestly: an offline replay, a disposable server check, and a human playtest establish different things.

Do not include real chat, captures, worlds, tokens, personal absolute paths, or unredacted logs. Use synthetic fixtures. Follow the [security reporting guidance](SECURITY.md) for vulnerabilities.

## Before opening a pull request

```powershell
py -3 scripts/check_release.py
py -3 -m build
py -3 scripts/check_wheel.py
.\scripts\check-powershell.ps1
```

The PowerShell check is Windows-specific and does not start or stop any processes. For live gameplay changes, run the disposable server verifier and describe any manual playtesting. Be respectful in issues and reviews; critique behavior and code rather than contributors.
