# qidi_update.py - install updates from GitHub, from the printer's own console
#
# Copyright (C) 2026  Budd
# Licensed under PolyForm Strict 1.0.0 - see LICENSE.md.
#
# WHAT IT IS
#   QIDI_UPDATE opens a dialog in the printer's web UI and walks brand -> model
#   -> release -> confirm, then downloads that release and installs it. Same
#   button machinery as the calibration wizard; no terminal, no SSH, no git.
#
#   COMMANDS lists every command this project registers, with its help line.
#
# WHY NOT MOONRAKER'S update_manager
#   `type: git_repo` needs git, which is not installed on these printers and
#   cannot be installed without root. `type: zip` works, but it takes
#   `assets[0]` - literally the first asset on the release, with no filtering -
#   so a release carrying both the Windows .exe and a module zip could install
#   the installer onto the printer. It also has its own fixed UI, which cannot
#   do the brand/model picker.
#
#   This fetches the release ZIPBALL instead (`/repos/{r}/zipball/{tag}`), which
#   GitHub generates from the tag automatically. Nothing to attach, nothing to
#   order, and the .exe can live on the same release for humans to download
#   without this ever seeing it.
#
# HOW BRAND AND MODEL ARE DISCOVERED
#   From the tag names, not a manifest:
#
#       qidi-max4-v2.1.1  ->  brand "qidi", model "max4", version "2.1.1"
#
#   The release list IS the source of truth, so adding a printer is just tagging
#   a release. Tags that do not match are ignored, so a typo produces nothing
#   rather than a phantom model in the menu. The version MUST start with a
#   digit, or a tag that merely CONTAINS "-v" (e.g. "not-a-valid-tag") would
#   parse as a real one and could sort above every genuine release.
#
# EVERY MODEL LIVES IN ONE REPO - each brand/model is its own folder
# (QIDI/Max4/, QIDI/Q2/, ...) with its own complete, self-contained codebase -
# deliberately no code shared between them. That means a release's zipball is
# a snapshot of the WHOLE repo, every model included, not just the one this
# release is for - and the codebases are near-identical in filenames. So a
# file is only ever installed when its path inside the zip is
# .../<brand>/<model>/qidi_*.py for the SPECIFIC brand/model this release's
# own tag names - see the comment on _stage(). Matching by filename alone
# would silently install one model's file onto another model's printer.
#
# THE RISK THIS FILE EXISTS TO MANAGE
#   A Klipper extra that fails to import STOPS KLIPPER STARTING. No console, no
#   Fluidd controls, no way to run this command to undo it - the printer's whole
#   UI is gone until someone SSHes in, and the people who most need a one-click
#   updater are exactly the people who cannot. A careless updater is a remote
#   brick button.
#
#   So nothing is installed until it has been proved to load:
#
#     1. download to a STAGING directory, never over the live files
#     2. py_compile every file            - catches syntax errors
#     3. import each one in a SUBPROCESS  - catches import-time errors too.
#        This works because the shipped modules import only the standard
#        library, so they load fine outside klippy.
#     4. back up the live files, timestamped, only then swap them in
#     5. keep that backup so QIDI_UPDATE ROLLBACK=1 restores it WITHOUT a
#        terminal
#
#   Step 3 is the one that earns its keep: py_compile passes a file that raises
#   the moment it is imported.
#
# WHY IT ALSO TOUCHES printer.cfg NOW (added 2026-09-23, found live)
#   A file landing in klippy/extras is not enough - Klipper only imports an
#   extra whose [section] exists in printer.cfg. The fresh installer
#   (printer_setup.sh) has always added any missing section; QIDI_UPDATE never
#   did, because every module it had ever shipped already had its section from
#   the original install. That gap was invisible until the first release that
#   added a BRAND NEW module (qidi_cal_wizard_bed.py / QIDI_CALIBRATE_BED,
#   v1.2.1): the .py file installed and Klipper restarted, but nothing had
#   ever added [qidi_cal_wizard_bed], so Klipper never loaded it and the
#   command silently did not exist - no error, because there was nothing to
#   error about, just a command that was never registered.
#
#   _ensure_sections() closes that gap the same way printer_setup.sh does:
#   any module just installed whose [section] is not already in printer.cfg
#   gets an empty one inserted above the #*# SAVE_CONFIG marker (Klipper
#   rewrites everything below that line on every save, so anything placed
#   after it is silently eaten). printer.cfg is backed up first, the same
#   backup-before-touch discipline as the module files themselves. Best-
#   effort and non-fatal: by the time this runs the files are already
#   validated and copied, so a printer.cfg that cannot be found or written is
#   reported, not treated as a reason to undo an otherwise-good install - the
#   user can add the section by hand, same as a DRY install.sh run would have
#   shown them to.
#
#   THIS HALTED A REAL PRINTER, ONCE (same day). The first version of this
#   handed _ensure_sections() EVERY qidi_*.py name INSTALLABLE matched -
#   including qidi_installer.py, the Windows PC-side installer, which is
#   valid importable Python (so py_compile and the import guard both pass
#   it) but defines no load_config. It got [qidi_installer] added to
#   printer.cfg, and Klipper halted on "Section 'qidi_installer' is not a
#   valid config section" - correctly, since it genuinely is not one.
#   Fixed by having _validate() report back WHICH imported files actually
#   define load_config, by inspecting the real imported module in the same
#   subprocess that already tests importing it - not by naming
#   qidi_installer.py specifically, so any future companion script that
#   is not a real extra is caught the same way without being listed here.
#
# USAGE
#   [qidi_update]
#   repo: Buddman69/Automm3AdaptivePA
#   #cfg_path: ~/printer_data/config/printer.cfg   # override only - normally
#                                                    # found automatically
#
#   QIDI_UPDATE                 pick brand, model, release, confirm
#   QIDI_UPDATE CHECK=1         print what is installed and what is newest
#   QIDI_UPDATE ROLLBACK=1      restore the backup taken by the last install
#   COMMANDS                    list every command this project provides

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

