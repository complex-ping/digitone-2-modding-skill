# digitone-2-modding-skill

An agent skill (Claude Code, Codex, Cursor, … via [skills.sh](https://skills.sh)) for **developing your own firmware mods
for the Elektron Digitone II** (OS 1.11):

- patch the ColdFire MAIN OS in C (UI, sequencer, parameters, p-locks, LFOs, screen, keys);
- add SHARC DSP audio code (effects, per-track processing, new machines);
- verify both in two emulators: the real OS on [digikit](https://github.com/m-dwyer/digikit), and a whole-DSP SHARC
  emulator included here;
- rebuild, re-sign and flash the `.syx` over USB.

It packs the reverse-engineering notes (stock addresses, hook sites, the ColdFire → DSP frame, free memory), the hardware
rules that the emulators cannot see (each one once silenced a real unit), and the tools: a ColdFire mod builder, a
SHARC boot-stream block adder, a DSP kernel DSL with a bit-exact model, static hardware-rule checks, a USB flasher, and
two worked examples that pass their emulator tests.

## Install

```bash
npx skills add julian-kominiak/digitone-2-modding-skill
```

Then ask your agent to set up the workspace (`scripts/dn2setup.py`). It checks every dependency and tells you what is
missing.

## You need
- **your own copy** of Elektron's Digitone II OS 1.11 `.syx` (not included; sha256 `2af43e65…72e0`);
- Python 3.11+, [dnfw](https://github.com/angellinares/dn2_firmware_explore) (container, signing, C builds), an m68k
  GCC (`gcc-m68k-linux-gnu`), [digikit](https://github.com/m-dwyer/digikit) for the ColdFire emulator; recommended
  PyPy, `cffi`, `mido` + `python-rtmidi`.

Details: [`skills/digitone-2-modding-skill/references/setup.md`](skills/digitone-2-modding-skill/references/setup.md).

## Examples
- [`double_time`](skills/digitone-2-modding-skill/examples/double_time): a ColdFire C hook. The sequencer clock runs at
  2x; checked in the ColdFire emulator (4 → 8 sixteenths in 300 blocks).
- [`track_trim`](skills/digitone-2-modding-skill/examples/track_trim): a SHARC DSP mod. Track 1 is 6 dB down in the
  main mix, from a new L2 code block, a CALL hook and an fxdsl kernel; checked by an A/B render against stock (ratio
  0.5000, track 2 unchanged).

## Disclaimer
Unofficial and not affiliated with or endorsed by Elektron. Elektron and Digitone are trademarks of Elektron Music
Machines. Flashing modified firmware is at your own risk. A unit can always be recovered by reflashing the stock OS over
DIN MIDI (hold [FUNC] at power-on, [TRIG 4]). Do not redistribute Elektron's firmware or modified images of it.

Licenses: MIT, except the digikit-derived files (GPL-2.0); see [THIRD-PARTY.md](THIRD-PARTY.md).
