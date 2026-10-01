$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogPath = Join-Path $Root "logs\god-session.log"
$Offset = 0L
$Identity = ""

Write-Host "Waiting for the Minecraft God transcript. This window follows log rotations automatically."
while ($true) {
    if (-not (Test-Path -LiteralPath $LogPath)) {
        Start-Sleep -Milliseconds 500
        continue
    }
    $File = Get-Item -LiteralPath $LogPath
    $CurrentIdentity = "$($File.CreationTimeUtc.Ticks):$($File.FullName)"
    if ($Identity -ne $CurrentIdentity -or $File.Length -lt $Offset) {
        if ($Identity) { Write-Host "--- transcript rotated; following the new active log ---" -ForegroundColor DarkGray }
        $Identity = $CurrentIdentity
        $Offset = 0L
    }
    if ($File.Length -gt $Offset) {
        $Stream = [System.IO.File]::Open($LogPath, 'Open', 'Read', 'ReadWrite,Delete')
        try {
            [void]$Stream.Seek($Offset, [System.IO.SeekOrigin]::Begin)
            $Reader = [System.IO.StreamReader]::new($Stream, [System.Text.UTF8Encoding]::new($false), $true, 4096, $true)
            try {
                $Text = $Reader.ReadToEnd()
                if ($Text) { Write-Host $Text -NoNewline }
            } finally {
                $Reader.Dispose()
            }
            $Offset = $Stream.Length
        } finally {
            $Stream.Dispose()
        }
    }
    Start-Sleep -Milliseconds 500
}
