# How this bug was found — reverse-engineering `filament_rack_wrapper.cpython-39.so`

Creality ships the CFS logic as **closed-source Cython-compiled extensions**
(`filament_rack_wrapper.cpython-39.so`, `box_wrapper.cpython-39.so`) in their Klipper fork. This
documents how the 220 °C purge bug was root-caused, so others can extend the work.

## Target

- `filament_rack_wrapper.cpython-39.so` — ELF 32-bit LSB, **ARM**, stripped, **Cython 0.29.21**,
  ~206 KB. Module class `FilamentRackWrapper`. Registers native G-code commands
  `FILAMENT_RACK_SET_TEMP`, `FILAMENT_RACK_FLUSH`, `FILAMENT_RACK_PRE_FLUSH`.
- SoC: Allwinner T113 (sun8iw20p1, dual Cortex-A7), Linux 5.4.61, OpenWrt 21.02.

## Why it's hard

Cython output is C-API soup (refcount helpers, `PyObject_*` calls), and on ARM **PIC** every string
and global is reached through GOT-indirected loads. Ghidra's *decompiler* produces struct-offset
noise with no inline strings, and its *reference analyzer* doesn't resolve most of these refs. A
generic decompiler alone gives you very little.

## Method that worked

Inspired by the approach in
<https://frederickalt.github.io/blog/reversing-cython-binaries-with-ai/>:

1. **Decompile with Ghidra (headless via PyGhidra).** Ghidra 12.x dropped Jython, so drive it from
   Python with `pyghidra` (needs a full JDK ≥ 21, not a JRE).

2. **Rebuild the Cython string table.** Parse `__Pyx_StringTabEntry` records `{PyObject **p; const
   char *s; Py_ssize_t n; ...}` (20-byte stride) out of memory. Each entry maps the address of an
   interned-string global (`p`, in `.bss`/`.data`) to its text (`s`, in `.rodata`). For this module:
   ~314 globals → strings. (Watch the length field: `n` can be `len+1`.) Ghidra rebases the `.so` to
   image base `0x10000`; account for that.

3. **Attach strings to code.** Ghidra *does* resolve many code→global refs via
   **`instruction.getReferencesFrom()`** (forward), even though `getReferencesTo` (reverse) and
   rodata-string refs come back empty. For each instruction, map its ref target through the
   string-table map → recover the strings/dict-keys/format-strings each function touches.

4. **Name the methods.** Every Cython impl references its own qualname
   (`extras.filament_rack_wrapper.FilamentRackWrapper.<name>`) for tracebacks — match those to name
   functions. (Some refs Ghidra doesn't resolve; fall back to identifying a function by the unique
   log/format strings it uses.)

5. **Translate the annotated C → Python.** With strings + named CPython API calls + control flow,
   the logic reads cleanly. See [`filament_rack_wrapper_partial.py`](filament_rack_wrapper_partial.py).

Tools used: Ghidra 12.1.2 (PyGhidra), `arm-linux-gnueabihf-objdump`, capstone, plus the device's own
`material_database.json` / `tn_data.json` / `klippy.log` for ground truth.

## What `get_material_target_temp` actually does

```python
def get_material_target_temp(self, type=None):
    material_type = self.filament_rack_data['material_type'] if type in (None, -1) else type
    if material_type in (None, -1):
        logging.warning("failed to obtain consumable type"); return -1
    if not os.path.exists(self.material_database):
        logging.error("get material target temp fail"); return -1
    try:
        data = json.load(open(self.material_database))
        for item in data['result']['list']:
            if material_type == item['base']['id']:               # string compare — the bug
                temp = int(item['kvParam']['nozzle_temperature'])
                logging.info("material database get nozzle temp: %s" % temp)
                return temp
    except Exception as e:
        logging.error(e)
    logging.error("get material target temp fail")
    return -1
```

`self.material_database` = `/mnt/UDISK/creality/userdata/box/material_database.json`.

## The mismatch (ground truth)

- Slots store `material_type` as a **6-digit** zero-padded id: `"003001"` (Hyper ABS), `"000007"`
  (Generic ASA) — the box stores `"0"+id` everywhere (`tn_data.json`, `material_box_info.json`).
- The database uses **5-digit** `base.id`: all 96 entries are 5 chars (`"03001"`, `"00007"`, ...).
- The compare is a **string** equality, so it never matches; `int()`-normalized it would.

```
stored material_type = '003001'
exact string match in base.id?   -> False
int-normalized match?            -> True   (-> '03001' Hyper ABS, nozzle_temperature 260)
```

Result: `get_material_target_temp` returns `-1`, and the purge path uses the hardcoded
`Tn_extrude_temp = 220`. Every material is affected; ASA is just the most visible failure.

## The deployed flow (config)

```
LOAD_MATERIAL -> LOAD_MATERIAL_HEATING (-> FILAMENT_RACK_PRE_FLUSH, FILAMENT_RACK_SET_TEMP)
              -> LOAD_MATERIAL_MATERIAL_FLUSH (-> FILAMENT_RACK_FLUSH)
              -> LOAD_MATERIAL_END
QUIT_MATERIAL -> QUIT_MATERIAL_HEATING (-> FILAMENT_RACK_SET_TEMP)
```

`FILAMENT_RACK_SET_TEMP` is the temperature step; wrapping it (see the fix) is enough to correct the
purge temperature for all of these without editing `gcode_macro.cfg`.
