$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$interpreterFile = Join-Path $PSScriptRoot 'python-path.txt'
if (-not (Test-Path -LiteralPath $interpreterFile)) {
    throw 'Run install.ps1 first to select the Python interpreter.'
}
$musicPython = (Get-Content -LiteralPath $interpreterFile -Raw).Trim()
& $musicPython (Join-Path $PSScriptRoot 't3music.py') @args
exit $LASTEXITCODE
