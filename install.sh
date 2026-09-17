#!/usr/bin/env bash
# install.sh - install the QIDI auto-calibration extras onto a printer
#
# Copyright (C) 2026  Budd
# Licensed under PolyForm Strict 1.0.0 - see LICENSE.md.
#
#   ./install.sh 192.168.1.132
#   ./install.sh 192.168.1.132 --diagnostics   also install the probe tools
#   ./install.sh 192.168.1.132 --keep-old      do NOT remove superseded modules
#   ./install.sh 192.168.1.132 --clean-logs    also delete rotated logs
#   ./install.sh 192.168.1.132 --dry-run       show what would happen
#
# Default login is qidi / qiditech. You are asked for the password ONCE.
#
# WHY ONE SSH CONNECTION, AND NOT SSH MULTIPLEXING
#   This used to open a ControlMaster and reuse it for ~20 commands. That does
#   not work on Windows. Git Bash's ssh is MSYS2/Cygwin, whose Unix sockets are
#   emulated over TCP and CANNOT PASS FILE DESCRIPTORS - which is exactly what a
#   multiplexed session needs. The symptom is deceptive: the master starts, and
#   `ssh -O check` cheerfully reports "Master running", but every real session
#   through it dies with
#
#       mux_client_request_session: read from master failed: Connection reset
#
#   and ssh then falls back to a fresh connection, asking for the password
#   again - or failing outright in a script with no terminal. Since the README
#   tells Windows users to use Git Bash, that broke the documented path.
#
#   So: everything the printer has to do is assembled into ONE script here,
#   with the module files carried along as a base64 tar payload, and handed to a
#   single `ssh ... bash -s`. One connection, one password, no multiplexing, and
#   it behaves the same on Windows, macOS and Linux. The restarts afterwards go
#   through Moonraker over HTTP, which needs no password at all.
#
# WHAT IT HAS TO GET RIGHT, and why
#   * printer.cfg carries a "#*# <--- SAVE_CONFIG --->" block that Klipper
#     REWRITES on every save. Anything below it is destroyed. Our sections are
#     inserted ABOVE that marker.
#   * printer.cfg is backed up before any edit, timestamped, on the printer.
#   * Klipper caches imported modules, so a plain RESTART does not reload an
#     edited extra. The install does a full service restart, then a firmware
#     restart, which is the only sequence that reliably picks up new files.
#   * There is no passwordless sudo on these machines, so everything happens as
#     the qidi user. klippy/extras is writable by qidi, which is why this works.

set -euo pipefail

HOST="${1:-}"
[ -n "$HOST" ] || {
  echo "usage: $0 <printer-ip> [--diagnostics] [--keep-old] [--clean-logs] [--dry-run]"
  exit 1
}
shift || true

USER_NAME="${QIDI_USER:-qidi}"
EXTRAS="/home/${USER_NAME}/klipper/klippy/extras"
CFG="/home/${USER_NAME}/printer_data/config/printer.cfg"
LOGS="/home/${USER_NAME}/printer_data/logs"
MOONRAKER="http://${HOST}:7125"
DIAG=0; CLEAN=1; DRY=0; CLEANLOGS=0

for a in "$@"; do
  case "$a" in
    --diagnostics) DIAG=1 ;;
    --clean)       CLEAN=1 ;;      # kept for compatibility; now the default
    --keep-old)    CLEAN=0 ;;
    --clean-logs)  CLEANLOGS=1 ;;
    --dry-run)     DRY=1 ;;
    *) echo "unknown option: $a"; exit 1 ;;
  esac
done

# The six modules QIDI_CALIBRATE needs. Section name == module name, so there is
# no versioned-copy confusion to inherit.
CORE=(qidi_flow_ramp.py qidi_pa_envelope.py qidi_pa_measure.py
      qidi_pa_table.py qidi_auto_cal.py qidi_cal_wizard.py)
CORE_SECTIONS=(qidi_flow_ramp qidi_pa_envelope qidi_pa_measure
               qidi_pa_table qidi_auto_cal qidi_cal_wizard)

# Read-only probes. Not needed to calibrate; useful for bringing up a new
# machine or diagnosing a sensor. QIDI_CS_READ is in the README's first-run
# checklist, so these are worth having.
DIAG_FILES=(qidi_cs_locate.py qidi_cs_read.py qidi_cs_proto.py
            qidi_cs_timing.py qidi_cs_clock.py qidi_cs_validate.py
            qidi_cs_bulk.py qidi_cs_batch.py)
DIAG_SECTIONS=(qidi_cs_locate qidi_cs_read qidi_cs_proto qidi_cs_timing
               qidi_cs_clock qidi_cs_validate qidi_cs_bulk qidi_cs_batch)

# Leftovers from older versions are DISCOVERED on the printer, not listed here.
#
# Klipper caches imported modules, so during development editing an installed
# extra needed a fresh NAME - which left qidi_flow_ramp8.py, qidi_flow_search4.py
# and qidi_cs_locate_v3.py behind. Any hardcoded list of those goes stale the
# moment another rename happens. So instead: a qidi_*.py in klippy/extras that
# carries OUR copyright line and is not a module this project ships is a
# leftover.
#
# The copyright check is what makes it safe - without it, an unrelated third
# party extra called qidi_something.py would be deleted. Files without the
# marker are reported and left alone.
OWNER_MARKER='Copyright (C) 2026  Budd'

