$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Set-Location -LiteralPath (Split-Path $PSScriptRoot -Parent)
if (!(Test-Path .venv/Scripts/python.exe)) { py -3 -m venv .venv }
& ./.venv/Scripts/python.exe -m pip install -r requirements-lock.txt
if ($LASTEXITCODE -ne 0) { throw '의존성 설치 실패' }
if (!(Test-Path .env)) { Copy-Item .env.example .env }
