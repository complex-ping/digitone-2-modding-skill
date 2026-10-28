# Third-party code and references

This repository contains **no Elektron firmware** and no data extracted from it. Every tool reads your own copy of the
Digitone II OS 1.11 image at run time.

| file | origin | license |
|---|---|---|
| `scripts/fastemu.py`, `scripts/fastblock.c` | native re-implementations of parts of [digikit](https://github.com/m-dwyer/digikit) (its fast block stepper, idle detection and pixel HLE) | GPL-2.0 (as digikit) |
| `scripts/digikit-local.patch` | a patch to digikit | GPL-2.0 (as digikit) |
| `scripts/visa.py` | own work; 48-bit ISA field positions cross-checked against MAME `src/devices/cpu/sharc/sharc_dasm.cpp` (Ville Linde, BSD-3-Clause) and ADI's SHARC Processor Programming Reference | MIT |
| everything else | own work | MIT (LICENSE) |

Used at run time, not included: [dnfw / dn2_firmware_explore](https://github.com/angellinares/dn2_firmware_explore)
(AGPL-3.0), [digikit](https://github.com/m-dwyer/digikit) (GPL-2.0), [digiemu](https://github.com/irpina/digiemu)'s
patched Unicorn (GPL-2.0), Unicorn, mido, cffi.
