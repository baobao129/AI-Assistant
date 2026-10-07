# Host-side development/verification environment for the Compose dependencies.
#
#   ./scripts/init-env.ps1              # once, to create .env
#   ./scripts/ensure-live-env.ps1       # once, if .env predates later phases
#   . ./scripts/dev-env.ps1             # dot-source so the variables stay in your shell
#   uv run python -m scripts.seed_demo
#   uv run python -m scripts.verify_live
#   uv run uvicorn backend.api.main:app --host 127.0.0.1 --port 8001
#
# Every credential comes from .env; nothing is hard-coded here. Containers keep using
# Compose service names (mysql, milvus) and never container-to-container localhost.

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$envPath = Join-Path $root '.env'

function Get-DotEnvValue {
    param([Parameter(Mandatory = $true)][string]$Key)
    $line = Get-Content -LiteralPath $envPath -ErrorAction SilentlyContinue |
        Select-String "^$Key=" | Select-Object -First 1
    if (-not $line) { return '' }
    return ($line.Line -split '=', 2)[1]
}

if (-not (Test-Path -LiteralPath $envPath)) {
    throw ".env is missing; run ./scripts/init-env.ps1 (and ./scripts/ensure-live-env.ps1) first."
}

$mysqlPassword = Get-DotEnvValue 'MYSQL_PASSWORD'
$rootPassword = Get-DotEnvValue 'MYSQL_ROOT_PASSWORD'
$readonlyPassword = Get-DotEnvValue 'MYSQL_READONLY_PASSWORD'
$bootstrapPassword = Get-DotEnvValue 'AUTH_BOOTSTRAP_PASSWORD'
foreach ($pair in @{
    MYSQL_PASSWORD           = $mysqlPassword
    MYSQL_ROOT_PASSWORD      = $rootPassword
    MYSQL_READONLY_PASSWORD  = $readonlyPassword
    AUTH_BOOTSTRAP_PASSWORD  = $bootstrapPassword
}.GetEnumerator()) {
    if (-not $pair.Value) {
        throw "$($pair.Key) is missing from .env; run ./scripts/ensure-live-env.ps1."
    }
}

$env:MYSQL_HOST = '127.0.0.1'
$env:MYSQL_PORT = Get-DotEnvValue 'MYSQL_HOST_PORT'
if (-not $env:MYSQL_PORT) { $env:MYSQL_PORT = '13306' }
$env:MYSQL_DATABASE = Get-DotEnvValue 'MYSQL_DATABASE'
if (-not $env:MYSQL_DATABASE) { $env:MYSQL_DATABASE = 'enterprise_agent' }
$env:MYSQL_USER = Get-DotEnvValue 'MYSQL_USER'
if (-not $env:MYSQL_USER) { $env:MYSQL_USER = 'agent_app' }
$env:MYSQL_PASSWORD = $mysqlPassword
$env:MYSQL_ROOT_PASSWORD = $rootPassword
$env:MYSQL_READONLY_USER = Get-DotEnvValue 'MYSQL_READONLY_USER'
if (-not $env:MYSQL_READONLY_USER) { $env:MYSQL_READONLY_USER = 'agent_readonly' }
$env:MYSQL_READONLY_PASSWORD = $readonlyPassword
$env:MILVUS_URI = 'http://127.0.0.1:19530'

# Host runs use the deterministic provider and the extractive reader unless overridden.
$env:EMBEDDING_PROVIDER = 'deterministic'
$env:EMBEDDING_DIMENSION = '64'
$env:LLM_PROVIDER = 'extractive'
$env:PARSER_PROVIDER = 'lightweight'
$env:CHECKPOINT_MODE = 'sqlite'
$env:CHECKPOINT_SQLITE_PATH = 'runtime/dev-checkpoints.sqlite'
$env:LOG_LEVEL = 'INFO'

# Same values for the pytest live-database fixtures and the acceptance scripts.
$env:TEST_MYSQL_HOST = $env:MYSQL_HOST
$env:TEST_MYSQL_PORT = $env:MYSQL_PORT
$env:TEST_MYSQL_DATABASE = $env:MYSQL_DATABASE
$env:TEST_MYSQL_USER = $env:MYSQL_USER
$env:TEST_MYSQL_PASSWORD = $env:MYSQL_PASSWORD
$env:TEST_MYSQL_ROOT_PASSWORD = $env:MYSQL_ROOT_PASSWORD
$env:TEST_MYSQL_READONLY_USER = $env:MYSQL_READONLY_USER
$env:TEST_MYSQL_READONLY_PASSWORD = $env:MYSQL_READONLY_PASSWORD
$env:TEST_MILVUS_URI = $env:MILVUS_URI
$env:TEST_AUTH_USERNAME = Get-DotEnvValue 'AUTH_BOOTSTRAP_USERNAME'
if (-not $env:TEST_AUTH_USERNAME) { $env:TEST_AUTH_USERNAME = 'demo' }
$env:TEST_AUTH_PASSWORD = $bootstrapPassword
$env:TEST_LLM_PROVIDER = 'extractive'

Write-Host "Host development environment prepared (MySQL 127.0.0.1:$($env:MYSQL_PORT), Milvus $($env:MILVUS_URI))."
