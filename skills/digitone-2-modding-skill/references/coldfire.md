# ColdFire (section 3, MAIN OS @ 0x40000400): stock facts, hooks, UI

Disassemble: `cfdis.sh START END`; look up: `cfx.sh refs|at|fn ADDR` (callers, context, a function). A built image:
objdump its `build/X/code.bin` at the code address. dnfw's `csrc/include/dn2_111.h` names ~150 verified addresses —
check there before re-deriving one. [V emulator] = seen in the ColdFire emulator; [HW] = confirmed on a unit.

## Adding code
- **C (preferred):** `cfmod.Mod` = dnfw's startup loader (a 138-byte cave at `0x4028da6e` copies the appended area to its
  load address at boot and calls your init) + `dnfw.patch.cbuild` (m68k GCC, `-mcpu=5475 -Os`). Hooks = `jsr stub` or
  `jmp stub` over N displaced bytes; the build asserts those bytes against stock. Template: `examples/double_time`.
- Calling convention: GCC ColdFire (args on the stack, result in d0; d0/d1/a0/a1 scratch). A hook in the middle of a
  function must keep d0/d1/a0/a1 the code after it reads (the stub saves them). A hook replacing a stock function's entry
  must keep the callee's exit d0/d1/a0/a1 (callers read them) and copy every stack argument.
