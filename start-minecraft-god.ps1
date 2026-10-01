$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServerDir = Join-Path $Root "server"
Set-Location $Root

$paperJar = Join-Path $ServerDir "paper.jar"
$eulaFile = Join-Path $Root "eula.txt"
$pluginJar = Join-Path $Root "plugins\MinecraftGodBridge.jar"
$javaPathFile = Join-Path $ServerDir "java-path.txt"
if (-not (Test-Path $paperJar) -or -not (Test-Path $pluginJar)) {
    throw "The server is not set up yet. Run .\setup-minecraft-god.ps1 first."
}
if (-not (Test-Path $eulaFile) -or -not ((Get-Content -Raw -LiteralPath $eulaFile) -match "eula=true")) {
    throw "The Minecraft EULA is not accepted. Run .\setup-minecraft-god.ps1 -AcceptEula first."
}
$javaExe = if (Test-Path $javaPathFile) { (Get-Content -Raw -LiteralPath $javaPathFile).Trim() } else { (Get-Command java).Source }
if (-not (Test-Path $javaExe)) { throw "The configured Java runtime was not found. Run setup again." }

$env:MINECRAFT_RCON_PASSWORD_FILE = Join-Path $ServerDir "rcon-password.txt"
$env:MINECRAFT_PLAYER = (Get-Content -Raw -LiteralPath (Join-Path $ServerDir "god-player.txt")).Trim()
$env:MINECRAFT_EVENTS_FILE = Join-Path $Root "state\events.jsonl"
$env:MINECRAFT_LEDGER_FILE = Join-Path $Root "state\god_ledger.sqlite3"
$env:MINECRAFT_PROFILE_DIR = Join-Path $Root "state\players"
$env:MINECRAFT_MAX_GODS = "32"
$env:MINECRAFT_GOD_IDLE_SECONDS = "120"
$env:MINECRAFT_GOD_STORY_INTERVAL_SECONDS = "180"
$env:MINECRAFT_GOD_MAX_THREAD_TURNS = "12"
$env:MINECRAFT_GOD_LOG_MAX_BYTES = "26214400"
$env:MINECRAFT_EVENT_BASE_URL = "http://127.0.0.1:8765"
$npmCodex = Join-Path $env:APPDATA "npm\codex.cmd"
if (Test-Path $npmCodex) { $env:CODEX_CLI_PATH = $npmCodex }
$readyFile = Join-Path $Root "logs\god-controller.ready"
Remove-Item -LiteralPath $readyFile -Force -ErrorAction SilentlyContinue
$env:MINECRAFT_GOD_READY_FILE = $readyFile
$env:MINECRAFT_GOD_CONTROLLER_LOCK_FILE = Join-Path $Root "logs\god-controller.lock"

$hubOut = Join-Path $Root "logs\event-hub.out.log"
$hubErr = Join-Path $Root "logs\event-hub.err.log"
$godOut = Join-Path $Root "logs\god-controller.out.log"
$godErr = Join-Path $Root "logs\god-controller.err.log"

$hub = Start-Process -FilePath (Get-Command py).Source -ArgumentList @("-3", "-m", "god_bridge.event_hub") -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput $hubOut -RedirectStandardError $hubErr -PassThru
try {
    $ready = $false
    for ($i = 0; $i -lt 40; $i++) {
        try { Invoke-WebRequest -Uri "http://127.0.0.1:8765/healthz" -UseBasicParsing -TimeoutSec 1 | Out-Null; $ready = $true; break } catch { Start-Sleep -Milliseconds 250 }
    }
    if (-not $ready) { throw "The event hub did not start. Check logs\event-hub.err.log." }

    $controller = Start-Process -FilePath (Get-Command py).Source -ArgumentList @("-3", "-m", "god_bridge.controller") -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput $godOut -RedirectStandardError $godErr -PassThru
    try {
        $godReady = $false
        for ($i = 0; $i -lt 180; $i++) {
            if (Test-Path $readyFile) { $godReady = $true; break }
            if ($controller.HasExited) { throw "The god controller stopped. Check logs\god-controller.err.log." }
            Start-Sleep -Milliseconds 500
        }
        if (-not $godReady) { throw "The god controller did not become ready. Check logs\god-controller.err.log." }
        Write-Host "God controller is waiting for players to join. Each player receives a persistent personal god."
        Write-Host "Starting Paper server. Connect from Minecraft Java Edition to localhost:25565."
        & $javaExe -Xms4G -Xmx4G -jar $paperJar --nogui
    } finally {
        if ($controller -and -not $controller.HasExited) { Stop-Process -Id $controller.Id -Force -ErrorAction SilentlyContinue }
        # Also clean up a controller that may have been restarted independently
        # while this launcher kept the Paper server alive.
        Get-CimInstance Win32_Process | Where-Object {
            $_.CommandLine -match 'god_bridge\.controller' -and $_.CommandLine -match 'python|py\.exe'
        } | ForEach-Object { & taskkill.exe /PID $_.ProcessId /T /F | Out-Null }
    }
} finally {
    if ($hub -and -not $hub.HasExited) { Stop-Process -Id $hub.Id -Force -ErrorAction SilentlyContinue }
}
