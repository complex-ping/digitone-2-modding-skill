# SPDX-License-Identifier: GPL-2.0  (native re-implementation of parts of digikit, github.com/m-dwyer/digikit)
"""Faster digikit ColdFire runs for the harnesses (cfkit, dn2capture).

Measured 2026-09-27 on the stock boot400M snapshot, 20-30M instructions (cProfile + wall clock):
  * exact `spin` (Unicorn `count=`)          ~1.0-1.2 M instr/s
  * digikit `spin(fast=True)`                 ~2.0 M instr/s; 5.4M Python block-hook calls per 21M instructions
                                                 and ~1M Python setPixel/getPixel HLE calls were most of the time.
What this module changes (nothing in the digikit checkout is edited):
  1. `install()` swaps digikit's Python block stepper for the same stepper in C (fastblock.c, built here with the
     system clang, registered through uc_hook_add): identical semantics -- budget in block entries, 3.87
     instructions per entry, stop at the first block after the budget -- minus the Python call per block.
  2. `setup()` = emucheck.setup + the native stepper. (Tried and rejected: dropping the Python bitmap/soft-float
     HLEs -- faster, but the screen is never drawn / the UI never comes up.)
  3. `run_until(m, pc, until, limit)` replaces `emu_start(..., count=N)` for "run this vector to its return", which
     paid the counted-mode tax for the whole frame.
Like digikit's fast mode, the instruction clock is an estimate (timers can fire up to one basic block late), so
runs are not bit-identical to exact runs. Use exact mode for determinism claims, this for functional tests.
"""
import ctypes
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dn2paths as P  # noqa: E402
DIGIKIT = str(P.DIGIKIT)
sys.path.insert(0, DIGIKIT); sys.path.insert(0, os.path.join(DIGIKIT, 'tools'))

from unicorn import UC_HOOK_BLOCK, UC_HOOK_CODE  # noqa: E402
from unicorn.unicorn_py3 import unicorn as _U  # noqa: E402

PER_BLOCK = 3.87                   # digikit's measured instructions per block entry (longrun._FastStepper)


class _State(ctypes.Structure):
    _fields_ = [('left', ctypes.c_int64), ('blocks', ctypes.c_uint64), ('total', ctypes.c_uint64),
                ('stop', ctypes.c_void_p), ('reg_read', ctypes.c_void_p), ('sp_reg', ctypes.c_int32),
                ('sp_above', ctypes.c_uint32), ('sp_stops', ctypes.c_uint64), ('idle_hit', ctypes.c_uint32),
                ('pad', ctypes.c_uint32), ('idle_stops', ctypes.c_uint64)]


def _lib():
    src, out = os.path.join(HERE, 'fastblock.c'), os.path.join(HERE, 'fastblock.dylib')
    if not os.path.exists(out) or os.path.getmtime(out) < os.path.getmtime(src):
        subprocess.run(['clang', '-O2', '-shared', '-fPIC', '-o', out, src], check=True)
    return ctypes.CDLL(out)


class NativeStepper:
    """digikit longrun._FastStepper with the block hook in C. Same interface: run(pc, step) -> instructions."""

    def __init__(self, m, per_block=PER_BLOCK):
        self.m, self.per_block, self.steps = m, per_block, 0
        self.lib = _lib()
        self.st = _State()
        self.st.stop = ctypes.cast(_U.uclib.uc_emu_stop, ctypes.c_void_p).value
        self.st.reg_read = ctypes.cast(_U.uclib.uc_reg_read, ctypes.c_void_p).value
        from unicorn.m68k_const import UC_M68K_REG_A7
        self.st.sp_reg = UC_M68K_REG_A7
        handle = _U.uc_hook_h()
        err = _U.uclib.uc_hook_add(m.uc._uch, ctypes.byref(handle), UC_HOOK_BLOCK,
                                   ctypes.cast(self.lib.fast_block, ctypes.c_void_p),
                                   ctypes.cast(ctypes.pointer(self.st), ctypes.c_void_p),
                                   ctypes.c_uint64(1), ctypes.c_uint64(0))
        if err:
            raise RuntimeError(f'uc_hook_add: {err}')
        self.handle = handle

    @property
    def blocks(self):
        return self.st.blocks

    @property
    def left(self):
        return self.st.left

    idle = None                    # IdleSkip, when setup() installed it

    def run(self, pc, step):
        self.steps += 1
        self.st.blocks = 0
        self.st.left = max(1, int(step / self.per_block))
        self.st.idle_hit = 0
        self.m.uc.emu_start(pc, 0)
        ran = max(1, int(self.st.blocks * self.per_block))
        if self.st.idle_hit and self.idle is not None:
            self.idle.passed(step - ran)
            return max(ran, step)                   # the rest of the step passed idle
        return ran


