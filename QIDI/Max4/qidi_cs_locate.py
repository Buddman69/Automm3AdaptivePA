# qidi_cs_locate.py - Stage A: locate the live CS1237 instance and map its API
#
# Copyright (C) 2026  Budd
# Licensed under PolyForm Strict 1.0.0 - see LICENSE.md.
#
# PURPOSE
#   `cs1237` is not a registered Klipper object - only `probe_air` is.  The
#   CS1237 instance is built inside air.so and hangs off PrinterAirProbe as an
#   attribute under a name we do not know.  This module finds it by *type*
#   (isinstance against the real class, imported from the same .so klippy
#   already loaded) rather than by guessing names, then reports what is on it.
#
#   The question it exists to answer: does a bulk_sensor helper actually exist
#   on that instance, and what is it called?  CS1237 does not expose the
#   add_client() method mainline ADXL345 does, so the bulk path cannot be
#   assumed - it has to be looked at.
#
# SAFETY - this module calls nothing
#   Strictly read-only.  No motion, no heating, no writes to printer state, and
#   no method or property invocation on the sensor at all.
#
#   The API surface is read from __dict__ and from the class MRO's descriptors.
#   Neither triggers a getter.  This matters: on a Cython object, `getattr` for
#   a name backed by a getset_descriptor *runs vendor code*, which is exactly
#   what safety rule 3 forbids.  Reading the descriptor object out of the class
#   dict tells us the name and kind while invoking none of it.
#
#   Traversal prefers obj.__dict__ (raw, cannot fire a property).  Cython types
#   often have no __dict__, so there is a fallback to filtered getattr using the
#   same SKIP_PREFIXES list as qidi_pa_discover.py.
#
# USAGE
#   [qidi_cs_locate]
#
#   QIDI_CS_LOCATE                      find the CS1237 instance, map its API
#
#   Results are appended to ~/printer_data/qidi_pa/locate.json

import json
import logging
import os
import time

try:
    from . import cs1237 as _cs1237_mod
except Exception:
    _cs1237_mod = None

try:
    from . import bulk_sensor as _bulk_mod
except Exception:
    _bulk_mod = None

# Never getattr a name starting with these, even if it looks like data.
# Same list as qidi_pa_discover.py - see SAFETY.md rule 3.
SKIP_PREFIXES = ('set_', 'do_', 'run_', 'cmd_', 'home_', 'probe_', 'start_',
                 'stop_', 'calibrate_', 'move_', 'reset_', 'write_', 'send_',
                 'clear_', 'init_', 'load_', 'save_', 'delete_', 'update_')

SKIP_NAMES = ('printer', 'reactor', 'gcode', 'config', 'objects', 'logger',
              'mutex', 'lock', 'webhooks', 'toolhead')

# Descriptor types whose lookup would execute vendor code.
LIVE_DESCRIPTORS = ('getset_descriptor', 'member_descriptor', 'property',
                    'cached_property')

DEFAULT_MAX_DEPTH = 5
DEFAULT_MAX_NODES = 4000


def _classify(obj, name):
    # What kind of thing is `name` on `obj`, without looking it up on obj.
    for klass in type(obj).__mro__:
        if name in klass.__dict__:
            kind = type(klass.__dict__[name]).__name__
            return kind, klass.__name__
    return 'instance_attr', None


def _is_method_kind(kind):
    return ('function' in kind or 'method' in kind
            or kind in ('classmethod', 'staticmethod'))