- ColdFire `movem` only takes `(An)` / `d16(An)`: `lea -12(%sp),%sp; movem.l %d1/%a0-%a1,(%sp)`.
- No libc: a local struct / array initialised with zeros becomes a `memset` call and the link fails — use static const
  data or field-by-field stores. Every symbol a hook names must be an entry (else `--gc-sections` drops it). Appending
  asm to a .S file: check its last section directive (code after `.bss` lands in zeroed BSS → the boot dies in the
  exception reporter `0x4011ea6a`; symptom: a stub address past `code.bin`'s size). Recount `sp@(n)` after every push.
- **Asm cave:** `0x4028ea02` (170 B, proven). Never the ~64 KB of zeros near `0x402e2000` (the .data initialiser).
- Heap: malloc `0x4011ffe8` (veneer `0x40120264`), free `0x401201ee` (veneer `0x40120270`); 16-byte granules.

## Parameters and sounds
- **Param table** `0x401f7fc8`: 320 records × 60 B (`dnfw params`). Words: +0 handler, +0x08 group, +0x0C id, **range =
  lo16 @+0x16, default = lo16 @+0x1A**, **MIDI CC = hi16 of +0x20**, NRPN +0x24, names @+48 long / +52 page / +56 short.
  Entry number = record index + 1. To add parameters, relocate the table (dnfw `paramtable.relocate`) and append records
  (copy a template record of the same kind; the drawer reads the record AFTER an entry — the last needs a guard record).
- **Value text** `0x400c243c(entry, value)`: the formatter of entry e = the FIRST word of the next 60-byte block — a cloned
  record must set it. Formatters: `0x400e3404` OFF/ON, `0x400e331c` LEN fractions, `0x400e2ecc` plain numbers. Enum
  formatters print ERR past their table: live values must stay in the record's range.
- **Value slots:** a sound's 101 u16 values at `sound + 0x14 + 2·slot` (slots 0 / 65 / 100 are holes: no free slot).
  Entry → slot `0x400dbcc4`. Live sound of track t = `*(0x800052A0) + 52 + 1163·t`. Stored sounds have no free bytes
  except the reserved 4th-LFO lane (dnfw's LFO4 uses it).
- **Value setter** (`0x40037b5x..c50`; args entry, value, track, record byte fp+27 → d7, event byte fp+31 → d6): entry →
  slot; **bound `slot <= 100` @`0x40037bd0`**; write @`0x40037be8`; then d7 → live-record call `0x4003f1ae(0x4018a97a(),
  track, entry, value)`; d6 → SoundParamChangedInfo event; else `0x4002585a(value, track, slot)`. Getter bound
  @`0x4003717c`. **Every stock value / lock / LFO routine bounds `slot <= 100`**: a new slot needs every one hooked — when a
  gesture works on stock pages but not on yours, find the routine and its bound first.
- **FX / global params to the DSP:** mirror slot(id) = `0x80006828 + 2·id`, copied into the frame by `0x400275a2..690`.
- **Knob turn (sound pages)** `0x40063fd6(view, entry, delta, func, ...)` → model vt[44] → clamp `0x40037326` → the setter.
  [FUNC] held = stock sets the record default. Knob reset: push + [NO] (one knob), [PAGE] + [PLAY] (page).
- **Sound load clamps the machine byte** (`0x400dd2ae`: kept only if machine + 1 < 6). Save copies live +0xDE → stored +0xF4.
  Sound converters: load `0x400dd1ea` (stored +338 → live +358 for the arp block), save `0x400dd6a6`. Sound save rewrites
  the LFO DEST stored words (slots 4 / 12 / 20) after its hooks: their low bytes are not free space.
- **Every per-sound side table needs a reset hook:** a sound reset to defaults (`0x400e7e66`; kit constructor `0x400e7044`
  per sound) and heap kits (CREATE NEW = `0x400306e2`) reuse memory.
- VOICE SETUP per track: kit = `0x4003e41a(0x4018a97a())`; VELOCITY TO VOL `0x4004b304/0x4004b334(0x40030e66(kit, t)[, v])`.
  Kit object = root + 0xF4 (root = `0x4018a97a()`); active track = `0x40046bfe(root + 48)`; voice settings kit + 17092,
  5 bytes per track (+2 bit 7 = unison OFF, low bits amount − 1, +3 spread). Layering masks: kit data (vtable[52]) +
  0x5CE0 + 2t, setter `0x40031620`.

## P-locks (pattern lock table = model vtable[40]: 80 records × 258 B `{id, track, u16[128]}`)
Index byte per (track, slot) at table + 20640 + 101·track + slot → every lock routine refuses or clamps slot > 100:
recorder `0x4003ceee(model, track, step, slot, value)`, reader `0x4003cdba`, remover `0x4003d00e`, step clear `0x4003d14e`,
recount (the trig's lock marker) `0x4003ce3c`. Held-trig turn `0x40037cb6` → `0x4004f8fe` → recorder; held-trig knob
click `0x40037d22` → `0x4004f944` → remover; live record `0x4003f1ae`. Model "changed" = vtable[16](model, 0).
Playback: note preparer `0x400d8654` → lock-list builder `0x400d85e8(track, list, table, ., step, step, 0)` (list at note
record +84) → applier `0x400db092(VOICE, list, 0x80003af0)` from the note path `0x40026c20` and the trigless loop
`0x4002664e`. The builder runs AFTER the note: a lock placed now plays from the step's next pass. A note resets locks;
trigless locks persist until the next note; a knob turn ends that knob's playing lock. Pattern save `0x400de75e` / load
`0x400dccc0` + `0x400de5e6` (ids < 128). Your own lock records: a header id no stock slot uses, and make the copy / paste /
recount / clear routines skip or handle them (dnfw's arp p-locks found every site).

## Trigs, patterns, sequencer
- Edit context of the active track: `ctx = 0x4003efde(0x4018a97a())` = P + 70 + 132 t (P = app + 17428 + 2388 pattern);
  ctx +60 = track, +48 = the pattern-level object. `vtable[40](ctx)` = step words (u16 per step: note trig 0x0381, lock
  trig 0x7801; bit 0 = a trig), TRIG PAGE defaults at +0x480.. (+0x481 LEN, +0x485 NOTE).
- Place a trig as the grid does: `0x40054690(ctx, step, mask 1, set 1)`. A trig's NOTE / VEL / LEN are 6-byte records
  `0x400350b6(*(ctx + 48), track, step, vector of {NOTE, VEL, LEN, 0})` (0xff = not locked). LEN table `0x402a0f48`:
  14 = one step, 78 = 16 steps, 127 = INF.
- **Clock:** audio ISR `0x40027a62`; internal clock: d0 = tempo word (BPM × 120 = half-units a block) loaded at
  **`0x40027ac0`**; "now" `0x80005398` += 2·d0, countdown `0x800053a0` −= 2·d0; at ≤ 0 it forces the tick ISR `0x400d987a`
  (one per block at most). 96 ppqn subtick = 225,000 units; 24 ppqn = 900,000 (MIDI clock out). Counters: `0x42c5a9d0` =
  16ths since start, `0x42c5a9d4` = 24 ppqn tick 0..5 in the 16th (both flip one tick before the step plays). Arp ticks are
  90,000 units (240 / quarter). Warping the step at 0x40027ac0 moves every note, micro timing, retrig, length and arp tick.
- Play state: (u32 `0x42c5a994` | `0x42c5a918`) == 1. Playhead (dnfw docs): `0x446483d0 + t` current step of track t.
- Swing: sequencer getter `0x400d7cd0` (pattern-level struct D = pattern + 0x1dd40: +18 tempo, +20 length, +24 swing,
  +26 scale; playing pattern `*0x42c5a9a0`, its lock table at +97872).
- **Arp:** step `0x4002a0bc(track, id)` → note / −1 rest; state `0x405984a8 + 40 t`; per-track arp clock counter
  `0x4058e918[t]` (from 2880 down, a step when bit SPD of `0x401d5bd4[counter]` is set). ARP menu: vtable `0x401d4870`
  (key `0x40019716`, draw `0x40018818`, encoder `0x40018e2a`; F / G unused). [ARPEGGIATOR] reaches the menu as key 51.
- KEYBOARD SETUP: pattern P = `0x4003efa8(app)`; keyboard object P + 44 (+29 SCALE, +31 ROOT); scale intervals
  `0x4020dea0 + 32 s` (8 ints, −1 ends), names `0x402a0e28`.
- MACHINE menu = [FUNC] + [SYN]; machine list `{0,2,1,3,4}` at `0x401ddd58`; descriptor dispatch `0x400c248e` (type ≤ 4,
  RAM table `0x42432b24 + 44 t`); per-type accessors all bounded `type <= 4` cluster at `0x400dc144..0x400dc376`; machine
  setter `0x4004cc08`. A new machine = re-enter every accessor (digikit `tools/machinepatch.py` lists the DT2 pattern).

## Voices and the audio ISR
- Audio ISR `0x40025e36` (one per DSP block): tx hook site `0x40025e82` → driver sends the frame built LAST ISR → notes /
  locks `0x40026xxx` → mirror smoother `0x4002717e` (EMAC one-pole `0x400db160` into mirror `0x800068e4`) → LFO tick
  `0x400272d4` (evaluator `0x40137a8e` bound / `0x40137a9e`, mirror `0x80006906 + 202·voice`) → frame build. The ISR tail
  forces INTC0 source 44 (vector 108 = sequencer tick `0x400d7864`), which forces 57 (vector 121 = step processing).
- Voice → track: note record (fp−36) +16; `0x8000dda0[voice]`; sound on voice v = `0x800052A8[v]`.
- **Audio ISR budget [HW]:** period ~0.67 ms. Stock already takes ~64 % of each block while playing; the UI gets what the
  ISR leaves (a laggy UI = your ISR work). An ISR that runs past its period sends its frame late; a frame later than its
  render is dropped with its note-ons (silent trigs, worse with more tracks on one step). Our code costs ~4x more cycles
  per instruction than stock's: DDR data (0x46xxxxxx) and cache misses; SRAM `0x8000xxxx` is cheap. Rules: skip what
  nothing uses, never call per-voice / per-track helpers in loops, defer per-note work off note blocks, count DDR accesses
  not instructions. The DSP reports its own cycles per block (reply word 0) — show it on screen to measure.
- C in the ISR: keep it short and non-blocking; no allocation.

## UI
- Panel keys: event code (event+12) → switch @`0x40072bf0`; key-held test `0x4011f7e4(code)` = bitmap `0x445a1e30` bit
  (code−1). RECORD 19, PLAY 20, STOP 21, FUNC 17, YES 10, LEFT 13, RIGHT 15, UP 11, DOWN 14, [MOD] 6. Key events: +16 flags
  (1 down, 2 [FUNC] held at the press, 8 repeat, 0x10 up). Encoder events: +12 knob 1..8 (9 = LEVEL), +16 detents,
  +20 pushed; stock steps a value via `0x4011336e(view + k, idx, 0x4011313e(ev, 1, k), table 0x44507dec, pushed)`.
- Panel input path: a separate MCU on UART8 → eDMA ring → RX callback `0x4011fcdc` (in the interrupt): tag 3 = encoder
  (`0x4011fc2c(ch, d)`), tag 2 = buttons.
- **Redraw:** UI loop `0x4002e464` tests screen +32; pass `0x4011d32a` draws each layer (vtable[16]); request a redraw with
  `0x4011c7ba(widget)`. A UI path of yours that changes a page must leave that request set like the stock key it imitates
  (else ~1 s on HW; the emulator never shows the latency).
- **Pixels:** the frame being drawn = `*(u8 **)0x402a0b88` (1024 B: byte = page + 8·column, bit n = row 8·(7 − page) + n);
  the driver `0x40131cf2` sends the bytes that differ from `*0x402a0b8c`, then swaps the two (the Screenshot RPC reads
  0x8c). Hook the end of the screen pass (`0x4011d398`, 6 bytes, d1/a0/a1 dead) to draw over everything. **The panel can
  be sent a frame mid-draw: never draw the stock screen and then fix it up** (the stock version flashes) — patch the stock
  draw's coordinates / strings, or draw it all yourself. Text: `0x40115668(ctx, font, x centre, y, 1, 0, width template,
  format, ...)`, font object `0x40115d04(0x44507ed8)`; sprite `0x401157fc(ctx, sprite, x, y, 0)` ORs (not for big areas).
- **Knob drawing** (sound pages, class vtable `0x401e20b4`): value = `0x4006538e(page, entry, &locked)`, drawer
  `0x4006448a(page, ctx, x, y, entry, value, ...)` → widget vtable[96] → picture. Companion row per entry at
  `0x4243325c + 68·entry` (321 rows; accessor bound `0x400c2418`): 17 words = 3 function objects (picture words 5–8,
  value fetch 9–16). The plain dial is a sprite sheet (invoker `0x400c0822`, frame 12 + (v·72 + 0x3f80)/0x7f00 of
  `0x4464580c`). Row 305 steps one value per detent; row 52 accelerates. Don't borrow LFO DEST's row 77 (it opens the
  DEST browser).
- **Pages:** mode registrations `0x40061300..` (count, mode, list): mode 5 = [FX] list `0x401e0054` {16}; page record +0
  page name, +4 MODE name, +8.. 8 entries, +40 flags. [MOD] mode object: vtable +128 = set-index `0x40063ade` (opens the
  page, does NOT request a redraw).
- DEST list: built on MOD page open `0x4003951e` (walk bound `0x400395b8`, rank table `0x4028bfc4`, group names
  `0x401f76f4 + 4(g−5)`); stepping `0x400399e0(owner, value, delta << 8, mask 0x1e00)`; DEST menu gate `0x40039a9a`.
- SETTINGS menu: SystemSettings ctor `0x4010b96e`, items vector at +4 / +8; a value item from
  `0x401170d8(obj, name, f2, drawer, edit, -1, 8)`, appended with `0x4011690e(menu, item)` (MASTER TUNE is the model).
- TEMPO menu: pattern view vtable `0x401f0054` (key `0x400b4e4e`, draw `0x400b463c`, encoders `0x400b480c` via the thunk
  `0x400b4bbe`), song view `0x401ed844`. Opened by MainScreenView on [TEMPO] RELEASE (`0x4005f2e6`); [FUNC] + [TEMPO]
  press = tap tempo (factory `0x4018eb4a`). Panel codes: [SETTINGS] = (0, 7) → code 8; code = channel × 8 + bit + 1.
- Find gestures in the manual first (e.g. "press and hold a [PARAMETER] key" shows every knob's value as a number).
