$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
. (Join-Path $Root "scripts\runtime.ps1")

# This deliberately targets only the controller wrapper and its child Codex
# app-server processes. Paper and the event hub are sibling processes and are
# left running, so connected players remain in the world.
$controllers = Get-CimInstance Win32_Process | Where-Object {
    (Test-ProjectRuntimeProcess $_) -and $_.CommandLine -match 'run_controller.py'
} | Sort-Object ProcessId
foreach ($controller in $controllers) {
    & taskkill.exe /PID $controller.ProcessId /T /F | Out-Null
    Start-Sleep -Milliseconds 500
}

Initialize-GodEnvironment
$env:MINECRAFT_GOD_LOG_DIR = Join-Path $Root "logs"
Remove-Item -LiteralPath $env:MINECRAFT_GOD_READY_FILE -Force -ErrorAction SilentlyContinue

$godOut = Join-Path $Root "logs\god-controller.out.log"
$godErr = Join-Path $Root "logs\god-controller.err.log"
$python = (Get-Command py -ErrorAction Stop).Source
Start-Process -FilePath $python -ArgumentList @("-3", ('"' + (Join-Path $Root "run_controller.py") + '"')) -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput $godOut -RedirectStandardError $godErr | Out-Null

for ($i = 0; $i -lt 60; $i++) {
    if (Test-Path -LiteralPath $env:MINECRAFT_GOD_READY_FILE) {
        Write-Host "God controller restarted. Paper and the event hub were left running."
        exit 0
    }
    Start-Sleep -Milliseconds 500
}
throw "The god controller did not become ready. Check logs\god-controller.err.log."
