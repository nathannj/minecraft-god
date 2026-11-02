$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$files = @(Get-ChildItem -LiteralPath $Root -Filter '*.ps1') + @(Get-ChildItem -LiteralPath (Join-Path $Root 'scripts') -Filter '*.ps1')
foreach ($file in $files) {
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$null, [ref]$parseErrors) | Out-Null
    if ($parseErrors) { throw "$($file.Name): $parseErrors" }
}
. (Join-Path $Root 'scripts\runtime.ps1')
$inside = [pscustomobject]@{ CommandLine = 'python.exe "' + (Join-Path $Root 'run_controller.py') + '"' }
$outside = [pscustomobject]@{ CommandLine = 'python.exe "C:\OtherGame\run_controller.py"' }
$legacy = [pscustomobject]@{ CommandLine = 'python.exe -m god_bridge.controller' }
if (-not (Test-ProjectRuntimeProcess $inside)) { throw 'Project controller did not match.' }
if ((Test-ProjectRuntimeProcess $outside) -or (Test-ProjectRuntimeProcess $legacy)) { throw 'A process from another project matched.' }
$env:MINECRAFT_MAX_GODS = '7'
Initialize-GodEnvironment
if ($env:MINECRAFT_MAX_GODS -ne '7') { throw 'Launcher overwrote the configured god limit.' }
Write-Host 'PowerShell checks passed: syntax, project process boundaries, and user settings.'
