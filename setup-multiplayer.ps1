param([switch]$SkipBuild, [switch]$SkipVoiceModel, [switch]$SkipClient)
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
Set-Location $Root
. (Join-Path $Root 'scripts/runtime.ps1')
$running = @(Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and (Test-ProjectRuntimeProcess $_) })
if ($running) { throw 'Stop this project before installing multiplayer plugins.' }
$lock = Get-Content -Raw (Join-Path $Root 'versions.lock.json') | ConvertFrom-Json
$pluginsDir = Join-Path $Root 'plugins'
New-Item -ItemType Directory -Force $pluginsDir | Out-Null
foreach ($entry in @(@{ Key='voicechat'; Name='voicechat.jar'; Algorithm='SHA512' }, @{ Key='xaero_tracker'; Name='XaeroTracker.jar'; Algorithm='SHA256' })) {
    $version = $lock.($entry.Key)
    $destination = Join-Path $pluginsDir $entry.Name
    $expected = $version.($entry.Algorithm.ToLowerInvariant())
    if ((Test-Path -LiteralPath $destination) -and (Get-FileHash -LiteralPath $destination -Algorithm $entry.Algorithm).Hash.ToLowerInvariant() -eq $expected) { continue }
    $temporary = $destination + '.download'
    & curl.exe -fL --retry 3 -o $temporary $version.url
    if ($LASTEXITCODE -ne 0) { throw "Download failed: $($entry.Key)" }
    if ((Get-FileHash -LiteralPath $temporary -Algorithm $entry.Algorithm).Hash.ToLowerInvariant() -ne $expected) { throw "Checksum mismatch: $($entry.Key)" }
    Move-Item -LiteralPath $temporary -Destination $destination -Force
}
foreach ($entry in @(@{ Folder='voicechat'; Template='voicechat-server.properties'; Name='voicechat-server.properties' }, @{ Folder='XaeroTracker'; Template='XaeroTracker.yml'; Name='config.yml' })) {
    $folder = Join-Path $pluginsDir $entry.Folder
    New-Item -ItemType Directory -Force $folder | Out-Null
    $destination = Join-Path $folder $entry.Name
    if (-not (Test-Path -LiteralPath $destination)) { Copy-Item -LiteralPath (Join-Path $Root ('templates/' + $entry.Template)) -Destination $destination }
}
if (-not $SkipBuild) {
    $javaExe = (Get-Content -Raw (Join-Path $Root 'server/java-path.txt')).Trim()
    $env:JAVA_HOME = Split-Path (Split-Path $javaExe)
    & (Join-Path $Root 'paper-plugin/gradlew.bat') -p (Join-Path $Root 'paper-plugin') build --no-daemon
    if ($LASTEXITCODE -ne 0) { throw 'Multiplayer bridge build failed.' }
    Copy-Item -LiteralPath (Join-Path $Root 'paper-plugin/build/libs/minecraft-god-paper-plugin-0.1.0.jar') -Destination (Join-Path $pluginsDir 'MinecraftGodBridge.jar') -Force
}
if (-not $SkipVoiceModel) {
    $voicePython = Join-Path $Root '.tools/voice-venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $voicePython)) {
        & py -3 -m venv (Join-Path $Root '.tools/voice-venv')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the local speech environment.' }
    }
    & $voicePython -m pip install 'faster-whisper==1.2.1' 'av==16.0.1'
    if ($LASTEXITCODE -ne 0) { throw 'Could not install local speech recognition.' }
    $env:MINECRAFT_GOD_HOME = $Root
    & $voicePython -m god_bridge.voice
    if ($LASTEXITCODE -ne 0) { throw 'Could not prepare the speech model. Proximity voice and maps are installed.' }
}
if (-not $SkipClient) {
    & py -3 -m god_bridge.client_pack --install
    if ($LASTEXITCODE -ne 0) { throw 'Client profile installation failed.' }
}
Write-Host 'Multiplayer installed: maps, friend tracking, proximity voice and local AI hearing.'
Write-Host 'Use the Minecraft AI God launcher profile. Friends can import dist/Minecraft-AI-God-26.2.mrpack.'
Write-Host 'Remote players need TCP 25565 and UDP 24454 reachable. RCON and the event hub stay private.'
