# build_exe.ps1 - build the Windows installer executable
#
# Copyright (C) 2026  Budd
# Licensed under PolyForm Strict 1.0.0 - see LICENSE.md.
#
#   .\build_exe.ps1
#
# Produces dist\QIDI-Max4-Calibration-Installer.exe - one file, no Python
# needed on the machine that runs it. Named with the model in it because the
# Q2 build (a separate codebase, QIDI/Q2/) produces its own installer too -
# without a model in the filename, two unrelated printers' installers would
# be indistinguishable sitting in the same Downloads folder.
#
# WHY THE MODULES ARE BUNDLED IN
#   --add-data puts every klippy extra and printer_setup.sh INSIDE the exe.
#   PyInstaller unpacks them to a temp directory at run time and the installer
#   reads them from there (see resource() in qidi_installer.py). That keeps the
#   exe self-contained: it never downloads anything, which matters because the
#   licence does not permit redistribution - the exe IS the distribution.
#
# ON ANTIVIRUS
#   Windows SmartScreen and some scanners flag freshly built, unsigned,
#   PyInstaller executables on sight. Nothing here can prevent that; the fix is
#   a code-signing certificate. Do NOT request administrator rights to try to
#   look more legitimate - it makes no difference to a scanner, and the
#   installer genuinely does not need admin: it writes only over SSH to the
#   printer, and touches nothing on the PC outside its own temp directory.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$Name = "QIDI-Max4-Calibration-Installer"

$Modules = @(
    "qidi_flow_ramp.py", "qidi_pa_envelope.py", "qidi_pa_measure.py",
    "qidi_pa_table.py", "qidi_auto_cal.py", "qidi_cal_wizard.py",
    "qidi_update.py",
    "qidi_cs_locate.py", "qidi_cs_read.py", "qidi_cs_proto.py",
    "qidi_cs_timing.py", "qidi_cs_clock.py", "qidi_cs_validate.py",
    "qidi_cs_bulk.py", "qidi_cs_batch.py",
    "printer_setup.sh"
)

Write-Host "checking sources ..." -ForegroundColor Cyan
$missing = $Modules | Where-Object { -not (Test-Path $_) }
if ($missing) {
    Write-Host "missing files:" -ForegroundColor Red
    $missing | ForEach-Object { Write-Host "   $_" }
    exit 1
}
Write-Host "   all $($Modules.Count) present"

# paramiko is the only dependency that is not in the standard library.
Write-Host "checking paramiko ..." -ForegroundColor Cyan
& python -c "import paramiko" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "   installing paramiko"
    & python -m pip install --quiet paramiko
    if ($LASTEXITCODE -ne 0) { Write-Host "pip failed" -ForegroundColor Red; exit 1 }
}
Write-Host "   ok"

# On Windows --add-data uses ';' between source and destination. '.' puts each
# file at the root of the bundle, which is where resource() looks.
$addData = @()
foreach ($m in $Modules) { $addData += @("--add-data", "$m;.") }

Write-Host "building ..." -ForegroundColor Cyan
& python -m PyInstaller `
    --onefile `
    --console `
    --name $Name `
    --clean `
    --noconfirm `
    @addData `
    qidi_installer.py

if ($LASTEXITCODE -ne 0) { Write-Host "build failed" -ForegroundColor Red; exit 1 }

$exe = Join-Path "dist" "$Name.exe"
if (-not (Test-Path $exe)) { Write-Host "no exe produced" -ForegroundColor Red; exit 1 }

$size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host ""
Write-Host "built $exe  ($size MB)" -ForegroundColor Green
Write-Host "Give people this one file - everything is inside it."
Write-Host ""
Write-Host "A COPY OF THIS FILE IS ALSO TRACKED IN GIT, at the repo root of" -ForegroundColor Yellow
Write-Host "this folder (QIDI-Max4-Calibration-Installer.exe) - this build" -ForegroundColor Yellow
Write-Host "only wrote dist\, it did NOT update that tracked copy or the" -ForegroundColor Yellow
Write-Host "GitHub Release asset. After a real change, copy dist\$Name.exe" -ForegroundColor Yellow
Write-Host "over it, commit, and re-upload to the release, or the three" -ForegroundColor Yellow
Write-Host "will quietly drift out of sync." -ForegroundColor Yellow
