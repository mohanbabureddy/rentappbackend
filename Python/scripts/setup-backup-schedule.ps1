<#
.SYNOPSIS
  One-time setup: store the backup credentials (encrypted) and schedule the daily backup.

.DESCRIPTION
  1. Asks (hidden) for the three secrets the backup needs, and stores them in
       %USERPROFILE%\RentAppBackups\secrets.xml
     encrypted with Windows DPAPI -- only YOUR Windows account on THIS laptop can
     read it. They are never written to the repo, OneDrive, or the log.
  2. Tests them with a read-only check first; nothing is scheduled if that fails.
  3. Registers a Windows scheduled task that runs the backup
       - a few minutes after you log in, and
       - daily at 1:00 PM (or the first moment after that the laptop is on),
     and only once per day even though there are two triggers. It works on
     battery, waits for network, retries 3 times 15 minutes apart, and only runs
     while you're logged in (so it needs no stored Windows password).

  The three secrets:
    PROD_DATABASE_URL     Supabase > Connect > "Session pooler" URI (postgresql://...)
    SUPABASE_URL          https://<project-ref>.supabase.co     (same value as on Render)
    SUPABASE_SERVICE_KEY  the service_role key                  (same value as on Render)

.PARAMETER RunAt
  Daily time, e.g. '13:00' (default).
.PARAMETER SkipFiles
  Database only -- don't ask for / back up the uploaded files.
.PARAMETER Remove
  Remove the scheduled task. Add -ForgetSecrets to also delete the stored credentials.
.PARAMETER TaskName
  Name of the task (default 'RentApp Production Backup').

.EXAMPLE
  powershell -File scripts\setup-backup-schedule.ps1
  powershell -File scripts\setup-backup-schedule.ps1 -Remove
#>
[CmdletBinding()]
param(
    [string]$RunAt = '13:00',
    [switch]$SkipFiles,
    [switch]$Remove,
    [switch]$ForgetSecrets,
    [string]$TaskName = 'RentApp Production Backup'
)

$ErrorActionPreference = 'Stop'
$home_   = if ($env:RENTAPP_BACKUP_HOME) { $env:RENTAPP_BACKUP_HOME } else { Join-Path $env:USERPROFILE 'RentAppBackups' }
$secrets = Join-Path $home_ 'secrets.xml'
$backupScript = Join-Path $PSScriptRoot 'backup-prod.ps1'

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed the scheduled task '$TaskName'."
    } else { Write-Host "No scheduled task named '$TaskName'." }
    if ($ForgetSecrets -and (Test-Path $secrets)) { Remove-Item $secrets; Write-Host 'Deleted the stored credentials.' }
    Write-Host "Your existing backups in $home_ and in MySQL were left alone."
    return
}

$keys = if ($SkipFiles) { @('PROD_DATABASE_URL') } else { @('PROD_DATABASE_URL', 'SUPABASE_URL', 'SUPABASE_SERVICE_KEY') }
New-Item -ItemType Directory -Force -Path $home_ | Out-Null

$store = @{}
if (Test-Path $secrets) { $store = Import-Clixml $secrets }
foreach ($k in $keys) {
    $fromEnv = (Get-Item "Env:\$k" -ErrorAction SilentlyContinue)
    if ($fromEnv) {                                              # already set in this window: use it, don't ask
        $store[$k] = ConvertTo-SecureString $fromEnv.Value -AsPlainText -Force
    } elseif ($store.ContainsKey($k)) {
        $answer = Read-Host "$k is already stored. Keep it? [Y/n]"
        if ($answer -match '^(n|no)$') { $store[$k] = Read-Host "New $k (input hidden)" -AsSecureString }
    } else {
        $store[$k] = Read-Host "Enter $k (input hidden)" -AsSecureString
    }
}
$store | Export-Clixml -Path $secrets        # SecureStrings are encrypted per-user by Windows DPAPI
Write-Host "Credentials saved (encrypted for your Windows account only): $secrets"

# ---- prove they work before scheduling anything ----
Write-Host "`nTesting the connection (read-only, nothing is written)..."
$checkArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $backupScript, '-Check')
if ($SkipFiles) { $checkArgs += '-SkipFiles' }
& powershell.exe @checkArgs
if ($LASTEXITCODE -ne 0) {
    throw "The connection test failed (see above), so nothing was scheduled. Fix the value and run this again."
}

# ---- register the task ----
$argLine = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$backupScript`" -Scheduled" + $(if ($SkipFiles) { ' -SkipFiles' } else { '' })
$action  = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argLine -WorkingDirectory (Split-Path -Parent $PSScriptRoot)
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$atLogon.Delay = 'PT5M'                                          # let Wi-Fi and MySQL come up first
$daily   = New-ScheduledTaskTrigger -Daily -At $RunAt
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RunOnlyIfNetworkAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($atLogon, $daily) -Settings $settings -Principal $principal `
    -Description 'Daily backup of the production rent app database and uploaded files to this laptop.' -Force | Out-Null

Write-Host "`nScheduled: '$TaskName'"
Write-Host "  runs 5 minutes after you log in, and daily at $RunAt (or as soon as the laptop is on after that)"
Write-Host "  at most one successful backup per day; log: $(Join-Path $home_ 'backup.log')"
Write-Host "  run it now:   Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  remove it:    powershell -File scripts\setup-backup-schedule.ps1 -Remove"
