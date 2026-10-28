# The two emulators: what they are, how to drive them, what they cannot see

## SHARC (whole DSP) — `sim.py` / `simc.py` + `dn2frame.py`
- `sim.py`: SIMD, all access sizes, loops / stacks, PRM-sourced semantics; `simc.py` + `simc_core.c` = the same interpreter in
  C (cffi, compiled on first import into `scripts/build_simc/`; `DN2_SIM=py` forces Python). ~200 blocks / s vs ~7 (PyPy)
  or ~1 (CPython). `pip install cffi` for the interpreter you run. Change semantics in BOTH files, then `simc_check.py`.
- Boot once: `dn2setup.py --boot` (or `dn2boot.py 700 OUT.snap.gz SEC7.bin`, ~4 min). A snapshot holds only the words the
  boot WROTE; other reads fall back to the image, so a mod whose changed words the boot never wrote (new L2 blocks, code
  that runs per block) reuses the stock snapshot directly (`dspsnap.py NEW_SEC7` says REUSE or NEEDS BOOT).
- Run: `D.Frame(D.image_from_sec7(SEC7), snap)`, `f.block(packet)` = one 32-sample block; `D.render(frame, packets)` →
  (L, R, instruction counts); `D.write_wav`. Packets from `dn2synth.Song().assign(voice, Sound('WAVETONE').set('FILTER
  .BASE', 40)).track(voice, t).note(block, voice, note, vel, length).frames(n)` (stock parameter values from your own
  parameter dump, the frame base from a real OS capture). Your side-channel data: `dn2synth.side_packet(words)`.
- Probes: `f.s.on_insn = hook` traces (Python speed; set `f.s.on_insn_pcs` to the PCs you need and the C core stops only
  there); `mem.watch`; `frame.s.mem.on_uninit = fn(pa)` to see reads of never-written RAM; `dsptrace.py` (per-voice words
  monotone in a knob / shaped like an envelope); call-order diffs between two renders find routing.
- **Trust it for:** logic and arithmetic in the real frame, stock-vs-mod A/B (the template test: same frames through stock
  and your section 7, compare outputs), memory footprint, the PRM rules it encodes. **Not for:** timing, caches, stalls,
  interrupts, DMA, the branch predictor, ECC / boot-ROM loading, power-on memory contents, anything the PRM forbids that an
  interpreter executes "correctly" (hence the static checks). Instruction counts under-predict the unit's load.

## ColdFire (the real OS) — digikit + `fastemu.py` + `cfkit.py`
- digikit runs the MAIN OS in Unicorn with HLEs (flash, SD, panel, screen). `fastemu`: a native block stepper (10–30 M
  instr / s), native idle skip / ISR return / pixel HLE; `cold_boot` + `to_screen` boot an image to the main screen (~1 min
  first time, cached per image as `<image dir>/emu/main.snap`). Run your scripts with digikit's venv
  (`.venv-fast/bin/python`) from the digikit checkout.
- `cfkit.Kit(section_3.bin, stock.syx, symbols.txt, outdir)`: `tap / press / release / turn(enc, delta, n) / turn_to`,
  `frames(n)` (runs the audio ISR and delivers the forced sequencer interrupts; returns the SPI frames the OS sent),
  `play(n, bpm)`, `notes(frames)`, `call(fn, args)` (run any firmware / your function), `sound / mach / slot`, `ring(entry,
  n)` (record every call of a function: return address + 7 args), `knobs()`, `hot()`, `watch_reads()`, `h.png(name)`.
  Panel wiring: TRIG n = (3 + (n−1)//8, (n−1)%8); RECORD / PLAY / STOP (2,2)/(2,3)/(2,4); [FX] (0,4); FUNC (2,0); NO (1,3),
  YES (1,1); knob push (5,k). `dn2capture.py` records the SPI frames of a gesture from a digikit snapshot.
- EMAC: plain Unicorn gets fractional MAC wrong (halves it) — digiemu's patched Unicorn fixes it; cfkit / dn2capture also
  run every EMAC instruction of the OS in Python (`cfemac.py`, rows from `dn2setup.py`).

### ColdFire emulator gotchas (each one cost a debugging session)
- Its timing is not the unit's: the idle skip fires widget timers far more often per audio block (redraws look smooth that
  are ~1 / s on HW); an image change that adds no executed code can shift a held note by frames. Tests must wait for the
  event, then check the property — never measure latency by accident. Test the redraw REQUEST, not the time.
- Encoders register in bursts: unheld single detents do nothing; send 11-detent messages (`turn_to`), ~10 M instructions
  apart to add up. Small-range knobs step only on big bursts. Held turns jump. In menus, a pushed encoder's turns may never
  reach the view ([FUNC] + turn does).
- Hold keys / pushes ≥ 12 M instructions (`press(k, 12_000_000)`): a 3 M push can fall between UI polls and be lost depending
  on how much code the UI loop runs. Keys right after encoder bursts are sometimes dropped (stock too): retry.
- A button GROUP's whole mask is sent: a combo in one group needs `panelin.press(m, prof, ch, bit, held=mask)`.
- Python code hooks don't fire under the fast stepper (use `ring()`, a RAM trampoline; or `uc.ctl_remove_cache(addr,
  addr + 2)` to drop the translated block). Unicorn memory hooks work but slow the UI enough to change its behaviour: poll
  memory between frames; find heap addresses by diffing memory between two gestures.
- The panel TX ring is never drained: after ~4 KB of LED traffic every LED write spins (cfkit NOPs the wait at `0x40002256`).
- The UI resets the tempo word `0x402A0DEC`: set it right before PLAY. Captures can land mid-redraw: capture until two in a
  row are equal, or render `*(0x402a0b8c)` (the last frame sent).
- No DTCN2 timer (reads 0): time things in audio blocks. Every idle voice is sent as track 0.
- Start-state flakes: a long test can lose input depending only on boot timing (a BSS 4 bytes longer flips which test
  fails). Before bisecting C, rerun from a few start offsets (0 / 1k / 100k / 1M extra instructions): a real bug fails at
  every offset.
- A mid-run snapshot must save digikit's checkpoint components AND timers (else a frozen screen).

## Measuring on the unit (when HW and emulator disagree)
- The Screenshot RPC (`dn2rpc.c shot`, macOS CoreMIDI) returns the unit's live frame buffer: a mod can publish a debug page
  there (draw a 1 KB block of counters into the buffer the RPC reads, tagged, and read it over USB) — the way to read live
  DSP / ColdFire state without a reflash per question. dnfw's `usbprobe` mod (HELLO / STATS / PEEK over SysEx) is another.
- DSP load: the reply's word 0 (cycles per block) or EMUCLK stamps around your code, summed and published as above.
- ColdFire ISR time: stamp DTCN2 at your hook entry / exit and before the ISR's RTE (`0x40027bbe`), publish max / avg.