class IdleSkip:
    """digikit's do_halt, native: a spin at an idle `bra *` stops the run (fast_idle) and the rest of the step counts
    as idle. What do_halt did per spin is done per stop: eDMA TX completions delivered, vector 32 (reschedule) raised
    once every `idle_yield` idle instructions (one spin = one instruction)."""

    def __init__(self, m, stepper_, addrs, tx, idle_yield=20000):
        self.m, self.tx, self.idle_yield, self.spins = m, tx, idle_yield, 0
        self.handles = []
        for a in addrs:
            h = _U.uc_hook_h()
            err = _U.uclib.uc_hook_add(m.uc._uch, ctypes.byref(h), UC_HOOK_CODE,
                                       ctypes.cast(stepper_.lib.fast_idle, ctypes.c_void_p),
                                       ctypes.cast(ctypes.pointer(stepper_.st), ctypes.c_void_p),
                                       ctypes.c_uint64(a), ctypes.c_uint64(a))
            if err:
                raise RuntimeError(f'uc_hook_add idle {a:#x}: {err}')
            self.handles.append(h)
        stepper_.idle = self

    def passed(self, n):
        before = self.spins // self.idle_yield
        self.spins += max(1, n)
        if self.tx is not None:
            self.tx.deliver()
        if self.spins // self.idle_yield != before:
            self.m.raise_vector(32)


class _Pixels(ctypes.Structure):
    _fields_ = [('reg_read', ctypes.c_void_p), ('reg_write', ctypes.c_void_p), ('mem_read', ctypes.c_void_p),
                ('mem_write', ctypes.c_void_p), ('r_a7', ctypes.c_int32), ('r_d0', ctypes.c_int32),
                ('r_pc', ctypes.c_int32), ('pad', ctypes.c_int32), ('sets', ctypes.c_uint64), ('gets', ctypes.c_uint64)]


class NativePixels:
    """digikit emu/hle.py's setPixel / getPixel HLE in C (fastblock.c): the main screen in ~1/3 of the time."""

    def __init__(self, m, stepper_, set_pixel, get_pixel):
        from unicorn.m68k_const import UC_M68K_REG_A7, UC_M68K_REG_D0, UC_M68K_REG_PC
        L = _U.uclib
        self.st = _Pixels(*(ctypes.cast(f, ctypes.c_void_p).value for f in (L.uc_reg_read, L.uc_reg_write,
                                                                            L.uc_mem_read, L.uc_mem_write)),
                          UC_M68K_REG_A7, UC_M68K_REG_D0, UC_M68K_REG_PC, 0, 0, 0)
        self.handles = []
        for addr, fn in ((set_pixel, stepper_.lib.fast_set_pixel), (get_pixel, stepper_.lib.fast_get_pixel)):
            h = _U.uc_hook_h()
            err = L.uc_hook_add(m.uc._uch, ctypes.byref(h), UC_HOOK_CODE, ctypes.cast(fn, ctypes.c_void_p),
                                ctypes.cast(ctypes.pointer(self.st), ctypes.c_void_p),
                                ctypes.c_uint64(addr), ctypes.c_uint64(addr))
            if err:
                raise RuntimeError(f'uc_hook_add pixel {addr:#x}: {err}')
            self.handles.append(h)
        m._native_pixels = self


def stepper(m):
    s = getattr(m, '_fast_stepper_obj', None)
    if not isinstance(s, NativeStepper):
        s = NativeStepper(m)
        m._fast_stepper_obj = s
    return s


def install():
    """Make every `spin(..., fast=True)` in this process use the native stepper."""
    import emu.longrun as L
    L._fast_stepper = stepper


def run_isr(m, vector, limit=5_000_000):
    """Raise `vector` at the current PC and run its handler until it has returned (SP back at the interrupted
    context's, native check) or ~`limit` instructions. -> (instructions, returned). The CPU is left at the first
    block after the return -- the interrupted code, or the task the handler switched to."""
    from unicorn.m68k_const import UC_M68K_REG_A7, UC_M68K_REG_PC
    uc = m.uc
    s = stepper(m)
    sp = uc.reg_read(UC_M68K_REG_A7)
    m.halt_vec = None
    if not m.raise_vector(vector):
        raise RuntimeError(f'vector {vector}: no handler or masked')
    stops = s.st.sp_stops
    s.st.blocks = 0
    s.st.left = max(1, int(limit / s.per_block))
    s.st.sp_above = sp
    uc.emu_start(uc.reg_read(UC_M68K_REG_PC), 0)
    s.st.sp_above = 0
    return int(s.st.blocks * s.per_block), s.st.sp_stops != stops


