param(
    [ValidatePattern('^[A-Za-z0-9_]{1,16}$')][string]$PlayerName = "Player",
    [switch]$AcceptEula,
    [switch]$SkipCodexCheck
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
. (Join-Path $Root "scripts\runtime.ps1")
$running = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $PID -and (Test-ProjectRuntimeProcess $_)
})
if ($running) { throw "Stop this project's runtime before installing or updating files." }
$ServerDir = Join-Path $Root "server"
$ToolsDir = Join-Path $Root ".tools"
$pluginsDir = Join-Path $Root "plugins"
$versions = Get-Content -Raw (Join-Path $Root "versions.lock.json") | ConvertFrom-Json
$UserAgent = "MinecraftGodBridge/0.1.0 (local installer)"

& py -3 -c "import sys; assert sys.version_info >= (3,11), 'Python 3.11 or newer is required'"
if ($LASTEXITCODE -ne 0) { throw "Install Python 3.11 or newer with its Windows launcher." }
if (-not $SkipCodexCheck) {
    & py -3 -m god_bridge.codex_runtime check
    if ($LASTEXITCODE -ne 0) { throw "Install the Codex CLI version in versions.lock.json, then retry." }
}
New-Item -ItemType Directory -Force -Path $ServerDir, $ToolsDir, $pluginsDir, (Join-Path $Root "state"), (Join-Path $Root "logs") | Out-Null

function Get-VerifiedDownload {
    param([string]$Url, [string]$Destination, [string]$ExpectedHash, [string]$Algorithm = "SHA256")
    if ((Test-Path -LiteralPath $Destination) -and (Get-FileHash -LiteralPath $Destination -Algorithm $Algorithm).Hash.ToLowerInvariant() -eq $ExpectedHash) { return }
    $temporary = $Destination + ".download"
    & curl.exe -fL --retry 3 --retry-delay 2 -A $UserAgent -o $temporary $Url
    if ($LASTEXITCODE -ne 0) { throw "Download failed: $Url" }
    if ((Get-FileHash -LiteralPath $temporary -Algorithm $Algorithm).Hash.ToLowerInvariant() -ne $ExpectedHash) {
        Remove-Item -LiteralPath $temporary -Force
        throw "Checksum mismatch. Refusing to install $Url"
    }
    Move-Item -LiteralPath $temporary -Destination $Destination -Force
}

# Use an existing Java 25+ installation, otherwise bootstrap a pinned JDK.
$javaCommand = Get-Command java -ErrorAction SilentlyContinue
$javaExe = if ($javaCommand) { $javaCommand.Source } else { $null }
$javaMajor = 0
if ($javaExe) {
    $javaVersion = & $javaExe --version | Select-Object -First 1
    if ($javaVersion -match '(\d+)') { $javaMajor = [int]$matches[1] }
}
if ($javaMajor -lt 25) {
    $jdkRoot = Join-Path $ToolsDir ("corretto-" + $versions.java.version)
    $jdkZip = Join-Path $ToolsDir ("corretto-" + $versions.java.version + ".zip")
    $javaExe = Get-ChildItem -LiteralPath $jdkRoot -Recurse -Filter java.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
    if (-not $javaExe) {
        Get-VerifiedDownload $versions.java.url $jdkZip $versions.java.sha256
        Expand-Archive -LiteralPath $jdkZip -DestinationPath $jdkRoot -Force
        $javaExe = Get-ChildItem -LiteralPath $jdkRoot -Recurse -Filter java.exe | Select-Object -First 1 -ExpandProperty FullName
    }
}
if (-not $javaExe) { throw "Java installation could not be found." }
$env:JAVA_HOME = Split-Path -Parent (Split-Path -Parent $javaExe)
$env:Path = (Join-Path $env:JAVA_HOME "bin") + ";" + $env:Path
$javaExe | Set-Content -LiteralPath (Join-Path $ServerDir "java-path.txt") -NoNewline

Write-Host "Installing Paper $($versions.minecraft) build $($versions.paper_build)..."
Get-VerifiedDownload $versions.paper.url (Join-Path $ServerDir "paper.jar") $versions.paper.sha256
$versions.minecraft | Set-Content -LiteralPath (Join-Path $ServerDir "paper-version.txt") -NoNewline
$versions.paper_build | Set-Content -LiteralPath (Join-Path $ServerDir "paper-build.txt") -NoNewline
Get-VerifiedDownload $versions.camera.url (Join-Path $pluginsDir "SpectatorCamera.jar") $versions.camera.sha512 "SHA512"
$cameraDir = Join-Path $pluginsDir "SpectatorCamera"
New-Item -ItemType Directory -Force -Path $cameraDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $cameraDir "renders") | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $cameraDir "config.yml"))) {
    Copy-Item -LiteralPath (Join-Path $Root "templates\SpectatorCamera.yml") -Destination (Join-Path $cameraDir "config.yml")
}

