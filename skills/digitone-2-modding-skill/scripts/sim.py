"""sim.py — whole-program SHARC+ (ADSP-21569) interpreter for the Digitone II section-7 image.

Goal: run the REAL audio frame (boot/init, then the render + mixer + FX + output stage) offline, so firmware mods
can be tested without flashing. Built on visa.py (decoder). Differences from emu.py (kept for the FX unit tests):
  * byte-addressed memory with byte / short / normal / long-word (LW) accesses; NW-space (0x80000..0xEFFFF, x4)
    and SW-space (0x120000..0x1DFFFF, x2) aliases of L1; MMR space stubbed (reads 0 unless hooked)
  * SIMD: PEy (S registers, ASTATy) executes every compute when MODE1.PEYEN; DAG accesses of data registers move
    two words (addr, addr+4)
  * complete ALU / multiplier (fixed incl. MR accumulators) / shifter / multifunction sets, standard ASTAT/STKY bits
  * PC stack, loop stack (LCNTR / CURLCNTR / termination conditions), status stack, delayed branches (2 slots)
  * decoded instructions are cached per address
Unmodelled things raise NotImplementedError — never silently mis-execute. Interrupts are not delivered.
"""
from __future__ import annotations
import math, struct
import visa
from visa import B, sext

M32 = 0xFFFFFFFF


def f_of(u):
    u &= M32
    if (u & 0x7F800000) == 0 and (u & 0x7FFFFF):      # denormal operand -> signed zero (SHARC)
        u &= 0x80000000
    return struct.unpack('<f', struct.pack('<I', u))[0]


def u_of(f):
    if f != f:
        return 0xFFFFFFFF                                # SHARC NaN = all ones
    try:
        u = struct.unpack('<I', struct.pack('<f', f))[0]
    except OverflowError:
        u = 0x7F800000 if f > 0 else 0xFF800000
    if (u & 0x7F800000) == 0:
        u &= 0x80000000                                  # flush denormal results to zero
    return u


def s32(v):
    v &= M32
    return v - (1 << 32) if v & 0x80000000 else v


# ASTAT bits
AZ, AV, AN, AC, AS, AI, MN, MV, MU, MI, AF, SV, SZ, SS, BTF = 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 18
# STKY stack bits
PCFL, PCEM, SSOV, SSEM, LSOV, LSEM = 21, 22, 23, 24, 25, 26
PEYEN = 1 << 21
BDCST1, BDCST9 = 1 << 23, 1 << 22
CBUFEN = 1 << 24
ALUSAT = 1 << 13
TRUNCB = 1 << 15


class Stop(Exception):
    pass


class HWRuleError(Exception):
    """Code that executes but breaks a SHARC+ hardware rule the interpreter otherwise can't see (hwrules.py)."""


class Memory:
    def __init__(self, image):
        self.image = image
        self.words = {}                     # 4-aligned physical byte address -> u32 (WRITTEN by the program)
        self.cache = {}                     # values read from the image (not saved in snapshots)
        self.mmr = {}                       # MMR address -> u32 (as written)
        self.read_hooks = {}                # MMR address -> fn(sim) -> u32
        self.write_hooks = {}               # MMR address -> fn(sim, value)
        self.watch = None                   # optional fn(addr, size, value, is_write)
        self.regions = sorted(image.regions)
        self.sim = None
        self.misaligned = 0
        self.sw_space = {}                   # data accesses into short-word address space (addr -> count)
        self.on_uninit = None               # optional fn(pa) -> u32 for reads of never-initialised RAM

    @staticmethod
    def phys(a):
        a &= M32
        if 0x80000 <= a < 0xF0000:
            return a * 4
        if 0x120000 <= a < 0x1E0000:
            return a * 2
        return a

    @staticmethod
    def is_mmr(a):
        return a < 0x80000 or 0x30000000 <= a < 0x40000000

    def _load(self, pa):
        la = pa + 0x28000000 if 0x240000 <= pa < 0x3C0000 else pa
        for base, data in self.regions:
            if base <= la < base + len(data) - 3:
                return struct.unpack_from('<I', data, la - base)[0]
            if base <= la < base + len(data):
                b = data[la - base:] + b'\0\0\0'
                return struct.unpack_from('<I', b, 0)[0]
        if self.on_uninit:                  # never loaded by the boot stream nor written: garbage on hardware
            return self.on_uninit(pa) & M32
        return 0

    def rw(self, pa):                       # physical, 4-aligned
        v = self.words.get(pa)
        if v is None:
            v = self.cache.get(pa)
            if v is None:
                v = self._load(pa)
                self.cache[pa] = v
        return v

    def r(self, a, size=4, sext_=False):
        a &= M32
        if self.is_mmr(a):
            h = self.read_hooks.get(a)
            v = h(self.sim) if h else self.mmr.get(a, 0)
            return v & M32
        if 0x120000 <= a < 0x1E0000: self.sw_space[a] = self.sw_space.get(a, 0) + 1   # 16-bit space: see ystep
        pa = self.phys(a)
        pa = self.force_align(pa, size)
        if size == 4:
            if pa & 3:                                   # misaligned word (packed structs): LE bytes
                v = 0
                for k in range(4):
                    q = pa + k
                    v |= ((self.rw(q & ~3) >> (8 * (q & 3))) & 0xFF) << (8 * k)
                self.misaligned += 1
            else:
                v = self.rw(pa)
        elif size == 2:
            if (pa & 3) == 3:
                v = ((self.rw(pa & ~3) >> 24) & 0xFF) | ((self.rw(pa + 1) & 0xFF) << 8)
            else:
                v = (self.rw(pa & ~3) >> (8 * (pa & 3))) & 0xFFFF
            if sext_ and v & 0x8000: v |= 0xFFFF0000
        else:
            v = (self.rw(pa & ~3) >> (8 * (pa & 3))) & 0xFF
            if sext_ and v & 0x80: v |= 0xFFFFFF00
        if self.watch: self.watch(a, size, v, False)
        return v

    def w(self, a, v, size=4):
        a &= M32; v &= M32
        if self.is_mmr(a):
            self.mmr[a] = v
            h = self.write_hooks.get(a)
            if h: h(self.sim, v)
            return
        pa = self.phys(a)
        pa = self.force_align(pa, size)
        if self.watch: self.watch(a, size, v, True)
        if size == 4:
            if pa & 3:
                for k in range(4):
                    q = pa + k; base = q & ~3; sh = 8 * (q & 3)
                    self.words[base] = (self.rw(base) & ~(0xFF << sh)) | (((v >> (8 * k)) & 0xFF) << sh)
                self.misaligned += 1
            else:
                self.words[pa] = v
        else:
            base = pa & ~3; sh = 8 * (pa & 3); mask = (0xFFFF if size == 2 else 0xFF) << sh
            old = self.rw(base)
            self.words[base] = (old & ~mask) | ((v << sh) & mask)

    def force_align(self, pa, size):
        """SHARC+ PRM table 6-13 (Sizes and Alignment Restrictions): internal SISD byte/short/normal-word accesses
        need no alignment; internal SIMD normal words and ALL external (DDR) short/normal words are force-aligned to
        the next lower legal address."""
        # Byte-addressed DDR behaves like internal memory here: the 'external normal word' rows of table 6-13 are for
        # normal-word/IMDW-mode external accesses. SISD unaligned NW reads are needed by the wavetable interpolator
        # (32-bit load of two packed 16-bit samples at a short-aligned index, 0x1c454a).
        if size == 4 and pa & 3 and self.sim is not None and self.sim.simd():
            return pa & ~3
        return pa

    def rf(self, a): return f_of(self.r(a))
    def wf(self, a, f): self.w(a, u_of(f))


class PE:
    """One processing element: 16 data registers, ASTAT, STKY, MR accumulators (fixed-point, 80-bit ints)."""
    def __init__(self):
        self.R = [0] * 16
        self.ast = 0
        self.sticky = 0
        self.MRF = 0
        self.MRB = 0