try:
    from urllib.request import Request, urlopen
except ImportError:                                  # pragma: no cover
    from urllib2 import Request, urlopen             # type: ignore

DEFAULT_REPO = "Buddman69/Automm3AdaptivePA"
API = "https://api.github.com"
HTTP_TIMEOUT = 20.0
MAX_ZIP_BYTES = 25 * 1024 * 1024     # a source zip is ~1 MB; this is a sanity cap

# brand-model-vX.Y.Z  ->  ('qidi', 'max4', '2.1.1')
#
# The version MUST start with a digit. Without that, "not-a-valid-tag" parses
# happily as brand "not", model "a", version "alid-tag" - because "-v" occurs
# inside the word "valid" - and then sorts ABOVE every real version, because a
# non-numeric chunk outranks a numeric one in version_key. A junk tag would
# have been offered as the newest release.
TAG_RE = re.compile(r'^([A-Za-z0-9]+)-([A-Za-z0-9.]+)-v(\d[A-Za-z0-9.\-_]*)$')

# Only files matching this are ever written into klippy/extras. A release
# cannot drop arbitrary filenames onto the printer.
INSTALLABLE = re.compile(r'^qidi_[A-Za-z0-9_]+\.py$')

# Anchor for inserting a missing config section, identical to printer_setup.sh's
# grep pattern - the two must always agree on what counts as "the marker".
SAVE_CONFIG_RE = re.compile(r'^#\*# <-* SAVE_CONFIG', re.MULTILINE)

PRETTY = {'qidi': 'QIDI', 'max4': 'Max4', 'q2': 'Q2'}


def pretty(s):
    return PRETTY.get(s.lower(), s.replace('_', ' ').title())


def version_key(v):
    """Sort versions numerically where possible, so 1.10 beats 1.9."""
    parts = []
    for chunk in re.split(r'[.\-_]', v):
        parts.append((0, int(chunk)) if chunk.isdigit() else (1, chunk))
    return parts


