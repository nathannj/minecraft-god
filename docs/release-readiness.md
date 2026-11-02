# Release evidence and remaining work

This is a public-repository candidate, not a claim of perfection. The release-hardening pass was verified locally on **12 October 2026**, on Windows with Python 3.12 and Java 25.

## Verified locally

| Check | Evidence |
| --- | --- |
| Python regression suite | **147 tests passed**, including authentication, identity/path boundaries, objective routing, imports, camera locking, budgets, backups, and restore validation. |
| Java build | Pinned Gradle 9.1.0 / Paper API 26.2 build 133 compiled successfully. |
| Durable delivery | Real HTTP tests cover failure/retry, shutdown, restart, acknowledgement, urgency, rejected payloads, and authentication failures. |
| Event contract | Java-generated payloads pass the Python HTTP boundary; dimensional block watches match only the correct world. |
| Clean source setup | Installer succeeded from a separate copy with no game configuration. It reused an available Java 25 runtime and verified cached downloads where present. A new EULA remained unaccepted until an existing acceptance file was supplied for verification. |
| Disposable Paper world | **11 live checks passed** on Paper 26.2 build 133, including exact block batches, footprint scans, rejected oversized requests, template rotation, and two SpectatorCamera 2.0.9 PNG views. |
| Codex protocol | Real CLI 0.151.0 app-server initialization, read-only thread creation, and the scoped Minecraft MCP inventory passed without starting a model turn. |
| Distributable package | Wheel installed into an empty environment and passed the offline demo, diagnostics, packaged-prompt import, and MCP handshake outside the source tree. |
| Script and static checks | PowerShell parsing, process-boundary tests, preserved settings, and Ruff checks passed. |
| Dependency audit | No known vulnerabilities reported for the audited Python tooling after updating the isolated validation environment's pip. The local project itself is not a PyPI advisory record. The unused legacy Node renderer and its vulnerable dependency tree were removed from the repository. |

These observations establish specific functionality on one machine. They do not establish multiplayer capacity, model response latency, narrative quality, or an operating-system security boundary. Camera test images are genuine renderer output from a small disposable fixture, not showcase gameplay.

## Reproduce the checks

### Multiplayer addition, 28 October 2026

- **163 Python tests passed**, plus Java build/outbox/event-contract tests, Ruff, PowerShell and release checks.
- Paper 26.2 build 133 loaded Simple Voice Chat 2.6.27, XaeroTracker 1.4.0 and the AI hearing integration together. The UDP voice server started and shortcut/preference commands registered; 11 disposable-server checks passed.
- A separate test-only Paper plugin encoded a spoken fixture with the native Opus codec and fed the production microphone/recipient callbacks. The authenticated upload and real CPU recognizer produced: “Bob, meet me at the temple. God, can you guide us home?” The event retained Bob as a listener three blocks away and routed to the speaker/listener gods only. No model turns or real microphones were used.
- All four downloaded client mods matched the pack checksums; their declared dependencies, bundled XaeroLib, Fabric version and installed launcher profile were checked.
- A two-person microphone/map/waypoint playtest and remote UDP connectivity remain unverified. The dedicated game Codex login was absent on this installation; an interactive login is required before the normal launcher can run AI decisions.

See the [multiplayer guide](multiplayer.md) for the added verification commands.

```powershell
py -3 -m pip install ".[dev,audit]"
.\paper-plugin\gradlew.bat -p paper-plugin build --no-daemon
py -3 -m pytest -q
py -3 -m ruff check god_bridge tests scripts
.\scripts\check-powershell.ps1
py -3 scripts/check_release.py
py -3 -m build
py -3 scripts/check_wheel.py
py -3 -m god_bridge.codex_runtime protocol-check
py -3 -m god_bridge.verify_server --camera
```

The server verifier requires a completed setup and an already accepted Minecraft EULA. Its private report and console log are written under `.tools/paper-verification-*`. Protocol checks may establish service connections, but do not submit a model turn. The pure offline demo needs no network at all.

## Human work before the first public release

- [ ] Run a full fresh-machine installation with the documented versions, including first-time Java bootstrap and game-runtime login.
- [ ] Play a complete solo story: pray, receive a concrete objective, move between dimensions, complete it without another prayer, and verify the god responds correctly.
- [ ] Play with two people: verify private messages, separate memories, protected builds, permissioned interventions, reconnects, and restarts.
- [ ] Test a disconnect or exhausted model allowance during a decision, then verify recovery without repeating a confirmed world action.
- [ ] Measure response times and token use on declared hardware; decide sensible advertised capacity from those results.
- [ ] Record a concise live gameplay demonstration and add representative screenshots with participant consent.
- [ ] Review source/history and license/attribution, create the GitHub repository, configure its security/branch settings, and run the workflows there.

The GitHub workflows are checked in but have not run on GitHub for this repository. No public remote, tag, or release is created by this local preparation pass. Existing live worlds and login credentials were not replaced by the verification runs.

## Next engineering improvements

The largest remaining design work is maintainability of the bridge/controller orchestration, explicit retention policies for durable history, larger multiplayer load tests, and broader host isolation. Treat those as scoped follow-up changes with measured evidence rather than reasons to advertise the prototype as production-ready.
