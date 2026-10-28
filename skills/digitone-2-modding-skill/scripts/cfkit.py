"""cfkit: drive the real Digitone II OS 1.11 in the ColdFire emulator (digikit + fastemu) from Python.

Run with digikit's venv (patched Unicorn, see the skill's references/emulators.md):
    $DN2_WORKSPACE/repos/digikit/.venv-fast/bin/python my_test.py

    from cfkit import Kit, Harness, PLAY, STOP, RECORD, TRIG
    k = Kit('build/MY/section_3.bin', STOCK_SYX, 'build/MY/symbols.txt', 'out/')   # boots once per image, then cached
    k.tap('FX'); k.h.png('fx.png')             # panel keys by name (KEYS) or (channel, bit); screenshots
    k.h.turn(enc, +1, 11)                      # encoder bursts (knob pushed while turning; unheld: hold=False)
    k.call(0x400DBCC4, [36])                   # run a firmware (or your C) function, return d0 (registers restored)
    k.select(1, track=0)                       # MACHINE menu: 0 FM TONE, 1 WAVETONE, 2 FM DRUM, 3 SWARMER
    k.sound(t), k.mach(t), k.slot(t, s)        # live sound of track t, its machine byte, its value slot s (u16)
    frames = k.play(n, bpm=300)                # PLAY, n audio blocks (the SPI frames the OS sent), STOP
    k.notes(frames)                            # [(frame index, voice)] of track 0's notes
    r = k.ring(0x4006448A, 8); r.calls()       # record every call of a function: return address + 7 stack args
    k.knobs()                                  # (entry, value) of every knob the sound pages drew
    k.hot(n) / k.watch_reads(addr)             # where the CPU sits / who reads an address
    h.frames(n, each=k.tail_writer(words))     # fake the DSP reply's unused tail before every frame

Gotchas (references/emulators.md has them all): h.run ignores Python code hooks -> use ring(); encoders register only in
bursts (11 detents); hold keys >= 12M instructions; the UI resets the tempo word, play() sets it right before PLAY.
"""
import argparse, collections, os, struct, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dn2paths as P  # noqa: E402
sys.path.insert(0, str(P.DIGIKIT)); sys.path.insert(0, str(P.DIGIKIT / 'tools'))
from unicorn import UC_HOOK_CODE, UC_HOOK_BLOCK, UC_HOOK_MEM_READ  # noqa: E402
from unicorn.m68k_const import (UC_M68K_REG_A2, UC_M68K_REG_A3, UC_M68K_REG_A7, UC_M68K_REG_D0,  # noqa: E402
                                UC_M68K_REG_D1, UC_M68K_REG_D2, UC_M68K_REG_PC)
import unicorn.m68k_const as mc  # noqa: E402
import emucheck  # noqa: E402
from emu import panel, panelin  # noqa: E402
from emu.longrun import spin  # noqa: E402
INTFRCH0 = 0xFC048010

VECTOR, DRIVER, COUNTER, GATE = 191, 0x400CF7BE, 0x402876F8, 0x4058E54C
TXLIST, BUILD, BUILT = 0x80002510, 0x400CF906, 0x400CF950
RECORD, PLAY, STOP = (2, 2), (2, 3), (2, 4)
INTFRCH0, SEQ_FORCE, SEQ_VECTOR = 0xFC048010, 0x1000, 108   # ISR tail 0x40027afc forces INTC0 source 44 = vector 108
                                                           # (0x400d7864, the sequencer tick); fastemu never delivers it
