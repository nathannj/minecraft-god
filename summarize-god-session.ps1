$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
& (Get-Command py -ErrorAction Stop).Source -3 -m god_bridge.log_report @args
