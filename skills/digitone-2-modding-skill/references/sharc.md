# SHARC+ (section 7): reading, adding code, the audio path

## Reading
- `visa.py 0xADDR [n]`: a validated VISA disassembler (stock section 7: 0 invalid opcodes, all 5,172 branch / call / loop
  targets on instruction boundaries). Addresses are short words (sw). Load byte address: L2 `2·sw + 0x28000000`; L1
  `0x20000000 + 2·(sw − 0xB80000)`; DDR code sw 0x800000.. = 0x80A00000... Data `0x002xxxxx` in code = loader `0x282xxxxx`.
- Encodings that are not obvious (each caused desyncs): words are 16-bit little-endian, the instruction's MSW first;
  32-bit 'b' forms are the 48-bit form with a marker in the compute field; Type 19a in SHARC+ is `000101 sc` (0x15xx =
  `MODIFY(I7, n)`); 14d = 0x1a/0x1b. Access sizes (l, x, w) differ per type — `visa.access_size()`; `(0,0,0)` = BYTE.
- The stock code is ADI C-compiler output: CJUMP / RFRAME frames, `udiv`, callee-saved R3, R5–R7, R9–R11, R13–R15; the C
  runtime keeps M5 = 0, M6 = 1 (and M13 = 0, M14 = 1).
- `sharcatlas.py`: functions, callers, callees, and `reread` / `confirm` to read a stretch compactly and record it. A name from structure alone is often wrong; read the code, and run it (`sharc_checks.py`) when a mod relies on it.
- Wrong tools, do not use: digikit's own VISA decoder and the SLEIGH SHARC Ghidra module (wrong tables), the SHARC+ PRM's
  Type 2a/2b figures.

## Map of the code (OS 1.11 and 1.12: section 7 is the same)
Addresses are short words. FreeRTOS and C library names are the upstream ones; the rest describe what the code does, found by reading it (`sharcatlas.py`).
[run] marks routines `sharc_checks.py` runs and compares; everything else here is a reading.
- **Start:** `Dsp_main` 1ca6d9 sets up clocks, SPU, audio state, PCG, the SPI link and codec SPORTs, creates the audio task, then `vTaskStartScheduler` b887ac. `Audio_task` 1c9e76, `Audio_runCommand` 1c9d6b.
- **Render:** `Render_block` 1c2712, `Render_voices` 1c8ef1, `Mix_fxBus` 1c1738, `Mix_main` 1c1f1e, `Mix_outputs` 1c244d.
- **Machines:** `FMTone_render` 1c54c7, `FMDrum_render` 1c778b, `Wavetone_render` 1c6d4a, `Swarmer_render` 1c85ad.
- **Filters (ids 0 MULTIMODE, 1 LOWPASS 4, 2 EQUALIZER, 3 COMB-, 4 LEGACY LP/HP, 5 COMB+; 2 + 4 and 3 + 5 share code):** `MultimodeFilter_block` b82342, `LowpassFilter_block` b80f2e, `EqFilter_block` b81a16, `CombFilter_block` b806f5, `LegacyFilter_block` b81526. Envelope `Adsr_block` b80345.
- **Wave tables:** ROM s16 at `0x2001e888 + 2 * 0x401 * i`, read through `Wave_bankPtrSine` 1c4b1e, with a byte-equal copy for FM Drum at `0x200779f8`. The band limited tables at `0x2002f0d0..` are not ROM data: `Wave_buildTables` 1c463b makes them at boot by additive synthesis: sine at `0x2002f0d0`, then 48 tables per bank (notes 24 + 2i, harmonics under 23 kHz, cos² window): triangle `0x2002f8d8`, saw `0x20047938`, square `0x2005f998`.
- **Reverb:** feedback delay network, 8 lines, two 4 x 4 Hadamard matrices, 8 allpass diffusers: `Reverb_init` b83dc1, `Reverb_linesInit` b83cbd, `Reverb_block` b836bc, `Reverb_core` b82d1a, `Reverb_allpassBank` b82a5b.
- **Delay:** `Delay_init` b848cb, `Delay_block` b8482c, `Delay_writeRing` b83f72, `Delay_readTaps` b8428b, `Delay_saturate` b84099. **Chorus:** `Chorus_init` b84f51, `Chorus_block` b84d5c, `Chorus_core` b84a3b.
- **Master compressor:** `Comp_init` b85501, `Comp_block` b8541d, `Comp_sidechainFilter` b84fd3, `Comp_gainComputer` b85070, `Comp_gainSmoother` b85111, `Comp_applyGain` b851e5, `Comp_mix` b852d2. **Overdrive:** `Master_overdrive` 1cdc30.
- **Math [run]:** `cosf` 1c1018, `sinf` 1c10e4, `tanf` 1c125e, `log10f` 1c0b60, `powf` 1c0c76, `expf` 1c11a5, `sqrtf` 1c122e, `floorf` 1c07cc, `float_div` 1c06ba, `udiv32` b8b4dc, `memset` 1c0bde, `memcpy_bytes` 1c0756, `rand` 1c0df6. Some return with RTS instead of the C frame; `sharc_checks.py` shows how to call each. `floorf` is `FIX` with MODE1.TRUNC, which goes toward minus infinity; the `TRUNC` instruction goes toward zero.
- **RTOS:** FreeRTOS queue, tasks, timers, event groups, stream buffers, heap 4, found by their assert strings. 8 priorities, heap `0x2d08b8..0x2dd0b0`, `Rtos_assert` 1cd978 is configASSERT, `vPortEnterCritical` b8afca, `vPortExitCritical` b8afd7. Port: `xPortStartScheduler` b8b3c8, `pxPortInitialiseStack` b8af28, yield ISR `0xb8a947`, tick ISR `0xb8afe4`, core timer period 1,000,000.
- **Drivers:** ADI services. PCG `Pcg_open` b8db33 (registers `0x310c9300`, `0x310ca300`), GPIO `Gpio_init` b8cabb, `Gpio_setDirection` b8cb1b, SEC `Sec_init` b8ec2c (`0x31089000`, SCI `0x31089400`, sources `0x31089800 + 8n`), interrupts `Int_install` b8ce24, `Int_uninstall` b8ced5, `Irq_registerHandler` b8b782, DMA `Dma_start` b8e46a, SPU `Spu_open` b8edee, SPI `Spi_open` 1cc753, SPORT `Sport_open` 1ccd08.
- **Shared with Digitakt II (OS 1.17):** comparing instruction sequences with immediates masked, 388 of 471 functions (data excluded) are at least 80 % present there: RTOS, drivers, FX, filters, envelopes, math, `Audio_task`. The machines, `Render_*`, `Mix_main` and `Mix_outputs` are the Digitone's own. A Digitakt finding on the shared part should carry over; check the constants. Digitakt's image gives few names of its own: a task string, `RPC dispatcher`, and two asserts from `lib\esp5-dsp\dsp-lib\sampler\digitakt_rompler_update.c`.

