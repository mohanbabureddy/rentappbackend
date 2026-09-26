<#
.SYNOPSIS
  Back up PRODUCTION (database + uploaded files) to this laptop.

.DESCRIPTION
  * Database: every table is copied into a NEW local MySQL schema named
    rent_app_prod_backup_<date>_<time> -- never into your rent_app dev database.
  * Files: the Aadhaar uploads (Supabase Storage) are mirrored into
    %USERPROFILE%\RentAppBackups\aadhaar -- deliberately OUTSIDE OneDrive so ID
    documents never sync to the cloud. Existing files aren't re-downloaded and
    files deleted from production are kept.
  * Production is only READ. Old auto-named schemas beyond the newest 14 are dropped.
  * Everything is logged to %USERPROFILE%\RentAppBackups\backup.log.

  Credentials come from (in order): environment variables already set, the
  encrypted store created by setup-backup-schedule.ps1, or -- when you run this by
  hand -- a hidden prompt. See scripts\backup_prod_to_local.py for the safety rules.

.PARAMETER Check
  Look only: connect read-only, list tables/row counts/files. Writes and downloads nothing.
.PARAMETER Scheduled
  How the scheduled task runs it: never prompts, and does nothing if a backup already
  succeeded today (so "at logon" + "daily" triggers still mean one backup a day).
.PARAMETER SkipFiles
  Database only.
.PARAMETER FilesDir
  Where the files go instead of %USERPROFILE%\RentAppBackups (never inside OneDrive).
.PARAMETER Schema
  Custom schema name (must contain "backup"). Custom-named schemas are never auto-pruned.
.PARAMETER Replace
  Overwrite that schema if it already has tables.

.EXAMPLE
  powershell -File scripts\backup-prod.ps1 -Check
  powershell -File scripts\backup-prod.ps1
#>
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$Scheduled,
    [switch]$SkipFiles,
    [string]$FilesDir,
    [string]$Schema,
    [switch]$Replace
)

$ErrorActionPreference = 'Stop'
$pythonDir = Split-Path -Parent $PSScriptRoot
$python    = Join-Path $pythonDir '.venv\Scripts\python.exe'
$script    = Join-Path $PSScriptRoot 'backup_prod_to_local.py'

# RENTAP_BACKUP_HOME lets tests point everything at a scratch folder.
$home_     = if ($env:RENTAPP_BACKUP_HOME) { $env:RENTAPP_BACKUP_HOME } else { Join-Path $env:USERPROFILE 'RentAppBackups' }
$secrets   = Join-Path $home_ 'secrets.xml'
$logFile   = Join-Path $home_ 'backup.log'
$marker    = Join-Path $home_ 'last_success.txt'
$secretKeys = 'PROD_DATABASE_URL', 'SUPABASE_URL', 'SUPABASE_SERVICE_KEY'

function Write-Log([string]$msg) {
    $line = "{0:yyyy-MM-dd HH:mm:ss}  {1}" -f (Get-Date), $msg
    Write-Host $line
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}
function ConvertTo-Plain([securestring]$s) {
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}

if (-not (Test-Path $python)) { throw "Python not found at $python. Run this from the project, with its .venv set up." }
New-Item -ItemType Directory -Force -Path $home_ | Out-Null

if ($Scheduled -and -not $Check -and (Test-Path $marker) -and ((Get-Content $marker -Raw).Trim() -eq (Get-Date -Format 'yyyy-MM-dd'))) {
    Write-Log 'Scheduled run: a backup already succeeded today - nothing to do.'
    exit 0
}

# ---- credentials: env vars > encrypted store > (manual runs only) hidden prompt ----
$loadedFromEnv = @{}
foreach ($k in $secretKeys) { if (Get-Item "Env:\$k" -ErrorAction SilentlyContinue) { $loadedFromEnv[$k] = $true } }
$set = @()
if (Test-Path $secrets) {
    $stored = Import-Clixml $secrets
    foreach ($k in $secretKeys) {
        if (-not $loadedFromEnv[$k] -and $stored.ContainsKey($k)) { Set-Item "Env:\$k" (ConvertTo-Plain $stored[$k]); $set += $k }
    }
}
$needed = if ($SkipFiles) { @('PROD_DATABASE_URL') } else { $secretKeys }
foreach ($k in $needed) {
    if (-not (Get-Item "Env:\$k" -ErrorAction SilentlyContinue)) {
        if ($Scheduled) { Write-Log "FAILED: $k is not stored. Run scripts\setup-backup-schedule.ps1 once to save it."; exit 2 }
        $secure = Read-Host "Enter $k (input hidden)" -AsSecureString
        Set-Item "Env:\$k" (ConvertTo-Plain $secure); $set += $k
    }
}

$scriptArgs = @($script)
if ($Check)     { $scriptArgs += '--check' }
if ($SkipFiles) { $scriptArgs += '--skip-files' }
if ($FilesDir)  { $scriptArgs += @('--files-dir', $FilesDir) } else { $scriptArgs += @('--files-dir', $home_) }
if ($Schema)    { $scriptArgs += @('--schema', $Schema) }
if ($Replace)   { $scriptArgs += '--replace' }

$code = 1
try {
    Push-Location $pythonDir
    Write-Log ("Starting backup{0}" -f $(if ($Check) { ' (check only)' } elseif ($Scheduled) { ' (scheduled)' } else { '' }))
    # Native stderr must not turn into terminating errors; we go by the exit code.
    $ErrorActionPreference = 'Continue'
    & $python @scriptArgs 2>&1 | ForEach-Object { Write-Log ([string]$_) }
    $code = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
}
finally {
    Pop-Location
    foreach ($k in $set) { Remove-Item "Env:\$k" -ErrorAction SilentlyContinue }   # the secrets never outlive this run
}

if ($code -eq 0) {
    if (-not $Check) { Set-Content -Path $marker -Value (Get-Date -Format 'yyyy-MM-dd') -Encoding ASCII }
    Write-Log 'Finished OK.'
} else {
    Write-Log "FINISHED WITH ERRORS (exit code $code)."
}
exit $code
