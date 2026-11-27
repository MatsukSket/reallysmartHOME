[CmdletBinding()]
param(
    [switch]$Console,
    [switch]$NoBrowser,
    [switch]$UseGpu,
    [string]$GodotPath = 'godot',
    [string]$Model = 'qwen2.5:3b'
)

$ErrorActionPreference = 'Stop'
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'Сначала создай .venv. Команды есть в README.md.'
}

& $pythonPath -c "import importlib.util as u; import sys; sys.exit(not all(u.find_spec(p) for p in ('chromadb', 'sentence_transformers')))"
if ($LASTEXITCODE -ne 0) {
    throw 'Установи зависимости полного режима из requirements/rag.txt. Команда есть в README.md.'
}

$ragRoot = $env:RAG_ROOT
if (-not $ragRoot) {
    $ragRoot = Join-Path $PSScriptRoot 'runtime\rag'
} elseif (-not [System.IO.Path]::IsPathRooted($ragRoot)) {
    $ragRoot = Join-Path (Join-Path $PSScriptRoot 'backend') $ragRoot
}
$pointer = Join-Path $ragRoot 'CURRENT'
if (-not (Test-Path -LiteralPath $pointer -PathType Leaf)) {
    throw 'Индекс Chroma отсутствует. Собери его по разделу «Chroma и локальная LLM» в README.md.'
}
$version = (Get-Content -LiteralPath $pointer -Raw).Trim()
if (-not $version -or -not (Test-Path -LiteralPath (Join-Path $ragRoot "$version\chroma.sqlite3") -PathType Leaf)) {
    throw 'Указатель CURRENT ссылается на отсутствующий индекс. Пересобери индекс по инструкции в README.md.'
}

function Read-OllamaModels {
    try {
        return Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2
    } catch {
        return $null
    }
}

$models = Read-OllamaModels
if ($null -eq $models) {
    $ollama = Get-Command ollama -CommandType Application -ErrorAction SilentlyContinue
    if (-not $ollama) {
        throw 'Установи Ollama и убедись, что команда ollama доступна в PowerShell.'
    }

    Write-Host 'Запускаю локальный сервис Ollama…'
    $previousHost = $env:OLLAMA_HOST
    try {
        $env:OLLAMA_HOST = '127.0.0.1:11434'
        Start-Process -FilePath $ollama.Source -ArgumentList 'serve' -WindowStyle Hidden
    } finally {
        $env:OLLAMA_HOST = $previousHost
    }
    $deadline = (Get-Date).AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 300
        $models = Read-OllamaModels
    } until ($null -ne $models -or (Get-Date) -ge $deadline)
}
if ($null -eq $models) {
    throw 'Ollama не ответила на порту 11434. Запусти ollama serve вручную, чтобы увидеть ошибку сервиса.'
}
if ($Model -notin @($models.models.name)) {
    throw "Модель $Model отсутствует. Выполни: ollama pull $Model"
}

$launchArgs = @('demo.py')
if (-not $Console) {
    $launchArgs += @('--godot', $GodotPath)
}
if (-not $NoBrowser) {
    $launchArgs += '--video'
}

$previousAssistant = $env:ASSISTANT
$previousModel = $env:OLLAMA_MODEL
$previousGpu = $env:OLLAMA_NUM_GPU
$previousUrl = $env:OLLAMA_BASE_URL
Push-Location -LiteralPath $PSScriptRoot
try {
    $env:ASSISTANT = 'ollama'
    $env:OLLAMA_MODEL = $Model
    $env:OLLAMA_BASE_URL = 'http://127.0.0.1:11434'
    $env:OLLAMA_NUM_GPU = if ($UseGpu) { $null } else { '0' }
    & $pythonPath @launchArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    $env:ASSISTANT = $previousAssistant
    $env:OLLAMA_MODEL = $previousModel
    $env:OLLAMA_NUM_GPU = $previousGpu
    $env:OLLAMA_BASE_URL = $previousUrl
    Pop-Location
}
