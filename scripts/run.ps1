$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "未找到项目虚拟环境。请先执行 scripts\setup.ps1"
}

& $VenvPython -m uvicorn app.main:app --app-dir (Join-Path $ProjectRoot 'backend') --host 127.0.0.1 --port 8000
