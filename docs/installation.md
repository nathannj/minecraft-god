# Installation

The supported full-stack setup is Windows x64. Python's offline tools are portable; the repository includes Windows/Linux CI jobs. Linux/macOS server hosting is a manual integration and is not the supported installer path.

## Prerequisites

- Minecraft Java Edition, client version **26.2**.
- Python **3.11 or newer**, with the Windows `py` launcher.
- Node.js and npm to install **Codex CLI 0.151.0**. The CLI uses a separate game login; a working desktop login alone is not sufficient.
- Internet access for pinned dependencies, Minecraft assets, and live model decisions.
- Enough memory for a 4 GiB server heap plus Python, Codex sessions, camera rendering, and the Minecraft client. Reduce the heap and god limit on smaller machines; no multiplayer capacity claim has been benchmarked.

Java 25+ is required. Setup uses a suitable Java already on PATH or downloads the checksum-pinned Corretto archive. Gradle comes from the checked-in wrapper. Node packages are not used for rendering: the camera is a Paper plugin.

## First installation

Open PowerShell in the downloaded/cloned repository. Read the [Minecraft EULA](https://aka.ms/MinecraftEULA); include `-AcceptEula` only if you accept it.

```powershell
npm install -g @openai/codex@0.151.0
.\setup-minecraft-god.ps1 -PlayerName YourName -AcceptEula
py -3 -m god_bridge.codex_runtime login
py -3 -m god_bridge.doctor
.\start-minecraft-god.ps1
```

Setup without `-AcceptEula` is also supported and leaves a new installation's EULA unaccepted. The installer does not sign in for you or change global MCP registrations. `-SkipCodexCheck` is for installation testing without the CLI; it does not enable live gameplay.

Connect the matching Minecraft client to `localhost:25565`. A joining player's authenticated UUID creates a persistent profile. Use `/pray` for private speech. `/god` is an alias. Ordinary activity is accumulated; important events and registered objective conditions can wake a god automatically.

The legacy `PlayerName` setting supplies a fallback for unscoped administrative tooling. Player gods are assigned from join events, not from that one name.

## Existing checkouts

Setup also installs multiplayer maps, friend tracking, proximity voice and local speech recognition, plus a separate **Minecraft AI God** client profile. Friends can import the generated Modrinth pack. See [multiplayer setup and shortcuts](multiplayer.md).

1. Stop the old launcher and save the world. Very old controller processes launched with `python -m god_bridge.controller` cannot be attributed safely to a checkout; close their original console if necessary. New launchers use absolute project entry points.
2. Make a private backup using the [operations guide](operations.md).
3. Run setup again while the game is stopped. Existing world/profile data and camera configuration are preserved. The installer refreshes the bridge plugin configuration to use the generated event token.
4. Sign in with `py -3 -m god_bridge.codex_runtime login` for the separate game runtime.
5. Run doctor, then the isolated server verification before returning to your world.

The new event service requires bearer authentication for both polling and delivery. An older plugin still sending incompatible payloads will have those events retained in `plugins/MinecraftGodBridge/pending-events.queue.rejected.jsonl`. Review that file locally; do not publish it.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `py` is missing | Install Python with its Windows launcher, then open a new terminal. |
| PowerShell refuses scripts | Use the `.cmd` launchers supplied with the project or follow your organization's execution-policy requirements. |
| Wrong Codex version | Install the pinned version or set `MINECRAFT_CODEX_CLI_PATH` to that executable. The desktop app's CLI override is intentionally ignored. |
| Login missing | Run `py -3 -m god_bridge.codex_runtime login`. Your normal Codex login/config is separate. |
| Model unavailable | Select a model your account can use with `MINECRAFT_GOD_MODEL`; see [configuration](configuration.md). |
| Event service returns 401 | Rerun setup while stopped so the plugin and controller use the same generated token. Do not disable authentication. |
| Port already occupied | Stop the existing project instance. Default ports are 25565, 25575, and 8765. Custom port sets need coordinated manual configuration. |
| Camera fails or times out | Run `py -3 -m god_bridge.verify_server --camera`; inspect its private console log. First render can download assets. Exact block scans still work without images. |
| God stops responding | Check the controller error log, account allowance, configured turn limit, and session summary. Queued work remains for a later allowed turn. |

The automated installer binds new game servers to loopback. Hosting for other machines requires deliberate network/firewall configuration; never expose RCON or the event feed to the internet. Existing `server.properties` network choices are preserved, so an upgrade does not silently change an operator's binding.
