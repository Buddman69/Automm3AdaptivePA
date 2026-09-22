"""QIDI auto-calibration installer - the Windows .exe.

Copyright (C) 2026  Budd
Licensed under PolyForm Strict 1.0.0 - see LICENSE.md.

Asks for the printer's address and login, copies the calibration modules onto
it, edits printer.cfg, removes anything left by an older version, and restarts
Klipper. After this the user never needs a terminal again - everything else
happens in the printer's own web console.

WHY PARAMIKO AND NOT ssh.exe
    Windows ships an OpenSSH client, but there is no supported way to hand it a
    password non-interactively - no sshpass, and its own prompt cannot be driven
    from a pipe. Git Bash's ssh is worse still: its Cygwin sockets cannot pass
    file descriptors, so connection multiplexing silently fails (see the comment
    at the top of install.sh). paramiko speaks SSH directly, so one connection
    with one password works the same everywhere.

WHY IT CARRIES THE MODULES RATHER THAN DOWNLOADING THEM
    The licence does not permit redistribution, so an installer that fetched the
    modules would be putting that on whoever passed the .exe around. Instead
    PyInstaller bundles them INTO the executable and they are read from there.
    The .exe is the distribution, and it comes from one place.

ONE SHARED PRINTER-SIDE SCRIPT
    Everything done on the printer lives in printer_setup.sh, which install.sh
    also sends. There is exactly one copy of the logic that edits printer.cfg.
"""

import base64
import io
import os
import socket
import sys
import tarfile
import time

APP = "QIDI auto-calibration installer"

DEFAULT_USER = "qidi"
DEFAULT_PASS = "qiditech"
SSH_PORT = 22
MOONRAKER_PORT = 7125

# qidi_update.py is the BOOTSTRAP: you cannot run a console command to install
# the thing that gives you console commands, so this installer puts it there.
# After that QIDI_UPDATE maintains everything, including itself.
#
# qidi_flow_bed_search.py / qidi_pa_bed_measure.py / qidi_auto_cal_bed.py are
# the BED routine (QIDI_AUTO_CALIBRATE_BED) - separate files from the chute
# versions, so nothing about them can change what the chute routine does.
CORE = ["qidi_flow_ramp.py", "qidi_pa_envelope.py", "qidi_pa_measure.py",
        "qidi_pa_table.py", "qidi_auto_cal.py", "qidi_cal_wizard.py",
        "qidi_update.py", "qidi_flow_bed_search.py", "qidi_pa_bed_measure.py",
        "qidi_auto_cal_bed.py"]
CORE_SECTIONS = ["qidi_flow_ramp", "qidi_pa_envelope", "qidi_pa_measure",
                 "qidi_pa_table", "qidi_auto_cal", "qidi_cal_wizard",
                 "qidi_update", "qidi_flow_bed_search", "qidi_pa_bed_measure",
                 "qidi_auto_cal_bed"]

DIAG = ["qidi_cs_locate.py", "qidi_cs_read.py", "qidi_cs_proto.py",
        "qidi_cs_timing.py", "qidi_cs_clock.py", "qidi_cs_validate.py",
        "qidi_cs_bulk.py", "qidi_cs_batch.py"]
DIAG_SECTIONS = ["qidi_cs_locate", "qidi_cs_read", "qidi_cs_proto",
                 "qidi_cs_timing", "qidi_cs_clock", "qidi_cs_validate",
                 "qidi_cs_bulk", "qidi_cs_batch"]

# Cleanup compares against every module the project ships, never just the ones
# being installed now - see install.sh for why that distinction matters.
KNOWN = CORE + DIAG
OWNER_MARKER = "Copyright (C) 2026  Budd"

REDDIT_URL = "https://www.reddit.com/user/Sport_Subject/"
WISHLIST_URL = ("https://www.amazon.com.au/hz/wishlist/ls/"
                "2ZFL750GMOMU1?ref_=wl_share")


def resource(name):
    """Files are bundled into the .exe by PyInstaller and unpacked to a temp
    directory at run time; when running as a plain script they sit next to it."""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def rule(ch="="):
    print(ch * 66)


def ask(prompt, default):
    try:
        v = input("%s [%s]: " % (prompt, default)).strip()
    except EOFError:
        return default
    return v or default


def ask_password(prompt, default):
    """Shown, not hidden. The default is printed on the line above and in the
    README, so masking it would protect nothing and only make people wonder
    whether their typing registered."""
    return ask(prompt, default)


def ask_yes(prompt, default=True):
    d = "Y/n" if default else "y/N"
    while True:
        v = ask("%s (%s)" % (prompt, d), "Y" if default else "N").lower()
        if v in ("y", "yes"):
            return True
        if v in ("n", "no"):
            return False
        print("   please answer y or n")