FILES=("${CORE[@]}"); SECTIONS=("${CORE_SECTIONS[@]}")
if [ "$DIAG" = 1 ]; then
  FILES+=("${DIAG_FILES[@]}"); SECTIONS+=("${DIAG_SECTIONS[@]}")
fi

# Cleanup compares against EVERY module this project ships, not just the ones
# being installed on this run. Comparing against FILES would mean a plain
# install with no --diagnostics deleted the eight diagnostic modules. Not
# installing something is not a reason to remove it.
KNOWN=("${CORE[@]}" "${DIAG_FILES[@]}")

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HERE/printer_setup.sh" ] || {
  echo "missing $HERE/printer_setup.sh - it carries the printer-side logic"; exit 1; }
for f in "${FILES[@]}"; do
  [ -f "$HERE/$f" ] || { echo "missing source file: $HERE/$f"; exit 1; }
done

echo "=============================================================="
echo " QIDI auto-calibration installer"
echo "   printer : ${USER_NAME}@${HOST}"
echo "   modules : ${#FILES[@]}  (diagnostics: $([ "$DIAG" = 1 ] && echo yes || echo no))"
echo "   cleanup : $([ "$CLEAN" = 1 ] && echo "superseded modules removed" || echo "no (--keep-old)")"
echo "   logs    : $([ "$CLEANLOGS" = 1 ] && echo "rotated logs deleted" || echo untouched)"
[ "$DRY" = 1 ] && echo "   DRY RUN - nothing will be changed"
echo "=============================================================="
echo
echo "-> connecting (password is 'qiditech' unless you changed it)"
echo "   you are asked once; everything happens over this one connection"
echo

# ---------------------------------------------------------------------------
# Build the script the printer will run, and stream it over one connection.
# Settings go first as plain assignments, then the payload, then the logic.
# ---------------------------------------------------------------------------
{
  printf 'EXTRAS=%q\n'        "$EXTRAS"
  printf 'CFG=%q\n'           "$CFG"
  printf 'LOGS=%q\n'          "$LOGS"
  printf 'DRY=%q\n'           "$DRY"
  printf 'CLEAN=%q\n'         "$CLEAN"
  printf 'CLEANLOGS=%q\n'     "$CLEANLOGS"
  printf 'OWNER_MARKER=%q\n'  "$OWNER_MARKER"
  printf 'KNOWN=%q\n'         "${KNOWN[*]}"
  printf 'SECTIONS=%q\n'      "${SECTIONS[*]}"
  printf 'STAMP=%q\n'         "$(date +%Y%m%d-%H%M%S)"

  # The modules themselves, as a base64 tar. Skipped on a dry run - there is
  # nothing to extract, and it keeps the dry run instant.
  printf "PAYLOAD='"
  if [ "$DRY" = 0 ]; then
    tar czf - -C "$HERE" "${FILES[@]}" | base64
  fi
  printf "'\n"

  # The printer-side logic lives in printer_setup.sh, shared with the Windows
  # installer so the two cannot drift apart.
  cat "$HERE/printer_setup.sh"
} | ssh -o StrictHostKeyChecking=accept-new "${USER_NAME}@${HOST}" 'bash -s'

[ "$DRY" = 1 ] && exit 0

# ---------------------------------------------------------------------------
# Restarts go through Moonraker over HTTP - no password, no second connection.
# ---------------------------------------------------------------------------
echo
echo "-> restarting klipper (service restart, then firmware restart)"
curl -s --max-time 30 -X POST "${MOONRAKER}/machine/services/restart?service=klipper" >/dev/null || true
sleep 30
curl -s --max-time 30 -X POST "${MOONRAKER}/printer/firmware_restart"        >/dev/null || true
sleep 30

STATE="$(curl -s --max-time 10 "${MOONRAKER}/printer/info" || true)"
echo
if echo "$STATE" | grep -q '"state": *"ready"'; then
  echo "   klipper is ready"
else
  echo "   WARNING: klipper did not report ready. Check the printer UI."
  echo "   $STATE"
fi

cat <<'DONE'

==============================================================
 Installed.

 NEXT - and do not skip this:

   1. QIDI_CS_READ SECONDS=10
      Press the nozzle by hand. The value must move. This proves
      the load cell is reachable before anything heats.
      (needs --diagnostics; skip if you did not install those)

   2. Measure counts-per-gram-force for THIS machine.
      The shipped default (182.96) belongs to one specific cell
      and mount. Every abort threshold depends on it, and it is
      the ONLY overload protection the printer has.
      See README.md, "Calibrating counts per gram".

   3. QIDI_CALIBRATE
      Type this into the printer's web console - either in a
      browser at the printer's IP, or OrcaSlicer's Device tab,
      which is the same page. A dialog asks six questions.
      Nothing heats or moves until you press START.

      Choose DRY RUN on the last screen the first time. It
      shows the plan and heats nothing.

   4. QIDI_CALIBRATE again, and press START.
      Watched, with the machine in front of you.

   You do not need this terminal again.

--------------------------------------------------------------

 My Reddit
   https://www.reddit.com/user/Sport_Subject/

 Help me get things I need to make more things, anything helps :)
   https://www.amazon.com.au/hz/wishlist/ls/2ZFL750GMOMU1?ref_=wl_share

==============================================================
DONE