def _safe_children(obj):
    # (name, value) pairs that are safe to look at, plus the names we refused.
    # Prefers __dict__, which is the raw slot and cannot fire a property.
    #
    # `skipped` matters as much as `out`.  On a Cython cdef class with no
    # instance dict every attribute is a getset_descriptor, so a strict walk
    # legitimately returns nothing - and without this list the report would say
    # "found nothing" while hiding the fact that there was plenty there we
    # declined to read.  Names cost nothing to collect and invoke no code.
    out, skipped = [], []
    d = getattr(type(obj), '__dictoffset__', None)
    inst = None
    if d is None or d != 0:
        try:
            inst = object.__getattribute__(obj, '__dict__')
        except Exception:
            inst = None
    if isinstance(inst, dict):
        for name, val in inst.items():
            if not isinstance(name, str) or name.startswith('_'):
                continue
            if name in SKIP_NAMES:
                continue
            out.append((name, val))
        return out, 'dict', skipped
    # Fallback for Cython extension types with no instance dict.
    try:
        names = dir(obj)
    except Exception:
        return out, 'none', skipped
    for name in sorted(names):
        if name.startswith('_') or name in SKIP_NAMES:
            continue
        kind, _owner = _classify(obj, name)
        if _is_method_kind(kind):
            continue
        if name.startswith(SKIP_PREFIXES):
            skipped.append({'name': name, 'kind': kind, 'why': 'skip_prefix'})
            continue
        # Do not look up anything whose lookup would run vendor code.
        if kind in LIVE_DESCRIPTORS:
            skipped.append({'name': name, 'kind': kind, 'why': 'live_descriptor'})
            continue
        try:
            out.append((name, getattr(obj, name)))
        except Exception:
            continue
    return out, 'dir', skipped


def _scalars(obj):
    # Scalar values straight out of the instance __dict__.
    #
    # Safe for the same reason traversal is: __dict__ is the raw slot, so this
    # reads stored values and runs no getter.  Worth having because the numbers
    # answer design questions - cs_fil_f says whether the driver filters, and
    # WeighCalibration's fields are the counts-to-gf scale.
    out = {}
    try:
        inst = object.__getattribute__(obj, '__dict__')
    except Exception:
        return out
    if not isinstance(inst, dict):
        return out
    for name, val in inst.items():
        if not isinstance(name, str) or name.startswith('_'):
            continue
        if isinstance(val, bool) or val is None:
            out[name] = val
        elif isinstance(val, (int, float, str)):
            out[name] = val
        elif isinstance(val, (list, tuple)):
            # Note the length either way.  A short list of non-scalars must not
            # vanish silently - for bulk_queue.raw_samples the count IS the
            # answer, and "absent" and "empty" mean very different things.
            if len(val) <= 12 and all(
                    isinstance(v, (int, float, str, bool)) or v is None
                    for v in val):
                out[name] = list(val)
            else:
                out[name] = '<%s len=%d>' % (type(val).__name__, len(val))
        elif isinstance(val, dict):
            if len(val) <= 12 and all(
                    isinstance(v, (int, float, str, bool)) or v is None
                    for v in val.values()):
                out[name] = {str(k): v for k, v in val.items()}
            else:
                out[name] = '<dict len=%d>' % (len(val),)
    return out


def _api_surface(obj):
    # Full name -> kind map for an object, invoking nothing.
    methods, data, unknown = [], [], []
    try:
        names = dir(obj)
    except Exception:
        return {'methods': [], 'data': [], 'unknown': []}
    for name in sorted(names):
        if name.startswith('__'):
            continue
        kind, owner = _classify(obj, name)
        entry = {'name': name, 'kind': kind, 'defined_on': owner}
        if _is_method_kind(kind):
            # A Cython method's __doc__ often carries its signature, which is
            # the only documentation these drivers have.  Reading __doc__ off
            # the descriptor in the class dict invokes nothing.
            for klass in type(obj).__mro__:
                if name in klass.__dict__:
                    doc = getattr(klass.__dict__[name], '__doc__', None)
                    if isinstance(doc, str) and doc.strip():
                        entry['doc'] = doc.strip()[:200]
                    break
            methods.append(entry)
        elif kind in LIVE_DESCRIPTORS:
            data.append(entry)
        else:
            unknown.append(entry)
    # Instance attributes carry their value's type, which is free information.
    try:
        inst = object.__getattribute__(obj, '__dict__')
    except Exception:
        inst = None
    if isinstance(inst, dict):
        for e in unknown:
            if e['name'] in inst:
                e['value_type'] = type(inst[e['name']]).__name__
    return {'methods': methods, 'data': data, 'unknown': unknown}


