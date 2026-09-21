# printer_setup.sh - the printer side of the install.
#
# Copyright (C) 2026  Budd
# Licensed under PolyForm Strict 1.0.0 - see LICENSE.md.
#
# NOT RUN DIRECTLY. Both installers stream this to the printer over one SSH
# connection, after prepending the settings it reads as plain assignments:
#
#   EXTRAS CFG LOGS DRY CLEAN CLEANLOGS OWNER_MARKER KNOWN SECTIONS STAMP
#   PAYLOAD   - the modules, as a base64 tar (empty on a dry run)
#
# It lives in its own file so install.sh and the Windows .exe cannot drift
# apart: there is one copy of the logic that touches printer.cfg, and both
# send exactly these bytes.

set -eu

die() { echo "   ERROR: $*"; exit 1; }

[ -d "$EXTRAS" ] || die "no $EXTRAS - is this a Klipper host?"
[ -w "$EXTRAS" ] || die "$EXTRAS is not writable"
[ -f "$CFG" ]    || die "no $CFG"
echo "   ok - klippy/extras is writable, printer.cfg found"
echo

# -- what is already here ---------------------------------------------------
# Ours, by our own copyright line. Anything else named qidi_*.py belongs to
# somebody else and is never touched.
OURS=""; FOREIGN=""
for f in "$EXTRAS"/qidi_*.py; do
  [ -e "$f" ] || continue
  b="$(basename "$f")"
  if grep -q "$OWNER_MARKER" "$f" 2>/dev/null; then
    OURS="$OURS $b"
  else
    FOREIGN="$FOREIGN $b"
  fi
done

STALE=""
for b in $OURS; do
  keep=0
  for k in $KNOWN; do [ "$b" = "$k" ] && { keep=1; break; }; done
  [ "$keep" = 0 ] && STALE="$STALE $b"
done

if [ "$DRY" = 1 ]; then
  echo "would install these sections above the SAVE_CONFIG marker:"
  for s in $SECTIONS; do echo "   [$s]"; done
  if [ "$CLEAN" = 1 ]; then
    echo "would remove these superseded modules and their sections:"
    if [ -n "$STALE" ]; then
      for b in $STALE; do echo "   $b"; done
    else
      echo "   (none)"
    fi
  fi
  [ -n "$FOREIGN" ] && {
    echo "would leave alone (not ours - no copyright marker):"
    for b in $FOREIGN; do echo "   $b"; done
  }
  if [ "$CLEANLOGS" = 1 ]; then
    echo "would delete rotated logs, never the live ones. Currently:"
    echo "   $(du -sh "$LOGS" 2>/dev/null | cut -f1) total, $(ls "$LOGS"/*.log.[0-9]* 2>/dev/null | wc -l) rotated files"
  fi
  echo
  echo "DRY RUN - nothing was changed."
  exit 0
fi

# -- back up, then install --------------------------------------------------
cp "$CFG" "${CFG}.bak-${STAMP}"
echo "-> printer.cfg backed up to printer.cfg.bak-${STAMP}"

printf '%s' "$PAYLOAD" | base64 -d | tar xzf - -C "$EXTRAS"
echo "-> modules copied"

# Insert any missing section ABOVE the SAVE_CONFIG marker. Klipper rewrites
# everything below that line, so a section placed after it is silently eaten on
# the next save.
echo "-> ensuring config sections"
for s in $SECTIONS; do
  if grep -q "^\[$s\]$" "$CFG"; then
    echo "   [$s] already present"
  else
    # Anchor on SAVE_CONFIG itself, not just on a "#*# <" prefix - the loose
    # form would insert at the first autosave-looking line it met.
    if grep -q '^#\*# <-* SAVE_CONFIG' "$CFG"; then
      sed -i "0,/^#\*# <-* SAVE_CONFIG/s//[$s]\n\n&/" "$CFG"
    else
      printf '\n[%s]\n' "$s" >> "$CFG"
    fi
    echo "   [$s] added"
  fi
done

# -- remove what previous versions left behind ------------------------------
if [ "$CLEAN" = 1 ]; then
  echo "-> modules left by older versions"
  if [ -z "$STALE" ]; then
    echo "   nothing to remove"
  else
    for b in $STALE; do
      s="${b%.py}"
      # The file and its config section must go together. A section naming a
      # module that no longer exists is a config error and Klipper will not
      # start.
      rm -f "$EXTRAS/$b"
      sed -i "/^\[$s\]$/d" "$CFG"
      echo "   removed $b and any [$s] section"
    done
  fi
  [ -n "$FOREIGN" ] && {
    echo "   left alone (not ours - no copyright marker):"
    for b in $FOREIGN; do echo "      $b"; done
  }
fi

# -- rotated logs -----------------------------------------------------------
if [ "$CLEANLOGS" = 1 ]; then
  echo "-> removing rotated logs"
  before="$(du -sh "$LOGS" 2>/dev/null | cut -f1)"
  # Rotated only. The LIVE klippy.log and moonraker.log are never touched:
  # Klipper holds klippy.log open, and it is what any crash report needs.
  rm -f "$LOGS"/*.log.[0-9]* 2>/dev/null || true
  after="$(du -sh "$LOGS" 2>/dev/null | cut -f1)"
  echo "   logs ${before:-?} -> ${after:-?}"
fi

echo
echo "-> printer side done"
