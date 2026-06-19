# Reconstructed from filament_rack_wrapper.cpython-39.so (Cython 0.29.21, ARM)
# Method: FilamentRackWrapper.get_material_target_temp  (impl @ 0x24bec)
#
# Confidence: HIGH for control flow + data navigation (recovered from resolved
# string table + CPython API call sequence + Ghidra decompilation).
# The match FIELD ('id') and the int() normalization are INFERRED from the data
# format (slot stores a 6-digit zero-padded filamentId; DB keys on base.id) plus
# the PyNumber_Long calls around the comparison. Marked [INFERRED] below.
#
# This is the function that emits "get material target temp fail" — which the
# printer's own klippy.log shows firing 75x. On that failure the CFS purge path
# falls back to Tn_extrude_temp = 220 (box.cfg) / default_extruder_temp = 220.

import os, json, logging

class _Reconstruction:

    def get_material_target_temp(self, type=None):
        # 1) Resolve which material id to look up.
        #    When called with no/None/-1 type, read it from the live slot data.
        if type is None or type == -1:
            material_type = self.filament_rack_data['material_type']   # e.g. "003001"
        else:
            material_type = type

        if material_type is None or material_type == -1:
            logging.warning("failed to obtain consumable type")
            return -1                              # sentinel -> caller uses 220 fallback

        # 2) Load the runtime material database.
        if not os.path.exists(self.material_database):           # /mnt/UDISK/creality/userdata/box/material_database.json
            logging.error("get material target temp fail")
            return -1
        try:
            with open(self.material_database, 'r') as f:
                data = json.load(f)

            # 3) Linear search of result.list, matching the slot's material id.
            for item in data['result']['list']:
                base = item['base']
                # [INFERRED] match field = base['id']; comparison normalised to int
                # ("003001" -> 3001 == "03001" -> 3001), which is why numeric ids work.
                if int(base['id']) == int(material_type):
                    kvParam = item['kvParam']
                    temp = int(kvParam['nozzle_temperature'])       # ASA & ABS both = 260 in DB
                    logging.info("material database get nozzle temp: %s" % temp)
                    logging.info("get material temp: %d" % temp)
                    return temp
        except Exception as e:
            # int("E4001") -> ValueError, missing key, etc. all land here
            logging.error(e)

        # 4) Not found / error -> the bug: caller falls back to 220.
        logging.error("get material target temp fail")
        return -1
