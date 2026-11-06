$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw '无法创建 Python 虚拟环境' }
}
& $pythonPath -c "import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(name) for name in ['fastapi', 'uvicorn', 'report_workbench', 'multipart', 'markdown_it', 'httpx']) else 1)"
if ($LASTEXITCODE -ne 0) {
    & $pythonPath -m pip install -e .
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败' }
}
& $pythonPath -m uvicorn report_workbench.app:app --host 127.0.0.1 --port 8767