class QidiUpdate:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.repo = config.get('repo', DEFAULT_REPO).strip().strip('/')
        # Override only - normally found from Klipper's own start args (see
        # _cfg_path). Escape hatch for a host where that is ever wrong.
        self.cfg_path_override = config.get('cfg_path', None)
        self.extras = os.path.dirname(os.path.abspath(__file__))
        data = config.get('data_dir', '~/printer_data/qidi_update')
        self.data_dir = os.path.expanduser(data)
        self.state_path = os.path.join(self.data_dir, 'installed.json')
        self.backup_dir = os.path.join(self.data_dir, 'backup')
        self.releases = []          # cached from the last fetch
        self.sel = {}
        self.gcode.register_command('QIDI_UPDATE', self.cmd_UPDATE,
                                    desc=self.cmd_UPDATE_help)
        self.gcode.register_command('_QIDI_UPD_STEP', self.cmd_STEP,
                                    desc="Internal - QIDI_UPDATE's buttons.")
        self.gcode.register_command('COMMANDS', self.cmd_COMMANDS,
                                    desc=self.cmd_COMMANDS_help)

    # -- dialog plumbing (same contract as qidi_cal_wizard) ----------------
    def _raw(self, line):
        self.gcode.respond_raw("// action:" + line)

    def _close(self):
        self._raw("prompt_end")

    def _paint(self, title, lines, buttons, footer):
        self._close()
        self._raw("prompt_begin " + title)
        for t in lines:
            self._raw("prompt_text " + (t if t else " "))
        for label, cmd, colour in buttons:
            self._raw("prompt_button %s|%s|%s" % (label, cmd, colour))
        for label, cmd, colour in footer:
            self._raw("prompt_footer_button %s|%s|%s" % (label, cmd, colour))
        self._raw("prompt_show")

    def _cancel(self):
        return ("Cancel", "_QIDI_UPD_STEP CANCEL=1", "error")

    # -- github -----------------------------------------------------------
    def _get(self, url, binary=False):
        # ALWAYS the GitHub JSON Accept header, binary request or not.
        #
        # This looks backwards - 'binary=True' asks for bytes, so
        # 'application/octet-stream' looks like the right Accept - and that IS
        # correct for downloading a release ASSET. It is wrong here, because
        # the zipball URL is not an asset download: it is an ordinary
        # api.github.com endpoint that content-negotiates on Accept like every
        # other GitHub API call, then 302-redirects to codeload.github.com for
        # the actual bytes. Sending octet-stream to api.github.com itself gets
        # "415 Unsupported Media Type" before the redirect ever happens -
        # found by testing this against the real API, since the test suite
        # mocks _get() and could not have caught it. codeload.github.com does
        # not content-negotiate at all, so the JSON header rides along
        # harmlessly once redirected.
        req = Request(url, headers={'User-Agent': 'qidi-update',
                                    'Accept': 'application/vnd.github+json'})
        resp = urlopen(req, timeout=HTTP_TIMEOUT)
        try:
            if binary:
                return resp.read(MAX_ZIP_BYTES + 1)
            return json.loads(resp.read().decode('utf-8'))
        finally:
            resp.close()

    def fetch_releases(self):
        """Every release whose tag parses, newest version first per model."""
        data = self._get("%s/repos/%s/releases?per_page=100" % (API, self.repo))
        out = []
        for r in data or []:
            tag = (r.get('tag_name') or '').strip()
            m = TAG_RE.match(tag)
            if not m:
                continue            # not ours, or a typo - ignore, never guess
            brand, model, ver = m.group(1), m.group(2), m.group(3)
            out.append({'tag': tag, 'brand': brand.lower(),
                        'model': model.lower(), 'version': ver,
                        'prerelease': bool(r.get('prerelease')),
                        'published': r.get('published_at') or ''})
        out.sort(key=lambda d: version_key(d['version']), reverse=True)
        return out

    # -- installed state --------------------------------------------------
    def installed(self):
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except Exception:
            return {}

    def _installed_label(self):
        d = self.installed()
        return d.get('tag') or "unknown (never updated from here)"

    # -- the screens ------------------------------------------------------
    def _screen_brand(self):
        brands = sorted({r['brand'] for r in self.releases})
        b = [(pretty(x), "_QIDI_UPD_STEP SET=brand VAL=%s" % x, "primary")
             for x in brands]
        return ("Update 1/4  -  Brand",
                ["Installed: %s" % self._installed_label(),
                 "%d release(s) found in %s" % (len(self.releases), self.repo)],
                b, [self._cancel()])

    def _screen_model(self):
        models = sorted({r['model'] for r in self.releases
                         if r['brand'] == self.sel.get('brand')})
        b = [(pretty(x), "_QIDI_UPD_STEP SET=model VAL=%s" % x, "primary")
             for x in models]
        return ("Update 2/4  -  Model",
                ["Brand: %s" % pretty(self.sel.get('brand', '')),
                 "Pick the printer this is being installed on."],
                b, [("Back", "_QIDI_UPD_STEP GOTO=brand", "secondary"),
                    self._cancel()])

    def _matching(self):
        return [r for r in self.releases
                if r['brand'] == self.sel.get('brand')
                and r['model'] == self.sel.get('model')]

    def _screen_release(self):
        b = []
        for r in self._matching():
            label = "%s%s" % (r['version'], "   (pre-release)"
                              if r['prerelease'] else "")
            b.append((label, "_QIDI_UPD_STEP SET=tag VAL=%s" % r['tag'],
                      "secondary" if r['prerelease'] else "primary"))
        lines = ["%s %s" % (pretty(self.sel.get('brand', '')),
                            pretty(self.sel.get('model', ''))),
                 "Newest first."]
        if not b:
            lines.append("No releases for this model yet.")
        return ("Update 3/4  -  Release", lines, b,
                [("Back", "_QIDI_UPD_STEP GOTO=model", "secondary"),
                 self._cancel()])

    def _screen_confirm(self):
        return ("Update 4/4  -  Confirm",
                ["Installed:  %s" % self._installed_label(),
                 "Install:    %s" % self.sel.get('tag', '?'),
                 "",
                 "Files are downloaded, compiled and test-imported",
                 "before anything is replaced. The current files are",
                 "backed up first, and QIDI_UPDATE ROLLBACK=1 restores",
                 "them. Any brand-new command also gets an empty",
                 "section added to printer.cfg, backed up first too.",
                 "",
                 "Klipper restarts afterwards."],
                [("INSTALL", "_QIDI_UPD_STEP GO=1", "primary")],
                [("Back", "_QIDI_UPD_STEP GOTO=release", "secondary"),
                 self._cancel()])

    def _show(self, name):
        title, lines, buttons, footer = getattr(self, '_screen_' + name)()
        self._paint(title, lines, buttons, footer)

    # -- the install ------------------------------------------------------
    def _stage(self, gcmd, tag):
        """Download the zipball and extract only THIS RELEASE'S OWN MODEL'S
        modules into a temp dir.

        WHY A PATH CHECK, NOT JUST A NAME MATCH
          Every model's code lives in the same repo, side by side
          (QIDI/Max4/, QIDI/Q2/, ...), so a release's zipball is a snapshot of
          the WHOLE repo at that tag - not just the one model's folder.
          Matching qidi_*.py by basename alone would pull whichever model's
          copy the zip happened to iterate last, and the two codebases are
          deliberately near-identical in filenames while being otherwise
          unrelated, so this is not a rare collision - it is every file.

          The tag itself says which model a release is for, and it was
          VALIDATED against TAG_RE when the release list was fetched, so it is
          re-parsed here rather than trusting self.sel to have been filled in
          the same way - the two should always agree, but this makes it
          certain rather than assumed. A file is kept only when its immediate
          parent directory is that model and the one above that is the brand,
          case-insensitively - matching the QIDI/<Model>/ layout this repo
          uses. Anything outside a two-deep brand/model path, or under a
          DIFFERENT model, is skipped.
        """
        m = TAG_RE.match(tag)
        if not m:
            raise gcmd.error("update: '%s' is not a brand-model-vX.Y.Z tag"
                             % (tag,))
        brand, model = m.group(1).lower(), m.group(2).lower()

        url = "%s/repos/%s/zipball/%s" % (API, self.repo, tag)
        gcmd.respond_info("update: downloading %s" % (tag,))
        blob = self._get(url, binary=True)
        if len(blob) > MAX_ZIP_BYTES:
            raise gcmd.error("update: download is larger than %d MB - refusing"
                             % (MAX_ZIP_BYTES // (1024 * 1024),))
        staging = tempfile.mkdtemp(prefix='qidi_upd_')
        zpath = os.path.join(staging, 'release.zip')
        with open(zpath, 'wb') as f:
            f.write(blob)
        names = []
        with zipfile.ZipFile(zpath) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                # Never trust a path from inside the zip for WHERE to write -
                # only the basename is ever used as a destination filename, so
                # nothing can land outside the staging directory.
                parts = info.filename.replace(chr(92), '/').split('/')
                if len(parts) < 3:
                    continue          # not inside <brand>/<model>/ at all
                model_dir, brand_dir, base = parts[-2], parts[-3], parts[-1]
                if model_dir.lower() != model or brand_dir.lower() != brand:
                    continue          # some OTHER model's file in this zip
                if not INSTALLABLE.match(base):
                    continue
                if info.file_size > 2 * 1024 * 1024:
                    continue
                with z.open(info) as src, \
                        open(os.path.join(staging, base), 'wb') as dst:
                    shutil.copyfileobj(src, dst)
                names.append(base)
        if not names:
            shutil.rmtree(staging, ignore_errors=True)
            raise gcmd.error("update: found no qidi_*.py under %s/%s/ in %s"
                             % (m.group(1), m.group(2), tag))
        return staging, sorted(set(names))

    def _validate(self, gcmd, staging, names):
        """Guards 2 and 3: it must compile, AND it must import.

        Also answers a question _ensure_sections() needs: not every
        qidi_*.py this project ships is a Klipper extra. qidi_installer.py in
        particular is the Windows PC-side installer - it imports fine (it is
        valid Python) but defines no load_config, so it is not something
        printer.cfg should ever have a [section] for. Found live: an earlier
        version of this method returned nothing, so _ensure_sections() was
        handed EVERY name including qidi_installer.py, added
        [qidi_installer] to printer.cfg, and Klipper halted on "Section
        'qidi_installer' is not a valid config section" - it imports cleanly,
        so the compile/import guards above never catch this, but Klipper's
        own object-creation step does, the hard way. The subprocess already
        importing each module is asked to also report whether it defines
        load_config, at no extra cost - and this is checked by ACTUALLY
        importing and inspecting the module, not by guessing from the
        filename, so any future companion script that is not a real extra is
        caught the same way without needing to be named here."""
        import py_compile
        for n in names:
            try:
                py_compile.compile(os.path.join(staging, n), doraise=True)
            except Exception as e:
                raise gcmd.error("update: %s will not compile - NOTHING was "
                                 "changed. %s" % (n, str(e)[:160]))
        extras = set()
        for n in names:
            mod = n[:-3]
            try:
                p = subprocess.Popen(
                    [sys.executable, '-c',
                     'import %s as m, sys; '
                     'sys.stdout.write("1" if hasattr(m, "load_config") '
                     'else "0")' % mod],
                    cwd=staging, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE)
                out, err = p.communicate(timeout=30)
                rc = p.returncode
            except Exception as e:
                raise gcmd.error("update: could not test-import %s (%s) - "
                                 "NOTHING was changed" % (n, str(e)[:120]))
            if rc != 0:
                tail = (err or b'').decode('utf-8', 'replace').strip()
                raise gcmd.error(
                    "update: %s compiles but fails on import - NOTHING was "
                    "changed. Klipper would not have restarted. %s"
                    % (n, tail[-200:]))
            if out.strip() == b'1':
                extras.add(n)
        gcmd.respond_info("update: %d file(s) compiled and imported cleanly"
                          % (len(names),))
        return extras

    # -- printer.cfg section insertion -------------------------------------
    def _cfg_path(self):
        if self.cfg_path_override:
            return os.path.expanduser(self.cfg_path_override)
        args = self.printer.get_start_args() or {}
        p = args.get('config_file')
        if not p:
            raise RuntimeError("printer's start args have no config_file")
        return os.path.expanduser(p)

    def _ensure_sections(self, gcmd, names):
        """Add an empty [section] for any just-installed module that does not
        already have one - see the file header, WHY IT ALSO TOUCHES
        printer.cfg NOW. `names` must already be filtered to files that
        define load_config (_validate()'s return value) - a real Klipper
        extra, not a companion script that merely happens to be named
        qidi_*.py. Best-effort: the module files are already validated and
        copied by the time this runs, so a problem here is reported, not
        treated as a reason to undo the install."""
        try:
            path = self._cfg_path()
        except Exception as e:
            gcmd.respond_info(
                "update: could not find printer.cfg (%s) - if any command "
                "is missing after restart, add its [section] by hand"
                % (str(e)[:120],))
            return []
        try:
            with open(path) as f:
                text = f.read()
        except Exception as e:
            gcmd.respond_info(
                "update: could not read %s (%s) - if any command is missing "
                "after restart, add its [section] by hand"
                % (path, str(e)[:120]))
            return []
        missing = [n[:-3] for n in sorted(names)
                  if not re.search(r'(?m)^\[%s\]\s*$' % re.escape(n[:-3]),
                                   text)]
        if not missing:
            return []
        try:
            backup = path + '.bak-upd-' + time.strftime('%Y%m%d-%H%M%S')
            shutil.copy2(path, backup)
            insert = ''.join('\n[%s]\n' % s for s in missing)
            m = SAVE_CONFIG_RE.search(text)
            new_text = (text[:m.start()] + insert + '\n' + text[m.start():]
                       if m else text + insert)
            tmp = path + '.tmp'
            with open(tmp, 'w') as f:
                f.write(new_text)
            os.replace(tmp, path)
        except Exception as e:
            gcmd.respond_info(
                "update: could not update %s (%s) - add these section(s) by "
                "hand: %s" % (path, str(e)[:120],
                              ', '.join('[%s]' % s for s in missing)))
            return []
        gcmd.respond_info("update: printer.cfg backed up to %s" % (backup,))
        gcmd.respond_info("update: added missing section(s): %s"
                          % (', '.join('[%s]' % s for s in missing),))
        return missing

    def _backup(self, names):
        stamp = time.strftime('%Y%m%d-%H%M%S')
        dest = os.path.join(self.backup_dir, stamp)
        os.makedirs(dest)
        saved = []
        for n in names:
            live = os.path.join(self.extras, n)
            if os.path.exists(live):
                shutil.copy2(live, os.path.join(dest, n))
                saved.append(n)
        return dest, saved

    def _install(self, gcmd, tag):
        staging, names = self._stage(gcmd, tag)
        try:
            extras = self._validate(gcmd, staging, names)
            dest, saved = self._backup(names)
            gcmd.respond_info("update: backed up %d file(s) to %s"
                              % (len(saved), dest))
            for n in names:
                shutil.copy2(os.path.join(staging, n),
                             os.path.join(self.extras, n))
            # Only files that actually define load_config get a printer.cfg
            # section - see _validate()'s docstring for why that check
            # matters (qidi_installer.py is a real, live example).
            self._ensure_sections(gcmd, extras)
            os.makedirs(self.data_dir, exist_ok=True)
            with open(self.state_path, 'w') as f:
                json.dump({'tag': tag, 'files': names, 'backup': dest,
                           'time': time.time()}, f, indent=1)
            gcmd.respond_info("update: installed %s (%d files)"
                              % (tag, len(names)))
            return names
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _rollback(self, gcmd):
        st = self.installed()
        dest = st.get('backup')
        if not dest or not os.path.isdir(dest):
            raise gcmd.error("update: no backup to roll back to")
        n = 0
        for f in sorted(os.listdir(dest)):
            if INSTALLABLE.match(f):
                shutil.copy2(os.path.join(dest, f),
                             os.path.join(self.extras, f))
                n += 1
        gcmd.respond_info("update: restored %d file(s) from %s" % (n, dest))
        gcmd.respond_info("update: run FIRMWARE_RESTART to load them")
        return n

    # -- commands ---------------------------------------------------------
    cmd_UPDATE_help = ("Install an update from GitHub, chosen from a dialog in "
                       "the printer's web UI. [CHECK=1] [ROLLBACK=1]")

    def cmd_UPDATE(self, gcmd):
        err_cls = type(gcmd.error("probe"))
        try:
            if gcmd.get_int('ROLLBACK', 0):
                return self._rollback(gcmd)
            self.releases = self.fetch_releases()
            if gcmd.get_int('CHECK', 0):
                gcmd.respond_info("update: installed %s"
                                  % (self._installed_label(),))
                if self.releases:
                    n = self.releases[0]
                    gcmd.respond_info("update: newest release %s (%s %s)"
                                      % (n['tag'], pretty(n['brand']),
                                         pretty(n['model'])))
                else:
                    gcmd.respond_info("update: no matching releases in %s"
                                      % (self.repo,))
                return
            if not self.releases:
                raise gcmd.error(
                    "update: %s has no releases tagged brand-model-vX.Y.Z - "
                    "nothing to install" % (self.repo,))
            self.sel = {}
            gcmd.respond_info("update: answer the dialog in the printer's web "
                              "UI. Nothing is changed until you press INSTALL.")
            self._show('brand')
        except err_cls:
            raise
        except Exception as e:
            logging.exception("qidi_update: unhandled error")
            raise gcmd.error("update: %s: %s" % (type(e).__name__, str(e)[:200]))

    def cmd_STEP(self, gcmd):
        err_cls = type(gcmd.error("probe"))
        try:
            if gcmd.get_int('CANCEL', 0):
                self._close()
                gcmd.respond_info("update: cancelled - nothing was changed.")
                return
            key = gcmd.get('SET', None)
            if key is not None:
                if key not in ('brand', 'model', 'tag'):
                    raise gcmd.error("update: cannot set '%s'" % (key,))
                val = gcmd.get('VAL', None)
                if not val:
                    raise gcmd.error("update: SET=%s needs VAL=" % (key,))
                self.sel[key] = val
                nxt = {'brand': 'model', 'model': 'release',
                       'tag': 'confirm'}[key]
                self._show(nxt)
                return
            if gcmd.get_int('GO', 0):
                tag = self.sel.get('tag')
                if not tag:
                    self._show('brand')
                    return
                self._close()
                names = self._install(gcmd, tag)
                gcmd.respond_info(
                    "update: done. Run FIRMWARE_RESTART to load the new "
                    "modules. If anything goes wrong, QIDI_UPDATE ROLLBACK=1 "
                    "puts the old ones back.")
                return names
            goto = gcmd.get('GOTO', None)
            self._show(goto if goto in ('brand', 'model', 'release',
                                        'confirm') else 'brand')
        except err_cls:
            raise
        except Exception as e:
            logging.exception("qidi_update: unhandled error")
            self._close()
            raise gcmd.error("update: %s: %s" % (type(e).__name__, str(e)[:200]))

    cmd_COMMANDS_help = "List every command this project provides."

    def cmd_COMMANDS(self, gcmd):
        err_cls = type(gcmd.error("probe"))
        try:
            help_map = getattr(self.gcode, 'gcode_help', {}) or {}
            ours = sorted(k for k in help_map
                          if k.startswith('QIDI_') or k == 'COMMANDS')
            if not ours:
                gcmd.respond_info("commands: none registered")
                return
            gcmd.respond_info("Commands from this project:")
            for name in ours:
                desc = (help_map.get(name) or '').strip()
                # One line each; the full help is on the command itself.
                gcmd.respond_info("  %-22s %s" % (name, desc[:90]))
            gcmd.respond_info("Type HELP for Klipper's own commands.")
        except err_cls:
            raise
        except Exception as e:
            logging.exception("qidi_update: unhandled error")
            raise gcmd.error("commands: %s: %s"
                             % (type(e).__name__, str(e)[:160]))


def load_config(config):
    return QidiUpdate(config)