foreach ($secretName in @("rcon-password.txt", "event-token.txt")) {
    $secretFile = Join-Path $ServerDir $secretName
    if (-not (Test-Path -LiteralPath $secretFile)) {
        $secret = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
        [System.IO.File]::WriteAllText($secretFile, $secret)
    }
}
$rconPassword = (Get-Content -Raw -LiteralPath (Join-Path $ServerDir "rcon-password.txt")).Trim()
$eventToken = (Get-Content -Raw -LiteralPath (Join-Path $ServerDir "event-token.txt")).Trim()
$PlayerName | Set-Content -LiteralPath (Join-Path $ServerDir "god-player.txt") -NoNewline
$bridgeDir = Join-Path $pluginsDir "MinecraftGodBridge"
New-Item -ItemType Directory -Force -Path $bridgeDir | Out-Null
# This generated file contains a credential and stays in ignored runtime data.
$bridgeConfigFile = Join-Path $bridgeDir "config.yml"
$preservedConfig = if (Test-Path -LiteralPath $bridgeConfigFile) {
    @(Get-Content -LiteralPath $bridgeConfigFile | Where-Object { $_ -notmatch '^(bridge-url|bridge-token|player-name):' })
} else { @('prayer-cooldown-seconds: 10') }
$pluginConfig = @(
    "bridge-url: http://127.0.0.1:8765/events",
    "bridge-token: '$eventToken'",
    "player-name: ''"
    $preservedConfig
) -join [Environment]::NewLine
[System.IO.File]::WriteAllText($bridgeConfigFile, $pluginConfig)

$properties = @{}
$propertiesFile = Join-Path $Root "server.properties"
if (Test-Path -LiteralPath $propertiesFile) {
    foreach ($line in Get-Content -LiteralPath $propertiesFile) {
        if ($line -match '^(?!#)([^=]+)=(.*)$') { $properties[$matches[1]] = $matches[2] }
    }
}
$defaults = @{ 'server-ip'='127.0.0.1'; 'server-port'='25565'; 'gamemode'='survival'; 'difficulty'='normal'; 'online-mode'='true'; 'motd'='Minecraft God - a personal god for every player' }
foreach ($key in $defaults.Keys) { if (-not $properties.ContainsKey($key)) { $properties[$key] = $defaults[$key] } }
$properties['enable-rcon'] = 'true'
$properties['rcon.port'] = '25575'
$properties['rcon.password'] = $rconPassword
$properties.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Name)=$($_.Value)" } | Set-Content -LiteralPath $propertiesFile

$eulaFile = Join-Path $Root "eula.txt"
if ($AcceptEula) { "eula=true" | Set-Content -LiteralPath $eulaFile }
elseif (-not (Test-Path -LiteralPath $eulaFile)) { "eula=false" | Set-Content -LiteralPath $eulaFile }

Write-Host "Building and testing the Paper bridge..."
& (Join-Path $Root "paper-plugin\gradlew.bat") -p (Join-Path $Root "paper-plugin") clean build --no-daemon
if ($LASTEXITCODE -ne 0) { throw "Plugin build failed; no plugin was installed." }
$pluginJar = Join-Path $Root "paper-plugin\build\libs\minecraft-god-paper-plugin-0.1.0.jar"
if (-not (Test-Path -LiteralPath $pluginJar)) { throw "Plugin build did not produce the expected JAR." }
Copy-Item -LiteralPath $pluginJar -Destination (Join-Path $pluginsDir "MinecraftGodBridge.jar") -Force

& (Join-Path $Root 'setup-multiplayer.ps1') -SkipBuild
if (-not $?) { throw 'Multiplayer installation failed.' }

Write-Host "Setup complete. Run: py -3 -m god_bridge.codex_runtime login"
Write-Host "Then check: py -3 -m god_bridge.doctor"
if (-not ((Get-Content -Raw -LiteralPath $eulaFile) -match '(?m)^eula=true\s*$')) {
    Write-Host "Read https://aka.ms/MinecraftEULA and rerun with -AcceptEula if you accept it."
}
Write-Host "Start with .\start-minecraft-god.ps1. Defaults accept connections from this computer only."
