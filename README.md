# K2 Plus CFS Purge / "220 °C" Fix

A fix for the Creality **K2 Plus** (and likely other CFS-equipped K-series printers) where
**the CFS purges/loads the nozzle at 220 °C regardless of the selected filament**. It's most
obvious with **ASA** (220 °C is far too cold → under-extrusion, clogs, failed loads), but the
bug actually affects **every** material — ABS just happens to be borderline-tolerable at 220 °C,
which is why it "seems to work."

This fix replaces the broken temperature selection with the **correct** behaviour: during a
filament change it heats to a temperature that is safe for **both** the outgoing (still-in-nozzle)
and incoming filament — the **overlap of their melt-safe ranges** — and **aborts with an error**
if the two materials have no common safe temperature (e.g. PLA ↔ PC).

> Config + one small Klipper extra. No firmware flashing, no binary patching, fully reversible.

---

## Root cause (verified on firmware V1.1.5.5 / `CR0CN240110C10`)

The CFS load/unload/purge path calls a **native command `FILAMENT_RACK_SET_TEMP`**, implemented in
Creality's closed-source Cython module `klippy/extras/filament_rack_wrapper.cpython-39.so`
(`FilamentRackWrapper.get_material_target_temp`). Reconstructed, it does:

```python
material_type = self.filament_rack_data['material_type']     # the slot's stored id
for item in json.load(open(self.material_database))['result']['list']:
    if material_type == item['base']['id']:                  # <-- STRING comparison
        return int(item['kvParam']['nozzle_temperature'])
logging.error("get material target temp fail")
return -1                                                     # -> caller falls back to 220 °C
```

The bug is a **format mismatch compared as strings**:

| Source | Value for Hyper ABS |
|---|---|
| Slot stores `material_type` (6-digit, zero-padded) | `"003001"` |
| Material DB `base.id` (5-digit) | `"03001"` |

`"003001" == "03001"` → **False**. It never matches, so `get_material_target_temp` returns `-1`
and the purge uses the hardcoded fallback **`Tn_extrude_temp = 220`** (from `box.cfg`). An
integer-normalized compare *would* match. ASA and ABS even carry **identical** temps (260 °C) in
the database, proving the data is fine and the **lookup** is broken.

### Proof from the printer's own logs

```
Tn_data[T1][material_type][2]: 003001   ->  get material target temp fail
Tn_data[T1][material_type][1]: -1       ->  get material target temp fail
```

`003001` is a valid, present material (Hyper ABS) and the lookup **still fails** — confirming this
hits every material, not just ASA. On the affected unit this fired **75×**.

Full methodology (Cython RE with Ghidra/PyGhidra): [`docs/REVERSE_ENGINEERING.md`](docs/REVERSE_ENGINEERING.md).

---

## What this fix does

`FILAMENT_RACK_SET_TEMP` is a native command, so we wrap it with Klipper's `rename_existing` (the
same mechanism Creality already uses for `PAUSE`, `RESUME`, …). The wrapper calls a small Klipper
extra ([`purge_temp_fix.py`](purge_temp_fix.py)) that:

1. Reads the **incoming** material (`filament_rack.material_type`) and the **outgoing /
   in-nozzle** material (`filament_rack.remain_material_type`).
2. Looks each up in `material_database.json` with a correct **integer-normalized** id match
   (fixing the 6-vs-5-digit bug), getting each material's `minTemp`/`maxTemp`.
3. Computes the safe overlap:
   - `low  = max(minTemp …)`  — every material must flow
   - `high = min(maxTemp …)`  — none may thermally degrade
4. If `low > high` → **incompatible materials**, raise an error and abort the change.
   Otherwise heats to the incoming material's print temp **clamped into `[low, high]`**.

Examples (computed against a real K2 Plus database):

```
ASA  <- ASA/ABS    overlap [240-280] -> purge @ 260 C
PETG <- PLA        overlap [220-240] -> purge @ 240 C
PLA  <- PC         overlap empty (250 > 240) -> ERROR, change aborted
unknown / -1       cannot resolve -> ERROR
```

---

## Install

You need root SSH on the printer (Settings → *Root account information*; default user `root`,
password `creality_2024`). Persistent data lives under `/mnt/UDISK`.

