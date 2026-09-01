$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

# This deliberately targets only the controller wrapper and its child Codex
# app-server processes. Paper and the event hub are sibling processes and are
# left running, so connected players remain in the world.
$controller = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match 'god_bridge\.controller' -and $_.CommandLine -match 'python|py\.exe'
} | Select-Object -First 1
if ($controller) {
    & taskkill.exe /PID $controller.ProcessId /T /F | Out-Null
    Start-Sleep -Milliseconds 500
}

$env:MINECRAFT_EVENT_BASE_URL = "http://127.0.0.1:8765"
$env:MINECRAFT_EVENTS_FILE = Join-Path $Root "state\events.jsonl"
$env:MINECRAFT_LEDGER_FILE = Join-Path $Root "state\god_ledger.sqlite3"
$env:MINECRAFT_PROFILE_DIR = Join-Path $Root "state\players"
$env:MINECRAFT_MAX_GODS = "32"
$env:MINECRAFT_GOD_READY_FILE = Join-Path $Root "logs\god-controller.ready"
$env:MINECRAFT_GOD_LOG_DIR = Join-Path $Root "logs"
Remove-Item -LiteralPath $env:MINECRAFT_GOD_READY_FILE -Force -ErrorAction SilentlyContinue

$godOut = Join-Path $Root "logs\god-controller.out.log"
$godErr = Join-Path $Root "logs\god-controller.err.log"
$python = (Get-Command py -ErrorAction Stop).Source
Start-Process -FilePath $python -ArgumentList @("-3", "-m", "god_bridge.controller") -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput $godOut -RedirectStandardError $godErr | Out-Null

for ($i = 0; $i -lt 60; $i++) {
    if (Test-Path -LiteralPath $env:MINECRAFT_GOD_READY_FILE) {
        Write-Host "God controller restarted. Paper and the event hub were left running."
        exit 0
    }
    Start-Sleep -Milliseconds 500
}
throw "The god controller did not become ready. Check logs\god-controller.err.log."
