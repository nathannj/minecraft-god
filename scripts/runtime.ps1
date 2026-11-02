# Shared by the project launch/restart/stop scripts. $Root is the checkout.
function Test-ProjectRuntimeProcess {
    param($Process)
    $command = [string]$Process.CommandLine
    if (-not $command) { return $false }
    foreach ($relative in @('run_controller.py', 'run_event_hub.py', 'run_bridge.py',
            'start-minecraft-god.ps1', 'launch-minecraft-god.cmd',
            'watch-god-session.ps1', 'watch-god-session.cmd', 'server\paper.jar')) {
        $absolute = Join-Path $Root $relative
        if ($command -match ('(?i)(?:^|[\s"''])' + [regex]::Escape($absolute) + '(?:$|[\s"''])')) {
            return $true
        }
    }
    return $false
}

function Set-DefaultEnvironment {
    param([string]$Name, [string]$Value)
    if (-not [Environment]::GetEnvironmentVariable($Name, 'Process')) {
        [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
    }
}

function Initialize-GodEnvironment {
    $env:MINECRAFT_GOD_HOME = $Root
    $env:MINECRAFT_RCON_PASSWORD_FILE = Join-Path $Root 'server\rcon-password.txt'
    $env:MINECRAFT_EVENT_TOKEN_FILE = Join-Path $Root 'server\event-token.txt'
    $env:MINECRAFT_EVENTS_FILE = Join-Path $Root 'state\events.jsonl'
    $env:MINECRAFT_LEDGER_FILE = Join-Path $Root 'state\god_ledger.sqlite3'
    $env:MINECRAFT_PROFILE_DIR = Join-Path $Root 'state\players'
    $env:MINECRAFT_GOD_READY_FILE = Join-Path $Root 'logs\god-controller.ready'
    $env:MINECRAFT_GOD_CONTROLLER_LOCK_FILE = Join-Path $Root 'logs\god-controller.lock'
    Set-DefaultEnvironment 'MINECRAFT_MAX_GODS' '2'
    Set-DefaultEnvironment 'MINECRAFT_GOD_IDLE_SECONDS' '120'
    Set-DefaultEnvironment 'MINECRAFT_GOD_STORY_INTERVAL_SECONDS' '180'
    Set-DefaultEnvironment 'MINECRAFT_GOD_MAX_THREAD_TURNS' '12'
    Set-DefaultEnvironment 'MINECRAFT_GOD_LOG_MAX_BYTES' '26214400'
    Set-DefaultEnvironment 'MINECRAFT_EVENT_BASE_URL' 'http://127.0.0.1:8765'
    Set-DefaultEnvironment 'MINECRAFT_JAVA_MIN_MEMORY' '1G'
    Set-DefaultEnvironment 'MINECRAFT_JAVA_MAX_MEMORY' '4G'
    Set-DefaultEnvironment 'MINECRAFT_VOICE_ENABLED' '1'
    Set-DefaultEnvironment 'MINECRAFT_VOICE_MODEL' 'base.en'
    Set-DefaultEnvironment 'MINECRAFT_VOICE_LANGUAGE' 'en'
}