class Sim:
    def __init__(self, image=None):
        self.img = image or visa.Image()
        self.mem = Memory(self.img); self.mem.sim = self
        self.px, self.py = PE(), PE()
        self.I = [0] * 16; self.M = [0] * 16; self.L = [0] * 16; self.Bx = [0] * 16
        self.M[5], self.M[6], self.M[7] = 0, 1, M32
        self.M[13], self.M[14], self.M[15] = 0, 1, M32
        self.sysr = {'MODE1': 0, 'MODE2': 0, 'IRPTL': 0, 'IMASK': 0, 'IMASKP': 0, 'LIRPTL': 0, 'MMASK': 0,
                     'FLAGS': 0, 'USTAT1': 0, 'USTAT2': 0, 'USTAT3': 0, 'USTAT4': 0}
        self.PX1 = 0; self.PX2 = 0
        self.pc = 0
        self.pcstk = []
        self.loopstk = []                    # [start, end, count|None, term_cond]
        self.ststk = []
        self.pending = None                  # (target, slots_left) for delayed branches
        self.icount = 0
        self.loops_checked, self.hw_violations = set(), []   # PRM loop rules, checked on each loop's first run
        self.pe_split = {}                                 # pc -> count of SIMD condition splits
        self.strict_hw = True                              # raise HWRuleError on a violation (stock never hits one)
        self.code = {}
        self.trace = None
        self.emuclk = 0
        self.stop_at = set()
        self.on_insn = None
        self.on_insn_pcs = None              # optional: the PCs on_insn acts on (a set, or a callable -> set); simc
                                             # then runs everything else in C and calls the hook only there

    # ------------------------------------------------------------------ helpers
    @property
    def MODE1(self): return self.sysr['MODE1']

    def simd(self): return bool(self.sysr['MODE1'] & PEYEN)

    def get_ureg(self, u, pe=None):
        g, r = (u >> 4) & 7, u & 0xF
        pe = pe or self.px
        if g == 0: return pe.R[r]
        if g == 1: return self.I[r]
        if g == 2: return self.M[r]
        if g == 3: return self.L[r]
        if g == 4: return self.Bx[r]
        if g == 5: return self.py.R[r]
        name = visa.ureg_name(u)
        if name == 'ASTATx': return self.px.ast
        if name == 'ASTATy': return self.py.ast
        if name == 'STKYx': return self.stky(self.px)
        if name == 'STKYy': return self.py.sticky
        if name in self.sysr: return self.sysr[name]
        if name == 'PX1': return self.PX1
        if name == 'PX2': return self.PX2
        if name == 'PX': return self.PX2
        if name == 'PCSTK': return self.pcstk[-1] if self.pcstk else 0
        if name == 'PCSTKP': return len(self.pcstk)
        if name == 'LCNTR': return self.lcntr_next if hasattr(self, 'lcntr_next') else 0
        if name == 'CURLCNTR': return (self.loopstk[-1][2] or 0) if self.loopstk else 0
        if name == 'LADDR': return (self.loopstk[-1][1]) if self.loopstk else 0
        if name in ('EMUCLK', 'EMUCLK2'): return self.icount & M32 if name == 'EMUCLK' else self.icount >> 32
        if name in ('TPERIOD', 'TCOUNT', 'FADDR', 'DADDR'): return self.sysr.get(name, 0)
        raise NotImplementedError(f'read ureg {name} @ {self.pc:#x}')

    def set_ureg(self, u, v, pe=None):
        v &= M32
        g, r = (u >> 4) & 7, u & 0xF
        pe = pe or self.px
        if g == 0: pe.R[r] = v; return
        if g == 1: self.I[r] = v; return
        if g == 2: self.M[r] = v; return
        if g == 3: self.L[r] = v; return
        if g == 4: self.Bx[r] = v; self.I[r] = v; return          # writing B also loads I (hardware)
        if g == 5: self.py.R[r] = v; return
        name = visa.ureg_name(u)
        if name == 'ASTATx': self.px.ast = v; return
        if name == 'ASTATy': self.py.ast = v; return
        if name == 'STKYx': self.px.sticky = v & ~((1 << PCFL) | (1 << PCEM) | (1 << SSOV) | (1 << SSEM) | (1 << LSOV) | (1 << LSEM)); return
        if name == 'STKYy': self.py.sticky = v; return
        if name in self.sysr: self.sysr[name] = v; return
        if name == 'PX1': self.PX1 = v; return
        if name == 'PX2': self.PX2 = v; return
        if name == 'PX': self.PX2 = v; self.PX1 = 0; return
        if name == 'PCSTK':
            if self.pcstk: self.pcstk[-1] = v
            else: self.pcstk.append(v)
            return
        if name == 'LCNTR': self.lcntr_next = v; return
        if name == 'CURLCNTR':
            if self.loopstk: self.loopstk[-1][2] = v
            return
        if name in ('TPERIOD', 'TCOUNT', 'FADDR', 'DADDR', 'EMUCLK', 'EMUCLK2'): self.sysr[name] = v; return
        raise NotImplementedError(f'write ureg {name} @ {self.pc:#x}')

    def stky(self, pe):
        v = pe.sticky
        if not self.pcstk: v |= 1 << PCEM
        if not self.ststk: v |= 1 << SSEM
        if not self.loopstk: v |= 1 << LSEM
        return v

    # ------------------------------------------------------------------ conditions
    def cond(self, c, pe=None):
        if c == 31: return True
        pe = pe or self.px
        a = pe.ast
        if c == 15:                                          # NOT LCE
            return bool(self.loopstk) and (self.loopstk[-1][2] or 0) != 1
        neg = c >= 16
        k = c - 16 if neg else c
        if k == 0: v = a >> AZ & 1
        elif k == 1: v = a >> AN & 1 and not (a >> AZ & 1)
        elif k == 2: v = (a >> AN & 1) or (a >> AZ & 1)
        elif k == 3: v = a >> AC & 1
        elif k == 4: v = a >> AV & 1
        elif k == 5: v = a >> MV & 1
        elif k == 6: v = a >> MN & 1
        elif k == 7: v = a >> SV & 1
        elif k == 8: v = a >> SZ & 1
        elif k in (9, 10, 11, 12): v = 0                    # FLAG0..3 inputs
        elif k == 13: v = a >> BTF & 1
        elif k == 14: v = 0                                  # BM
        else: raise NotImplementedError(f'cond {c}')
        v = bool(v)
        return (not v) if neg else v

    def branch_compute(self, cond, comp, els):
        """Compute of a Type 9a/11a branch (PRM 15-6): without ELSE it runs in each PE whose condition is true, with
        ELSE in each PE whose condition is false; the branch itself needs the condition in both PEs (cond_both).
        Conditions are evaluated before the compute changes any flags."""
        pes = (self.px, self.py) if self.simd() else (self.px,)
        run = [pe for pe in pes if (cond == 31 or self.cond(cond, pe)) != bool(els)]
        for pe in run: self._compute1(comp, pe)

    def cond_both(self, c):
        if not self.simd() or c == 31 or c == 15:
            return self.cond(c)
        cx, cy = self.cond(c, self.px), self.cond(c, self.py)
        if cx != cy:
            self.pe_split[self.pc] = self.pe_split.get(self.pc, 0) + 1     # PEs disagree (see cond_pe)
        return cx and cy

    # ------------------------------------------------------------------ flags
    @staticmethod
    def _set(pe, **bits):
        a = pe.ast
        for k, v in bits.items():
            b = globals()[k]
            a = (a | (1 << b)) if v else (a & ~(1 << b))
        pe.ast = a

    def _fixflags(self, pe, v, av=0, ac=0):
        v &= M32
        a = pe.ast & ~((1 << AZ) | (1 << AN) | (1 << AV) | (1 << AC) | (1 << AI) | (1 << AF))
        if v == 0: a |= 1 << AZ
        if v & 0x80000000: a |= 1 << AN
        if av: a |= 1 << AV
        if ac: a |= 1 << AC
        pe.ast = a

    def _fltflags(self, pe, u, ai=0, av=0):
        a = pe.ast & ~((1 << AZ) | (1 << AN) | (1 << AV) | (1 << AC) | (1 << AI))
        a |= 1 << AF
        if (u & 0x7FFFFFFF) == 0: a |= 1 << AZ
        if u & 0x80000000 and not ai: a |= 1 << AN
        if ai: a |= 1 << AI
        if av: a |= 1 << AV
        pe.ast = a

    def _shf(self, pe, v, sv=0, ss=0):
        a = pe.ast & ~((1 << SZ) | (1 << SV) | (1 << SS))
        if (v & M32) == 0: a |= 1 << SZ
        if sv: a |= 1 << SV
        if ss: a |= 1 << SS
        pe.ast = a

    # ------------------------------------------------------------------ compute
    def compute(self, c):
        if not c: return
        if self.simd():
            snap = list(self.py.R)
            self._compute1(c, self.px)
            self._compute1(c, self.py)
        else:
            self._compute1(c, self.px)

    def _compute1(self, c, pe):
        R = pe.R
        if c & 0x400000:
            op6 = (c >> 16) & 0x3F
            m, a = (c >> 12) & 0xF, (c >> 8) & 0xF
            xm, ym, xa, ya = (c >> 6) & 3, ((c >> 4) & 3) + 4, ((c >> 2) & 3) + 8, (c & 3) + 12
            if op6 in (0, 1):
                sel = (c >> 12) & 7
                acc = 'MRF' if sel < 4 else 'MRB'
                part = sel & 3
                if part == 3: raise NotImplementedError('MR part 3')
                if op6 == 0:                                   # Rn = MRxy
                    mr = getattr(pe, acc)
                    R[a] = (mr >> (32 * part)) & (M32 if part < 2 else 0xFFFF_FFFF)
                    if part == 2:
                        R[a] = s32((mr >> 64) & 0xFFFF) & M32
                else:                                          # MRxy = Rn
                    mr = getattr(pe, acc) & ((1 << 80) - 1)
                    sh = 32 * part
                    width = 32 if part < 2 else 16
                    mr = (mr & ~(((1 << width) - 1) << sh)) | ((R[a] & ((1 << width) - 1)) << sh)
                    setattr(pe, acc, mr)
                return
            X, Y, XA, YA = R[xm], R[ym], R[xa], R[ya]
            if op6 >= 0x20:
                s = op6 & 0xF
                if op6 & 0x10:
                    p = f_of(X) * f_of(Y); fx, fy = f_of(XA), f_of(YA)
                    R[m] = u_of(p); R[a] = u_of(fx + fy); R[s] = u_of(fx - fy)
                    self._fltflags(pe, R[s])
                else:
                    R[m] = self._fmul_fixed(pe, X, Y, 1, 1, 1, 1, None)
                    R[a] = self._addsub(pe, XA, YA, 0)
                    R[s] = self._addsub(pe, XA, YA, 1)
                return
            if op6 >= 0x18:
                p = f_of(X) * f_of(Y)
                R[m] = u_of(p)
                fx, fy = f_of(XA), f_of(YA)
                if op6 == 0x18: r = u_of(fx + fy)
                elif op6 == 0x19: r = u_of(fx - fy)
                elif op6 == 0x1a: r = u_of(float(s32(XA)) * 2.0 ** s32(YA))
                elif op6 == 0x1b: r = self._fix(pe, fx * 2.0 ** s32(YA)); R[a] = r; return
                elif op6 == 0x1c: r = u_of((fx + fy) / 2)
                elif op6 == 0x1d: r = u_of(abs(fx))
                elif fx != fx or fy != fy: r = 0xFFFFFFFF   # (MAX / MIN: a NaN passes, as in _falu)
                elif op6 == 0x1e: r = u_of(max(fx, fy))
                else: r = u_of(min(fx, fy))
                R[a] = r; self._fltflags(pe, r)
                return
            # fixed multiply (SSFR / MR acc) + fixed ALU
            kind = {0x04: ('R', '+'), 0x05: ('R', '-'), 0x06: ('R', 'avg'), 0x08: ('MRF+', '+'), 0x09: ('MRF+', '-'),
                    0x0a: ('MRF+', 'avg'), 0x0c: ('RMRF+', '+'), 0x0d: ('RMRF+', '-'), 0x0e: ('RMRF+', 'avg'),
                    0x10: ('MRF-', '+'), 0x11: ('MRF-', '-'), 0x12: ('MRF-', 'avg'), 0x14: ('RMRF-', '+'),
                    0x15: ('RMRF-', '-'), 0x16: ('RMRF-', 'avg')}.get(op6)
            if not kind: raise NotImplementedError(f'multifunc {op6:#x} @ {self.pc:#x}')
            mk, ak = kind
            prod = s32(X) * s32(Y) << 1                                  # SSF
            if mk == 'R':
                R[m] = self._fmul_fixed(pe, X, Y, 1, 1, 1, 1, None)
            elif mk in ('MRF+', 'MRF-'):
                pe.MRF = pe.MRF + prod if mk == 'MRF+' else pe.MRF - prod
            else:
                acc = pe.MRF + prod if mk == 'RMRF+' else pe.MRF - prod
                R[m] = self._mr_result(acc, 1, 1)
            if ak == '+': R[a] = self._addsub(pe, XA, YA, 0)
            elif ak == '-': R[a] = self._addsub(pe, XA, YA, 1)
            else: R[a] = ((s32(XA) + s32(YA)) >> 1) & M32; self._fixflags(pe, R[a])
            return
        cu = (c >> 20) & 3
        op = (c >> 12) & 0xFF
        n, x, y = (c >> 8) & 0xF, (c >> 4) & 0xF, c & 0xF
        if cu == 0:
            if op >> 4 == 0x7:
                X, Y = R[x], R[y]
                R[n] = self._addsub(pe, X, Y, 0); R[op & 0xF] = self._addsub(pe, X, Y, 1); return
            if op >> 4 == 0xF:
                fx, fy = f_of(R[x]), f_of(R[y])
                R[n] = u_of(fx + fy); R[op & 0xF] = u_of(fx - fy); self._fltflags(pe, R[op & 0xF]); return
            self._alu(pe, op, n, x, y); return
        if cu == 1:
            self._mul(pe, op, n, x, y); return
        if cu == 2:
            self._shift_reg(pe, op, n, x, y); return
        raise NotImplementedError(f'compute cu3 {c:#x} @ {self.pc:#x}')

    def _addsub(self, pe, X, Y, sub, ci=0):
        sx, sy = s32(X), s32(Y)
        r = sx - sy + ci if sub else sx + sy + ci
        ux = X & M32; uy = Y & M32
        carry = (ux + ((~uy) & M32) + 1 + ci > M32) if sub else (ux + uy + ci > M32)
        ov = r > 0x7FFFFFFF or r < -0x80000000
        if ov and self.sysr['MODE1'] & ALUSAT:
            r = 0x7FFFFFFF if r > 0 else -0x80000000
        self._fixflags(pe, r, av=ov, ac=carry)
        return r & M32

    def _alu(self, pe, op, n, x, y):
        R = pe.R
        X, Y = R[x], R[y]
        sx, sy = s32(X), s32(Y)
        ci = pe.ast >> AC & 1
        if op == 0x01: R[n] = self._addsub(pe, X, Y, 0)
        elif op == 0x02: R[n] = self._addsub(pe, X, Y, 1)
        elif op == 0x05: R[n] = self._addsub(pe, X, Y, 0, ci)
        elif op == 0x06: R[n] = self._addsub(pe, X, Y, 1, ci - 1)
        elif op == 0x09: R[n] = ((sx + sy) >> 1) & M32; self._fixflags(pe, R[n])
        elif op == 0x0a:
            self._fixflags(pe, 0 if sx == sy else (1 if sx > sy else -1))
            pe.ast = (pe.ast & 0x00FFFFFF) | (((pe.ast >> 25) & 0x7F) << 24) | ((1 << 31) if sx > sy else 0)
        elif op == 0x0b:
            ux, uy = X & M32, Y & M32
            self._fixflags(pe, 0 if ux == uy else (1 if ux > uy else -1))
            pe.ast = (pe.ast & 0x00FFFFFF) | (((pe.ast >> 25) & 0x7F) << 24) | ((1 << 31) if ux > uy else 0)
        elif op == 0x25: R[n] = self._addsub(pe, X, 0, 0, ci)
        elif op == 0x26: R[n] = self._addsub(pe, X, 0, 0, ci - 1)
        elif op == 0x29: R[n] = self._addsub(pe, X, 1, 0)
        elif op == 0x2a: R[n] = self._addsub(pe, X, 1, 1)
        elif op == 0x22: R[n] = self._addsub(pe, 0, X, 1)
        elif op == 0x30:
            v = abs(sx); ov = v > 0x7FFFFFFF
            if ov and self.sysr['MODE1'] & ALUSAT: v = 0x7FFFFFFF
            R[n] = v & M32; self._fixflags(pe, R[n], av=ov); self._set(pe, AS=sx < 0)
        elif op == 0x21: R[n] = X & M32; self._fixflags(pe, R[n])
        elif op == 0x40: R[n] = X & Y; self._fixflags(pe, R[n])
        elif op == 0x41: R[n] = (X | Y) & M32; self._fixflags(pe, R[n])
        elif op == 0x42: R[n] = (X ^ Y) & M32; self._fixflags(pe, R[n])
        elif op == 0x43: R[n] = (~X) & M32; self._fixflags(pe, R[n])
        elif op == 0x61: R[n] = min(sx, sy) & M32; self._fixflags(pe, R[n])
        elif op == 0x62: R[n] = max(sx, sy) & M32; self._fixflags(pe, R[n])
        elif op == 0x63:
            lim = abs(sy); R[n] = max(-lim, min(lim, sx)) & M32; self._fixflags(pe, R[n])
        else:
            self._falu(pe, op, n, x, y)

    def _falu(self, pe, op, n, x, y):
        R = pe.R
        X, Y = R[x], R[y]
        fx, fy = f_of(X), f_of(Y)
        sy = s32(Y)
        nan = lambda *v: any(t != t for t in v)
        if op == 0x81: r = u_of(fx + fy)
        elif op == 0x82: r = u_of(fx - fy)
        elif op == 0x91: r = u_of(abs(fx + fy))
        elif op == 0x92: r = u_of(abs(fx - fy))
        elif op == 0x89: r = u_of((fx + fy) / 2)
        elif op == 0x8a:
            az = fx == fy; an = fx < fy
            a = pe.ast & ~((1 << AZ) | (1 << AN) | (1 << AI))
            if az: a |= 1 << AZ
            if an: a |= 1 << AN
            if nan(fx, fy): a |= 1 << AI
            pe.ast = (a & 0x00FFFFFF) | (((a >> 25) & 0x7F) << 24) | ((1 << 31) if fx > fy else 0)
            return
        elif op == 0xa2: r = u_of(-fx)
        elif op == 0xb0: r = u_of(abs(fx))
        elif op == 0xa1: r = u_of(fx)
        elif op == 0xa5: r = u_of(fx)
        elif op == 0xbd:
            r = u_of(math.ldexp(fx, max(-300, min(300, sy)))) if fx == fx and abs(fx) != math.inf else u_of(fx)
        elif op == 0xad:
            if fx == 0 or fx != fx or abs(fx) == math.inf: R[n] = 0; self._fixflags(pe, 0); return
            mnt, e = math.frexp(abs(fx))                     # MANT: unsigned fraction 1.m left-justified
            R[n] = int(mnt * 2 * (1 << 31)) & M32; self._fixflags(pe, R[n]); return
        elif op == 0xc1:
            if fx == 0: R[n] = 0xFF800000 & M32; self._fixflags(pe, R[n]); return
            R[n] = (math.frexp(abs(fx))[1] - 1) & M32; self._fixflags(pe, R[n]); return
        elif op in (0xc9, 0xd9, 0xcd, 0xdd):
            v = fx * (2.0 ** sy if op in (0xd9, 0xdd) else 1.0)
            R[n] = self._fix(pe, v, trunc='zero' if op in (0xcd, 0xdd) else None); return
        elif op in (0xca, 0xda):
            v = float(s32(X)) * (2.0 ** sy if op == 0xda else 1.0)
            r = u_of(v)
        elif op == 0xc4:
            r = u_of(1.0 / fx) if fx != 0 else (0x7F800000 | (X & 0x80000000))
        elif op == 0xc5:
            r = u_of(fx ** -0.5) if fx > 0 else (0x7F800000 if fx == 0 else 0xFFFFFFFF)
        elif op == 0xe0: r = u_of(math.copysign(abs(fx), fy))
        elif op in (0xe1, 0xe2, 0xe3) and nan(fx, fy):
            r = 0xFFFFFFFF                               # (HW, read over USB: a NaN passes MIN / MAX / CLIP; Python's min/max drop it)
        elif op == 0xe1: r = u_of(min(fx, fy))
        elif op == 0xe2: r = u_of(max(fx, fy))
        elif op == 0xe3: r = u_of(max(-abs(fy), min(abs(fy), fx)))
        else:
            raise NotImplementedError(f'alu {op:#x} @ {self.pc:#x}')
        R[n] = r
        self._fltflags(pe, r, ai=(r == 0xFFFFFFFF), av=(r & 0x7FFFFFFF) == 0x7F800000)

    def _fix(self, pe, v, trunc=None):
        if v != v: r = 0xFFFFFFFF
        else:
            # PRM: TRUNC instruction -> toward 0 always; FIX -> toward -inf if MODE1.TRUNC else to nearest
            if trunc is None: trunc = 'floor' if self.sysr['MODE1'] & TRUNCB else False
            if abs(v) == math.inf: iv = 0x7FFFFFFF if v > 0 else -0x80000000
            elif trunc == 'zero': iv = int(v)
            elif trunc: iv = math.floor(v)
            else:
                iv = round(v)                                 # round-to-nearest-even
            ov = iv > 0x7FFFFFFF or iv < -0x80000000
            iv = max(-0x80000000, min(0x7FFFFFFF, iv))
            r = iv & M32
        self._fixflags(pe, r)
        return r

    def _mr_result(self, acc, frac, rnd):
        if frac:
            if rnd:
                acc += 1 << 31
            return (acc >> 32) & M32
        return acc & M32

    def _fmul_fixed(self, pe, X, Y, sx_, sy_, frac, rnd, _):
        x = s32(X) if sx_ else X & M32
        y = s32(Y) if sy_ else Y & M32
        p = x * y
        if frac: p <<= 1
        return self._mr_result(p, frac, rnd)

    def _mul(self, pe, op, n, x, y):
        R = pe.R
        if op == 0x30:
            r = u_of(f_of(R[x]) * f_of(R[y])); R[n] = r
            a = pe.ast & ~((1 << MN) | (1 << MV) | (1 << MU) | (1 << MI))
            if r & 0x80000000: a |= 1 << MN
            pe.ast = a
            return
        hi = (op >> 6) & 3
        if hi:
            ys, xs, frac, dst, rnd = op >> 5 & 1, op >> 4 & 1, op >> 3 & 1, op >> 1 & 3, op & 1
            xv = s32(R[x]) if xs else R[x] & M32
            yv = s32(R[y]) if ys else R[y] & M32
            p = xv * yv
            if frac: p <<= 1
            accname = 'MRB' if dst in (1, 3) else 'MRF'
            acc = getattr(pe, accname)
            if hi == 1: res = p
            elif hi == 2: res = acc + p
            else: res = acc - p
            res = s80(res)
            if dst in (2, 3):
                setattr(pe, accname, res)
            else:
                R[n] = self._mr_result(res, frac, rnd)
            return
        # hi == 0: MR operations, PRM Table 12-5: SAT 0000 f dd s | RND 0001 1 dd s | MRF=0 0x14 | MRB=0 0x16
        if op in (0x14, 0x16):
            setattr(pe, 'MRF' if op == 0x14 else 'MRB', 0); return
        if (op >> 4) == 0 or ((op >> 4) == 1 and op & 8):
            rnd = (op >> 4) == 1
            frac = bool(op & 8) or rnd
            dest = (op >> 1) & 3
            sgn = op & 1
            accn = 'MRB' if dest in (1, 3) else 'MRF'
            acc = getattr(pe, accn)
            if rnd:
                v = (acc + (1 << 31)) & ~((1 << 32) - 1)
            elif frac:
                lo, hi_ = (-(1 << 63), (1 << 63) - 1) if sgn else (0, (1 << 64) - 1)
                v = max(lo, min(hi_, acc))
            else:
                lo, hi_ = (-(1 << 31), (1 << 31) - 1) if sgn else (0, (1 << 32) - 1)
                v = max(lo, min(hi_, acc))
            if dest in (2, 3):
                setattr(pe, accn, v)
            else:
                R[n] = ((v >> 32) if frac else v) & M32
            return
        raise NotImplementedError(f'mul op {op:#x} @ {self.pc:#x}')

    def _shift_reg(self, pe, op, n, x, y):
        R = pe.R
        X = R[x] & M32
        sy8 = sext(R[y] & 0xFF, 8)
        if op in (0x00, 0x20, 0x04, 0x24, 0x08):
            if op in (0x00, 0x20): v = self._lsh(X, sy8)
            elif op in (0x04, 0x24): v = self._ash(X, sy8)
            else:
                k = sy8 % 32; v = ((X << k) | (X >> (32 - k))) & M32 if k else X
            if op in (0x20, 0x24): v |= R[n]
            R[n] = v & M32; self._shf(pe, R[n]); return
        if op in (0xc0, 0xc4, 0xc8, 0xcc):
            bit = R[y] & 31
            if op == 0xc0: R[n] = X | (1 << bit)
            elif op == 0xc4: R[n] = X & ~(1 << bit) & M32
            elif op == 0xc8: R[n] = X ^ (1 << bit)
            else:
                self._shf(pe, X & (1 << bit)); return
            self._shf(pe, R[n]); return
        if op in (0x40, 0x48, 0x44, 0x64, 0x4c, 0x6c):
            pos, ln = R[y] & 0x3F, (R[y] >> 6) & 0x3F
            self._field(pe, op, n, X, pos, ln); return
        if op in (0x80, 0x84):
            v = self._exp(X); R[n] = v & M32; self._shf(pe, 0 if False else 1, ss=bool(X & 0x80000000)); return
        if op == 0x88:
            v = 32 if X == 0 else 32 - X.bit_length()
            R[n] = v; self._shf(pe, v, sv=(v == 32)); return
        if op == 0x8c:
            nx = (~X) & M32
            v = 32 if nx == 0 else 32 - nx.bit_length()
            R[n] = v; self._shf(pe, v, sv=(v == 32)); return
        if op == 0x7c:                                       # BFFWRP = Rx
            pe.bffwrp = X & 0x7F; return
        if op == 0x70:                                       # Rn = BFFWRP
            R[n] = getattr(pe, 'bffwrp', 0); return
        raise NotImplementedError(f'shift {op:#x} @ {self.pc:#x}')

    def _bitdep(self, pe, X, ln):
        wrp, bff = getattr(pe, 'bffwrp', 0), getattr(pe, 'bff', 0)
        ln = min(ln, 32)
        pos = 64 - (wrp + ln)
        if ln and pos >= 0:
            bff |= (X & ((1 << ln) - 1)) << pos
        pe.bff, pe.bffwrp = bff & ((1 << 64) - 1), wrp + ln
        self._shf(pe, 1)                                     # SF (wrp >= 32) not modelled

    @staticmethod
    def _lsh(X, s):
        if s >= 32 or s <= -32: return 0
        return (X << s) & M32 if s >= 0 else X >> -s

    @staticmethod
    def _ash(X, s):
        v = s32(X)
        if s >= 32: return 0
        if s <= -32: return M32 if v < 0 else 0
        return (v << s) & M32 if s >= 0 else (v >> -s) & M32

    @staticmethod
    def _exp(X):
        v = s32(X)
        if v >= 0:
            lz = 32 if v == 0 else 32 - v.bit_length()
        else:
            nv = ~v & M32
            lz = 32 if nv == 0 else 32 - nv.bit_length()
        return (-(lz - 1)) & M32 if lz else 0

    def _field(self, pe, op, n, X, pos, ln):
        R = pe.R
        mask = (1 << ln) - 1 if ln < 64 else M32
        if op in (0x40, 0x48):                               # FEXT
            v = (X >> pos) & mask if pos < 32 else 0
            if op == 0x48 and ln and v & (1 << (ln - 1)): v |= ~mask
            R[n] = v & M32
        else:                                                # FDEP
            v = (X & mask)
            if op in (0x4c, 0x6c) and ln and v & (1 << (ln - 1)): v |= ~mask & 0xFFFFFFFFFFFF
            v = (v << pos) & M32
            if op in (0x64, 0x6c):
                keep = R[n] & ~((mask << pos) & M32) & M32 if op == 0x64 else R[n]
                v = (keep | v) & M32
            R[n] = v
        self._shf(pe, R[n])

    def shift_imm(self, comp, dataex):
        pes = (self.px, self.py) if self.simd() else (self.px,)
        for pe in pes:
            self._shift_imm1(pe, comp, dataex)

    def _shift_imm1(self, pe, comp, dataex):
        op = (comp >> 16) & 0x3F
        data = (dataex << 8) | ((comp >> 8) & 0xFF)
        n, x = (comp >> 4) & 0xF, comp & 0xF
        R = pe.R
        X = R[x] & M32
        d8 = sext(data & 0xFF, 8)
        if op in (0x00, 0x08): v = self._lsh(X, d8) | (R[n] if op == 0x08 else 0)
        elif op in (0x01, 0x09): v = self._ash(X, d8) | (R[n] if op == 0x09 else 0)
        elif op == 0x02:
            k = d8 % 32; v = ((X << k) | (X >> (32 - k))) & M32 if k else X
        elif op in (0x30, 0x31, 0x32, 0x33):
            bit = data & 31
            if op == 0x30: v = X | (1 << bit)
            elif op == 0x31: v = X & ~(1 << bit) & M32
            elif op == 0x32: v = X ^ (1 << bit)
            else:
                self._shf(pe, X & (1 << bit)); return
        elif op in (0x10, 0x12, 0x11, 0x19, 0x13, 0x1b):
            pos, ln = data & 0x3F, (data >> 6) & 0x3F
            fop = {0x10: 0x40, 0x12: 0x48, 0x11: 0x44, 0x19: 0x64, 0x13: 0x4c, 0x1b: 0x6c}[op]
            self._field(pe, fop, n, X, pos, ln); return
        elif op in (0x20, 0x21):
            R[n] = self._exp(X); self._shf(pe, 1, ss=bool(X & 0x80000000)); return
        elif op == 0x22:
            v = 32 if X == 0 else 32 - X.bit_length(); R[n] = v; self._shf(pe, v, sv=v == 32); return
        elif op == 0x23:
            nx = ~X & M32; v = 32 if nx == 0 else 32 - nx.bit_length(); R[n] = v; self._shf(pe, v, sv=v == 32); return
        elif op == 0x1f:                                     # BFFWRP = data7 (bit FIFO write pointer)
            pe.bffwrp = data & 0x7F; return
        elif op == 0x1d:                                     # BITDEP Rx BY bitlen12 (PRM: BFF |= FDEP Rx BY
            self._bitdep(pe, X, data & 0xFFF); return        # 64-(wrp+len):len; wrp += len)
        else:
            raise NotImplementedError(f'shift imm {op:#x} @ {self.pc:#x}')
        R[n] = v & M32
        self._shf(pe, R[n])

    # ------------------------------------------------------------------ DAG
    def _sc(self, addr):
        a = addr & M32
        if 0x80000 <= a < 0xF0000 or a < 0x80000:          # NW-space: word addressing
            return 1
        if 0x120000 <= a < 0x1E0000:
            return 1
        return 4

    def dag_ea(self, i, m, pre, scale=True, width=4):
        """Effective address; in byte space the modifier is scaled by the access size: BW 1, SW 2, NW 4 and LW 4
        (PRM 6-9: "scaling is by the size of the access (except in the case of (lw))"; table 6-2 lists dm(mod,In)
        and dm(mod,In)(lw) with scaled_mod = mod << 2 — the WAVETONE 2:1 decimator 0xb8286b steps LW pairs by M2=2)."""
        base = self.I[i] & M32
        self.ea_i = i
        mv = s32(self.M[m])
        if scale: mv *= (min(width, 4) if self._sc(base) == 4 else 1)
        if pre:
            return (base + mv) & M32
        self._update_i(i, base, mv)
        return base

    def _update_i(self, i, base, mv):
        L = self.L[i]
        nv = base + mv
        if L and (self.sysr['MODE1'] & CBUFEN):
            Bx = self.Bx[i]
            Lb = L * self._sc(Bx)
            if nv >= Bx + Lb: nv -= Lb
            elif nv < Bx: nv += Lb
        self.I[i] = nv & M32

    @staticmethod
    def ystep(addr, size):
        """Address offset of PEy's implicit access in SIMD (PRM 7-5/7-6, 6-3x): byte space = next location of the
        access size (BW +1, SW +2, NW +4 bytes); short-word space = alternate location (+2); normal-word space +1."""
        if 0x80000 <= addr < 0xF0000:
            return 1
        if 0x120000 <= addr < 0x1E0000:
            return 2
        return size

    def bcast(self, i):
        """SIMD broadcast load (MODE1.BDCST1 for I1, BDCST9 for I9): PEy reads the SAME location as PEx (PRM 14-33)."""
        return (i == 1 and self.sysr['MODE1'] & BDCST1) or (i == 9 and self.sysr['MODE1'] & BDCST9)

    def dreg_access(self, addr, reg, write, size=4, sx=False, lw=False, ureg=None):
        """Memory <-> register with SIMD pairing for data registers."""
        mem = self.mem
        is_data = ureg is None or (ureg >> 4) == 0
        u = ureg if ureg is not None else reg
        if u == 0x6B:                                    # PX: 48-bit transfer (instruction-word layout)
            self.px48(addr, write); return
        step = 4 if addr >= 0x240000 or addr >= 0x20000000 else 1
        if lw:
            addr &= ~7 if step == 4 else ~1                 # LW accesses are force-aligned to a long word
            r0 = u & ~1
            if write:
                mem.w(addr, self.get_ureg(r0)); mem.w(addr + step, self.get_ureg(r0 | 1))
            else:
                self.set_ureg(r0, mem.r(addr)); self.set_ureg(r0 | 1, mem.r(addr + step))
            return
        if size != 4:
            step = self.ystep(addr, size)
        if write:
            mem.w(addr, self.get_ureg(u), size)
            if self.simd():
                # SIMD: data registers store their PEy complement in the implicit lane; any other ureg (I/M/L/B,
                # system) is broadcast to both lanes -- the compiler clears buffers with `DM(I4,M4)=M12`, M4=2 (0x1c1f50)
                yv = self.py.R[u & 0xF] if is_data else self.py.ast if u == 0x76 else \
                    self.py.sticky if u == 0x78 else self.get_ureg(u)      # ASTATx/STKYx store their y complement
                mem.w(addr + step, yv, size)
        else:
            v = mem.r(addr, size, sx)
            if is_data and self.simd():
                vy = mem.r(addr + (0 if self.bcast(self.ea_i) else step), size, sx)
                self.set_ureg(u, v); self.py.R[u & 0xF] = vy
            else:
                self.set_ureg(u, v)

    def px48(self, a, write):
        """48-bit word at NW-space address a = 3 short words at sw 2*base + 3*(a-base) (block base 0x10000-aligned)."""
        a &= M32
        if not (0x80000 <= a < 0xF0000):
            raise NotImplementedError(f'48-bit access outside NW space {a:#x} @ {self.pc:#x}')
        base = a & ~0xFFFF
        sw = 2 * base + 3 * (a - base)
        pa = 2 * sw
        if write:
            w0, w1, w2 = (self.PX2 >> 16) & 0xFFFF, self.PX2 & 0xFFFF, (self.PX1 >> 16) & 0xFFFF
            for k, w in enumerate((w0, w1, w2)): self.mem.w(pa + 2 * k, w, 2)
        else:
            w0, w1, w2 = (self.mem.r(pa + 2 * k, 2) for k in range(3))
            self.PX2 = (w0 << 16) | w1
            self.PX1 = w2 << 16

    # ------------------------------------------------------------------ run
    def insn(self, pc):
        c = self.code.get(pc)
        if c is None:
            ins = self.img.insn(pc)
            if ins is None or ins.kind == 'invalid':
                raise RuntimeError(f'bad insn at {pc:#x}')
            c = ins
            self.code[pc] = c
        return c

    def run(self, max_insns=10_000_000, until=None):
        n = 0
        while True:
            if until is not None and self.pc in until and self.pending is None:
                return 'until'
            if self.pc in self.stop_at and self.pending is None:
                return 'stop'
            self.step()
            n += 1
            if n >= max_insns:
                return 'max'

    def step(self):
        pc = self.pc
        ins = self.insn(pc)
        if self.trace is not None:
            self.trace.append(pc)
        if self.on_insn: self.on_insn(self, ins)
        nxt = pc + ins.size
        self.icount += 1
        tgt = self.exec(ins, nxt)                 # returns taken target or None
        if self.pending is not None:              # we are in a delay slot of an earlier branch
            t, k = self.pending
            k -= 1
            if k == 0:
                self.pending = None
                self.pc = t
            else:
                self.pending = (t, k)
                self.pc = nxt
            return
        if tgt is not None:
            if isinstance(tgt, tuple):            # delayed
                self.pending = (tgt[0], 2)
                self.pc = nxt
            else:
                self.pc = tgt
            return
        # hardware loop end check
        while self.loopstk and pc == self.loopstk[-1][1]:
            top = self.loopstk[-1]
            if top[3] is None:                    # counter loop
                top[2] -= 1
                if top[2] > 0:
                    self.pc = top[0]; return
                self.loopstk.pop()
            else:
                if top[3] == 31:                  # FOREVER
                    self.pc = top[0]; return
                if self.cond(top[3]):
                    self.loopstk.pop()
                else:
                    self.pc = top[0]; return
            # nested loop ending on the same instruction: continue checking the outer one
        self.pc = nxt

    def branch(self, tgt, ins, call=False, nxt=None, la=False):
        db = '(DB)' in ins.text
        if call:
            self.pcstk.append((nxt + (self.insn(nxt).size + self.insn(nxt + self.insn(nxt).size).size) if db else nxt))
        if la and self.loopstk:
            self.loopstk.pop()
        return (tgt,) if db else tgt

    # ------------------------------------------------------------------ execute one instruction
    def exec(self, ins, nxt):
        t = ins.type
        x = ins.x
        self.ea_i = None                                 # I register of this instruction's access (broadcast loads)
        comp = B(x, 22, 0)
        if t in ('3a', '3b', '3d'):
            return self.exec3(ins, x, t, comp)
        if t == '2a':
            if self.cond_both(B(x, 37, 33)) if not self.simd() else True:
                if self.simd():
                    c = B(x, 37, 33)
                    if self.cond(c, self.px): self._compute1(comp, self.px)
                    if self.cond(c, self.py): self._compute1(comp, self.py)
                else:
                    self.compute(comp)
            return None
        if t in ('4a', '4b', '4d'):
            return self.exec4(ins, x, t, comp)
        if t == '2b':
            self.compute(B(x, 38, 16)); return None
        if t in ('5a_move', '5b_move'):
            cond = B(x, 37, 33)
            src = (B(x, 42, 38) << 2) | B(x, 32, 31)
            dst = B(x, 29, 23)
            ok = self.cond_both(cond)
            v = self.get_ureg(src)
            # SIMD: a data-register destination gets PEy's complement: S<src> for a data source, the complementary
            # system register for ASTAT/STKY, and the same value for any other ureg ("R5 = I8 loads R5 and S5", PRM 6-x)
            if (src >> 4) == 0: vy = self.py.R[src & 0xF]
            elif (src >> 4) == 5: vy = self.px.R[src & 0xF]
            elif src == 0x76: vy = self.py.ast
            elif src == 0x78: vy = self.py.sticky
            else: vy = v
            if t == '5a_move' and comp and ok: self.compute(comp)
            if ok:
                self.set_ureg(dst, v)
                if self.simd() and (dst >> 4) == 0:
                    self.py.R[dst & 0xF] = vy
            return None
        if t == '6a_nomem':
            if self.cond_both(B(x, 37, 33)): self.shift_imm(comp, B(x, 30, 27))
            return None
        if t in ('15a', '15b'):
            if t == '15b':
                g, i, d, l = B(x, 37, 37), B(x, 43, 41), B(x, 40, 40), B(x, 39, 39)
                ur, data = B(x, 29, 23), sext(B(x, 22, 16), 7)
            else:
                g, i, d, l = B(x, 44, 44), B(x, 43, 41), B(x, 40, 40), B(x, 39, 39)
                ur, data = B(x, 38, 32), sext(B(x, 31, 0), 32)
            ii = i + (8 if g else 0)
            self.check_same_dag(ur, g, store=bool(d), post=False)
            base = self.I[ii] & M32
            self.ea_i = ii
            a = (base + data * (4 if self._sc(base) == 4 else 1)) & M32
            self.dreg_access(a, None, d, lw=bool(l), ureg=ur)
            return None
        if t == '2c':
            self.short_compute(B(x, 43, 40), B(x, 39, 36), B(x, 35, 32)); return None
        if t == '3c':
            i, m, d, r = B(x, 43, 41), B(x, 40, 38), B(x, 37, 37), B(x, 35, 32)
            a = self.dag_ea(i, m, False)
            self.dreg_access(a, r, d)
            return None
        if t in ('17a', '17b'):
            ur = B(x, 38, 32)
            data = B(x, 31, 0) if t == '17a' else sext(B(x, 31, 16), 16) & M32
            self.set_ureg(ur, data)
            if self.simd() and (ur >> 4) == 0: self.py.R[ur & 0xF] = data
            return None
        if t == '8a':
            rel, bl, la, cond = B(x, 40, 40), B(x, 39, 39), B(x, 38, 38), B(x, 37, 33)
            if self.cond_both(cond):
                tgt = ins.target
                return self.branch(tgt, ins, call=bool(bl), nxt=nxt, la=bool(la))
            return None
        if t in ('7a', '7a_nw', '7b', '7d'):
            cond = B(x, 37, 33)
            ok = self.cond_both(cond)
            if t != '7b' and comp and ok: self.compute(comp)
            if not ok: return None
            g, i, m, idis = B(x, 38, 38), B(x, 32, 30), B(x, 29, 27), B(x, 26, 24)
            if t == '7b': idis = B(x, 2, 0) if False else idis
            off = 8 if g else 0
            if t == '7d':
                breg, toby = B(x, 28, 28), B(x, 27, 27)
                if breg:
                    v = self.Bx[i + off]; self.Bx[i + off] = (v * 4 if toby else v // 4) & M32
                else:
                    v = self.I[i + off]; self.I[i + off] = (v * 4 if toby else v // 4) & M32
                return None
            base = self.I[i + off] & M32
            mv = s32(self.M[m + off])
            if t == '7a_nw': mv *= self._sc(base)
            elif t == '7a' and B(x, 23, 23): mv *= 2 if self._sc(base) == 4 else 1        # (SW)
            if idis:
                old = base
                self._update_i(i + off, base, mv)
                self.I[(i ^ idis) + off] = self.I[i + off]; self.I[i + off] = old
            else:
                self._update_i(i + off, base, mv)
            return None
        if t == '19a':
            sc = B(x, 41, 40)
            g, i, idis, data = B(x, 38, 38), B(x, 34, 32), B(x, 37, 35), sext(B(x, 31, 0), 32)
            if sc == 2 and B(x, 39, 39): raise NotImplementedError('BITREV')
            off = 8 if g else 0
            if sc == 1: data *= 4 if B(x, 39, 39) else 2          # (NW) / (SW)
            base = self.I[i + off] & M32
            if idis:
                self.I[(i ^ idis) + off] = (base + data) & M32
            else:
                self._update_i(i + off, base, data)
            return None
        if t in ('16a', '16b'):
            i, m, g = B(x, 43, 41), B(x, 40, 38), B(x, 37, 37)
            data = B(x, 31, 0) if t == '16a' else sext(B(x, 31, 16), 16) & M32
            a = self.dag_ea(i + (8 if g else 0), m + (8 if g else 0), False)
            self.mem.w(a, data)
            if self.simd():
                self.mem.w(a + 4, data)                     # immediate store broadcast to both SIMD lanes
            return None
        if t in ('9a', '9b'):
            rel, bl, la, cond = B(x, 40, 40), B(x, 39, 39), B(x, 38, 38), B(x, 37, 33)
            e = B(x, 25, 25) if t == '9a' else 0
            take = self.cond_both(cond)                 # evaluated before the compute changes flags
            if t == '9a' and comp: self.branch_compute(cond, comp, e)
            if take:
                if rel:
                    tgt = ins.target
                else:
                    ii, mm = B(x, 32, 30) + 8, B(x, 29, 27) + 8
                    tgt = (self.I[ii] + s32(self.M[mm])) & 0xFFFFFF
                return self.branch(tgt, ins, call=bool(bl), nxt=nxt, la=bool(la))
            return None
        if t == '25a':
            self.px.R[2], self.I[6] = self.I[6], self.I[7]
            return (ins.target,)
        if t in ('25c', '25a_rframe'):
            self.I[7] = self.I[6]
            self.I[6] = self.mem.r(self.I[6] & M32)
            return None
        if t == '18a':
            bop, sreg, data = B(x, 39, 37), B(x, 35, 32), B(x, 31, 0)
            u = 0x70 | sreg
            cur = self.get_ureg(u)
            if bop == 0: self.set_ureg(u, cur | data)
            elif bop == 1: self.set_ureg(u, cur & ~data)
            elif bop == 2: self.set_ureg(u, cur ^ data)
            elif bop in (4, 5):                         # BIT TST / XOR: BTF in ASTATx, and ASTATy in SIMD
                hit = (cur & data) == data if bop == 4 else cur == data
                for pe in ((self.px, self.py) if self.simd() else (self.px,)): self._set(pe, BTF=hit)
            else: raise NotImplementedError(f'18a bop {bop}')
            return None
        if t in ('12a_imm', '12a_ureg', '13a'):
            tgt = ins.target
            if self.pc not in self.loops_checked:            # PRM 4-42: last 11 loop instructions must be 48-bit ISA
                self.loops_checked.add(self.pc)
                import hwrules
                bad = [v for v in hwrules.loop_violations(self.img, ins, tgt) if v[0] != 'LOOP-CALL']   # stock has 2
                if bad:
                    self.hw_violations.append((self.pc, bad))
                    if self.strict_hw:
                        raise HWRuleError(f'loop @ {self.pc:#x}: {bad[:3]}')
            if t == '12a_imm': cnt = B(x, 39, 24)
            elif t == '12a_ureg': cnt = self.get_ureg(B(x, 38, 32))
            else:
                cnt = self.lcntr_next if B(x, 37, 33) == 15 else None       # DO .. UNTIL LCE uses LCNTR
            if cnt is not None:
                if cnt == 0: raise NotImplementedError(f'LCNTR = 0 @ {self.pc:#x}')
                self.loopstk.append([nxt, tgt, cnt, None])
            else:
                self.loopstk.append([nxt, tgt, None, B(x, 37, 33)])
            return None
        if t in ('21a/22a/26a', '21c/22c'):
            if 'IDLE' in ins.text: raise Stop(f'IDLE @ {self.pc:#x}')
            return None
        if t == '6a_mem':
            if self.cond_both(B(x, 37, 33)):
                i, m, g, d, dx, r = B(x, 43, 41), B(x, 40, 38), B(x, 32, 32), B(x, 31, 31), B(x, 30, 27), B(x, 26, 23)
                pre = (list(self.px.R), list(self.py.R))
                self.shift_imm(comp, dx)
                ii, mm = i + (8 if g else 0), m + (8 if g else 0)
                a = self.dag_ea(ii, mm, False)
                self._pairmove(a, r, d, pre, ii)
            return None
        if t in ('14a', '14d'):
            return self.exec14(ins, x, t)
        if t in ('1a', '1b'):
            if t == '1a' and comp: pre = (list(self.px.R), list(self.py.R))
            else: pre = (self.px.R, self.py.R)
            dmd, dmi, dmm, pmd, dmr, pmi, pmm, pmr = B(x, 44, 44), B(x, 43, 41), B(x, 40, 38), B(x, 37, 37), B(x, 36, 33), B(x, 32, 30), B(x, 29, 27), B(x, 26, 23)
            if t == '1a' and comp: self.compute(comp)
            a1 = self.dag_ea(dmi, dmm, False)
            a2 = self.dag_ea(pmi + 8, pmm + 8, False)
            self._pairmove(a1, dmr, dmd, pre, dmi)
            self._pairmove(a2, pmr, pmd, pre, pmi + 8)
            return None
        if t in ('5a_swap', '5b_swap'):
            cond = B(x, 37, 33)
            ok = self.cond_both(cond)
            if t == '5a_swap' and comp and ok: self.compute(comp)
            if ok:
                r, s = B(x, 26, 23), B(x, 41, 38)
                self.px.R[r], self.py.R[s] = self.py.R[s], self.px.R[r]
            return None
        if t == '20a':
            if B(x, 39, 39): self.loopstk.append(['push', 0, 0, None])
            if B(x, 38, 38):
                if self.loopstk: self.loopstk.pop()
            if B(x, 37, 37): self.ststk.append((self.px.ast, self.py.ast, self.sysr['MODE1']))
            if B(x, 36, 36):
                if self.ststk:
                    self.px.ast, self.py.ast, self.sysr['MODE1'] = self.ststk.pop()
            if B(x, 35, 35): self.pcstk.append(0)
            if B(x, 34, 34):
                if self.pcstk: self.pcstk.pop()
            return None
        if t in ('11a', '11c'):
            cond = B(x, 37, 33)
            e = B(x, 25, 25) if t == '11a' else 0
            take = self.cond_both(cond)
            if t == '11a' and comp: self.branch_compute(cond, comp, e)
            if take:
                if not self.pcstk: raise Stop(f'RTS with empty PC stack @ {self.pc:#x}')
                tgt = self.pcstk.pop()
                if 'RTI' in ins.text and self.ststk: pass
                return (tgt,) if '(DB)' in ins.text else tgt
            return None
        raise NotImplementedError(f'exec {t} @ {self.pc:#x}: {ins.text}')

    def _pairmove(self, a, r, d, pre, i=None):
        step = self.ystep(a, 4)
        if d:
            self.mem.w(a, pre[0][r])
            if self.simd(): self.mem.w(a + step, pre[1][r])
        else:
            self.px.R[r] = self.mem.r(a)
            if self.simd(): self.py.R[r] = self.mem.r(a + (0 if self.bcast(i) else step))

    def short_compute(self, op, n, r):
        pes = (self.px, self.py) if self.simd() else (self.px,)
        for pe in pes:
            R = pe.R
            if op == 0: R[n] = self._addsub(pe, R[n], R[r], 0)
            elif op == 1: R[n] = self._addsub(pe, R[n], R[r], 1)
            elif op == 2: R[n] = R[r]; self._fixflags(pe, R[n])
            elif op == 3:
                self._alu(pe, 0x0a, 0, n, r)
            elif op == 4: R[n] = (~R[r]) & M32; self._fixflags(pe, R[n])
            elif op == 5: R[n] = self._addsub(pe, R[r], 1, 0)
            elif op == 6: R[n] = self._addsub(pe, R[r], 1, 1)
            elif op == 7: R[n] = (s32(R[n]) * s32(R[r])) & M32
            elif op == 8: self._falu(pe, 0x81, n, n, r)
            elif op == 9: self._falu(pe, 0x82, n, n, r)
            elif op == 10: self._falu(pe, 0xca, n, r, 0)
            elif op == 11: self._falu(pe, 0x8a, 0, n, r)
            elif op == 12: R[n] = R[n] & R[r]; self._fixflags(pe, R[n])
            elif op == 13: R[n] = R[n] | R[r]; self._fixflags(pe, R[n])
            elif op == 14: R[n] = R[n] ^ R[r]; self._fixflags(pe, R[n])
            elif op == 15: self._mul(pe, 0x30, n, n, r)

    def _size3(self, x, t):
        if t == '3a':
            return 4, False, bool(B(x, 30, 30))
        if t == '3b':
            s = visa.SIZE_3B.get((B(x, 30, 30), B(x, 16, 16), B(x, 17, 17)))
        else:                                       # 3d: same (l, x, w) scheme with l = ex bit 18
            s = visa.SIZE_3B.get((B(x, 18, 18), B(x, 16, 16), B(x, 17, 17)))
        return self._sizes(s)

    @staticmethod
    def _sizes(s):
        if s is None: raise NotImplementedError('access size?')
        return {'': (4, False, False), ' (LW)': (4, False, True), ' (BW)': (1, False, False),
                ' (BWSE)': (1, True, False), ' (SW)': (2, False, False), ' (SWSE)': (2, True, False)}[s]

    def check_same_dag(self, ur, g, store, post):
        """PRM types 3/15: "the Ureg may not be from the same DAG (DAG1 or DAG2) as Ia/Mb". Stock code only ever
        breaks this with LOADS that do not update the index (pre-modify / immediate offset: ~1.2k sites), so those
        are tolerated; a same-DAG STORE or a post-modify LOAD `M4 = DM(I0, M6)` (builds with either were
        silent on HW) is an error. The interpreter would otherwise execute them 'correctly'."""
        grp, rn = ur >> 4, ur & 0xF
        if 1 <= grp <= 4 and (rn >= 8) == bool(g) and (store or post):
            v = ('DAG-SAME', self.pc, visa.ureg_name(ur), 'store' if store else 'post-modify load')
            if (self.pc, [v]) not in self.hw_violations:
                self.hw_violations.append((self.pc, [v]))
            if self.strict_hw:
                raise HWRuleError(f'same-DAG {v[3]} of {v[2]} @ {self.pc:#x} (PRM types 3/15)')

    def exec3(self, ins, x, t, comp):
        cond = B(x, 37, 33)
        u, i, m, g, d, ur = B(x, 44, 44), B(x, 43, 41), B(x, 40, 38), B(x, 32, 32), B(x, 31, 31), B(x, 29, 23)
        size, sx, lw = self._size3(x, t)
        self.check_same_dag(ur, g, store=bool(d), post=bool(u))
        ok = self.cond_both(cond)
        if not ok:
            return None
        # (the registers before the compute: only a data-register store reads them)
        preR = (list(self.px.R), list(self.py.R)) if (t == '3a' and comp and d and (ur >> 4) == 0) else None
        val = self.get_ureg(ur) if d else None
        if t == '3a' and comp: self.compute(comp)
        ii, mm = i + (8 if g else 0), m + (8 if g else 0)
        a = self.dag_ea(ii, mm, pre=not u, width=8 if lw else size)
        if d and preR is not None and (ur >> 4) == 0:
            self.mem.w(a, preR[0][ur & 0xF], size)
            if self.simd() and not lw: self.mem.w(a + self.ystep(a, size), preR[1][ur & 0xF], size)
            if lw: self.mem.w(a + 4, preR[0][(ur & 0xF) | 1]); self.mem.w(a, preR[0][ur & 0xE])
            return None
        self.dreg_access(a, None, d, size, sx, lw, ureg=ur)
        return None

    def exec4(self, ins, x, t, comp):
        cond = B(x, 37, 33)
        i, g, d, u, r = B(x, 43, 41), B(x, 40, 40), B(x, 39, 39), B(x, 38, 38), B(x, 26, 23)
        if t == '4a': size, sx, lw = 4, False, False
        elif t == '4b': size, sx, lw = self._sizes(visa.SIZE_4B.get((B(x, 18, 18), B(x, 16, 16), B(x, 17, 17))))
        else: size, sx, lw = self._sizes(visa.SIZE_4B.get((B(x, 18, 18), B(x, 16, 16), B(x, 17, 17))))
        ok = self.cond_both(cond)
        if not ok: return None
        preR = (list(self.px.R), list(self.py.R))
        if t == '4a' and comp: self.compute(comp)
        data = sext(B(x, 32, 27), 6)
        ii = i + (8 if g else 0)
        base = self.I[ii] & M32
        data *= (size if self._sc(base) == 4 else 1)
        if u:
            a = base; self._update_i(ii, base, data)
        else:
            a = (base + data) & M32
        if d:
            self.mem.w(a, preR[0][r], size)
            if self.simd(): self.mem.w(a + self.ystep(a, size), preR[1][r], size)
        else:
            self.px.R[r] = self.mem.r(a, size, sx)
            if self.simd(): self.py.R[r] = self.mem.r(a + (0 if self.bcast(ii) else self.ystep(a, size)), size, sx)
        return None

    def exec14(self, ins, x, t):
        if t == '14a':
            g, d, l, ur, addr = B(x, 41, 41), B(x, 40, 40), B(x, 39, 39), B(x, 38, 32), B(x, 31, 0)
            self.dreg_access(addr, None, d, 4, False, bool(l), ureg=ur)
            return None
        d, ex, l, w, xx, r, addr = B(x, 40, 40), B(x, 39, 39), B(x, 38, 38), B(x, 37, 37), B(x, 36, 36), B(x, 35, 32), B(x, 31, 0)
        if w:                                        # normal word, (LW) if l   (PRM Type 14d, see visa.py)
            self.dreg_access(addr, r, d, 4, False, bool(l)); return None
        self.dreg_access(addr, r, d, 2 if l else 1, bool(xx) and not d)
        return None


def s80(v):
    v &= (1 << 80) - 1
    return v - (1 << 80) if v & (1 << 79) else v


# ---------------------------------------------------------------------- snapshots
def save_state(s, path):
    import pickle, gzip
    st = dict(words=s.mem.words, mmr=s.mem.mmr, px=vars(s.px), py=vars(s.py), I=s.I, M=s.M, L=s.L, Bx=s.Bx,
              sysr=s.sysr, PX1=s.PX1, PX2=s.PX2, pc=s.pc, pcstk=s.pcstk, loopstk=s.loopstk, ststk=s.ststk,
              pending=s.pending, icount=s.icount, lcntr=getattr(s, 'lcntr_next', 0))
    with gzip.open(path, 'wb', compresslevel=3) as f:
        pickle.dump(st, f, protocol=4)


def load_state(path, image=None, cls=None):
    import pickle, gzip
    with gzip.open(path, 'rb') as f:
        st = pickle.load(f)
    s = (cls or Sim)(image)
    s.mem.words = st['words']; s.mem.mmr = st['mmr']
    s.px.__dict__.update(st['px']); s.py.__dict__.update(st['py'])
    s.I, s.M, s.L, s.Bx = st['I'], st['M'], st['L'], st['Bx']
    s.sysr = st['sysr']; s.PX1, s.PX2 = st['PX1'], st['PX2']
    s.pc, s.pcstk, s.loopstk, s.ststk = st['pc'], st['pcstk'], st['loopstk'], st['ststk']
    s.pending, s.icount, s.lcntr_next = st['pending'], st['icount'], st['lcntr']
    return s


# ---------------------------------------------------------------------- rough DDR cache model (estimates only)
class CacheModel:
    """Set-associative LRU model of the SHARC+ L1 data cache for DDR (addr >= 0x80000000) accesses.
    Counts misses so a block's cost can be estimated as insns + misses * penalty. Parameters are ASSUMPTIONS
    (ADSP-2156x: 64-byte lines; size/ways configurable by the firmware) — use for relative comparisons."""
    def __init__(self, size=32 * 1024, line=64, ways=2):
        self.line, self.ways = line, ways
        self.sets = size // (line * ways)
        self.tags = [[] for _ in range(self.sets)]
        self.misses = 0; self.accesses = 0

    def access(self, a):
        if a < 0x80000000: return
        self.accesses += 1
        ln = a // self.line
        s = self.tags[ln % self.sets]
        if ln in s:
            s.remove(ln); s.append(ln); return
        self.misses += 1
        s.append(ln)
        if len(s) > self.ways: s.pop(0)


def attach_cache(s, **kw):
    cm = CacheModel(**kw)
    r0, w0 = s.mem.r, s.mem.w
    def r(a, size=4, sx=False):
        cm.access(a & M32); return r0(a, size, sx)
    def w(a, v, size=4):
        cm.access(a & M32); return w0(a, v, size)
    s.mem.r, s.mem.w = r, w
    return cm
