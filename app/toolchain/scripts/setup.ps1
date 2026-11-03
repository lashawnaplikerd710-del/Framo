param([string]$PythonPath)
$ErrorActionPreference = 'Stop'
$skillRoot = Split-Path -Parent $PSScriptRoot
$venvPath = Join-Path $skillRoot '.venv'
$venvPython = Join-Path $venvPath 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    if (-not $PythonPath) {
        $bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
        if (Test-Path -LiteralPath $bundledPython) { $PythonPath = $bundledPython }
        else {
            $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
            if ($pythonCommand -and $pythonCommand.Source -notlike '*WindowsApps*') { $PythonPath = $pythonCommand.Source }
            else { throw 'Pass -PythonPath with the full path to a real Python 3.12+ executable.' }
        }
    }
    & $PythonPath -c 'import sys; assert sys.version_info >= (3,12), "Python 3.12+ required"'
    if ($LASTEXITCODE -ne 0) { throw 'Python version check failed.' }
    & $PythonPath -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
}
& $venvPython -m pip install -r (Join-Path $skillRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed; preserve the output and fix the reported dependency.' }
& $venvPython (Join-Path $PSScriptRoot 'jianying.py') doctor
if ($LASTEXITCODE -ne 0) { throw 'Environment check failed.' }
Write-Output "Ready: $venvPython"
