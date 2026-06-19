# K2 Plus CFS Purge / "220 °C" Fix

A fix for the Creality **K2 Plus** (and likely other CFS-equipped K-series printers) where
**the CFS purges/loads the nozzle at 220 °C regardless of the selected filament**. It's most
obvious with **ASA** (220 °C is far too cold → under-extrusion, clogs, failed loads), but the
bug actually affects **every** material — ABS just happens to be borderline-tolerable at 220 °C,
which is why it "seems to work."

> TL;DR: the firmware's per-material temperature lookup never matches, so the purge always falls
> back to a hardcoded **220 °C**. This is a config-only fix — no firmware flashing, no binary
> patching, fully reversible, and it survives reboots/cloud sync (it lives in your printer config).

---

## Root cause (verified on firmware V1.1.5.5 / `CR0CN240110C10`)

The CFS load/unload/purge path calls a **native command `FILAMENT_RACK_SET_TEMP`**, implemented in
Creality's closed-source Cython module `klippy/extras/filament_rack_wrapper.cpython-39.so`
(`FilamentRackWrapper.get_material_target_temp`).

That function maps the slot's material to a nozzle temperature like this (reconstructed — see
[`docs/REVERSE_ENGINEERING.md`](docs/REVERSE_ENGINEERING.md)):

```python
material_type = self.filament_rack_data['material_type']     # the slot's stored id
for item in json.load(open(self.material_database))['result']['list']:
    if material_type == item['base']['id']:                  # <-- STRING comparison
        return int(item['kvParam']['nozzle_temperature'])
logging.error("get material target temp fail")              # <-- falls through
return -1                                                    # -> caller uses 220 °C
```

The bug is a **format mismatch compared as strings**:

| Source | Value for Hyper ABS |
|---|---|
| Slot stores `material_type` (6-digit, zero-padded) | `"003001"` |
| Material DB `base.id` (5-digit) | `"03001"` |

`"003001" == "03001"` → **False**. It never matches, so `get_material_target_temp` returns `-1`
and the purge uses the hardcoded fallback **`Tn_extrude_temp = 220`** (from `box.cfg`).
An integer-normalized compare (`int("003001") == int("03001")`) *would* match — but the code
doesn't do that. ASA and ABS even carry **identical** temps (260 °C) in the database, proving the
data is fine and the **lookup** is broken.

### Proof from the printer's own logs

```
Tn_data[T1][material_type][2]: 003001   ->  get material target temp fail
Tn_data[T1][material_type][2]: unknown  ->  get material target temp fail
Tn_data[T1][material_type][1]: -1       ->  get material target temp fail
```

`003001` is a valid, present material (Hyper ABS) and the lookup **still fails** — confirming this
hits every material, not just ASA. On the affected unit this fired **75×** (`get material target
temp fail`) plus 24× for the max-temp variant.

---

## The fix

`FILAMENT_RACK_SET_TEMP` is a native command, so we wrap it with Klipper's `rename_existing`
(the same mechanism Creality already uses for `PAUSE`, `RESUME`, etc.). Instead of the broken
lookup's 220 °C, the purge heats to the **temperature already chosen for the loaded material**
(what the UI/slicer set for that material), with a safe floor if none is set.

See [`k2_asa_purge_fix.cfg`](k2_asa_purge_fix.cfg).

```ini
[gcode_macro K2_PURGE_FIX]
variable_min_purge_temp: 240     # floor used only if no sane target is preset

[gcode_macro FILAMENT_RACK_SET_TEMP]
rename_existing: _FACTORY_FILAMENT_RACK_SET_TEMP
gcode:
    {% set tgt   = printer.extruder.target|int %}
    {% set floor = printer['gcode_macro K2_PURGE_FIX'].min_purge_temp|int %}
    {% set t = tgt if tgt >= 200 else floor %}
    { action_respond_info("K2_PURGE_FIX: purge temp %d (was target %d, factory would use 220)" % (t, tgt)) }
    M104 S{t}
    M109 S{t}
```

### Install

You need root SSH on the printer (Settings → *Root account information*; default user `root`,
password `creality_2024`). Persistent data lives under `/mnt/UDISK`.

```sh
# 1) copy the fix into your printer config
scp -O k2_asa_purge_fix.cfg root@<printer-ip>:/mnt/UDISK/printer_data/config/

# 2) back up printer.cfg and add the include (after the existing includes)
ssh root@<printer-ip> '
  C=/mnt/UDISK/printer_data/config
  cp $C/printer.cfg $C/printer.cfg.bak.$(date +%Y%m%d_%H%M%S)
  grep -q k2_asa_purge_fix.cfg $C/printer.cfg || \
    sed -i "/\[include box.cfg\]/a [include k2_asa_purge_fix.cfg]" $C/printer.cfg
'

# 3) restart Klipper (printer must be idle)
curl -X POST http://<printer-ip>:7125/printer/firmware_restart
```

> `scp -O` forces the legacy SCP protocol — the printer's dropbear has no SFTP server.
> Make sure the `[include k2_asa_purge_fix.cfg]` line comes **after** `[include box.cfg]`,
> so the native `FILAMENT_RACK_SET_TEMP` exists before we rename it.

### Verify

On your next CFS load/change you'll see in the console:

```
K2_PURGE_FIX: purge temp 260 (was target 260, factory would use 220)
```

### Tuning

- `min_purge_temp` (default **240**) is only used when no sane extruder target is set. It's safe
  for ASA/ABS/PETG and harmless for a brief PLA purge. Lower it if you print mostly PLA.
- To revert: delete the `[include k2_asa_purge_fix.cfg]` line (or restore the `printer.cfg.bak.*`)
  and restart Klipper.

---

## Notes / scope

- Tested on **K2 Plus**, firmware **V1.1.5.5** (`CR0CN240110C10`). The same module/bug appears on
  other CFS K-series builds; paths may differ slightly.
- This does **not** touch the closed-source `.so` and does not flash firmware. It only overrides a
  G-code command in your own config.
- There is a separate, related community fix for **purge volume** (the ~100 mm hardcoded flush):
  `camaro4life18/Creality-K2-Series-Purge-Volume-Fix`. This repo is about the **temperature**.
- Alternative (riskier) fix: correct the `id` format in
  `/mnt/UDISK/creality/userdata/box/material_database.json`, but that file looks cloud-synced and
  may be overwritten — the config override here is the durable approach.

## Credits

Root-caused by reverse-engineering `filament_rack_wrapper.cpython-39.so` (Cython 0.29.21, ARM) with
Ghidra + a string-table/qualname resolver, cross-checked against the printer's `material_database.json`
and `klippy.log`. Methodology write-up: [`docs/REVERSE_ENGINEERING.md`](docs/REVERSE_ENGINEERING.md).
