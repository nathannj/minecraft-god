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

. (Join-Path $Root "scripts\runtime.ps1")
Initialize-GodEnvironment
& py -3 -m god_bridge.codex_runtime status
if ($LASTEXITCODE -ne 0) { throw "Sign in to the dedicated game runtime: py -3 -m god_bridge.codex_runtime login" }
if (-not (Test-Path -LiteralPath $env:MINECRAFT_EVENT_TOKEN_FILE)) { throw "Event token is missing. Run setup again." }
$existing = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $PID -and (Test-ProjectRuntimeProcess $_) -and $_.CommandLine -match 'run_controller.py|run_event_hub.py|paper.jar'
})
if ($existing) { throw "This project's runtime is already running. Use the stop or restart script first." }
New-Item -ItemType Directory -Force -Path (Join-Path $Root "logs") | Out-Null
$readyFile = Join-Path $Root "logs\god-controller.ready"
Remove-Item -LiteralPath $readyFile -Force -ErrorAction SilentlyContinue
$env:MINECRAFT_GOD_READY_FILE = $readyFile
$env:MINECRAFT_GOD_CONTROLLER_LOCK_FILE = Join-Path $Root "logs\god-controller.lock"

$hubOut = Join-Path $Root "logs\event-hub.out.log"
$hubErr = Join-Path $Root "logs\event-hub.err.log"
$godOut = Join-Path $Root "logs\god-controller.out.log"
$godErr = Join-Path $Root "logs\god-controller.err.log"

$hubPython = (Get-Command py).Source
$hubArguments = @("-3", ('"' + (Join-Path $Root "run_event_hub.py") + '"'))
if ($env:MINECRAFT_VOICE_ENABLED -eq '1') {
    $hubPython = Join-Path $Root '.tools/voice-venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $hubPython)) { throw 'Run .\setup-multiplayer.ps1 to install local speech recognition.' }
    $hubArguments = @(('"' + (Join-Path $Root "run_event_hub.py") + '"'))
}
$hub = Start-Process -FilePath $hubPython -ArgumentList $hubArguments -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput $hubOut -RedirectStandardError $hubErr -PassThru
try {
    $ready = $false
    for ($i = 0; $i -lt 40; $i++) {
        if ($hub.HasExited) { throw "The event hub stopped. Check logs\event-hub.err.log." }
        try { Invoke-WebRequest -Uri "http://127.0.0.1:8765/healthz" -UseBasicParsing -TimeoutSec 1 | Out-Null; $ready = $true; break } catch { Start-Sleep -Milliseconds 250 }
    }
    if (-not $ready) { throw "The event hub did not start. Check logs\event-hub.err.log." }

    $controller = Start-Process -FilePath (Get-Command py).Source -ArgumentList @("-3", ('"' + (Join-Path $Root "run_controller.py") + '"')) -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput $godOut -RedirectStandardError $godErr -PassThru
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
        & $javaExe "-Xms$env:MINECRAFT_JAVA_MIN_MEMORY" "-Xmx$env:MINECRAFT_JAVA_MAX_MEMORY" -jar $paperJar --nogui
    } finally {
        if ($controller -and -not $controller.HasExited) { & taskkill.exe /PID $controller.Id /T /F 2>$null | Out-Null }
        # Also clean up a controller that may have been restarted independently
        # while this launcher kept the Paper server alive.
        Get-CimInstance Win32_Process | Where-Object {
            (Test-ProjectRuntimeProcess $_) -and $_.CommandLine -match 'run_controller.py'
        } | ForEach-Object { & taskkill.exe /PID $_.ProcessId /T /F | Out-Null }
    }
} finally {
    if ($hub -and -not $hub.HasExited) { & taskkill.exe /PID $hub.Id /T /F 2>$null | Out-Null }
}
