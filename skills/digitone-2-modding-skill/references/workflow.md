# How to work: the loop, the rules, the spec

## The loop
1. **Spec first.** Ask the questions below in one round before building. A wrong guess costs a whole build + flash.
2. **Find it in stock.** `cfx.sh` / `visa.py`, the manual for the gesture, dnfw's notes and `dn2_111.h`. Read the routine
   you hook end to end; note every register and flag the code after your site reads.
3. **Write the test first**, in the emulator, with a **stock control in the same run** (a stock knob / key doing the same
   gesture: proves the gesture really happened). For a bug: reproduce it as a failing test on the faulty build, then fix,
   then see it pass.
4. **Build** (`cfmod` / `dspblocks`), run the static checks (`bootblocks`, `encodecheck`, `shapecheck`, `hwrules`,
   `sasm.lint`), run every test, `dnfw inspect` (21 ok), check the flash size.
5. **Flash one clearly named image** (`build/DN2_1.11_<NAME>_<N>.syx`, never overwrite an older one; keep your last
   hardware-good image and the stock one) with its sha256, what changed, and exactly what to try on the unit.
6. **Record** what the unit did (a log per build: what to test, the verdict) so the next session starts from facts.

## Rules (each one cost a hardware round trip)
- **Emulator before every flash.** Both emulators exist so that the unit only judges sound and feel.
- **Test what the hardware consumes:** the frames the ColdFire sends, the DSP's output — never a read-back of what your
  patch wrote. Drive the real path (sequencer, several tracks, a track other than 1: voices ≠ tracks) and the extremes
  (hard pan, 0 and 127, feedback max, every machine, unison).
- **Stock routines refuse new slots** (`slot <= 100` everywhere): when a gesture works on stock pages but not on yours,
  find the stock routine and its bound.
- **Stock state you read must hold for every variant:** a per-voice DSP word found on one machine / filter type must be
  checked on all of them.
- **SHARC: caves only in dead functions or new blocks; obey the hardware rules (sharc.md); justify any instruction shape
  stock never uses from the PRM.** Don't announce a root cause as certain until the hardware agrees.
- **After 2 failed flashes on one problem, stop:** restate the goal, what is proven, the unknown, and the options.
- **Keep levels honest:** measure worst cases, report them, no unasked gain compensation; a send-FX return is wet only;
  clamp every audio-writing build; never read memory you did not write.
- **The emulator counts instructions; the unit pays for memory.** Profile L2 / DDR accesses; when the unit's load and the
  emulator disagree, measure on the unit before optimising blind.
- **A laggy UI is the ColdFire's audio ISR:** keep per-block work small, skip what nothing uses, defer per-note work.
- **Check upstream first** (dnfw `git pull`): its notes may already have your plumbing.
- **Never overwrite your stock image**; downloads of third-party code: know where it came from.

## A silent unit (UI works, no sound from power-on)
The DSP never ran a block. Compare the builds for what the emulator cannot model first — block lengths / alignment
(`bootblocks.py`), encodings (`encodecheck.py`), uninitialised DDR, sizes (section 7 < 1 MiB, L2 above 0x200B0000) — that
finds it without probe flashes. Then split images (one build's ColdFire + the other's section 7: one flash says which side)
and bisect that side. Silent from a certain gesture on, until a restart: a NaN in a feedback state (DDR garbage, a
divide), or a clobbered register at a hook (diff registers at the callee).

## Spec questions (one round, 2–4 options each, your recommendation first)
| # | question |
|---|---|
| 1 | **Reference:** which sound / behaviour is "it" (another Elektron box, a plugin, a paper)? 1:1 port or inspired-by? |
| 2 | **Where:** a new machine, an insert on a page, a send / bus effect, a global screen, a key combo? Which keys? |
| 3 | **Knobs:** which parameters, order, names (≤ 4 chars), ranges, defaults; which encoder does what on a menu page |
| 4 | **Level:** unity (no compensation) or matched to the reference? |
| 5 | **Routing / stereo:** per voice or per track? before the filter (mono) or after pan (stereo)? sends get dry or wet? |
| 6 | **Modulation:** restarts per note or free-running? lockable per trig? an LFO destination? |
| 7 | **Storage:** per sound, per pattern, per project, global? kept by SAVE PROJECT and a power cycle? |
| 8 | **CPU:** acceptable worst case (all 16 voices / tracks)? If it does not fit: fewer voices, half rate, a cheaper variant? |
| 9 | **Done =** what will be tried on the unit, and what "right" means (A/B against the reference, by ear, a measurement) |
Write the answers next to the build's entry in your log before building.