## Adding code: where
- **New blocks in the boot stream** (`dspblocks.py`): code at **L2 0x20090000** (sw 0xBC8000), data at 0x20098000, more up
  to 0x200B0000 — all above where stock loads L2 code (0x2008823c) and HW-proven. **Not above 0x200B0000** (firmware.md).
  Big tables: DDR blocks (DDR code at sw 0x800000.. = 0x80A00000.. runs, but every fetch is a DDR access: slow).
- **Caves = unreferenced stock functions only** (`deadscan.py`: no branch / loop target, no immediate or data word points
  into it). NOT: the L1 block tails (`0x39C000–0x39FFFF` etc. are the caches → silence), the 12 NOPs after the SHBTB_CFG
  write at sw `0x1C12E8` (boot crackle), `0x1c0a6e..0x1c0b2f` (interrupt trampoline), `0x1c113e` (live at boot). A cave
  piece must end in a JUMP + NOP48 padding; a packer that splits label-free pieces ≤ 30 sw into dead functions works.
- **Hooks:** a `CALL` over one stock 48-bit instruction your routine replays (`dspblocks.call_hook`), or a `JUMP` that
  jumps back. Proven sites:
  - `0x1c1fef` `R2 = DM(0x257980)`: main mix, the 16 stereo track buses complete (no loop active) — per-track effects.
  - `0x1c3054` the FX-bus mixer's CJUMP `0x1c1738` — post-voice, per-voice stereo buffers ready. **The mixer takes R4 and R8
    as arguments: a hook between call site and callee must keep them** (a unit lost all audio when R8 was clobbered).
  - `0x1c9543` voice chain, after the pre-filter stages, before the filter dispatch (replay `I5 = I9; I1 = I9`, back to
    `0x1c9547`) — per voice, mono, pre-filter.
  - `0x1c90a6` engine dispatch (table `0x8052db90`, M4 = engine index): a new machine type needs a hook here.
  `vhooks`-style check: compare every register at the callee between stock and your build (the emulator ignores stray
  accesses, so a clobbered argument shows only as a register diff).

