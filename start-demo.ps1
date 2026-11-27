[CmdletBinding()]
param(
    [switch]$WithGodot,
    [switch]$OpenBrowser,
    [string]$GodotPath = 'godot'
)

$ErrorActionPreference = 'Stop'
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'Сначала создай .venv и установи зависимости из requirements/demo.txt. Команды есть в README.md.'
}

$launchArgs = @('demo.py')
if ($WithGodot) {
    $launchArgs += @('--godot', $GodotPath)
}
if ($OpenBrowser) {
    $launchArgs += '--video'
}

$previousAssistant = $env:ASSISTANT
Push-Location -LiteralPath $PSScriptRoot
try {
    $env:ASSISTANT = 'demo'
    & $pythonPath @launchArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    $env:ASSISTANT = $previousAssistant
    Pop-Location
}
