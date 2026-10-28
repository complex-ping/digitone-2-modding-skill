---
name: digitone-2-modding-skill
description: Develop your own firmware mods for the Elektron Digitone II (OS 1.11) — reverse-engineer and patch the ColdFire MAIN OS (UI, sequencer, parameters, p-locks, LFOs, screen) in C, add SHARC DSP audio code (effects, per-track processing, new machines), verify both in a ColdFire emulator (the real OS) and a whole-DSP SHARC emulator, then rebuild, re-sign and flash the .syx. Use for any Digitone II / DN2 / ELE3 / ADSP-21569 SHARC+ / ColdFire MCF5445x firmware work, Elektron OS image unpacking or signing, DSP load, flash budget, or flashing over USB MIDI.
---

# Digitone II firmware modding

Everything here works on **your own copy of Elektron's Digitone II OS 1.11** (sha256 `2af43e65…72e0`). No Elektron code
ships with this skill; never commit or share extracted sections, snapshots or built images. Every mod is reversible:
reflash stock (references/setup.md "Recovery").

## Start
1. `python3 scripts/dn2setup.py` — checks / sets up the workspace (`$DN2_WORKSPACE`, default `~/dn2-workspace`): your stock
   image, **dnfw** (container + signing + C builds), the m68k toolchain, **digikit** (ColdFire emulator), PyPy. It prints
   the exact command for anything missing (ask the user before cloning or installing). Then `--boot` and `--template` once.
2. Work through an example, then copy it:
   - `examples/double_time/` — a **ColdFire C hook** (the sequencer clock runs at 2x): `build.py` → `test_emu.py` in the
     ColdFire emulator (stock control in the same test).
   - `examples/track_trim/` — a **SHARC DSP mod** (track 1 −6 dB in the main mix): a new L2 code block, a CALL hook,
     an fxdsl kernel; `test_emu.py` = A/B against stock in the SHARC emulator.
3. Flash: `scripts/rpc_flash.py IMAGE.syx` over USB (or Elektron Transfer), confirm on the unit.

## Read only what the task needs
| task | read |
|---|---|
| install, workspace, build, image size, flashing, recovery, other public work | `references/setup.md` |
| the hardware, the image format, free memory, the ColdFire → DSP frame and side channels, voices vs tracks | `references/firmware.md` |
| ColdFire: adding C, parameters, p-locks, trigs, sequencer clock, arp, the audio ISR budget, UI / screen / keys | `references/coldfire.md` |
| SHARC: reading, where code may go, hook sites, the hardware rules, fxdsl, the audio path, engines, FX | `references/sharc.md` |
| driving the emulators, what they cannot see, their gotchas, measuring on the unit | `references/emulators.md` |
| the working loop, rules, diagnosing a silent unit, **the spec questions to ask before building** | `references/workflow.md` |

## Tools (`scripts/`, all paths via `dn2paths.py`)
| tool | what |
|---|---|
| `dn2setup.py` | workspace check / setup from your stock image |
| `cfmod.py` | ColdFire mod builder: C / asm → linked at 0x46800000, dnfw loader, stock-checked hook sites, signed .syx, flash-size check |
| `dspblocks.py` | add L2 / DDR blocks to the SHARC boot stream, CALL hooks, 32-byte line padding |
| `fxdsl.py`, `sasm.py`, `sisa.py` | DSP kernels in Python (code + bit-exact model) / a verifying mini assembler / 48-bit packer |
| `visa.py`, `cfdis.sh`, `cfx.sh` | SHARC disassembler; ColdFire disassembly, xrefs, functions |
| `deadscan.py` | unreferenced SHARC functions (caves) |
| `hwrules.py`, `shapecheck.py`, `encodecheck.py`, `bootblocks.py` | the static hardware-rule checks the emulator cannot do |
| `sim.py`, `simc.py`, `dn2frame.py`, `dn2boot.py`, `dspsnap.py`, `dn2synth.py`, `dsptrace.py`, `simc_check.py` | whole-DSP emulator: boot, render real patches to WAV, A/B, trace |
| `cfkit.py`, `fastemu.py`, `cfemac.py`, `cfhooks.py`, `dn2capture.py` | ColdFire emulator harness on digikit: panel, screen, sequencer, firmware calls, call rings |
| `rpc_flash.py`, `transport.py`, `send_syx.py`, `dn2rpc.c` | flash over USB MIDI RPC, start / stop, DIN-paced recovery send, Screenshot RPC |

## Rules that cost hardware round trips (details: references/workflow.md)
- **Settle the spec** in one round of questions first; **reproduce a bug as a failing emulator test** (with a stock
  control) before fixing it; **emulator + static checks before every flash**; flash one clearly named image with its
  sha256 and a list of what to try.
- **Voices ≠ tracks.** **Stock routines bound `slot <= 100`.** **Hooks keep every register the code after them reads.**
- **SHARC:** whole 32-byte boot blocks, stock encodings only, the PRM loop / DAG / branch rules, no L2 above 0x200B0000,
  never read memory you did not write (power-on DDR is NaN garbage). The emulator runs all of these "fine".
- **The unit pays for memory, not instructions**; the ColdFire's audio ISR budget is small (late frames = silent trigs).
- **Image ≤ 2 MiB as received** (`cfmod.flash_check`); section 7 < 1 MiB.
- After 2 failed flashes on one problem, stop and restate what is proven.
