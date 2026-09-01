param(
    [string]$PlayerName = "IAmBoreder",
    [switch]$AcceptEula
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServerDir = Join-Path $Root "server"
$ToolsDir = Join-Path $Root ".tools"
$PaperVersion = "26.2"
$UserAgent = "MinecraftGodBridge/0.1.0 (local setup)"
$CameraPluginVersion = "2.0.9"
$CameraPluginUrl = "https://cdn.modrinth.com/data/ZBC8eO4p/versions/xOL4O91X/SpectatorCamera-v2.0.9.jar"
$CameraPluginSha512 = "4758b1e7e607fd7b43e2146a361644f081aea04b63e8eb8164119eb982aa0852d521bb55e0a5068bb6c3b32d956aa4e360a6695715d0052d7e49880045decf40"

function Set-ServerProperty([hashtable]$Properties, [string]$Key, [string]$Value) {
    $Properties[$Key] = $Value
}

Write-Host "Setting up Minecraft God in $Root"
New-Item -ItemType Directory -Force -Path $ServerDir, $ToolsDir, (Join-Path $Root "state"), (Join-Path $Root "logs") | Out-Null

$pluginsDir = Join-Path $Root "plugins"
$cameraJar = Join-Path $pluginsDir "SpectatorCamera.jar"
$cameraDownload = Join-Path $ToolsDir "SpectatorCamera-v$CameraPluginVersion.jar"
New-Item -ItemType Directory -Force -Path $pluginsDir | Out-Null
$cameraNeedsDownload = -not (Test-Path $cameraDownload)
if (-not $cameraNeedsDownload) {
    $cameraNeedsDownload = (Get-FileHash -Algorithm SHA512 -LiteralPath $cameraDownload).Hash.ToLowerInvariant() -ne $CameraPluginSha512
}
if ($cameraNeedsDownload) {
    Write-Host "Downloading SpectatorCamera $CameraPluginVersion..."
    & curl.exe -fL --retry 5 --retry-all-errors --retry-delay 2 -A $UserAgent -o $cameraDownload $CameraPluginUrl
    if ($LASTEXITCODE -ne 0 -or (Get-Item -LiteralPath $cameraDownload).Length -eq 0) { throw "SpectatorCamera download failed." }
}
$cameraActualSha = (Get-FileHash -Algorithm SHA512 -LiteralPath $cameraDownload).Hash.ToLowerInvariant()
if ($cameraActualSha -ne $CameraPluginSha512) { throw "SpectatorCamera checksum verification failed." }
Copy-Item -LiteralPath $cameraDownload -Destination $cameraJar -Force
$cameraConfigDir = Join-Path $Root "plugins\SpectatorCamera"
New-Item -ItemType Directory -Force -Path $cameraConfigDir | Out-Null
$cameraConfigTemplate = Join-Path $Root "config\SpectatorCamera.yml"
$cameraConfig = Join-Path $cameraConfigDir "config.yml"
if (-not (Test-Path $cameraConfig)) { Copy-Item -LiteralPath $cameraConfigTemplate -Destination $cameraConfig }

$java = Get-Command java -ErrorAction SilentlyContinue
$javaExe = if ($java) { $java.Source } else { $null }
$javaVersion = if ($javaExe) { (& $javaExe --version | Select-Object -First 1) } else { "" }
$javaMajor = 0
if ($javaVersion -match "(\d+)") { $javaMajor = [int]$matches[1] }
if ($javaMajor -lt 25) {
    $correttoVersion = "25"
    $javaHomeRoot = Join-Path $ToolsDir "corretto-$correttoVersion"
    $correttoZip = Join-Path $ToolsDir "amazon-corretto-$correttoVersion-windows-x64.zip"
    $javaExe = Get-ChildItem -Path $javaHomeRoot -Recurse -Filter "java.exe" -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
    if (-not $javaExe) {
        if (-not (Test-Path $correttoZip) -or (Get-Item -LiteralPath $correttoZip).Length -eq 0) {
            Write-Host "Java 25 is required for Paper $PaperVersion. Downloading Amazon Corretto 25..."
            & curl.exe -fL --retry 5 --retry-all-errors --retry-delay 2 -o $correttoZip "https://corretto.aws/downloads/latest/amazon-corretto-25-x64-windows-jdk.zip"
            if ($LASTEXITCODE -ne 0 -or (Get-Item -LiteralPath $correttoZip).Length -eq 0) { throw "Java 25 download failed." }
        }
        New-Item -ItemType Directory -Force -Path $javaHomeRoot | Out-Null
        Expand-Archive -LiteralPath $correttoZip -DestinationPath $javaHomeRoot -Force
        $javaExe = Get-ChildItem -Path $javaHomeRoot -Recurse -Filter "java.exe" | Select-Object -First 1 -ExpandProperty FullName
    }
    if (-not $javaExe) { throw "Java 25 was downloaded but java.exe could not be found." }
    $javaVersion = (& $javaExe --version | Select-Object -First 1)
}
Write-Host "Using $javaVersion"
$javaHome = Split-Path -Parent (Split-Path -Parent $javaExe)
$env:JAVA_HOME = $javaHome
$env:Path = (Join-Path $javaHome "bin") + ";" + $env:Path

$paperJar = Join-Path $ServerDir "paper.jar"
$paperVersionFile = Join-Path $ServerDir "paper-version.txt"
if (-not (Test-Path $paperJar) -or -not (Test-Path $paperVersionFile) -or (Get-Content -Raw -LiteralPath $paperVersionFile).Trim() -ne $PaperVersion) {
    Write-Host "Finding the latest stable Paper $PaperVersion build..."
    $headers = @{ "User-Agent" = $UserAgent; "Accept" = "application/json" }
    $build = Invoke-RestMethod -Uri "https://fill.papermc.io/v3/projects/paper/versions/$PaperVersion/builds/latest" -Headers $headers
    if ($build.channel -ne "STABLE") { throw "Paper did not return a stable build for $PaperVersion." }
    $download = $build.downloads.'server:default'.url
    $expectedSha = $build.downloads.'server:default'.checksums.sha256
    Write-Host "Downloading Paper $PaperVersion build $($build.id)..."
    $paperDownload = Join-Path $ServerDir "paper.download.jar"
    & curl.exe -fL --retry 5 --retry-all-errors --retry-delay 2 -A $UserAgent -o $paperDownload $download
    if ($LASTEXITCODE -ne 0 -or (Get-Item -LiteralPath $paperDownload).Length -eq 0) {
        throw "Paper download failed."
    }
    $actualSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $paperDownload).Hash.ToLowerInvariant()
    if ($actualSha -ne $expectedSha.ToLowerInvariant()) {
        Remove-Item -LiteralPath $paperDownload -Force
        throw "Paper checksum verification failed."
    }
    Copy-Item -LiteralPath $paperDownload -Destination $paperJar -Force
    Remove-Item -LiteralPath $paperDownload -Force
    $PaperVersion | Set-Content -LiteralPath $paperVersionFile -NoNewline
}