TRIG = lambda n: (3 + (n - 1) // 8, (n - 1) % 8)


def main_screen(image, syx, log=print):
    """The machine for a build's section_3.bin at its main screen. First use per image: cold boot (~10 s) + run to the
    main screen (~60 s, mostly the Python pixel HLE drawing the boot screens) and save it; later uses restore that
    snapshot (~1 s). Cache: <image dir>/emu/{boot400M,main}.snap, dropped when section_3.bin is newer.
    -> the setup 8-tuple + the snapshot path."""
    import fastemu
    from emu.snapshot import save
    from unicorn.m68k_const import UC_M68K_REG_PC as _PC
    cache = os.path.join(os.path.dirname(os.path.abspath(image)), 'emu')
    os.makedirs(cache, exist_ok=True)
    boot, main = os.path.join(cache, 'boot400M.snap'), os.path.join(cache, 'main.snap')
    for f in (boot, main):
        if os.path.exists(f) and os.path.getmtime(f) < os.path.getmtime(image):
            os.remove(f)
    import json
    import unicorn.m68k_const as K
    cregs = os.path.join(cache, 'main.cregs.json')          # digikit snapshots keep D/A/PC/SR only
    names = ('CR_USP', 'CR_ISP', 'CR_MSP', 'CR_VBR', 'CR_CACR')
    if os.path.exists(main) and os.path.exists(cregs):
        t = fastemu.setup(main, syx)
        for k, v in json.load(open(cregs)).items():
            t[0].uc.reg_write(getattr(K, 'UC_M68K_REG_' + k), v)
        return t + (main,)
    if not os.path.exists(boot):
        fastemu.cold_boot(open(image, 'rb').read(), syx, boot, log=log)
    t = fastemu.to_screen(boot, syx, log=log)
    m = t[0]
    m.uc.reg_write(_PC, t[3])
    save(m, main, extra={'n': 0, 'seen': [], 'tasks': {}, 'fastemu': True, 'main_screen': True},
         components=dict(t[1].get('checkpoint_components', {})))   # SD card / UART / eDMA models, timers
    json.dump({k: m.uc.reg_read(getattr(K, 'UC_M68K_REG_' + k)) for k in names}, open(cregs, 'w'))
    log(f'control registers: {open(cregs).read()}')
    log(f'saved {main}')
    return t + (main,)


class Harness:
    def __init__(self, snap, syx, symbols=None, out='emu_out', extra_hooks=()):
        self.sym = {l.split()[1]: int(l.split()[0], 16) for l in open(symbols) if l.strip()} if symbols else {}
        self.out = out
        os.makedirs(out, exist_ok=True)
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import fastemu
        self.fastemu = fastemu
        if snap.endswith('.snap'):
            self.m, ev, st, pc, inq, at, self.pits, self.profile = fastemu.setup(snap, syx)
        else:                                   # a build's section_3.bin: boot once per image, cached next to it
            self.m, ev, st, pc, inq, at, self.pits, self.profile, _ = main_screen(snap, syx)
        self.uc = self.m.uc
        self.pc = pc
        import cfemac
        cfemac.Emac(self.uc).install(cfemac.load_rows(str(P.DATA / 'emac_rows_1.11.txt')))   # (dn2setup.py makes it)
        self.pend, self.sent, self.hooks = {}, [], []
        self.seq, self.forced_n, self.isr_open = True, {}, 0
        # The panel TX ring is never drained in this emulator: after ~4 KB of LED traffic every LED write spins for ever at
        # 0x4000221c (hit at random by long UI tests). NOP the "no room yet" branch for every run.
        if self.r(0x40002256, 2) == b'\x65\xc4':
            self.w(0x40002256, b'\x4e\x71'); self.uc.ctl_flush_tb()

    r = lambda s, va, n: bytes(s.uc.mem_read(va, n))
    w = lambda s, va, b: s.uc.mem_write(va, bytes(b))
    u32 = lambda s, va: struct.unpack('>I', s.r(va, 4))[0]
    u16 = lambda s, va: struct.unpack('>H', s.r(va, 2))[0]

    def run(self, n):
        self.pc, done, stop = spin(self.m, self.pc, n, pits=self.pits, fast=True)

    def png(self, name):
        buf = panel.read(self.m, self.profile.fb_front)
        if buf is not None:
            panel.write_png(buf, os.path.join(self.out, name))
        return buf

    def wait_main(self):
        buf = panel.read(self.m, self.profile.fb_front)
        if buf and emucheck.frame_on_fraction(buf) >= emucheck.MAIN_SCREEN_ON_FRACTION:
            return True
        for _ in range(40):
            self.run(15_000_000)
            buf = panel.read(self.m, self.profile.fb_front)
            if buf and emucheck.frame_on_fraction(buf) >= emucheck.MAIN_SCREEN_ON_FRACTION:
                return True
        return False

    def press(self, key, n=3_000_000):
        self.pc = panelin.press(self.m, self.profile, key[0], key[1]); self.run(n)

    def release(self, key, n=10_000_000):
        self.pc = panelin.release(self.m, self.profile, key[0], key[1]); self.run(n)

    def tap(self, key):
        self.press(key, 500_000); self.release(key, 15_000_000)

    def turn(self, enc, delta, times, hold=True):
        if hold:
            self.press((5, enc))
        for _ in range(times):
            self.pc = panelin.encoder(self.m, self.profile, enc, delta); self.run(3_000_000)
        if hold:
            self.release((5, enc))

    def turn_to(self, enc, read, want, burst=11, tries=16):
        """Turn encoder `enc` (not held) until read() == want; True when reached. Under this emulator's timing the UI's
        encoder accumulator steps a small-range knob only after a big burst (TYPE, 4 values: one 11-detent message is
        about a third of a step, 12 in one message can be two) and a held turn is coarse (jumps to the end), so this
        sends unheld 11-detent messages and reads back after each, correcting direction on overshoot."""
        for _ in range(tries):
            cur = read()
            if cur == want:
                return True
            self.pc = panelin.encoder(self.m, self.profile, enc, burst if want > cur else -burst); self.run(10_000_000)
        return read() == want

    def _driver(self, uc_, addr, size, user):
        uc = self.uc
        sp = uc.reg_read(UC_M68K_REG_A7)
        ret, tx_len, tx, rx_len, rx = struct.unpack('>IIIII', self.r(sp, 20))
        n = self.u32(0x42440958)
        self.pend.update(sp=sp, ret=ret, n=min(n, 2800), tx=self.r(tx, tx_len),
                         regs=[uc.reg_read(x) for x in (UC_M68K_REG_D2, UC_M68K_REG_A2, UC_M68K_REG_A3)])
        uc.reg_write(UC_M68K_REG_D0, tx_len); uc.reg_write(UC_M68K_REG_D1, self.pend['n'])
        uc.reg_write(UC_M68K_REG_A2, tx_len); uc.reg_write(UC_M68K_REG_A3, tx)
        uc.reg_write(UC_M68K_REG_PC, BUILD)

    def _built(self, uc_, addr, size, user):
        uc, p = self.uc, self.pend
        k = p['n'] // 2
        lst = self.r(TXLIST, 4 * k)
        self.sent.append((b''.join(lst[4 * j + 2:4 * j + 4] for j in range(k)), p['tx']))
        for x, v in zip((UC_M68K_REG_D2, UC_M68K_REG_A2, UC_M68K_REG_A3), p['regs']):
            uc.reg_write(x, v)
        uc.reg_write(UC_M68K_REG_D0, 0); uc.reg_write(UC_M68K_REG_A7, p['sp'] + 4); uc.reg_write(UC_M68K_REG_PC, p['ret'])

    def frames(self, n, between=150_000, each=None):
        uc = self.uc
        if not self.hooks:
            self.hooks.append(uc.hook_add(UC_HOOK_CODE, self._driver, begin=DRIVER, end=DRIVER))
            self.hooks.append(uc.hook_add(UC_HOOK_CODE, self._built, begin=BUILT, end=BUILT))
        start = len(self.sent)
        for i in range(n):
            self.run(between)
            uc.mem_write(GATE, bytes(4)); uc.mem_write(COUNTER, bytes(4))
            self.nframe = getattr(self, 'nframe', 0) + 1
            uc.mem_write(0xFC045640, struct.pack('>I', 0x4E6E0100 if self.nframe & 1 else 0x4E6E0A00))
            uc.reg_write(UC_M68K_REG_PC, self.pc)
            n_, ok_ = self.fastemu.run_isr(self.m, VECTOR, 2_000_000)
            self.isr_open += not ok_
            self.pc = uc.reg_read(UC_M68K_REG_PC)
            if self.seq:
                self.forced()
            if each:
                each(i)
        return [x for x, _ in self.sent[start:]]          # each frame as the SPI DMA list sent it (2688 B + the padding)

    def forced(self):
        """Deliver the software interrupts the ISRs force on INTC0 (fastemu does not): the audio ISR's tail forces
        source 44 (vector 108, the sequencer tick 0x400d7864), which forces source 57 (vector 121) in turn."""
        uc = self.uc
        for _ in range(8):
            hi, lo = self.u32(INTFRCH0), self.u32(INTFRCH0 + 4)
            if not (hi | lo):
                return
            vec = (96 + hi.bit_length() - 1) if hi else (64 + lo.bit_length() - 1)
            self.forced_n[vec] = self.forced_n.get(vec, 0) + 1
            uc.reg_write(UC_M68K_REG_PC, self.pc)
            n_, ok_ = self.fastemu.run_isr(self.m, vec, 5_000_000)
            self.isr_open += not ok_
            self.pc = uc.reg_read(UC_M68K_REG_PC)
            if self.u32(INTFRCH0) == hi and self.u32(INTFRCH0 + 4) == lo:
                raise SystemExit(f'vector {vec} left its force bit set')

    def count(self, addrs):
        hits = collections.Counter()
        for a in addrs:
            self.uc.hook_add(UC_HOOK_CODE, lambda uc_, addr, size, user: hits.update([addr]), begin=a, end=a)
        return hits



KEYS = {'TRIG': (0, 0), 'SYN': (0, 1), 'FLTR': (0, 2), 'AMP': (0, 3), 'FX': (0, 4), 'LFO': (0, 5), 'FUNC': (2, 0),
        'KEYB': (2, 1), 'YES': (1, 1), 'UP': (1, 2), 'NO': (1, 3), 'LEFT': (1, 4), 'DOWN': (1, 5), 'RIGHT': (1, 6), 'TRK': (1, 7)}
REGS = [getattr(mc, f'UC_M68K_REG_{x}') for x in 'D0 D1 D2 D3 D4 D5 D6 D7 A0 A1 A2 A3 A4 A5 A6 A7 SR'.split()]
SPARE = 0x46A00000                       # unmapped in digikit: 2 MB for stacks, rings, scratch buffers
CALL_SP, CALL_RET = 0x46A80000, 0x46A8F000
RING0 = 0x46B00000                       # rings at RING0 + 0x10000 * n
KNOB_DRAWER = 0x4006448A                 # the sound pages' knob drawer (page, ctx, x, y, entry, value, ...)
BE = lambda v: struct.pack('>I', v & 0xFFFFFFFF)


class Ring:
    """Calls of `entry`: a jmp to a RAM trampoline that copies the return address + 7 stack words into a ring of n entries."""
    def __init__(self, k, entry, nbytes, slot, n=256, stack_off=0):
        self.k, self.slot, self.n = k, slot, n
        displaced = k.h.r(entry, nbytes)
        code = bytes.fromhex('2f082f092f00') + bytes.fromhex('2039') + BE(slot) + bytes.fromhex('0280') + BE(n - 1)
        code += bytes.fromhex('eb88') + bytes.fromhex('41f9') + BE(slot + 16) + bytes.fromhex('d1c0')
        code += bytes.fromhex('43ef') + struct.pack('>H', 12 + stack_off)
        code += bytes.fromhex('20d9') * 8 + bytes.fromhex('52b9') + BE(slot) + bytes.fromhex('201f225f205f')
        code += displaced + bytes.fromhex('4ef9') + BE(entry + nbytes)
        tr = slot + 16 + 32 * n
        k.h.w(slot, b'\0' * (16 + 32 * n))
        k.h.w(tr, code)
        k.h.w(entry, bytes.fromhex('4ef9') + BE(tr) + bytes.fromhex('4e71') * ((nbytes - 6) // 2))
        k.uc.ctl_remove_cache(entry, entry + nbytes)
        self.seen = 0

    def count(self):
        return self.k.h.u32(self.slot)

    def calls(self, since_last=True):
        c = self.count()
        start = max(self.seen if since_last else 0, c - self.n)
        out = [[self.k.h.u32(self.slot + 16 + 32 * (i % self.n) + 4 * j) for j in range(8)] for i in range(start, c)]
        self.seen = c
        return out


class Kit:
    def __init__(self, snap, syx, symbols, out):
        self.h = h = Harness(snap, syx, symbols, out)
        self.uc, self.sym = h.uc, h.sym
        print('main screen', h.wait_main(), flush=True)
        try:
            self.uc.mem_map(SPARE, 0x200000)
        except Exception:
            pass
        self.rings = 0
        self._knob_ring = None

    # -- firmware calls -------------------------------------------------------------------------------------------------
    def call(self, fn, args=()):
        uc, h = self.uc, self.h
        saved = [uc.reg_read(x) for x in REGS]
        h.w(CALL_SP, BE(CALL_RET) + b''.join(BE(v) for v in args))
        uc.reg_write(mc.UC_M68K_REG_A7, CALL_SP)
        h.fastemu.run_until(h.m, fn, CALL_RET, 5_000_000)
        d0 = uc.reg_read(mc.UC_M68K_REG_D0)
        for x, v in zip(REGS, saved):
            uc.reg_write(x, v)
        uc.reg_write(mc.UC_M68K_REG_PC, h.pc)
        return d0

    # -- sounds ---------------------------------------------------------------------------------------------------------
    def sound(self, t):
        return self.h.u32(0x800052A0) + 52 + 1163 * t

    def mach(self, t):
        return self.h.r(self.sound(t) + 0xDE, 1)[0]

    def slot(self, t, s, value=None):
        a = self.sound(t) + 0x14 + 2 * s
        if value is not None:
            self.h.w(a, struct.pack('>H', value))
        return self.h.u16(a)

    # -- panel ----------------------------------------------------------------------------------------------------------
    def tap(self, key, settle=15_000_000):
        self.h.tap(KEYS[key] if isinstance(key, str) else key); self.h.run(settle)

    def combo(self, a, b):
        h, A, B = self.h, KEYS[a], KEYS[b]
        h.pc = panelin.press(h.m, h.profile, *A); h.run(3_000_000)
        held = (1 << A[1]) if B[0] == A[0] else 0
        h.pc = panelin.press(h.m, h.profile, B[0], B[1], held); h.run(3_000_000)
        h.pc = panelin.release(h.m, h.profile, B[0], B[1], held); h.run(3_000_000)
        h.pc = panelin.release(h.m, h.profile, *A); h.run(20_000_000)

    def select(self, want, track=0, tries=3):
        """MACHINE menu ([FUNC] + [SYN]) on the active track: 0 FM TONE, 1 WAVETONE, 2 FM DRUM, 3 SWARMER (menu order 0 2 1 3)."""
        order = [0, 2, 1, 3]
        for _ in range(tries):
            if self.mach(track) == want:
                return True
            self.tap('SYN', 30_000_000)
            self.combo('FUNC', 'SYN')
            steps = order.index(want) - order.index(self.mach(track))
            for key in ['DOWN' if steps > 0 else 'UP'] * abs(steps) + ['YES', 'NO']:
                self.tap(key, 20_000_000)
        return self.mach(track) == want

    def select_filter(self, want, track=0, tries=8):
        """The FLTR machine (sound byte 0xdf: 0 multimode, 1 lowpass 4, 2 EQ, 3/5 comb, 4 legacy): [FUNC]+[SYN], RIGHT
        (the FLTR column), DOWN x n, YES -- tried n = 0.. until the byte is `want` (the list order is not 0..5)."""
        for n in range(tries):
            if self.h.r(self.sound(track) + 0xDF, 1)[0] == want:
                return True
            self.tap('SYN', 20_000_000); self.combo('FUNC', 'SYN'); self.tap('RIGHT', 20_000_000)
            for _ in range(n):
                self.tap('DOWN', 20_000_000)
            self.tap('YES', 30_000_000); self.tap('NO', 20_000_000)
        return self.h.r(self.sound(track) + 0xDF, 1)[0] == want

    def hold(self, key, frames=80, each=None):
        """Hold a page key (the page then shows every knob's value as a number: manual "press and hold a [PARAMETER] key");
        needs ~80 frames before it draws. Returns the screen (png) while held, then releases."""
        k = KEYS[key] if isinstance(key, str) else key
        self.h.press(k, 500_000); self.h.frames(frames, each=each)
        png = self.h.png(f'held_{key}.png')
        self.h.release(k); self.h.frames(6, each=each)
        return png

    def tail(self, words):
        """Fake the unused tail of the DSP reply (16 u16 at 0x80005e40, a free DSP -> ColdFire channel) -- pass as
        h.frames(each=k.tail_writer(words)) so it is there before every frame."""
        self.h.w(0x80005E40, b''.join(struct.pack('>H', x) for x in words))

    def tail_writer(self, words):
        return lambda i: self.tail(words)

    # -- sequencer ------------------------------------------------------------------------------------------------------
    def play(self, frames, bpm=300, between=150_000, each=None):
        h = self.h
        h.w(0x402A0DEC, BE(bpm * 120))                    # (the UI resets the tempo word: set it right before PLAY)
        start = len(h.sent)
        h.tap(PLAY)
        h.frames(frames, between=between, each=each)
        h.tap(STOP); h.frames(4)
        return [x for x, _ in h.sent[start:]]

    @staticmethod
    def notes(frames, track=0, gap=30):
        """(frame, voice) of each note of `track` (frame note-on bits +0x22, voice -> track +0xa5a); one per note (unison = several voices)"""
        out = []
        for i, x in enumerate(frames):
            on = int.from_bytes(x[0x22:0x24], 'big')
            for v in range(16):
                if on >> v & 1 and int.from_bytes(x[0xa5a + 2 * v:0xa5c + 2 * v], 'big') == track and \
                        (not out or i > out[-1][0] + gap):
                    out.append((i, v))
        return out

    # -- probes ---------------------------------------------------------------------------------------------------------
    def ring(self, entry, nbytes, n=256, stack_off=0):
        r = Ring(self, entry, nbytes, RING0 + 0x10000 * self.rings, n, stack_off)
        self.rings += 1
        return r

    def knobs(self):
        """(entry, value) of every knob the sound pages drew since the last call."""
        if self._knob_ring is None:
            self._knob_ring = self.ring(KNOB_DRAWER, 8)
        return [(c[5], c[6] & 0xFFFF) for c in self._knob_ring.calls()]

    def hot(self, samples=40, step=2_000_000):
        pcs = collections.Counter()
        for _ in range(samples):
            self.h.run(step); pcs[hex(self.h.pc)] += 1
        return pcs

    def watch_reads(self, addr, instrs=20_000_000, size=2):
        pcs = collections.Counter()
        hk = self.uc.hook_add(UC_HOOK_MEM_READ, lambda uc, acc, a, sz, v, u: pcs.update([hex(uc.reg_read(mc.UC_M68K_REG_PC))]),
                              begin=addr, end=addr + size - 1)
        self.h.run(instrs)
        self.uc.hook_del(hk)
        return pcs