class QidiCSLocate:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.max_depth = config.getint('max_depth', DEFAULT_MAX_DEPTH,
                                       minval=1, maxval=8)
        self.max_nodes = config.getint('max_nodes', DEFAULT_MAX_NODES,
                                       minval=50, maxval=20000)
        out_dir = config.get('out_dir', '~/printer_data/qidi_pa')
        self.out_dir = os.path.expanduser(out_dir)
        self.report_path = os.path.join(self.out_dir, 'locate.json')
        self.root_name = config.get('root', 'probe_air')

        self.gcode.register_command('QIDI_CS_LOCATE', self.cmd_LOCATE,
                                    desc=self.cmd_LOCATE_help)

    def _guard(self, gcmd, fn):
        # Klipper treats an unhandled exception in a gcode command as an
        # INTERNAL ERROR and latches every MCU into shutdown - a stray
        # NameError did exactly that on 2026-09-15, and it took a
        # FIRMWARE_RESTART to clear. A gcode.error is by contrast a clean
        # command failure that leaves the printer running, so anything
        # unexpected is converted into one here.
        # Derive the clean-failure class from gcmd rather than importing it,
        # so this works against the mock harness too.
        err_cls = type(gcmd.error("probe"))
        try:
            return fn(gcmd)
        except err_cls:
            raise
        except Exception as e:
            logging.exception("qidi_cs_locate: unhandled error")
            raise gcmd.error("qidi_cs_locate: internal error, printer left running: "
                             "%s: %s" % (type(e).__name__, str(e)[:160]))

    # ------------------------------------------------------------------ utils

    def _record(self, kind, payload):
        entry = {'kind': kind, 'time': time.time(), 'payload': payload}
        try:
            os.makedirs(self.out_dir, exist_ok=True)
            data = []
            if os.path.exists(self.report_path):
                try:
                    with open(self.report_path, 'r') as f:
                        data = json.load(f)
                    if not isinstance(data, list):
                        data = [data]
                except Exception:
                    data = []
            data.append(entry)
            tmp = self.report_path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=1, default=repr)
            os.replace(tmp, self.report_path)
            return True
        except Exception:
            logging.exception("qidi_cs_locate: could not write report")
            return False

    def _targets(self):
        # Classes worth flagging when we meet an instance of one.
        targets = []
        if _cs1237_mod is not None:
            for name in ('CS1237', 'CS1237Command'):
                klass = getattr(_cs1237_mod, name, None)
                if isinstance(klass, type):
                    targets.append(('cs1237.' + name, klass))
        if _bulk_mod is not None:
            for name in dir(_bulk_mod):
                if name.startswith('_'):
                    continue
                klass = getattr(_bulk_mod, name, None)
                if isinstance(klass, type):
                    targets.append(('bulk_sensor.' + name, klass))
        return targets

    def _walk(self, root, targets):
        # Breadth-first so the shortest path to each hit is the one reported.
        hits, tree, seen = [], [], set()
        refused, scalars = [], {}
        queue = [(root, self.root_name, 0)]
        seen.add(id(root))
        while queue and len(tree) < self.max_nodes:
            obj, path, depth = queue.pop(0)
            # Collect here, while we hold the object.  Resolving the path again
            # later would use getattr, and a data descriptor on the class wins
            # over __dict__ - so that would not be the same read.
            vals = _scalars(obj)
            if vals:
                scalars[path] = vals
            for label, klass in targets:
                try:
                    if isinstance(obj, klass):
                        hits.append({'path': path, 'matches': label,
                                     'type': type(obj).__name__})
                except Exception:
                    continue
            if depth >= self.max_depth:
                continue
            children, how, skipped = _safe_children(obj)
            for s in skipped:
                refused.append(dict(s, path=path + '.' + s['name']))
            for name, val in children:
                if val is None or isinstance(val, (str, bytes, bool, int,
                                                   float)):
                    continue
                oid = id(val)
                if oid in seen:
                    continue
                seen.add(oid)
                child_path = path + '.' + name
                tree.append({'path': child_path,
                             'type': type(val).__name__,
                             'module': getattr(type(val), '__module__', None),
                             'via': how})
                queue.append((val, child_path, depth + 1))
        return hits, tree, refused, scalars

    # --------------------------------------------------------------- commands

    cmd_LOCATE_help = ("Find the live CS1237 instance by type and map its API. "
                       "Calls nothing - strictly read-only. [ROOT=probe_air]")

    def cmd_LOCATE(self, gcmd):
        return self._guard(gcmd, self._run_LOCATE)

    def _run_LOCATE(self, gcmd):
        root_name = gcmd.get('ROOT', self.root_name)
        root = self.printer.lookup_object(root_name, None)
        if root is None:
            raise gcmd.error("qidi_cs_locate: no such object: %s" % (root_name,))

        if _cs1237_mod is None:
            gcmd.respond_info("qidi_cs_locate: WARNING could not import "
                              "extras.cs1237 - type matching disabled")
        if _bulk_mod is None:
            gcmd.respond_info("qidi_cs_locate: WARNING could not import "
                              "extras.bulk_sensor")

        targets = self._targets()
        gcmd.respond_info("qidi_cs_locate: walking %s (%s), %d target classes"
                          % (root_name, type(root).__name__, len(targets)))

        hits, tree, refused, scalars = self._walk(root, targets)

        gcmd.respond_info("qidi_cs_locate: %d objects reachable, %d type hits, "
                          "%d attributes refused"
                          % (len(tree), len(hits), len(refused)))
        for h in hits:
            gcmd.respond_info("  HIT %-44s %s" % (h['path'], h['matches']))
        if not hits:
            gcmd.respond_info(
                "qidi_cs_locate: no CS1237 or bulk_sensor instance reachable "
                "without invoking code. That is a result, not a failure - see "
                "the refused list below for what stands between us and it.")
        # What a stricter-than-necessary walk chose not to look at.  This is the
        # evidence for the safety rule 3 decision, gathered without breaking it.
        if refused:
            live = [r for r in refused if r['why'] == 'live_descriptor']
            gcmd.respond_info("qidi_cs_locate: %d live descriptors not read "
                              "(reading them would run vendor code):"
                              % (len(live),))
            for r in live[:25]:
                gcmd.respond_info("    skip %-44s %s" % (r['path'], r['kind']))

        # Full API surface for each hit, plus the root itself for context.
        surfaces = {}
        for h in hits:
            try:
                obj = self._resolve(root, h['path'])
            except Exception:
                continue
            surfaces[h['path']] = _api_surface(obj)
        surfaces[root_name] = _api_surface(root)

        for path, surf in surfaces.items():
            gcmd.respond_info("qidi_cs_locate: %s -> %d methods, %d live "
                              "descriptors, %d plain"
                              % (path, len(surf['methods']), len(surf['data']),
                                 len(surf['unknown'])))
            for e in surf['methods']:
                gcmd.respond_info("    def  %s" % (e['name'],))
            vals = scalars.get(path, {})
            for e in surf['unknown']:
                name = e['name']
                if name in vals:
                    gcmd.respond_info("    attr %-28s = %s"
                                      % (name, repr(vals[name])[:60]))
                else:
                    vt = e.get('value_type')
                    gcmd.respond_info("    attr %-28s %s"
                                      % (name, vt if vt else e['kind']))

        # Scalars from everywhere else in the subtree - WeighCalibration's
        # fields are here, and they are the counts-to-gf scale.
        for path in sorted(scalars):
            if path in surfaces:
                continue
            gcmd.respond_info("qidi_cs_locate: %s" % (path,))
            for name, val in sorted(scalars[path].items()):
                gcmd.respond_info("    %-28s = %s" % (name, repr(val)[:60]))

        ok = self._record('locate', {
            'root': root_name,
            'root_type': type(root).__name__,
            'cs1237_imported': _cs1237_mod is not None,
            'bulk_sensor_imported': _bulk_mod is not None,
            'targets': [t[0] for t in targets],
            'hits': hits,
            'tree': tree,
            'refused': refused,
            'scalars': scalars,
            'surfaces': surfaces})
        gcmd.respond_info("qidi_cs_locate: written to %s"
                          % (self.report_path if ok else "(not written)",))

    def _resolve(self, root, path):
        # Paths are built by _walk from names we already traversed safely.
        obj = root
        parts = path.split('.')
        for name in parts[1:]:
            obj = getattr(obj, name)
        return obj


def load_config(config):
    return QidiCSLocate(config)
