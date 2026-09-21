# qidi_cal_wizard.py - run the whole calibration from the Fluidd console
#
# Copyright (C) 2026  Budd
#
# WHAT IT IS
#   QIDI_CALIBRATE asks six questions as dialogs inside the printer's own web
#   UI, then runs QIDI_AUTO_CALIBRATE with the answers. Nothing else is needed:
#   no terminal, no SSH, no slicer plugin. It works identically in a browser
#   pointed at the printer and in OrcaSlicer's Device tab, because the Device
#   tab is a webview onto that same page.
#
# HOW THE DIALOG WORKS, AND WHAT IT CANNOT DO
#   Fluidd watches console output for lines of the form
#
#       // action:prompt_<verb> <payload>
#
#   and the verbs it implements are exactly these - read off the compiled
#   bundle's switch statement, not guessed:
#
#       begin <title>                    reset the dialog, set its title
#       text <line>                      a line of text
#       button <label>|<gcode>|<colour>  a button in the body
#       footer_button <same>             a button in the action bar
#       show / end                       open / close
#
#   THERE IS NO TEXT INPUT VERB. Fluidd's renderer handles type "text" and type
#   "button" and drops everything else, so a number can only be chosen, never
#   typed. That is why the geometry screen uses steppers rather than a field -
#   see _screen_geom.
#
#   BODY BUTTONS ARE ONE PER ROW. Each item renders into its own v-row holding
#   a `block` (full-width) button, so two buttons cannot share a line. Only
#   footer buttons sit side by side. Layouts here are built around that.
#
#   Clicking a button makes the BROWSER send that button's gcode back to the
#   printer - `handleClick(r) { this.sendGcode(r.command || r.text) }`. So the
#   whole wizard is a state machine whose transitions are the button commands:
#   every screen paints buttons that call _QIDI_CAL_STEP with the answer.
#
#   THERE IS NO CANCEL CALLBACK. Closing the dialog with the X only fires
#   prompt_end at the browser; the printer is never told. So every screen
#   carries an explicit Cancel, and an abandoned wizard must be inert - it is,
#   because nothing happens until the START button on the last screen.
#
# STATE
#   Held on this object, not in printer.cfg. It is a few numbers that only
#   matter between the first question and the START button, and losing them to
#   a restart costs one re-run of the wizard.
#
# USAGE
#   [qidi_cal_wizard]
#
#   QIDI_CALIBRATE                        ask everything
#   QIDI_CALIBRATE DRY=1                  same, but START does a dry run
#   QIDI_CALIBRATE LAYER=0.18 WIDTH=0.65  pre-seed, skip the geometry screens
#   QIDI_CALIBRATE TEMP=265 BLOCKS=20     pre-seed anything else
#
#   Pre-seeded values skip their screen. Seeding everything goes straight to
#   the review screen - but never straight to running, because the whole point
#   is that you see what is about to happen before you press START.
#
#   DRY=1 is a PREFERENCE, not a lock: it makes the dry run the primary button
#   and relabels the review screen. The real run is still reachable, clearly
#   marked. Note the reverse is also true - the dry-run button is on the review
#   screen whether or not DRY was given, so DRY=1 is only ever a convenience.

import logging

# Layer height and line width per nozzle. Width is nozzle + 0.02 throughout,
# which is what QIDI's own Orca profiles use. Layer is nozzle / 2 EXCEPT the
# 0.6, where 0.24 is used instead of QIDI's 0.30 standard - that is the height
# this machine actually prints at, and the table is only valid for the geometry
# it was measured at.
NOZZLE_GEOM = {
    0.2: (0.10, 0.22),
    0.4: (0.20, 0.42),
    0.6: (0.24, 0.62),
    0.8: (0.40, 0.82),
}

# Temperature starts at whatever the UI already has set, and is nudged from
# there. Presets were tried first and replaced: a fixed list is wrong for most
# filaments, and the one button that mattered - "use the current target" - was
# hidden whenever the printer was cold, which is exactly when you set up a
# calibration.
TEMP_STEPS = (20, 5, 1)
DEFAULT_TEMP = 275.0

