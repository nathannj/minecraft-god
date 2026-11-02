# Third-party notices

The [ISC license](LICENSE) applies to this project's original code. It does not relicense downloaded software, Minecraft assets, or third-party services.

| Component | How used | Upstream |
| --- | --- | --- |
| Minecraft Java Edition | Game/client/server assets; not bundled in releases | [Minecraft EULA](https://aka.ms/MinecraftEULA) |
| Paper | Downloaded server and compile-time API | [Paper project](https://github.com/PaperMC/Paper) |
| SpectatorCamera 2.0.9 (Camera For Streaming), by Alexsam1075 | Downloaded, unmodified camera plugin; not bundled in releases | [Modrinth project](https://modrinth.com/plugin/camera-for-streaming) — [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| Gradle | Checked-in wrapper scripts/JAR; distribution downloaded with a pinned checksum | [Gradle license](https://github.com/gradle/gradle/blob/master/LICENSE) — Apache License 2.0 |
| Amazon Corretto | Optional pinned Java bootstrap download | [Corretto 25](https://github.com/corretto/corretto-25) |
| OpenAI Codex | User-installed CLI and model service | [Codex project](https://github.com/openai/codex) |
| Simple Voice Chat | Downloaded server/client plugins; compile-time API | [Upstream](https://github.com/henkelmax/simple-voice-chat) |
| XaeroTracker | Downloaded Paper player-position integration | [Upstream](https://github.com/inf-mc/XaeroTracker) |
| Xaero's Minimap and World Map | Downloaded client mods, including bundled XaeroLib; not redistributed | [Minimap](https://modrinth.com/mod/xaeros-minimap), [World Map](https://modrinth.com/mod/xaeros-world-map) |
| Fabric Loader and API | Downloaded client runtime | [Fabric](https://fabricmc.net/) |
| Faster Whisper, CTranslate2, PyAV and Whisper weights | Optional local speech runtime and downloaded model | [Faster Whisper](https://github.com/SYSTRAN/faster-whisper), [Whisper](https://github.com/openai/whisper) |

The release archives contain this project's Python package and bridge plugin, not Paper, Minecraft assets, camera binaries, credentials, or world saves. The Gradle wrapper is build infrastructure distributed under its upstream license; its original license headers and [full license text](third_party/gradle-LICENSE.txt) are retained.