$passwordFile = Join-Path $ServerDir "rcon-password.txt"
if (-not (Test-Path $passwordFile)) {
    [guid]::NewGuid().ToString("N") | Set-Content -LiteralPath $passwordFile -NoNewline
}
$rconPassword = (Get-Content -Raw -LiteralPath $passwordFile).Trim()
$playerFile = Join-Path $ServerDir "god-player.txt"
$PlayerName | Set-Content -LiteralPath $playerFile -NoNewline
$javaExe | Set-Content -LiteralPath (Join-Path $ServerDir "java-path.txt") -NoNewline

$properties = @{}
$propertiesFile = Join-Path $Root "server.properties"
if (Test-Path $propertiesFile) {
    foreach ($line in Get-Content -LiteralPath $propertiesFile) {
        if ($line -match "^(?!#)([^=]+)=(.*)$") { $properties[$matches[1]] = $matches[2] }
    }
}
Set-ServerProperty $properties "enable-rcon" "true"
Set-ServerProperty $properties "rcon.port" "25575"
Set-ServerProperty $properties "rcon.password" $rconPassword
Set-ServerProperty $properties "server-port" "25565"
Set-ServerProperty $properties "gamemode" "survival"
Set-ServerProperty $properties "difficulty" "normal"
Set-ServerProperty $properties "online-mode" "true"
Set-ServerProperty $properties "motd" "$PlayerName's world - watched by God"
$properties.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Name)=$($_.Value)" } | Set-Content -LiteralPath $propertiesFile

