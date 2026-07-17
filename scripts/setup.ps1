$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = if ($env:PYTHON) { $env:PYTHON } else { 'python' }
$Venv = Join-Path $ProjectRoot '.venv'

& $Python -m venv $Venv
& (Join-Path $Venv 'Scripts\python.exe') -m pip install --upgrade pip
& (Join-Path $Venv 'Scripts\python.exe') -m pip install -e "${ProjectRoot}[dev]"
Write-Host "环境安装完成。运行 scripts\run.ps1 启动服务。"
