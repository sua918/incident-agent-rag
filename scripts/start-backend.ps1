param(
    [ValidateSet('1', '2')]
    [string]$Option
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
Set-Location -LiteralPath (Split-Path $PSScriptRoot -Parent)
# 옵션을 생략하면 .env의 설정을 그대로 사용한다.
if ($Option) { $env:LLM_BACKEND = $Option }
& ./.venv/Scripts/python.exe server.py
exit $LASTEXITCODE