$eulaFile = Join-Path $Root "eula.txt"
if ($AcceptEula) {
    "# Set by the user through setup-minecraft-god.ps1 -AcceptEula" | Set-Content -LiteralPath $eulaFile
    "eula=true" | Add-Content -LiteralPath $eulaFile
} elseif (-not (Test-Path $eulaFile)) {
    "eula=false" | Set-Content -LiteralPath $eulaFile
}

$gradleVersion = "9.1.0"
$gradleHome = Join-Path $ToolsDir "gradle-$gradleVersion"
$gradleCommand = Join-Path $gradleHome "bin\gradle.bat"
if (-not (Test-Path $gradleCommand)) {
    $zip = Join-Path $ToolsDir "gradle-$gradleVersion-bin.zip"
    if (-not (Test-Path $zip) -or (Get-Item -LiteralPath $zip).Length -eq 0) {
        Write-Host "Downloading Gradle $gradleVersion for the plugin build..."
        & curl.exe -fL --retry 5 --retry-all-errors --retry-delay 2 -o $zip "https://services.gradle.org/distributions/gradle-$gradleVersion-bin.zip"
        if ($LASTEXITCODE -ne 0 -or (Get-Item -LiteralPath $zip).Length -eq 0) {
            throw "Gradle download failed."
        }
    }
    Expand-Archive -LiteralPath $zip -DestinationPath $ToolsDir -Force
}

Write-Host "Building the Paper event plugin..."
& $gradleCommand -p (Join-Path $Root "paper-plugin") clean build --no-daemon
$pluginJar = Get-ChildItem (Join-Path $Root "paper-plugin\build\libs") -Filter "*.jar" | Where-Object { $_.Name -notmatch "-sources|-javadoc" } | Select-Object -First 1
if (-not $pluginJar) { throw "Plugin build did not produce a JAR." }
Copy-Item -LiteralPath $pluginJar.FullName -Destination (Join-Path $pluginsDir "MinecraftGodBridge.jar") -Force

$codex = Get-Command codex -CommandType Application -ErrorAction SilentlyContinue
if (-not $codex) { throw "Codex CLI was not found. Install/sign in to Codex, then run setup again." }
Write-Host "Registering the Minecraft God MCP server with Codex..."
& codex mcp remove minecraft_god 2>$null
& codex mcp add minecraft_god `
    --env "MINECRAFT_RCON_PASSWORD_FILE=$passwordFile" `
    --env "MINECRAFT_PLAYER=$PlayerName" `
    --env "MINECRAFT_ACTIVE_PLAYER_FILE=$(Join-Path $Root 'state\active_player.txt')" `
    --env "MINECRAFT_STATE_FILE=$(Join-Path $Root 'state\god_state.json')" `
    -- (Get-Command py).Source -3 (Join-Path $Root "run_bridge.py") --no-events

if (-not $AcceptEula) {
    Write-Host "`nSetup is complete, but the Minecraft EULA is not accepted yet."
    Write-Host "If you accept it, run this once:"
    Write-Host "  .\setup-minecraft-god.ps1 -AcceptEula"
} else {
    Write-Host "`nSetup is complete. Start the whole system with:"
    Write-Host "  .\start-minecraft-god.ps1"
}
