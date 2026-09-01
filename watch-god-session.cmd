@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "while (-not (Test-Path -LiteralPath '.\logs\god-session.log')) { Start-Sleep -Milliseconds 500 }; Get-Content -LiteralPath '.\logs\god-session.log' -Wait"