def run_until(m, pc, until, limit):
    """emu_start(pc, until) bounded by ~`limit` instructions without count= (native block budget)."""
    s = stepper(m)
    s.st.blocks = 0
    s.st.left = max(1, int(limit / s.per_block))
    m.uc.emu_start(pc, until)
    return int(s.st.blocks * s.per_block)


def setup(snapshot, syx, hle=True, bitmap=None, softfloat=None, idle_skip=True):
    """emucheck.setup + the native stepper. -> same 8-tuple.
    Keep both HLEs on (default). Measured: without soft-float the UI never comes up; without the bitmap HLE the
    machine runs 5x faster but the main screen is never drawn (checked to 1.1G instructions from reset)."""
    from emu import config, symbols
    from emu.dtim import Dtims, Timers
    from emu.longrun import build
    from emu.pit import Pits, intro_running
    import emu.dspboot as db
    import emu.hle as hle_mod
    profile = symbols.resolve(open(config.main_image(), 'rb').read())
    spins, find = [], db.find_idle_spins
    db.find_idle_spins = lambda image, load: spins.extend(find(image, load)) or []   # digikit's Python do_halt: off
    pixel, inst_bitmap = {}, hle_mod.install_bitmap

    def bitmap_native(at, stats=None, on_pixel=None, set_pixel=None, get_pixel=None):
        if on_pixel is not None:                 # a caller watching pixels keeps the Python HLE
            return inst_bitmap(at, stats, on_pixel, set_pixel=set_pixel, get_pixel=get_pixel)
        pixel.update(set=set_pixel, get=get_pixel)
    hle_mod.install_bitmap = bitmap_native
    try:
        from emu.snapshot import _load_blob
        timed = 'timers' in _load_blob(snapshot).get('components', {})   # a mid-run snapshot (m5seq.main_screen)
        m, ev, st, pc, inq, at = build(snapshot, syx=syx, unblock=True,
                                       softfloat=hle if softfloat is None else softfloat,
                                       bitmap=hle if bitmap is None else bitmap,
                                       dsp=True, slc=True, sdgate=True, esdhc=True,
                                       deferred_components=('timers',) if timed else ())
    finally:
        db.find_idle_spins = find
        hle_mod.install_bitmap = inst_bitmap
    if pixel:
        NativePixels(m, stepper(m), pixel['set'], pixel['get'])
    if idle_skip:                      # else the idle spins run natively, with no reschedule / TX delivery
        IdleSkip(m, stepper(m), spins, ev.get('edma_tx'))
    if timed:                          # the saved timer cadence, armed DMA timers intact (digikit's protocol)
        pits = ev['restore_checkpoint_timers']()
    else:
        intro = intro_running(m, profile.intro_pit3_isr)
        pits = Timers(Pits(m, hold=intro), Dtims(m, channels=(3,), hold=intro))
        if intro and profile.intro_done is not None:
            at(profile.intro_done, lambda uc, a, s, d: pits.release())
        ev['checkpoint_components']['timers'] = pits
    install()
    speed_options(m.uc)
    return m, ev, st, pc, inq, at, pits, profile


def speed_options(uc):
    """2026-10-04: with digiemu's patched Unicorn (work/repos/digikit/.venv-fast; irpina/digiemu patches/README.md) turn on
    NO_MEM_EXIT (2): no exit-request check after each guest load / store. Safe here: no memory hook of ours or digikit's stops
    the emulation (they stop from interrupt / block hooks). A stock library has no such export: nothing happens."""
    f = getattr(_U.uclib, 'uc_digikit_set_options', None)
    if f is None:
        return 0
    f.restype, f.argtypes = ctypes.c_int, [ctypes.c_void_p, ctypes.c_uint32]
    if f(uc._uch, 2):
        raise RuntimeError('uc_digikit_set_options failed')
    return 2


MAIN_LOAD = 0x40000400
EARLY = os.path.join(DIGIKIT, 'out/snapshots/dn2-1.11-early/boot0M.snap')   # stock, 500 instructions after reset


