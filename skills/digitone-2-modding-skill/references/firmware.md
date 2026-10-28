# The Digitone II and its OS 1.11 image

## Hardware [verified]
| part | role | firmware | read / run it with |
|---|---|---|---|
| ColdFire MCF5445x (V4e, big-endian) | UI, sequencer, parameters, p-locks, LFOs; builds the 2688-byte frame sent to the DSP every audio block | section 3 "MAIN OS" @ `0x40000400` (3,192,192 B, GCC C++ with RTTI) | `cfdis.sh` / `cfx.sh`, Ghidra `68000:BE:32:Coldfire`, cfkit (digikit) |
| SHARC+ ADSP-21569 (1 GHz) | all audio: 4 SYN machines, filters, amp, pan, chorus / delay / reverb, master | section 7 = ADI boot stream (L1 `0x20000000`, L2 `0x2008…`, DDR `0x80…`) | `visa.py` (disassembler), `sim.py` / `simc.py` (whole-DSP emulator) |

Audio I/O: physical inputs = IN L/R only. USB class-compliant mode adds 2 ch host -> DN2 (straight to main) and 2 ch back.
One DSP block = 32 samples at 48 kHz = 1,500 blocks / s; budget ~666,667 cycles a block.

## Container, build, sign [verified]
`.syx` = SysEx (`F0 00 20 3C 15 00 …`, 8-in-7 packing) → 8-byte preamble (length, checksum) → **ELE3** container of
aPLib-compressed sections: 5 meta, 2 bootstrap (holds the signing key material — never edit), **3 MAIN OS**, 4 updater,
**7 SHARC**, 8 accessory ARM. Integrity = per-packet checksum + 32-bit content checksum + HMAC-SHA256 trailer: dnfw
recomputes all of it (`dnfw build`, or its API as cfmod does). `dnfw extract` recovers sections to diff against stock.

## Memory a mod may use [HW-proven]
- **ColdFire RAM 0x46700000..0x46900000** unused by stock (cfmod links code at 0x46800000; dnfw's own mods use
  0x467c8000..0x467fffff). Further spare RAM is plausible above, but prove it before relying on it (fill, reboot, read).
- **SHARC L2 0x20088400..0x200B0000** (stock loads L2 code only up to 0x2008823c). **NOT above 0x200B0000**: stock data
  words point to 0x200C0008 / 0x200C1F84, the startup marks 0x200FA000..0x200FDFFF non-cacheable (DMA buffers), L2 shares
  its space with the boot ROM — a block there stopped a unit from starting (recovery over DIN).
- **SHARC DDR (512 MB)**: stock references up to 0x8052FBE0; 0x80800000.. (12 MB+) read back untouched by stock on HW.
  **Power-on DDR holds garbage (0xFFFFFFFF = NaN common)**: never read DDR you did not write first. A NaN passes
  FMIN / FMAX / CLIP on the SHARC; one reaching a feedback state (send FX) silences the whole unit until a restart. The
  emulator zero-fills DDR, so it never shows this: fill new DDR with 0xFFFFFFFF in a test.
- Proven-owned space for DSP state inside stock's own buffers: the delay's L/R rings are masked to 2^18 samples — their
  upper halves are free if you cap the delay time.

## The ColdFire → DSP link (every audio block)
- SPI frame 2688 B from ColdFire `0x80005e60`; transfer 2748 B; driver `0x400cf7be(2688, tx, 2748, rx)`; TX DMA list at
  `0x80002510` (4 B / entry: cmd | data).
- **Frame layout (per VOICE, big-endian halfwords; the DSP sees each 32-bit word's halves swapped — `dn2frame.swap16`):**
  +0x00 command (3 = render); +0x02+2v note << 8; +0x22 / +0x26 note-on mask (bit v); +0x24 / +0x28 note-off; +0x34+2v
  velocity << 8; +0x54+2v velocity curve; +0x74+2v voice level; +0x94+2v machine (0 FM TONE, 1 WAVETONE, 2 FM DRUM,
  3 SWARMER, 4 MIDI); +0xd8 tempo × 120; +0xda+146v the voice's sound record; +0x9c8+2·id FX / global params 25..69
  (chorus 25–31, delay 32–40, reverb 41–48, ext-in 49–59, master comp 60–67; DSP view `0x25CE54 + 2·id`, ×1/32512);
  +0xa58 master tune × 10; +0xa5a+2v voice → track. Values are raw parameter values (knob << 8 | fine). Details:
  `dn2synth.py` docstring.
- **Side channel (yours):** the DMA moves 1374 halfwords; the 30 halfwords after the 2688-byte frame (the data halves of
  TX list entries 1344..1373) are never rewritten by the driver and reach the DSP after the frame. Write your own packets
  there (a ColdFire hook right before the driver call; the DSP reads them from the link buffer). Use a header word and a
  checksum, round-robin per frame if you have more data than fits.
- **Reply (DSP → ColdFire, 2748 B) lands at `0x800053a4`.** Word 0 = the DSP's own cycle count for the block (halves
  swapped) = a free DSP-load meter. +0x16 = master compressor gain reduction. **+0xa9c..+0xabc (ColdFire `0x80005e40`,
  16 u16) is unused by stock** = your DSP → ColdFire channel (a DSP word = two big-endian u16 for the ColdFire, the low
  half first).
- **Late frames:** the DSP renders on the codec clock and does not wait for the ColdFire. A frame that lands after its
  render started is dropped (the block renders the previous frame again) — with it its note-on masks: "the trig lights,
  no sound". That happens when the ColdFire's audio ISR overruns its period (coldfire.md "Audio ISR budget").

## Voices are not tracks
The DSP renders 16 VOICES; the ColdFire allocates them per note and copies the track's sound into the voice record.
Unison takes several voices for one note. The note path, the lock applier, the LFO evaluator and the per-voice mirror
blocks are all per VOICE: map voice → track (`0x8000dda0[voice]`, frame +0xa5a) everywhere. The #1 bug source.
