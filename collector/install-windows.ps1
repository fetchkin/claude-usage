# Install the Claude usage collector on a Windows machine: pushes its Claude Code usage totals to the Fetchkin usage
# dashboard every 10 minutes (Task Scheduler, no window). From a machine on the tailnet, in PowerShell:
#
#   $f="$env:TEMP\install-cuc.ps1"; iwr https://laptop-fevkm5fu.tailaecd9a.ts.net:8443/collector/install-windows.ps1 -OutFile $f; & $f -Label "Office PC"
#
# Token: $env:USAGE_INGEST_TOKEN, else you're asked (input hidden). Uninstall: .\install-windows.ps1 -Uninstall
param(
  [string]$Label = $env:COMPUTERNAME,
  [string]$Device = ($env:COMPUTERNAME.ToLower() -replace '[^a-z0-9._-]', ''),
  [string]$Base = $(if ($env:USAGE_BASE) { $env:USAGE_BASE } else { 'https://laptop-fevkm5fu.tailaecd9a.ts.net:8443' }),
  [string]$Python = '',
  [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'
$Dir = Join-Path $HOME '.claude-usage-collector'
$TokenFile = Join-Path $HOME '.config\fetchkin\usage-collector.env'
$Task = "Claude Usage Collector ($Device)"

if ($Uninstall) {
  Unregister-ScheduledTask -TaskName $Task -Confirm:$false -ErrorAction SilentlyContinue
  Remove-Item -Recurse -Force $Dir -ErrorAction SilentlyContinue
  Write-Host "Removed task '$Task' and $Dir (token file kept: $TokenFile)"
  return
}

if (-not $Python) {
  $Python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
  if (-not $Python) { $Python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source }
}
if (-not $Python) { throw 'Python 3.8+ is needed (python.org or winget install Python.Python.3.12).' }
$Console = $Python -replace 'pythonw\.exe$', 'python.exe'

New-Item -ItemType Directory -Force -Path $Dir, (Split-Path $TokenFile) | Out-Null
Invoke-WebRequest "$Base/collector/claude_usage_collector.py" -OutFile (Join-Path $Dir 'claude_usage_collector.py') -UseBasicParsing

$Token = $env:USAGE_INGEST_TOKEN
if (-not $Token -and (Test-Path $TokenFile)) {
  Write-Host "Keeping the ingest token already saved in $TokenFile"
} else {
  if (-not $Token) {
    $s = Read-Host 'Paste the ingest token (USAGE_INGEST_TOKEN)' -AsSecureString
    $Token = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
  }
  if (-not $Token) { throw 'No token given.' }
  [IO.File]::WriteAllText($TokenFile, "USAGE_INGEST_TOKEN=$Token`n")
}

$Script = Join-Path $Dir 'claude_usage_collector.py'
$TaskArgs = "`"$Script`" --device $Device --label `"$Label`" --url $Base/api/ingest"
Write-Host 'Test push...'
& $Console $Script --device $Device --label $Label --url "$Base/api/ingest"
if ($LASTEXITCODE -ne 0) { throw "Test push failed (exit $LASTEXITCODE)" }

$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 10)
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 9)
Register-ScheduledTask -TaskName $Task -Action (New-ScheduledTaskAction -Execute $Python -Argument $TaskArgs) -Trigger $Trigger -Settings $Settings -Force | Out-Null
Write-Host "Installed task '$Task': pushes every 10 minutes."
