# Setup, building, flashing, recovery

## What you need
| piece | where | for |
|---|---|---|
| **Your own OS 1.11 `.syx`** | Elektron's Digitone II downloads (keep an untouched copy: it is your recovery image) | everything; sha256 `2af43e65…72e0` (dn2paths.STOCK_SHA256). Every address in this skill is 1.11's |
| **dnfw** | `git clone https://github.com/angellinares/dn2_firmware_explore $DN2_WORKSPACE/repos/dnfw` (AGPL, no firmware inside) | unpack / repack / sign containers, `cbuild` (C → ColdFire), the startup loader, boot-stream walker, `dnfw params`, `dnfw inspect` |
| **m68k GCC + binutils** | Linux / WSL: `apt install gcc-m68k-linux-gnu binutils-m68k-linux-gnu`. macOS: build GCC 13 + binutils 2.42 for `m68k-linux-gnu` from source (`--with-system-zlib`, `MAKEINFO=true`), install into `$DN2_WORKSPACE/m68k/bin` | ColdFire C mods (`-mcpu=5475`); objdump `-m m68k:cfv4e` is the only correct ColdFire V4e disassembler (`cfdis.sh`, `cfx.sh`) |
| **digikit** | `git clone https://github.com/m-dwyer/digikit $DN2_WORKSPACE/repos/digikit`, its README for the DN2 1.11 setup (venv, sections, boot snapshots), then `git apply scripts/digikit-local.patch` (ColdFire BITREV / BYTEREV, which Unicorn lacks) | the ColdFire emulator (real OS, panel, screen, sequencer) |
| **digiemu's patched Unicorn** (recommended) | `github.com/irpina/digiemu`, `tools/install-patched-unicorn.sh` into a copy of digikit's venv named `.venv-fast` | 2-6x faster ColdFire runs and **correct fractional EMAC** (plain Unicorn halves `0.5 x 0.5`: LFO / smoothing math wrong). cfkit also runs every EMAC instruction in Python as a fallback |
| PyPy 3.10+ | `brew install pypy3` / `apt install pypy3` | the SHARC emulator's Python paths, aPLib compression (~10x faster) |
| a C compiler + `cffi` | Xcode CLT / build-essential; `pip install cffi` | the SHARC emulator's C core (`simc`, ~30x the interpreter), fastemu's native stepper |
| `mido` + `python-rtmidi` | `pip install mido python-rtmidi` | `rpc_flash.py`, `transport.py`, `send_syx.py` |
| ADI docs | *SHARC Processor Programming Reference* (ADSP-214xx, rev 2.4: **its opcode tables are the correct ones**), *SHARC+ Core PRM* (its Type 2a/2b figures are wrong; grep the text for rules), the DN2 manual | encodings, hardware rules, panel gestures |

Then `python3 scripts/dn2setup.py` (checks everything, extracts sections, dumps the parameter table, caches the disassembly),
`--boot` (stock DSP boot snapshot, once) and `--template` (real OS frames for `dn2synth`, once). Set `DN2_WORKSPACE` if
you do not want `~/dn2-workspace`. Never commit or share anything from `extracted/`, `snap/`, `data/` or `build/`: it is
Elektron's code.

## Build, check, ship
- ColdFire: `cfmod.Mod(...)` → `.write(OUT.syx)` (examples/double_time/build.py). DSP: `dspblocks` + `sasm` / `fxdsl` →
  section 7, rebuilt with `cfmod.build_image` (examples/track_trim/build.py). Both: `Mod.write(out, sec7=...)`.
- Always: `python -m dnfw.cli.main inspect OUT.syx` must print 21 `[ok]` incl. "HMAC-SHA256 reproduced".
- **Image size: the running OS receives an upgrade into a 2 MiB buffer** (2,097,152 B = 8-byte preamble + container,
  padded to 101-byte packets). Over it the upload stalls and the unit keeps its old OS. Stock 1.11 uses ~1.87 MB:
  your mods share ~220 KB. `cfmod.flash_check` measures it (aPLib is nonlinear: measure, never estimate). Big DSP tables:
  store them compressible (delta-coded, byte-planar), zero what nothing reads.
- **Never change a section's unpacked length by in-place edits**: ColdFire code goes to the appended area (dnfw loader),
  DSP code into new boot-stream blocks (`dspblocks`); section 7 must stay under 1 MiB (the ColdFire's DSP loader refuses
  more: no DSP boot).

## Flashing
- **Elektron Transfer**: drag the `.syx` onto the running unit over USB; the unit asks UPGRADE OS NOW? → YES.
- **`rpc_flash.py IMG.syx`** (Transfer closed): the same MIDI RPC Transfer / Elektroid use (OsUpgradeStart 0x50 with a
  BIG-endian u32 size, Write 0x51 in 2 KB blocks with crc32, End 0x52 = commit). It commits explicitly (with the
  auto-commit flag the first upload after a boot is lost: the upgrade view is not open yet). Confirm on the unit, keep
  the USB quiet ~90 s while it writes flash; `--ping` checks the link, `--cancel` drops a pending upload. Python + mido can
  hang at exit in CoreMIDI: the script ends with `os._exit`; kill stale flashers before the next run.
- `transport.py start|stop` plays / stops the loaded pattern (SETTINGS > MIDI CONFIG > SYNC: TRANSPORT RECEIVE on).
- Don't flash or measure while a heavy local job runs: USB MIDI timing suffers.

## Recovery (always possible: reflash stock)
Hold **[FUNC]** at power-on → **[TRIG 4]** (OS upgrade) → send the stock `.syx` over a **DIN MIDI** cable (USB MIDI does not
work in this mode), ~12-15 min: `send_syx.py STOCK.syx 'YOUR MIDI INTERFACE'` paces it at DIN speed (keep the computer
otherwise idle: a stalled sender bursts and the bootloader has no retransmit). A raw SysEx send over USB to the running OS
is ignored.

## Other public work worth checking first
- **dnfw docs/** (~80 notes: SPI reply, SHARC load, MIDI RPC, usbprobe = a read-only SysEx PEEK mod with a screen grabber,
  sequencer playhead, arp hidden modes, waverider DSP notes) and **`csrc/include/dn2_111.h`** (~150 verified 1.11
  addresses). `git pull` it before re-deriving ColdFire plumbing: LFO4, arp p-locks, FX modulation came from there.
- dnfw's **mod platform** (`dnfw mods`): one loader + one appended area for many mods, with RAM declarations; its CODE
  chunks sit at 0x467c8000..0x467fffff -- if you combine with them, keep your code / RAM elsewhere (cfmod defaults to
  0x46800000).
- elektronmods.com (community mods, mostly DN1 / DT1); Elekloader (irpina: cross-device loader with conflict detection).