## The hardware rules (every one silenced or crashed a unit once; the emulator runs all of them "fine")
- **Whole 32-byte lines per boot block.** A code block of 8n + 4 bytes boots in the emulator and leaves the unit silent
  from power-on (L2 ECC per 64-bit word: the I-cache line fill over the block's end faults). `dspblocks.pad_lines`;
  `bootblocks.py SEC7` checks.
- **Encode exactly like stock.** visa.py decodes some fields narrower than the silicon: a negative shift immediate (Type 6a
  `LSHIFT/ASHIFT Rx BY -k`) needs its sign bits in the middle parcel — stock writes `023e 7800 ..` for all 368; with 0 there
  every decoder and emulator accept it and the unit raises ILOPI and stops. `encodecheck.py` (all instructions you emit vs
  the bits stock keeps fixed for that shape), `shapecheck.py` (any shape stock never uses needs a PRM justification).
- **Same-DAG:** never load a DAG register through the same DAG with post-modify (`M4 = DM(I0,M6)`), never store a DAG
  register through the same DAG (`DM(k,I6) = I0`). Load a data register, then `M4 = R15`; save I / M registers via `R2 = In`.
- **Hardware loops:** the last 11 instructions must be 48-bit ISA; a body shorter than 11 all ISA; end on a 48-bit
  instruction; no nested loops in your code.
- ≥ 12 valid 48-bit instructions after every branch in the same block; no two branches in one 48-bit window (≥ 2 sw
  apart): `NOP48, CALL, NOP48`; end your code with 12 NOP48.
- Copy load / store encodings from real firmware; `visa.access_size(ins) == ''` (`4bfe 003c` is a BYTE load).
- Keep every register / flag the code after a hook reads; save what you touch (an absolute save area in L2 data).
- Every audio-writing build: clamp the output, bounded work per block, only memory stock provably does not own.
  Never read memory you did not write (DDR power-on NaN).
- Section 7 under 1 MiB.
`hwrules.py SEC7 --baseline STOCK` (static: only new violations), `sim.strict_hw` (runtime `HWRuleError`), `sasm.lint`
(branch spacing, prefetch, same-DAG, register whitelist, loop shape).

## Writing DSP code
- **fxdsl** (`fxdsl.py` docstring): one Python source → SHARC code + a bit-exact float32 model. It keeps the rules above by
  construction (48-bit packed loop bodies via `sisa`, no same-DAG forms, branch spacing, stock access shapes) and clobbers
  only R0–R15 / I0–I4 / M4 (the caller saves them). Limits: no nested loops; no `if`, `fext` or shifts inside a loop
  (arithmetic masks: min / max / abs); compares lt / ge / eq / ne; `DM(k, I)` offsets −64..63 (inside a loop: 0 /
  post-modify only — load per-block constants into registers before the loop); 16 R/F registers, 5 pointers; one
  non-unit pointer step per loop (M4); no divide (Newton) and no integer multiply (shifts); loop counts are constants;
  `k.set(v, temp)` re-targets the op that made `temp` (wrap a value used twice in `k.var(...)` first). Kernel constants
  must not depend on the interpreter (no numpy fit at import: CPython and PyPy emitted different constants).
- `sasm.py` (mini assembler: every emitted instruction is decoded back and compared to the text you meant), `sisa.py`
  (48-bit ops + a dependency-safe packer for loop bodies). Encodings (16-bit LE words): `0F0r hi lo` Rr = imm32;
  `063E 00HH LLLL` JUMP abs; `06BE hi lo` CALL abs; `0700|cond<<1, off>>16, off` relative (EQ 0, LT 1, NE 16, GE 17, GT 18);
  2c short compute `C000|op<<8|n<<4|r` (8 fadd, 9 fsub, 15 fmul, 2 pass); 14a `100r/110r hi lo` DM(abs) load / store;
  `0ABE` RTS; NOP48 `0000 0000 0000`.
- Streamed rings (post-modify pointers, block offsets in state, a mirror of the first block after the end) cost 4–8
  instructions per sample-stage instead of 14. A loop costs ≥ max(memory ops, flops).
- Template: `examples/track_trim` (L2 block + CALL hook + fxdsl kernel + save / restore + emulator A/B test).

## The audio path (1.11) [emulator; the parts marked HW also on units]
- Audio task `0x1c9fe7` → `0x1c9d6b` (reads EMUCLK before / after: the DSP measures its own load, reply word 0) → cmd 3 →
  render `0x1c2712`: copy the 672-word frame to `0x25c48c` (at `0x1c302d`, just before the voice render; converters before
  it read the PREVIOUS block's copy) → **voice render `0x1c8ef1`** → FX-bus mixer `0x1c1738` → main mix `0x1c1f1e` →
  outputs `0x1c244d` → soft clipper `0x1c30cd` (±0.693). Main out floats at `0x268438` (L 0..31, R 32..63).
- **Link frames [HW]:** in = `0x2c29d0 + bank << 12`, reply = `0x2c49d0 + bank << 12`, bank = DM(`0x2c0450`). The SPI receive
  DMA ping-pongs between the two in-banks; its completion callback `0x1ca04a` (registered at `0x1cb7dc`) toggles the bank.
  A frame landing after its render started is never rendered (its note-on masks are lost) — a callback hook can merge a
  late frame's masks into the next block.
- **Voice chain** (each stage loops over the 16 voices' MONO buffers, DDR `0x804acf90 + 128v`, pointer table `0x254a60`):
  engine render → BR `0xb8251b` → OD pre `0x1cdb56` → DC filter `0xb80c39` → SRR pre `0xb8265b` → base-width filter pre
  (`0xb81368` from `0x1c959f`) → [`0x1c9543` hook site] → filter machine dispatch (table `0x8052dbc0`, `JUMP (M13,I12)` at
  `0x1c9595`; filter switch `0x1c9580` reads `0x254ad4 + 4v`, ≥ 7 = no filter) → OD / SRR post → base-width post → amp →
  pan `0x1c9ae7` → **stereo voice buffers `0x257ee8 + 256v`** (L 0..31, R 32..63; ~±0.07 full scale; main mix ×14.1254).
  Pre-filter level ~±0.1. Engine buffers are at a huge internal scale (SWARMER ~±3e4): never inject audio there.
- **Engines [HW]:** the dispatch at `0x1c90a6` (table `0x8052db90`) is a per-voice prep step; the RENDERS are separate 16-voice
  loops at `0x1c93e5..0x1c9447`, each testing the voice's engine word = param block `0x2554b8 + 564v + 436` (rewritten every
  block by `0x1c2997`): FM TONE `0x1c54c7`, WAVETONE `0x1c6d4a`, FM DRUM `0x1c778b`; 4 (MIDI) matches no loop. Stock renders
  every voice's engine + filter every block, sounding or not (16 FM TONE: ~331k instr; no engines ~154k): a voice of your own
  machine must set the engine word to 4 or it also pays for a stock render. Engine state `0x2495e0 + v·0x55c`;
  render(R4 state, R12 voice buffer, 32 samples).
- Voice → track map int16 `0x257898 + 2v`; per-voice mute bytes `0x2578c8 + v`; send gains (chorus `0x257a24 + 4v`, squared,
  mute-gated) into the buses delay `0x25af68`, reverb `0x25b268`, chorus `0x25b568` — taken from the VOICE buffers before
  the main mix (a bus effect does not reach the sends).
- **Track buses:** the main mix zeroes 16 stereo buses `0x258f68 + 256t` (L 32 words, R at +128 B), sums every voice buffer
  into its track's bus × 14.1254 (a stolen voice fades out over 31 samples via byte `0x2578b8 + v`); complete at
  `0x1c1fef`. Per-track mono sums `0x259f68 + 256t` after the FX mixer.
- **Note-on in the DSP:** in the frame copy `0x25c48c` word `0x25c4ac` bits 16 + v = note-on of voice v this block;
  `0x25c4b0` = off mask. Engines restart at a note-on (the pre-amp signal steps; the amp hides it — a resonant PRE effect
  must not hold that step).
- **External input:** per block `0x1c9d00` converts the codec receive ring (`0x2c0678 + 256 × DM(0x268a38)&1`, L R
  interleaved Q31) to floats L `0x268538`, R `0x2685b8`; after IN level / BAL / pan the FX-bus mixer writes IN_BUF
  `0x25bc68` (4 lines × 32); the main mix adds it × 14.1254.
- FX: chorus `0xb84d5c`, delay `0xb8482c` (2 × 2^19-float rings, state `0x8005912c`), reverb `0xb836bc`; chorus params
  `0x257950` (dry / wet forced 1.0: send FX return WET only); converter `~0x1c2d80..0x1c2fc5`.
- **Timing on the unit:** `Rd = EMUCLK` = `W(0x76BE, 0x8000 | d << 7 | 0x3F)`; the emulator's EMUCLK = its instruction
  count, the unit's = core cycles with stalls. Budget ~666k cycles a block; stock idle ~63–65 % on the unit.
- **Cost = memory, not instructions [HW]:** L2 / DDR accesses and DDR code fetches cost the unit several times their
  instruction count (+7 % instructions = +21 % load once; a per-voice DDR path ~10x its count). An idle path that touches
  DDR per voice is expensive: skip effects at mix 0 (but keep what later reads need), keep hot state in L1 / L2.
