$ErrorActionPreference = 'Stop'
$envTarget = Join-Path (Split-Path $PSScriptRoot -Parent) '.env'
if (Test-Path -LiteralPath $envTarget) {
    Write-Host '.env already exists; preserved.'
    exit 0
}
$templatePath = Join-Path (Split-Path $PSScriptRoot -Parent) '.env.example'
$template = Get-Content -LiteralPath $templatePath -Raw
foreach ($key in @('MYSQL_ROOT_PASSWORD', 'MYSQL_PASSWORD', 'MINIO_ACCESS_KEY', 'MINIO_SECRET_KEY')) {
    $bytes = [byte[]]::new(24)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $secretValue = [System.Convert]::ToHexString($bytes).ToLowerInvariant()
    $template = [regex]::Replace($template, "(?m)^$key=\r?$", "$key=$secretValue")
}
[System.IO.File]::WriteAllText($envTarget, $template)
Write-Host 'Created private .env with generated local credentials.'