# --------------------------------------------------------------------------
# finding the printer
# --------------------------------------------------------------------------

def local_prefix():
    """This machine's /24, to suggest where to look. Uses a UDP socket to a
    routable address - no packet is actually sent, it just makes the OS pick
    the interface it would route through."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()
    return ip.rsplit(".", 1)[0]


def looks_like_printer(host, timeout=0.35):
    """Moonraker on 7125 is the tell. Only a TCP connect - nothing is sent."""
    try:
        with socket.create_connection((host, MOONRAKER_PORT), timeout):
            return True
    except Exception:
        return False


def discover(prefix, limit=254):
    """Scan the local /24 for something answering on Moonraker's port. Threads,
    because 254 sequential connects at even a short timeout is a minute."""
    import concurrent.futures as cf
    hosts = ["%s.%d" % (prefix, i) for i in range(1, limit + 1)]
    found = []
    with cf.ThreadPoolExecutor(max_workers=64) as ex:
        for host, ok in zip(hosts, ex.map(looks_like_printer, hosts)):
            if ok:
                found.append(host)
    return found


def printer_name(host):
    """Ask Moonraker what it is, so the user can recognise their machine."""
    try:
        import json
        import urllib.request
        url = "http://%s:%d/printer/info" % (host, MOONRAKER_PORT)
        with urllib.request.urlopen(url, timeout=4) as r:
            d = json.load(r).get("result", {})
        return d.get("hostname") or d.get("state") or "?"
    except Exception:
        return "?"


def choose_host():
    print()
    print("Which printer?")
    print("  The address is whatever your router gave the printer - it is NOT")
    print("  the same on every machine, and it can change. You can see it in")
    print("  the printer's network settings, or let this look for it.")
    print()

    prefix = local_prefix()
    if prefix and ask_yes("   Search %s.1-254 for it now?" % prefix, True):
        print("   searching ...", end="", flush=True)
        found = discover(prefix)
        print(" done")
        if found:
            for h in found:
                print("      %-16s %s" % (h, printer_name(h)))
            if len(found) == 1 and ask_yes("   Use %s?" % found[0], True):
                return found[0]
        else:
            print("      nothing answering on port %d." % MOONRAKER_PORT)
            print("      Check the printer is on and on the same network.")
    return ask("   Printer IP address", "192.168.1.132")


# --------------------------------------------------------------------------
# the install itself
# --------------------------------------------------------------------------

def build_payload(files):
    """The modules, as a base64 tar - exactly what printer_setup.sh expects."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in files:
            path = resource(f)
            if not os.path.exists(path):
                raise SystemExit("this build is missing %s" % f)
            tar.add(path, arcname=f)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def sh_quote(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def build_script(files, sections, dry, clean, cleanlogs, user):
    home = "/home/%s" % user
    settings = {
        "EXTRAS": "%s/klipper/klippy/extras" % home,
        "CFG": "%s/printer_data/config/printer.cfg" % home,
        "LOGS": "%s/printer_data/logs" % home,
        "DRY": 1 if dry else 0,
        "CLEAN": 1 if clean else 0,
        "CLEANLOGS": 1 if cleanlogs else 0,
        "OWNER_MARKER": OWNER_MARKER,
        "KNOWN": " ".join(KNOWN),
        "SECTIONS": " ".join(sections),
        "STAMP": time.strftime("%Y%m%d-%H%M%S"),
    }
    out = ["%s=%s" % (k, sh_quote(v)) for k, v in settings.items()]
    out.append("PAYLOAD='%s'" % ("" if dry else build_payload(files)))
    with open(resource("printer_setup.sh"), "r", encoding="utf-8") as f:
        out.append(f.read())
    return "\n".join(out)


def run_remote(host, user, password, script):
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port=SSH_PORT, username=user, password=password,
                   timeout=15, allow_agent=False, look_for_keys=False)
    try:
        stdin, stdout, stderr = client.exec_command("bash -s", timeout=180)
        stdin.write(script)
        stdin.channel.shutdown_write()
        for line in stdout:
            print(line.rstrip())
        err = stderr.read().decode("utf-8", "replace").strip()
        code = stdout.channel.recv_exit_status()
        if err:
            print(err)
        return code
    finally:
        client.close()