def boot(image, syx, instrs=1_100_000_000, early=EARLY, log=print, until_screen=True):
    """Cold boot of a MAIN OS image without digikit's counted boot (~6 min -> well under 1 min).

    Restores a stock snapshot taken 500 instructions after reset -- before the startup calls at 0x4000053e, where a
    dnfw loader hooks in (it runs at ~1,359) -- overwrites the MAIN OS in RAM with `image` (a build's section 3,
    appended area included) and runs on in fast mode. Equivalent to booting the build as long as the first 500
    instructions (reset code, identical in every build here) did not read the MAIN OS image; the loader's own
    effects (page list, code in RAM) are checked by the tests. -> the setup 8-tuple, pc at the end of the boot.
    With `until_screen`, stops early once the main screen is drawn (after at least 300M instructions).
    """
    import time
    import emucheck
    from emu import panel
    from emu.longrun import spin
    t0 = time.time()
    m, ev, st, pc, inq, at, pits, profile = setup(early, syx)
    m.uc.mem_write(MAIN_LOAD, bytes(image))
    done = 0
    while done < instrs:
        pc, ran, stop = spin(m, pc, 25_000_000, pits=pits, fast=True)
        done += ran
        if stop != 'limit':
            log(f'boot: spin stopped at {done / 1e6:.0f}M: {stop}')
            break
        if until_screen and done >= 300_000_000:
            buf = panel.read(m, profile.fb_front)
            if buf and emucheck.frame_on_fraction(buf) >= emucheck.MAIN_SCREEN_ON_FRACTION:
                break
    log(f'boot: {done / 1e6:.0f}M instructions in {time.time() - t0:.1f}s')
    return m, ev, st, pc, inq, at, pits, profile



class _BootDone(Exception):
    pass


def cold_boot(image, syx, out_snap, instrs=400_000_000, log=print):
    """Stage 1 of a build's boot: digikit's cold-boot machine (emu.dspboot.run: flash HLE, SD/GPIO models, idle-spin
    scheduler ticks, task hooks) minus its global per-instruction coverage hook and counted emu_start, run in native
    block-bounded steps from dspboot's pre_start to `instrs`, saved as a snapshot, then dspboot.run is abandoned
    before it would restart from reset. Same rung as `emu.checkpoint make 400000000` (which took ~4 min).
    The cold-boot machine has no timer model, so the main screen never appears in this stage (checked to 1.5G):
    stage 2 is `to_screen()` from the snapshot, as digikit's own ladder resumes do."""
    import time
    import emu.dspboot as db
    from emu import symbols
    from emu.snapshot import save
    from unicorn.m68k_const import UC_M68K_REG_PC
    from unicorn.unicorn_py3.unicorn import Uc
    profile = symbols.resolve(bytes(image))
    orig = Uc.hook_add
    result = {}

    def hook_add(self, htype, callback, *a, **k):
        if getattr(callback, '__name__', '') == 'cover':       # dspboot's global counting/coverage hook
            return 0
        return orig(self, htype, callback, *a, **k)

    def pre_start(m):
        t0 = time.time()
        s = stepper(m)
        pc, done = profile.entry, 0
        while done < instrs:
            done += s.run(pc, min(25_000_000, instrs - done))
            pc = m.uc.reg_read(UC_M68K_REG_PC)
        info = save(m, out_snap, extra={'n': done, 'seen': [], 'tasks': {}, 'fastemu': True})
        log(f'cold boot: ~{done / 1e6:.0f}M in {time.time() - t0:.1f}s -> {out_snap} ({info["bytes_on_disk"]} B)')
        result['n'] = done
        raise _BootDone()

    Uc.hook_add = hook_add
    try:
        db.run(syx, bytes(image), limit=instrs, fast=True, verbose=False, pre_start=pre_start)
    except _BootDone:
        pass
    finally:
        Uc.hook_add = orig
    return result['n']


def to_screen(snapshot, syx, max_instrs=800_000_000, settle=20_000_000, log=print):
    """Stage 2: resume (setup(): timers + native stepper) and run until the main screen is drawn, + `settle`.
    -> the setup 8-tuple with pc at that point."""
    import time
    import emucheck
    from emu import panel
    from emu.longrun import spin
    t0 = time.time()
    m, ev, st, pc, inq, at, pits, profile = setup(snapshot, syx)
    done, seen = 0, None
    while done < max_instrs:
        pc, ran, stop = spin(m, pc, 10_000_000, pits=pits, fast=True)
        done += ran
        buf = panel.read(m, profile.fb_front)
        if seen is None and buf and emucheck.frame_on_fraction(buf) >= emucheck.MAIN_SCREEN_ON_FRACTION:
            seen = done
            log(f'resume: main screen at +{done / 1e6:.0f}M, {time.time() - t0:.1f}s')
        if seen is not None and done - seen >= settle:
            return m, ev, st, pc, inq, at, pits, profile
    raise RuntimeError(f'resume: no main screen after +{done / 1e6:.0f}M')