```sh
PRN=<printer-ip>

# 1) the Klipper extra (overlap logic)
scp -O purge_temp_fix.py   root@$PRN:/usr/share/klipper/klippy/extras/

# 2) the config (wraps FILAMENT_RACK_SET_TEMP + loads the extra)
scp -O k2_asa_purge_fix.cfg root@$PRN:/mnt/UDISK/printer_data/config/

# 3) back up printer.cfg and add the include AFTER [include box.cfg]
ssh root@$PRN '
  C=/mnt/UDISK/printer_data/config
  cp $C/printer.cfg $C/printer.cfg.bak.$(date +%Y%m%d_%H%M%S)
  grep -q k2_asa_purge_fix.cfg $C/printer.cfg || \
    sed -i "/\[include box.cfg\]/a [include k2_asa_purge_fix.cfg]" $C/printer.cfg
'

# 4) restart Klipper (printer must be idle)
curl -X POST http://$PRN:7125/printer/firmware_restart
```

> `scp -O` forces the legacy SCP protocol — the printer's dropbear has no SFTP server.
> The `[include k2_asa_purge_fix.cfg]` line must come **after** `[include box.cfg]` so the native
> `FILAMENT_RACK_SET_TEMP` exists before we rename it.

### Verify

```sh
# preview the temp for any change WITHOUT heating (target stays 0):
curl -X POST "http://$PRN:7125/printer/gcode/script?script=CFS_SMART_PURGE_SET_TEMP%20DRYRUN=1%20NEW=000003%20OLD=019001"
# -> "... Generic PETG[220-270], old HP-ASA[240-280] -> overlap [240-270] -> would purge @ 250 C"

# incompatible pair errors WITHOUT heating:
curl -X POST "http://$PRN:7125/printer/gcode/script?script=CFS_SMART_PURGE_SET_TEMP%20NEW=001001%20OLD=007002"
# -> "CFS purge: incompatible materials, no common safe temperature (...)"
```

On a real CFS change you'll see in the console:
`CFS_SMART_PURGE: new Generic ASA[240-280], old ... -> overlap [240-280] -> purge @ 260 C`.

### Tuning (`[purge_temp_fix]` in `k2_asa_purge_fix.cfg`)

- `strict_incompatible` (default **True**) — error when ranges don't overlap. Set **False** to
  instead warn and use the incoming material's own range (useful if a stale "remain" reading ever
  blocks a valid change).
- `absolute_min_temp` / `absolute_max_temp` — hard guard rails (default 170 / 320).
- `material_database` — path override (default `/mnt/UDISK/creality/userdata/box/material_database.json`).

### Revert

Delete `[include k2_asa_purge_fix.cfg]` from `printer.cfg` (or restore a `printer.cfg.bak.*`) and
restart Klipper. You can leave `purge_temp_fix.py` in place; it's inert without the config section.

---

## Notes / scope

- Tested on **K2 Plus**, firmware **V1.1.5.5** (`CR0CN240110C10`). Other CFS K-series builds share
  the module/bug; paths may differ slightly.
- This does **not** modify the closed-source `.so` and does not flash firmware.
- The Klipper extra lives in the OpenWrt overlay, so it survives reboots, but a **firmware OTA
  update may overwrite `/usr/share/klipper/...`** — re-copy `purge_temp_fix.py` after a firmware
  update (the config in `/mnt/UDISK` persists).
- Related community fix for the **purge volume** (the ~100 mm hardcoded flush):
  `camaro4life18/Creality-K2-Series-Purge-Volume-Fix`. This repo is about **temperature**.

## Files

| File | Goes to | Purpose |
|---|---|---|
| `purge_temp_fix.py` | `/usr/share/klipper/klippy/extras/` | overlap-temperature logic (Klipper extra) |
| `k2_asa_purge_fix.cfg` | `/mnt/UDISK/printer_data/config/` | wraps `FILAMENT_RACK_SET_TEMP`, loads the extra |
| `docs/REVERSE_ENGINEERING.md` | — | how the bug was found |
| `docs/filament_rack_wrapper_partial.py` | — | reconstructed stock function |

## Credits

Root-caused by reverse-engineering `filament_rack_wrapper.cpython-39.so` (Cython 0.29.21, ARM) with
Ghidra/PyGhidra + a string-table/qualname resolver, cross-checked against the printer's
`material_database.json` and `klippy.log`.
