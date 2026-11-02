[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "Low")]
param()

$ErrorActionPreference = "Stop"
$Root = [System.IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path)).TrimEnd('\')
$PaperJar = Join-Path $Root "server\paper.jar"
$PasswordFile = Join-Path $Root "server\rcon-password.txt"
$LedgerFile = Join-Path $Root "state\god_ledger.sqlite3"
$ReadyFile = Join-Path $Root "logs\god-controller.ready"
$LockFile = Join-Path $Root "logs\god-controller.lock"
. (Join-Path $Root "scripts\runtime.ps1")
Set-Location $Root

function Test-ContainsLiteral {
    param([string]$Text, [string]$Value)
    return $Text -and $Text.IndexOf($Value, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
}

function Test-AiGodProcess {
    param($Process)
    $command = [string]$Process.CommandLine
    if (-not $command) { return $false }
    if (Test-ProjectRuntimeProcess $Process) { return $true }
    if ((Test-ContainsLiteral $command (Join-Path $Root "run_bridge.py"))) { return $true }
    if ((Test-ContainsLiteral $command (Join-Path $Root "start-minecraft-god.ps1"))) { return $true }
    if ((Test-ContainsLiteral $command (Join-Path $Root "launch-minecraft-god.cmd"))) { return $true }
    if ((Test-ContainsLiteral $command (Join-Path $Root "watch-god-session.ps1"))) { return $true }
    if ((Test-ContainsLiteral $command (Join-Path $Root "watch-god-session.cmd"))) { return $true }
    if ((Test-ContainsLiteral $command $PaperJar) -and $command -match '(?i)java(?:\.exe)?') { return $true }
    return $false
}

function Get-AiGodProcesses {
    return @(Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $PID -and (Test-AiGodProcess $_)
    })
}

function Stop-AiGodProcessTrees {
    param([array]$Processes, [string]$Reason)
    if (-not $Processes) { return }

    $candidateIds = @{}
    foreach ($process in $Processes) { $candidateIds[[int]$process.ProcessId] = $true }
    # Kill only the highest matching processes. taskkill /T also handles their
    # descendants, including Codex app servers and MCP bridge children.
    $roots = @($Processes | Where-Object { -not $candidateIds.ContainsKey([int]$_.ParentProcessId) })
    foreach ($process in $roots) {
        $label = "$($process.Name) PID $($process.ProcessId)"
        if ($PSCmdlet.ShouldProcess($label, $Reason)) {
            & taskkill.exe /PID $process.ProcessId /T /F 2>$null | Out-Null
        }
    }
}

$initial = Get-AiGodProcesses
if (-not $initial) {
    Write-Host "No running Minecraft AI God processes were found."
} else {
    Write-Host "Found $($initial.Count) related process(es). Requesting a clean server shutdown..."
}

$paperRunning = @($initial | Where-Object {
    (Test-ContainsLiteral ([string]$_.CommandLine) $PaperJar) -and ([string]$_.CommandLine -match '(?i)java(?:\.exe)?')
})
$rconRequested = $false
if ($paperRunning -and (Test-Path -LiteralPath $PasswordFile)) {
    if ($PSCmdlet.ShouldProcess("Paper server", "save every world and stop through local RCON")) {
        try {
            $python = (Get-Command py -ErrorAction Stop).Source
            $env:MINECRAFT_GOD_STOP_PASSWORD_FILE = $PasswordFile
            $rconCode = @'
import os
from pathlib import Path
from god_bridge.rcon import RconClient

password = Path(os.environ["MINECRAFT_GOD_STOP_PASSWORD_FILE"]).read_text(encoding="utf-8").strip()
client = RconClient("127.0.0.1", 25575, password, timeout=4.0)
client.command("save-all flush")
client.command("stop")
'@
            & $python -3 -c $rconCode
            if ($LASTEXITCODE -ne 0) { throw "RCON helper exited with code $LASTEXITCODE" }
            $rconRequested = $true
            Write-Host "Paper accepted save-all and stop."
        } catch {
            Write-Warning "Clean RCON shutdown was unavailable: $($_.Exception.Message). Forced cleanup will continue."
        } finally {
            Remove-Item Env:\MINECRAFT_GOD_STOP_PASSWORD_FILE -ErrorAction SilentlyContinue
        }
    }
}

if ($rconRequested) {
    # Give Paper time to emit player-quit events and complete its first save
    # phase. These events are audit-only and do not wake the God model.
    Start-Sleep -Seconds 2
}

# Stop AI usage promptly, even if Paper is still finishing its disk flush.
$aiProcesses = @(Get-AiGodProcesses | Where-Object {
    $command = [string]$_.CommandLine
    (Test-ContainsLiteral $command (Join-Path $Root "run_controller.py")) -or
    (Test-ContainsLiteral $command (Join-Path $Root "run_bridge.py"))
})
Stop-AiGodProcessTrees $aiProcesses "stop the God controller, Codex sessions, and MCP bridges"

if ($rconRequested) {
    $deadline = (Get-Date).AddSeconds(20)
    do {
        $paperStillRunning = @(Get-AiGodProcesses | Where-Object {
            (Test-ContainsLiteral ([string]$_.CommandLine) $PaperJar) -and ([string]$_.CommandLine -match '(?i)java(?:\.exe)?')
        })
        if (-not $paperStillRunning) { break }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
}

# This is the guaranteed fallback: launcher, Paper, event hub, watchers, and
# any orphaned God/MCP process still carrying this project's command line.
Stop-AiGodProcessTrees (Get-AiGodProcesses) "stop remaining Minecraft AI God runtime processes"
Start-Sleep -Milliseconds 500

if ($PSCmdlet.ShouldProcess("player ledger", "mark all players offline so the next launch does not resume a God before anyone joins")) {
    try {
        if (Test-Path -LiteralPath $LedgerFile) {
            $python = (Get-Command py -ErrorAction Stop).Source
            $env:MINECRAFT_GOD_STOP_LEDGER_FILE = $LedgerFile
            $ledgerCode = @'
import os
from pathlib import Path
from god_bridge.ledger import GodLedger

ledger = GodLedger(Path(os.environ["MINECRAFT_GOD_STOP_LEDGER_FILE"]))
for player in ledger.players():
    ledger.set_online(player["player_id"], False)
'@
            & $python -3 -c $ledgerCode
            if ($LASTEXITCODE -ne 0) { throw "ledger helper exited with code $LASTEXITCODE" }
        }
    } catch {
        Write-Warning "Could not mark player profiles offline: $($_.Exception.Message)"
    } finally {
        Remove-Item Env:\MINECRAFT_GOD_STOP_LEDGER_FILE -ErrorAction SilentlyContinue
    }
}

foreach ($runtimeFile in @($ReadyFile, $LockFile)) {
    if ((Test-Path -LiteralPath $runtimeFile) -and $PSCmdlet.ShouldProcess($runtimeFile, "remove stale runtime marker")) {
        Remove-Item -LiteralPath $runtimeFile -Force -ErrorAction SilentlyContinue
    }
}

$survivors = Get-AiGodProcesses
if ($survivors -and -not $WhatIfPreference) {
    $details = ($survivors | ForEach-Object { "$($_.Name) PID $($_.ProcessId)" }) -join ", "
    throw "Shutdown was incomplete; related processes remain: $details"
}

if ($WhatIfPreference) {
    Write-Host "Dry run complete. No processes or state were changed."
} else {
    Write-Host "Minecraft AI God is fully stopped. No related server, controller, Codex God, MCP bridge, or event-hub process remains."
}