# qidi_auto_cal takes TEMP with above=150 and below=350, both EXCLUSIVE. The
# wizard must never hand it a value it will reject, so clamp inside those.
TEMP_MIN = 155.0
TEMP_MAX = 345.0

# Precision comes from transition count and averages down cleanly: at 3 blocks
# two runs were 21% apart, at 12 the run-to-run sd matched the within-run
# standard error. 12 is a floor, not a default to go below.
BLOCKS_CHOICES = [(12, "standard - about 4 min"),
                  (20, "better - about 6 min"),
                  (30, "best - about 9 min")]

POINTS_CHOICES = [(3, "quick - a coarse curve"),
                  (5, "standard"),
                  (7, "detailed - more table rows")]

# Stepper sizes for the geometry screen. Coarse moves a whole layer class,
# fine trims it.
GEOM_STEPS = (0.04, 0.01)

SCREENS = ('temp', 'nozzle', 'geom', 'blocks', 'points', 'review')

# Which answers each question screen supplies.
#
# A screen is skipped only when every answer it supplies was given ON THE
# COMMAND LINE - never merely because a value exists. Choosing a nozzle SEEDS a
# layer and width, and the user still has to see and be able to adjust them, so
# "has a value" is the wrong test. Getting this wrong skipped the geometry
# screen entirely the moment a nozzle was picked.
ANSWERS = {'temp': ('temp',), 'nozzle': ('nozzle',),
           'geom': ('layer', 'width'), 'blocks': ('blocks',),
           'points': ('points',)}

# Which screen an answer came from, so the handler knows where to advance from.
KEY_SCREEN = {'temp': 'temp', 'nozzle': 'nozzle', 'blocks': 'blocks',
              'points': 'points'}


