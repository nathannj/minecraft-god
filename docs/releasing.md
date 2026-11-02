# Release procedure

## Prepare the repository

1. Review [release evidence](release-readiness.md), [security](../SECURITY.md), and the publication diff. Confirm asset/code ownership and the chosen ISC license are appropriate for your contributions.
2. Run the tests, Java build, clean-wheel check, PowerShell checks, and `python scripts/check_release.py`. Inspect the current tree and reachable history for private data beyond the automated patterns.
3. Use a clean checkout for setup and disposable-world verification. Record exact versions and results. Never test a destructive scenario against a valued world.
4. Complete the human playtest checklist. Record a short real gameplay clip and capture representative images with participant consent. Do not substitute the synthetic replay for live evidence.
5. Create the GitHub repository under your chosen account, add its remote, and push only reviewed source. No repository URL is assumed in this project. Set a concise description, relevant topics, and the README as the landing page.
6. Enable private vulnerability reporting, dependency alerts, and available secret/push protection. Require the Verify workflow for protected branches. Run it on GitHub and inspect its result; a checked-in workflow has not necessarily run.

Suggested description: “Persistent personal AI gods for Minecraft: private prayers, observable quests, scoped world actions, and a shared pantheon.”

## Build a version

Keep the Python project version, Gradle plugin version, MCP server version, controller client version, and wheel smoke-check expectation aligned. Update `CHANGELOG.md`, dependency pins, and the release evidence. Build with:

```powershell
py -3 -m pip install ".[dev]"
.\paper-plugin\gradlew.bat -p paper-plugin clean build --no-daemon
py -3 -m pytest -q
py -3 scripts/check_release.py --tag v0.1.0
py -3 -m build
py -3 scripts/check_wheel.py
```

Pushing a `v*` tag runs the [draft release workflow](../.github/workflows/release.yml). It checks that the tag matches the package version, reruns verification, builds the wheel/source package/bridge JAR, and creates checksums. It creates a **draft** release for review; it does not publish to PyPI or automatically make the release public.

Inspect the assets and release notes, add the verified compatibility/limitations, then publish the draft when ready. Never upload the whole working directory: ignored runtime files are intentionally private.

## Before calling a release stable

Use measured evidence: several fresh installs, repeatable end-to-end gameplay, recovery from failures, and multiplayer behavior on declared hardware. A green unit-test run cannot establish narrative quality, response latency under load, or universal plugin compatibility.
