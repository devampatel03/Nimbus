<#
.SYNOPSIS
  Keeps the Nimbus pivot running unattended: relaunches a fresh Claude Code session
  whenever the previous one ends (session/usage limit, crash, context exhaustion)
  until STATE.md says DONE or docs/pivot/HALT exists.

.EXAMPLE
  .\scripts\pivot-runner.ps1                       # balanced, safe-ish permissions
  .\scripts\pivot-runner.ps1 -Mode economy -SleepMinutes 45
  New-Item docs\pivot\HALT                          # (from another terminal) stop after the current session

.NOTES
  - Run from anywhere; it cd's to the repo root (parent of /scripts).
  - Permissions: default uses --permission-mode acceptEdits plus the allow-list in
    .claude/settings.pivot.snippet.json (merge it into .claude/settings.json first).
    -Unsafe adds --dangerously-skip-permissions: use ONLY inside a disposable VM/container,
    never on your main Windows machine with real credentials.
  - Logs: docs\pivot\reports\runner\*.log (raw output - review before sharing).
#>
param(
  [ValidateSet("economy","balanced","turbo")] [string]$Mode = "balanced",
  [int]$MaxIterations = 60,
  [int]$SleepMinutes = 30,          # wait after a session that ended on a limit
  [int]$QuickRetrySeconds = 20,     # wait after a clean checkpoint/finish
  [int]$MaxConsecutiveFailures = 5,
  [switch]$Unsafe
)

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$state  = Join-Path $repo "docs\pivot\STATE.md"
$halt   = Join-Path $repo "docs\pivot\HALT"
$prompt = Join-Path $repo "docs\pivot\RESUME_PROMPT.md"
$logDir = Join-Path $repo "docs\pivot\reports\runner"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { Write-Error "claude CLI not found on PATH"; exit 2 }
if (-not (Test-Path $prompt)) { Write-Error "Missing $prompt - copy the pivot kit into the repo first."; exit 2 }

$fail = 0
for ($i = 1; $i -le $MaxIterations; $i++) {
  if (Test-Path $halt) { Write-Host "HALT file present - stopping."; break }
  if ((Test-Path $state) -and (Select-String -Path $state -Pattern '^status:\s*DONE' -Quiet)) { Write-Host "STATE is DONE - finished."; break }

  $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
  $log = Join-Path $logDir "session-$stamp.log"
  Write-Host "[$i/$MaxIterations] starting session ($Mode) -> $log"

  $text = (Get-Content -Raw $prompt) + "`n`nMode override for this session: $Mode."
  $cliArgs = @("-p", $text, "--permission-mode", "acceptEdits", "--output-format", "text")
  if ($Unsafe) { $cliArgs = @("-p", $text, "--dangerously-skip-permissions", "--output-format", "text") }

  & claude @cliArgs 2>&1 | Tee-Object -FilePath $log | Out-Null
  $exit = $LASTEXITCODE
  $tail = (Get-Content $log -Tail 40 -ErrorAction SilentlyContinue) -join "`n"

  if ($tail -match 'PIVOT-DONE') { Write-Host "Session reported PIVOT-DONE."; break }
  if ($tail -match 'PIVOT-HALTED') { Write-Host "Session reported PIVOT-HALTED."; break }

  if ($tail -match 'PIVOT-CHECKPOINTED') {
    $fail = 0
    # Limits usually reset on a window; wait before relaunching.
    if ($tail -match '(?i)limit|rate|quota|reset') { Write-Host "Checkpointed on a limit; sleeping $SleepMinutes min."; Start-Sleep -Seconds ($SleepMinutes * 60) }
    else { Start-Sleep -Seconds $QuickRetrySeconds }
    continue
  }

  # No checkpoint marker: crashed, limit hit hard, or auth/network problem.
  $fail++
  Write-Warning "Session ended without a marker (exit $exit). Consecutive failures: $fail"
  if ($tail -match '(?i)not logged in|authentication|invalid api key|login') { Write-Error "Auth problem - run `claude` interactively and log in. Stopping."; break }
  if ($fail -ge $MaxConsecutiveFailures) { Write-Error "Too many consecutive failures. Inspect $logDir. Stopping."; break }
  Start-Sleep -Seconds ($SleepMinutes * 60)
}

Write-Host "Runner finished. See docs\pivot\STATE.md and docs\pivot\reports\."