class QidiCalWizard:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.def_nozzle = config.getfloat('nozzle_diameter', 0.4, above=0.)
        self.def_temp = config.getfloat('default_temp', DEFAULT_TEMP,
                                        minval=TEMP_MIN, maxval=TEMP_MAX)
        self.state = {}
        self.seeded = set()
        self.visited = set()
        self.dry = False
        self.gcode.register_command('QIDI_CALIBRATE', self.cmd_CALIBRATE,
                                    desc=self.cmd_CALIBRATE_help)
        self.gcode.register_command('_QIDI_CAL_STEP', self.cmd_STEP,
                                    desc=self.cmd_STEP_help)

    # -- dialog plumbing ---------------------------------------------------
    def _raw(self, line):
        self.gcode.respond_raw("// action:" + line)

    def _close(self):
        self._raw("prompt_end")

    def _paint(self, title, lines, buttons, footer):
        """buttons and footer are (label, gcode, colour) triples."""
        self._close()                       # clear any stale dialog first
        self._raw("prompt_begin " + title)
        for t in lines:
            # Never emit a bare verb: Fluidd's regex makes the payload optional
            # and an empty one arrives as undefined, so a blank spacer line
            # carries a space instead of nothing.
            self._raw("prompt_text " + (t if t else " "))
        for label, cmd, colour in buttons:
            self._raw("prompt_button %s|%s|%s" % (label, cmd, colour))
        for label, cmd, colour in footer:
            self._raw("prompt_footer_button %s|%s|%s" % (label, cmd, colour))
        self._raw("prompt_show")

    def _target_temp(self):
        ext = self.printer.lookup_object('extruder', None)
        if ext is None:
            return 0.0
        try:
            return float(ext.get_status(self.reactor.monotonic())['target'])
        except Exception:
            return 0.0

    # -- the screens -------------------------------------------------------
    # Each returns (title, text lines, body buttons, footer buttons).

    def _screen_temp(self):
        """Steppers, like the geometry screen - see TEMP_STEPS for why this is
        not a list of presets."""
        big, mid, small = TEMP_STEPS
        b = []
        for step in (-big, -mid, -small, small, mid, big):
            b.append(("temp   %+d C" % step,
                      "_QIDI_CAL_STEP ADJ=temp BY=%d" % step,
                      "orange" if abs(step) == big else "amber"))
        started = self._target_temp()
        lines = ["%.0f C" % self.state.get('temp', DEFAULT_TEMP),
                 "Nozzle temperature to calibrate at.",
                 "Use the value you actually print this filament at."]
        if started >= 150.:
            lines.append("(started from the target already set: %.0f C)"
                         % started)
        return ("Calibrate 1/6  -  Temperature", lines, b,
                [("Use this", "_QIDI_CAL_STEP DONE=temp", "primary"),
                 self._cancel()])

    def _screen_nozzle(self):
        b = []
        for d in sorted(NOZZLE_GEOM):
            colour = "primary" if abs(d - self.def_nozzle) < 1e-6 else "secondary"
            h, w = NOZZLE_GEOM[d]
            b.append(("%.1f mm      (%.2f layer / %.2f width)" % (d, h, w),
                      "_QIDI_CAL_STEP SET=nozzle VAL=%.1f" % d, colour))
        return ("Calibrate 2/6  -  Nozzle",
                ["Which nozzle is fitted.",
                 "This only picks the starting geometry - you can adjust it "
                 "next."],
                b, [self._back('temp'), self._cancel()])

    def _screen_geom(self):
        """Steppers, because the dialog has no text input. Coarse and fine, one
        button per row - the renderer gives every item its own full-width row,
        so a -/+ pair cannot share a line."""
        h = self.state.get('layer')
        w = self.state.get('width')
        coarse, fine = GEOM_STEPS
        b = []
        for key, colour in (('layer', 'blue'), ('width', 'teal')):
            for step in (-coarse, -fine, fine, coarse):
                b.append(("%s   %+.2f" % (key, step),
                          "_QIDI_CAL_STEP ADJ=%s BY=%.2f" % (key, step),
                          colour))
        return ("Calibrate 3/6  -  Geometry",
                ["%.2f mm layer  /  %.2f mm line width" % (h, w),
                 "The table is only valid for the geometry it is measured at.",
                 "To jump straight to a value instead, cancel and run:",
                 "QIDI_CALIBRATE LAYER=0.18 WIDTH=0.65"],
                b,
                [("Use these", "_QIDI_CAL_STEP DONE=geom", "primary"),
                 self._back('nozzle'), self._cancel()])

    def _screen_blocks(self):
        b = [("%d blocks   %s" % (n, what),
              "_QIDI_CAL_STEP SET=blocks VAL=%d" % n,
              "primary" if n == 12 else "secondary")
             for n, what in BLOCKS_CHOICES]
        return ("Calibrate 4/6  -  Precision",
                ["How many square-wave blocks per flow point.",
                 "Precision comes from transition count and averages down "
                 "cleanly, so more blocks keeps helping."],
                b, [self._back('geom'), self._cancel()])

    def _screen_points(self):
        b = [("%d points   %s" % (n, what),
              "_QIDI_CAL_STEP SET=points VAL=%d" % n,
              "primary" if n == 5 else "secondary")
             for n, what in POINTS_CHOICES]
        return ("Calibrate 5/6  -  Flow points",
                ["How many flow rates to measure PA at.",
                 "Each one becomes two rows of the Orca table."],
                b, [self._back('blocks'), self._cancel()])

    def _screen_review(self):
        """The dry-run button is always here. DRY=1 on the command line only
        changes which one is offered first - it never removes the ability to
        do the real run, and never makes the real run the accidental choice."""
        s = self.state
        lines = ["%.0f C     %.1f mm nozzle" % (s['temp'], s['nozzle']),
                 "%.2f layer / %.2f width" % (s['layer'], s['width']),
                 "%d blocks at %d flow points" % (s['blocks'], s['points']),
                 ""]
        if self.dry:
            lines += ["DRY RUN - prints the plan only.",
                      "Nothing heats, nothing moves.", "",
                      self._command_line() + " DRY=1"]
            buttons = [("START DRY RUN - plan only",
                        "_QIDI_CAL_STEP GO=1 DRY=1", "primary"),
                       ("No - run it for real (heats and extrudes)",
                        "_QIDI_CAL_STEP GO=1", "error")]
        else:
            lines += ["This HEATS and EXTRUDES over the purge chute.",
                      "Stay with the machine.", "",
                      self._command_line()]
            buttons = [("START", "_QIDI_CAL_STEP GO=1", "primary"),
                       ("Dry run - plan only, no heat, no motion",
                        "_QIDI_CAL_STEP GO=1 DRY=1", "secondary")]
        return ("Calibrate 6/6  -  %s" % ("Dry run" if self.dry else "Ready"),
                lines, buttons, [self._back('points'), self._cancel()])

    def _back(self, screen):
        return ("Back", "_QIDI_CAL_STEP GOTO=%s" % screen, "secondary")

    def _cancel(self):
        return ("Cancel", "_QIDI_CAL_STEP CANCEL=1", "error")

    def _command_line(self):
        s = self.state
        return ("QIDI_AUTO_CALIBRATE TEMP=%.0f BLOCKS=%d POINTS=%d "
                "LAYER=%.3f WIDTH=%.3f"
                % (s['temp'], s['blocks'], s['points'], s['layer'], s['width']))

    # -- the state machine -------------------------------------------------
    def _show(self, name):
        title, lines, buttons, footer = getattr(self, '_screen_' + name)()
        self.visited.add(name)
        self._paint(title, lines, buttons, footer)

    def _advance(self, frm=None):
        """The next screen to show after `frm` (or the first, if None),
        skipping any whose answers were pre-seeded on the command line."""
        start = 0 if frm is None else SCREENS.index(frm) + 1
        for name in SCREENS[start:-1]:
            if not all(k in self.seeded for k in ANSWERS[name]):
                return name
        return 'review'

    def _unready(self):
        """The first screen that must be shown before this can launch, or None.

        Presence of a VALUE is not the test. The nozzle, layer and width always
        hold a value - they are seeded from the configured nozzle so that any
        screen can be painted - so a check for missing values would wave through
        questions the user never saw. A screen is satisfied only if it was
        actually shown, or its answer was given on the command line."""
        for name in SCREENS[:-1]:
            answered = all(k in self.seeded for k in ANSWERS[name])
            if not answered and name not in self.visited:
                return name
            if any(self.state.get(k) is None for k in ANSWERS[name]):
                return name
        return None

    def _set_nozzle(self, d, reseed=False):
        """Choosing a nozzle seeds the geometry. `reseed` is for a click on the
        nozzle screen, which should move the geometry to that nozzle's defaults;
        without it, a layer or width given on the command line survives."""
        self.state['nozzle'] = d
        h, w = NOZZLE_GEOM.get(round(d, 2),
                               (round(d / 2., 2), round(d + .02, 2)))
        for key, val in (('layer', h), ('width', w)):
            if key in self.seeded:
                continue        # typed on the command line - never clobber it
            if reseed or self.state.get(key) is None:
                self.state[key] = val

    def _adjust(self, key, by):
        noz = self.state.get('nozzle', self.def_nozzle)
        v = round(self.state.get(key, 0.) + by, 3)
        if key == 'temp':
            self.state[key] = min(max(v, TEMP_MIN), TEMP_MAX)
            return
        if key == 'layer':
            v = min(max(v, 0.04), round(0.75 * noz, 3))
        else:
            v = min(max(v, round(0.8 * noz, 3)), round(2.0 * noz, 3))
        self.state[key] = v

    # -- commands ----------------------------------------------------------
    cmd_CALIBRATE_help = ("Ask for the calibration settings as dialogs in the "
                          "printer's web UI, then run the whole thing. "
                          "[DRY=1] [TEMP=] [NOZZLE=] [LAYER=] [WIDTH=] "
                          "[BLOCKS=] [POINTS=] pre-seed and skip their "
                          "question.")

    def cmd_CALIBRATE(self, gcmd):
        err_cls = type(gcmd.error("probe"))
        try:
            self.state = {}
            self.seeded = set()
            self.visited = set()
            self.dry = bool(gcmd.get_int('DRY', 0))
            # Seed the geometry unconditionally, from the given nozzle or the
            # configured one. Every screen can then be painted at any time -
            # a stale button sending GOTO=geom used to crash on a None layer.
            noz = gcmd.get_float('NOZZLE', None, above=0.)
            if noz is not None:
                self.seeded.add('nozzle')
            self._set_nozzle(round(noz if noz is not None
                                   else self.def_nozzle, 2))
            # Temperature is a stepper now, so like the geometry it always
            # needs a value to step FROM. Prefer whatever the UI already has
            # set - that is how anyone arrives here - and fall back to the
            # configured default when the printer is cold.
            tgt = self._target_temp()
            self.state['temp'] = (tgt if tgt >= 150. else self.def_temp)
            for key, param in (('temp', 'TEMP'), ('layer', 'LAYER'),
                               ('width', 'WIDTH')):
                v = gcmd.get_float(param, None, above=0.)
                if v is not None:
                    self.state[key] = v
                    self.seeded.add(key)
            for key, param in (('blocks', 'BLOCKS'), ('points', 'POINTS')):
                v = gcmd.get_int(param, None, minval=1)
                if v is not None:
                    self.state[key] = v
                    self.seeded.add(key)
            gcmd.respond_info(
                "wizard: answer the dialog in the printer's web UI (or in "
                "OrcaSlicer's Device tab - same page). Nothing heats or moves "
                "until you press START.")
            self._show(self._advance())
        except err_cls:
            raise
        except Exception as e:
            logging.exception("qidi_cal_wizard: unhandled error")
            raise gcmd.error("wizard: internal error: %s: %s"
                             % (type(e).__name__, str(e)[:160]))

    cmd_STEP_help = "Internal - the wizard's dialog buttons call this."

    def cmd_STEP(self, gcmd):
        err_cls = type(gcmd.error("probe"))
        try:
            return self._step(gcmd)
        except err_cls:
            raise
        except Exception as e:
            logging.exception("qidi_cal_wizard: unhandled error")
            self._close()
            raise gcmd.error("wizard: internal error: %s: %s"
                             % (type(e).__name__, str(e)[:160]))

    def _step(self, gcmd):
        if gcmd.get_int('CANCEL', 0):
            self._close()
            gcmd.respond_info("wizard: cancelled - nothing was run.")
            return

        key = gcmd.get('SET', None)
        if key is not None:
            if key not in KEY_SCREEN:
                raise gcmd.error("wizard: cannot set '%s'" % (key,))
            val = gcmd.get_float('VAL', None)
            if val is None:
                raise gcmd.error("wizard: SET=%s needs VAL=" % (key,))
            if key == 'nozzle':
                self._set_nozzle(round(val, 2), reseed=True)
            elif key in ('blocks', 'points'):
                self.state[key] = int(val)
            else:
                self.state[key] = val
            self._show(self._advance(KEY_SCREEN[key]))
            return

        adj = gcmd.get('ADJ', None)
        if adj is not None:
            if adj not in ('layer', 'width', 'temp'):
                raise gcmd.error("wizard: cannot adjust '%s'" % (adj,))
            self._adjust(adj, gcmd.get_float('BY', 0.))
            # Repaint the screen the value belongs to, with the new number.
            self._show('temp' if adj == 'temp' else 'geom')
            return

        done = gcmd.get('DONE', None)
        if done is not None:
            if done not in SCREENS:
                raise gcmd.error("wizard: no screen '%s'" % (done,))
            self._show(self._advance(done))
            return

        if gcmd.get_int('GO', 0):
            unready = self._unready()
            if unready is not None:
                self._show(unready)     # refuse to launch half-answered
                return
            dry = gcmd.get_int('DRY', 0)
            self._close()
            cmd = self._command_line() + (" DRY=1" if dry else "")
            gcmd.respond_info("wizard: running  %s" % (cmd,))
            self.gcode.run_script_from_command(cmd)
            return

        goto = gcmd.get('GOTO', None)
        self._show(goto if goto in SCREENS else self._advance())


def load_config(config):
    return QidiCalWizard(config)