def restart_klipper(host):
    """Over Moonraker's HTTP API - no password, no second SSH connection.

    A firmware restart that lands too soon after another can fail with
    "Failed automated reset of MCU", which is transient. One retry clears it.
    """
    import json
    import urllib.request

    def post(path):
        url = "http://%s:%d%s" % (host, MOONRAKER_PORT, path)
        try:
            req = urllib.request.Request(url, data=b"", method="POST")
            urllib.request.urlopen(req, timeout=30).read()
        except Exception:
            pass

    def state():
        try:
            url = "http://%s:%d/printer/info" % (host, MOONRAKER_PORT)
            with urllib.request.urlopen(url, timeout=10) as r:
                d = json.load(r).get("result", {})
            return d.get("state", "?"), d.get("state_message", "")
        except Exception as e:
            return "?", str(e)

    print()
    print("-> restarting klipper (service restart, then firmware restart)")
    post("/machine/services/restart?service=klipper")
    time.sleep(30)
    post("/printer/firmware_restart")
    time.sleep(30)

    st, msg = state()
    if st != "ready":
        print("   not ready yet (%s) - retrying the firmware restart" % st)
        post("/printer/firmware_restart")
        time.sleep(35)
        st, msg = state()

    if st == "ready":
        print("   klipper is ready")
        return True
    print("   WARNING: klipper reports '%s'" % st)
    print("   %s" % msg.strip()[:300])
    print("   Try FIRMWARE_RESTART in the printer's web console.")
    return False


def finish(ok):
    print()
    rule()
    print(" Installed." if ok else " Installed, but Klipper needs a look.")
    print("""
 NEXT - and do not skip this:

   1. QIDI_CS_READ SECONDS=10
      Press the nozzle by hand. The value must move. This proves
      the load cell is reachable before anything heats.

   2. Measure counts-per-gram-force for THIS machine.
      The shipped default (201, measured on one Q2) belongs to
      one specific cell and mount. Every abort threshold depends
      on it, and it is the ONLY overload protection the printer
      has. See README.md, "The one number you must measure
      yourself".

   3. QIDI_CALIBRATE DRY=1
      Type this into the printer's web console - either in a
      browser at the printer's address, or OrcaSlicer's Device
      tab, which is the same page. A dialog asks six questions
      and heats nothing.

   4. QIDI_CALIBRATE
      Watched, with the machine in front of you.

   You do not need this window again.""")
    rule("-")
    print()
    print(" My Reddit")
    print("   " + REDDIT_URL)
    print()
    print(" Help me get things I need to make more things, anything helps :)")
    print("   " + WISHLIST_URL)
    rule()


def main():
    rule()
    print(" " + APP)
    print(" Puts the calibration modules on your QIDI printer.")
    rule()

    host = choose_host()

    print()
    print("Printer login")
    print("  These are the QIDI defaults and are almost certainly right.")
    print("  Change them only if you have set your own.")
    user = ask("   SSH username", DEFAULT_USER)
    password = ask_password("   SSH password", DEFAULT_PASS)

    print()
    diag = ask_yes("Install the diagnostic tools too? "
                   "(QIDI_CS_READ, needed by step 1 above)", True)
    cleanlogs = ask_yes("Delete the printer's old rotated logs? "
                        "(frees space; live logs are kept)", False)

    files = CORE + (DIAG if diag else [])
    sections = CORE_SECTIONS + (DIAG_SECTIONS if diag else [])

    print()
    rule("-")
    print("  printer : %s@%s" % (user, host))
    print("  modules : %d" % len(files))
    print("  old ones: removed if superseded")
    print("  logs    : %s" % ("rotated logs deleted" if cleanlogs
                              else "untouched"))
    rule("-")
    if not ask_yes("Go ahead?", True):
        print("Nothing was changed.")
        return 1

    print()
    try:
        script = build_script(files, sections, False, True, cleanlogs, user)
        code = run_remote(host, user, password, script)
    except Exception as e:
        print()
        print("   FAILED: %s: %s" % (type(e).__name__, e))
        name = type(e).__name__
        if "Auth" in name:
            print("   The username or password was not accepted.")
            print("   The QIDI defaults are %s / %s."
                  % (DEFAULT_USER, DEFAULT_PASS))
        elif isinstance(e, (socket.timeout, OSError)):
            print("   Could not reach %s. Check the printer is on, on the" % host)
            print("   same network, and that the address is right.")
        return 1

    if code != 0:
        print()
        print("   The printer reported a problem (exit %d)." % code)
        print("   Nothing was restarted. printer.cfg was backed up before any")
        print("   edit - look for printer.cfg.bak-* on the printer.")
        return 1

    ok = restart_klipper(host)
    finish(ok)
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        rc = 1
    # A double-clicked .exe closes its window the instant it returns, taking
    # every message with it.
    try:
        input("\nPress Enter to close.")
    except Exception:
        pass
    sys.exit(rc)
