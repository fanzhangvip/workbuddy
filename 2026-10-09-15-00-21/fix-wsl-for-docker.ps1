# fix-wsl-for-docker.ps1
#
# Problem : Docker Desktop 4.94 fails with
#             "Error response from daemon: Docker Desktop is unable to start"
#           Root cause: this machine only has the legacy INBOX WSL.
#           `wsl --version` returns "Invalid command line option: --version",
#           so Docker Desktop cannot read the WSL version and refuses to start
#           the Linux engine ("checking preconditions: WSL update required").
# Fix     : install / update WSL to the current Microsoft Store build.
#
# HOW TO RUN
#   1. Right-click PowerShell  ->  Run as administrator
#   2. Run:
#      powershell -ExecutionPolicy Bypass -File "C:\zero\workbuddy\2026-10-09-15-00-21\fix-wsl-for-docker.ps1"
#
# The script is idempotent - it is safe to run more than once.

$ErrorActionPreference = 'Continue'

function Step($m) { Write-Host ""; Write-Host "===== $m =====" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[ OK ]  $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[WARN]  $m" -ForegroundColor Yellow }
function Err($m)  { Write-Host "[FAIL]  $m" -ForegroundColor Red }

# ---------------------------------------------------------------- 0. admin check
$id  = [Security.Principal.WindowsIdentity]::GetCurrent()
$pri = New-Object Security.Principal.WindowsPrincipal($id)
$isAdmin = $pri.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Err "This script must run as Administrator. Right-click PowerShell -> Run as administrator."
    exit 1
}
Ok "Running with Administrator privileges."

# ---------------------------------------------------------------- 1. OS info
Step "1/7  System information"
$os = Get-CimInstance Win32_OperatingSystem
$cv = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
Write-Host ("        OS     : " + $os.Caption)
Write-Host ("        Build  : " + $os.BuildNumber + "." + $cv.UBR)

$ddJson = 'C:\Program Files\Docker\Docker\resources\componentsVersion.json'
if (Test-Path $ddJson) {
    $ddVer = (Get-Content $ddJson -Raw | ConvertFrom-Json).appVersion
    Write-Host ("        Docker : " + $ddVer)
}

# ---------------------------------------------------------------- 2. virtualization
Step "2/7  Hardware virtualization"
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
if ($cpu.VirtualizationFirmwareEnabled -eq $true) {
    Ok "Virtualization is enabled in firmware (VT-x / AMD-V)."
} elseif ($null -eq $cpu.VirtualizationFirmwareEnabled) {
    Warn "Could not read firmware virtualization state - check Task Manager > Performance > CPU."
} else {
    Err "Virtualization is DISABLED in BIOS/UEFI. Enable Intel VT-x or AMD-V first."
}

# ---------------------------------------------------------------- 3. windows features
Step "3/7  Required Windows features"
$needReboot = $false
$features = @('Microsoft-Windows-Subsystem-Linux', 'VirtualMachinePlatform')
foreach ($f in $features) {
    $item  = Get-WindowsOptionalFeature -Online -FeatureName $f -ErrorAction SilentlyContinue
    if ($null -eq $item) {
        Warn "$f not found on this system (skipped)."
    } elseif ($item.State -eq 'Enabled') {
        Ok "$f is enabled."
    } else {
        Warn "$f is $($item.State) - enabling it now ..."
        $res = Enable-WindowsOptionalFeature -Online -FeatureName $f -All -NoRestart
        if ($res.RestartNeeded) { $needReboot = $true }
        Ok "$f enabled."
    }
}

