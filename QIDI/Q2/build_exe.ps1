# build_exe.ps1 - build the Windows installer executable (Q2 build)
#
# Copyright (C) 2026  Budd
# Licensed under PolyForm Strict 1.0.0 - see LICENSE.md.
#
#   .\build_exe.ps1
#
# Produces dist\QIDI-Q2-Calibration-Installer.exe - one file, no Python
# needed on the machine that runs it. Named with the model in it because the
# Max4 build (a separate codebase, QIDI/Max4/) produces its own installer too
# - without a model in the filename, two unrelated printers' installers would
# be indistinguishable sitting in the same Downloads folder.
#
# THIS FOLDER IS A SEPARATE PROJECT FROM THE X-MAX 4 BUILD. It was copied from
# the X-Max 4 sources and altered for the Q2 (see the "QIDI Q2 BUILD" banner in
# each module). The two folders do not share state and are never cross-edited.
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

$Name = "QIDI-Q2-Calibration-Installer"

$Modules = @(
    "qidi_flow_ramp.py", "qidi_pa_envelope.py", "qidi_pa_measure.py",
    "qidi_pa_table.py", "qidi_auto_cal.py", "qidi_cal_wizard.py",
    "qidi_update.py",
    # The bed routine (QIDI_CALIBRATE_BED / QIDI_AUTO_CALIBRATE_BED) -
    # separate files from the chute versions above, so nothing about them can
    # change what the chute routine does. See CHANGELOG/.
    "qidi_flow_bed_search.py", "qidi_pa_bed_measure.py",
    "qidi_auto_cal_bed.py", "qidi_cal_wizard_bed.py",
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
Write-Host "This is NOT tracked in git - upload it to the GitHub Release" -ForegroundColor Yellow
Write-Host "manually (gh release upload <tag> $exe), or it exists only" -ForegroundColor Yellow
Write-Host "on this machine." -ForegroundColor Yellow
