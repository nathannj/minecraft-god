# Security and privacy

Minecraft God is an experimental local game integration. Operate it with trusted players, a world you can restore, and a dedicated game login. It is not an internet-facing service or a containment system for hostile model-generated code.

## Boundaries

- The event hub binds to loopback. Live reads and writes require a generated bearer token. Payloads have size limits, typed identities, finite coordinates, and a versioned event contract.
- Every player god receives a UUID-scoped bridge. Supported mutations pass command, ownership, player, and protected-region checks. Invalid configured UUIDs fail closed.
- The controller uses an isolated Codex home, disables personal plugins/apps and shell/browser tools, requests a read-only sandbox, and checks that the MCP inventory exactly matches the Minecraft bridge before model work.
- RCON and the Paper plugin remain privileged local components. Whoever controls their credential files, source, executable, or configuration controls the world.
- The camera is global within Paper and serialized across bridge processes. Captures remain private runtime data.

A model can still make a poor narrative decision, misinterpret a player, or request an allowed action with unwanted effects. Shared-world physics and indirect consequences are broader than the command checks. Stop the controller and restore a backup if needed. Do not run this under an account containing unrelated sensitive working files when using it with untrusted input; a dedicated OS account or isolated machine provides a stronger boundary than application settings alone.

## Data sent to the model

Relevant chat/prayers, player identifiers, compact telemetry, story memory, tool results, and requested camera images are included in model context. A private in-game prayer is private from other players, **not** from the local operator or the model service. Explain this to participants before playing. No general analytics service is added by this project; downloaded components and the model service have their own behavior and terms.

Local logs, player histories, screenshots, worlds, credentials, and Codex sessions must not be committed or attached to public bug reports. `scripts/check_release.py` scans publishable files and reachable history for common key patterns and the current local game secrets, but it cannot recognize every form of private information. Review the final files yourself and enable GitHub secret scanning/push protection where available.

## Reporting a vulnerability

Use the repository's **Security → Report a vulnerability** option when private reporting is enabled. If it is unavailable, open a minimal issue asking for a private contact method; do not publish credentials, exploit details, or player data. This project does not promise a response SLA or a bounty.

If a real credential was published, revoke or rotate it first. Removing the file from the latest commit does not remove it from Git history or previous downloads.
