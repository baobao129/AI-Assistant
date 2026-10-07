$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$envTarget = Join-Path $root '.env'
if (Test-Path -LiteralPath $envTarget) {
    Write-Host '.env already exists; preserved.'
    exit 0
}
$templatePath = Join-Path $root '.env.example'
$template = Get-Content -LiteralPath $templatePath -Raw

function New-LocalSecret {
    $bytes = [byte[]]::new(24)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [System.Convert]::ToHexString($bytes).ToLowerInvariant()
}

# Infrastructure credentials plus the read-only SQL Agent account and the demo
# session bootstrap. Generated once; an existing .env is never overwritten.
foreach ($key in @(
    'MYSQL_ROOT_PASSWORD',
    'MYSQL_PASSWORD',
    'MYSQL_READONLY_PASSWORD',
    'MINIO_ACCESS_KEY',
    'MINIO_SECRET_KEY',
    'AUTH_BOOTSTRAP_PASSWORD'
)) {
    $template = [regex]::Replace($template, "(?m)^$key=\r?$", "$key=$(New-LocalSecret)")
}

[System.IO.File]::WriteAllText($envTarget, $template)
Write-Host 'Created private .env with generated local credentials.'
Write-Host 'Demo sign-in user is AUTH_BOOTSTRAP_USERNAME with the generated AUTH_BOOTSTRAP_PASSWORD (read it from .env).'
