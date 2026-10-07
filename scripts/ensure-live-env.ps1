# Complete an existing .env with the local-only values that later phases introduced.
#
#   ./scripts/init-env.ps1          # first-time creation from .env.example
#   ./scripts/ensure-live-env.ps1   # add any still-missing local values to an older .env
#
# Nothing is printed except the missing key names; secret values stay in .env only.

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$envPath = Join-Path $root '.env'

if (-not (Test-Path -LiteralPath $envPath)) {
    Write-Host '.env does not exist; run ./scripts/init-env.ps1 first.'
    exit 1
}

$content = Get-Content -LiteralPath $envPath -Raw

function New-LocalSecret {
    $bytes = [byte[]]::new(24)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [System.Convert]::ToHexString($bytes).ToLowerInvariant()
}

# Secret values are generated; non-secret values keep the documented defaults.
$required = [ordered]@{
    'MYSQL_READONLY_USER'     = 'agent_readonly'
    'MYSQL_READONLY_PASSWORD' = (New-LocalSecret)
    'AUTH_BOOTSTRAP_USERNAME' = 'demo'
    'AUTH_BOOTSTRAP_PASSWORD' = (New-LocalSecret)
    'EMBEDDING_PROVIDER'      = 'siliconflow'
    'EMBEDDING_BASE_URL'      = 'https://api.siliconflow.cn/v1'
    'EMBEDDING_MODEL'         = 'BAAI/bge-m3'
    'EMBEDDING_DIMENSION'     = '1024'
    'PARSER_PROVIDER'         = 'lightweight'
    'SQL_AGENT_PROVIDER'      = 'auto'
    'MYSQL_HOST_PORT'         = '13306'
    'MILVUS_HOST_PORT'        = '19530'
}

$added = @()
foreach ($key in $required.Keys) {
    if ($content -notmatch "(?m)^$key=") {
        Add-Content -LiteralPath $envPath -Value "$key=$($required[$key])"
        $added += $key
    }
}

if ($added.Count -eq 0) {
    Write-Host '.env already contains every required local value.'
} else {
    Write-Host ("Added missing local values to .env: " + ($added -join ', '))
}
