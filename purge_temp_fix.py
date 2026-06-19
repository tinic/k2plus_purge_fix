# purge_temp_fix.py  --  Klipper extra for the Creality K2 Plus CFS
#
# Computes a SAFE purge temperature for a filament change as the OVERLAP of the
# melt-safe ranges of every material involved (the incoming material plus the
# one still in the nozzle):
#
#     low  = max(minTemp of all involved materials)   # all must flow
#     high = min(maxTemp of all involved materials)    # none may degrade
#
# If low > high the materials are incompatible (no common safe temperature) and
# the command raises an error instead of purging at a damaging temperature.
#
# This replaces the stock behaviour, where filament_rack_wrapper.so fails its
# material lookup (6-digit slot id vs 5-digit DB id, string compare) and falls
# back to a hardcoded 220 C for every material.
#
# Register the command CFS_SMART_PURGE_SET_TEMP; the config wrapper for the
# native FILAMENT_RACK_SET_TEMP calls it.

import os, json, logging


class PurgeTempFix:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        base_dir = config.get('base_dir', '/mnt/UDISK')
        self.db_path = config.get(
            'material_database',
            os.path.join(base_dir, 'creality/userdata/box/material_database.json'))
        # Optional hard guard rails (also clamped by product_param in heaters.py).
        self.abs_min = config.getint('absolute_min_temp', 170)
        self.abs_max = config.getint('absolute_max_temp', 320)
        # True  -> abort with an error when ranges do not overlap (correct/safe).
        # False -> warn and fall back to the incoming material's own range
        #          (use if a stale "remain" reading ever blocks a valid change).
        self.strict_incompatible = config.getboolean('strict_incompatible', True)
        self.gcode.register_command(
            'CFS_SMART_PURGE_SET_TEMP', self.cmd_set_temp,
            desc="Heat nozzle to a temp safe for both the outgoing and incoming "
                 "CFS filament; error if their melt ranges do not overlap.")

    # -- material database lookup (int-normalised id, handles 6 vs 5 digit) ----
    def _load_db(self):
        with open(self.db_path, 'r') as f:
            return json.load(f)

    @staticmethod
    def _id_eq(a, b):
        if str(a) == str(b):
            return True
        try:
            return int(str(a)) == int(str(b))
        except (TypeError, ValueError):
            return False

    def _lookup(self, mid):
        # returns dict(min,max,nozzle,name) or None
        if mid in (None, '', -1, '-1', 'unknown', 'None'):
            return None
        try:
            data = self._load_db()
            for item in data['result']['list']:
                base = item.get('base', {})
                if self._id_eq(base.get('id'), mid):
                    kv = item.get('kvParam', {})
                    nz = kv.get('nozzle_temperature')
                    return {
                        'min': int(base['minTemp']),
                        'max': int(base['maxTemp']),
                        'nozzle': int(nz) if nz is not None else None,
                        'name': base.get('name', str(mid)),
                    }
        except Exception as e:
            logging.exception("CFS_SMART_PURGE: db lookup failed: %s" % e)
        return None

    def _involved_ids(self, gcmd):
        # Explicit override wins (NEW=/OLD=); otherwise read the live CFS state.
        # The material fields live on the 'filament_rack' object (NOT 'box').
        new_id = gcmd.get('NEW', None)
        old_id = gcmd.get('OLD', None)
        if new_id is None or old_id is None:
            found = {}
            for objname in ('filament_rack', 'box'):
                try:
                    obj = self.printer.lookup_object(objname, None)
                    if obj is None:
                        continue
                    st = obj.get_status(self.reactor.monotonic())
                    for k in ('material_type', 'remain_material_type'):
                        v = st.get(k)
                        if k not in found and v not in (None, '', -1, '-1', 'None'):
                            found[k] = v
                except Exception as e:
                    logging.exception("CFS_SMART_PURGE: %s state read failed: %s"
                                      % (objname, e))
            if new_id is None:
                new_id = found.get('material_type')
            if old_id is None:
                old_id = found.get('remain_material_type')
        return new_id, old_id

    def cmd_set_temp(self, gcmd):
        dry = gcmd.get_int('DRYRUN', 0)
        new_id, old_id = self._involved_ids(gcmd)
        new = self._lookup(new_id)
        old = self._lookup(old_id)
        involved = [(tag, mid, r) for tag, mid, r in
                    (('new', new_id, new), ('old', old_id, old)) if r]

        # Could not resolve ANY material -> never block the load; fall back to the
        # stock behaviour (the renamed native command) so the operation proceeds.
        if not involved:
            msg = ("CFS_SMART_PURGE: could not resolve materials "
                   "(new=%s, old=%s); falling back to factory temp." % (new_id, old_id))
            gcmd.respond_info(msg)
            if dry:
                return
            try:
                self.gcode.run_script_from_command('_FACTORY_FILAMENT_RACK_SET_TEMP')
            except Exception as e:
                logging.exception("CFS_SMART_PURGE: factory fallback failed: %s" % e)
            return

        low = max(r['min'] for _, _, r in involved)
        high = min(r['max'] for _, _, r in involved)
        desc = ", ".join("%s %s[%d-%d]" % (t, r['name'], r['min'], r['max'])
                         for t, _, r in involved)

        if low > high:
            if self.strict_incompatible and not dry:
                raise gcmd.error(
                    "CFS purge: incompatible materials, no common safe temperature "
                    "(%s). Purge/change aborted." % desc)
            if dry:
                gcmd.respond_info(
                    "CFS_SMART_PURGE [DRYRUN]: INCOMPATIBLE (%s) -> would %s"
                    % (desc, "ABORT" if self.strict_incompatible else "use incoming only"))
            else:
                gcmd.respond_info(
                    "CFS_SMART_PURGE: WARNING incompatible (%s); using incoming "
                    "material only." % desc)
            ref = new or involved[0][2]
            low, high = ref['min'], ref['max']

        # Prefer the incoming material's print temp, clamped into the overlap.
        want = new['nozzle'] if (new and new['nozzle']) else high
        temp = max(low, min(high, want))
        temp = max(self.abs_min, min(self.abs_max, temp))

        if dry:
            gcmd.respond_info(
                "CFS_SMART_PURGE [DRYRUN]: %s -> overlap [%d-%d] -> would purge @ %d C"
                % (desc, low, high, temp))
            return

        gcmd.respond_info(
            "CFS_SMART_PURGE: %s -> overlap [%d-%d] -> purge @ %d C"
            % (desc, low, high, temp))

        pheaters = self.printer.lookup_object('heaters')
        heater = self.printer.lookup_object('extruder').heater
        pheaters.set_temperature(heater, temp, True)   # True = wait until reached


def load_config(config):
    return PurgeTempFix(config)