# ---------------------------------------------------------------- 4. quit docker
Step "4/7  Stopping Docker Desktop"
$names = @('Docker Desktop', 'com.docker.backend', 'com.docker.build', 'com.docker.dev-envs')
foreach ($n in $names) {
    Get-Process -Name $n -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 3
Ok "Docker Desktop stopped."

# ---------------------------------------------------------------- 5. update WSL
Step "5/7  Updating WSL to the Microsoft Store build"

$wslExe = Join-Path $env:SystemRoot 'System32\wsl.exe'
if (-not (Test-Path $wslExe)) { $wslExe = 'wsl.exe' }

$updated = $false

Write-Host "        > wsl --update"
& $wslExe '--update'
if ($LASTEXITCODE -eq 0) { $updated = $true }

if (-not $updated) {
    Warn "wsl --update failed (exit $LASTEXITCODE). Retrying via web download ..."
    Write-Host "        > wsl --update --web-download"
    & $wslExe '--update' '--web-download'
    if ($LASTEXITCODE -eq 0) { $updated = $true }
}

if (-not $updated) {
    Warn "Still failing. Installing the Store WSL package without a Linux distro ..."
    Write-Host "        > wsl --install --no-distribution"
    & $wslExe '--install' '--no-distribution'
    if ($LASTEXITCODE -eq 0) { $updated = $true; $needReboot = $true }
}

if (-not $updated) {
    Err "Automatic WSL update failed."
    Write-Host ""
    Write-Host "        Manual fallback - do BOTH of these:"
    Write-Host "        a) WSL2 kernel update package:"
    Write-Host "           https://aka.ms/wsl2kernel   (download wsl_update_x64.msi, run it)"
    Write-Host "        b) Current WSL release by hand:"
    Write-Host "           https://github.com/microsoft/WSL/releases   (latest wsl.*.x64.msi)"
    Write-Host "        Then reboot and run this script again."
    exit 2
}
Ok "WSL update command finished."

# ---------------------------------------------------------------- 6. verify WSL
Step "6/7  Restarting WSL and verifying the version"
& $wslExe '--shutdown'
Start-Sleep -Seconds 3

Write-Host "        > wsl --version"
& $wslExe '--version'
Write-Host ""
Write-Host "        > wsl -l -v"
& $wslExe '--list' '--verbose'
Write-Host ""

$probe = (& $wslExe '--version' 2>&1 | Out-String)
if ($probe -match 'WSL\s+version' -or $probe -match 'Kernel\s+version') {
    Ok "WSL now reports a real version - the Store build is in place."
} else {
    Warn "wsl --version still does not report a version."
    Warn "A reboot is very likely required. Reboot, then run this script again."
    $needReboot = $true
}

# ---------------------------------------------------------------- 7. start docker
Step "7/7  Starting Docker Desktop and waiting for the engine"
$dd = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
if (Test-Path $dd) {
    Start-Process $dd
    Write-Host "        Waiting for the Docker engine (up to 4 minutes) ..."
} else {
    Err "Docker Desktop.exe not found at $dd - start it manually."
}

$deadline = (Get-Date).AddSeconds(240)
$engineUp = $false
while ((Get-Date) -lt $deadline) {
    & docker info *> $null
    if ($LASTEXITCODE -eq 0) { $engineUp = $true; break }
    Write-Host "        ... waiting"
    Start-Sleep -Seconds 8
}

Write-Host ""
if ($engineUp) {
    Ok "Docker engine is UP."
    Write-Host ""
    Write-Host "  Next - start your postgres service:" -ForegroundColor Cyan
    Write-Host "    cd C:\zero\learningAssistant\learning-assistant"
    Write-Host "    docker compose up -d postgres"
    Write-Host "    docker compose ps"
} else {
    Warn "The Docker engine did not come up within 4 minutes."
    Write-Host ""
    Write-Host "  If this run changed a Windows feature or the WSL package, REBOOT now"
    Write-Host "  and run the script once more - that alone fixes most remaining cases."
    Write-Host ""
    Write-Host "  Still stuck? The decisive log line is in:"
    Write-Host "    %LOCALAPPDATA%\Docker\log\host\com.docker.backend.exe.log"
}

if ($needReboot) {
    Write-Host ""
    Write-Host "  *** A REBOOT IS REQUIRED to finish this repair. ***" -ForegroundColor Yellow
    Write-Host "      Reboot Windows, then start Docker Desktop normally."
}
Write-Host ""
