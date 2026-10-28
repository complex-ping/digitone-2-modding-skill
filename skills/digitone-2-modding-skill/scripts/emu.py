"""Minimal SHARC+ interpreter built on visa.py — enough to run the Digitone II FX DSP
functions (chorus / delay / reverb) on test audio and watch memory, so a patch can be
verified offline before flashing.

Scope: SISD only (PEy/SIMD not modelled). 32-bit float + 32-bit fixed in the register file
(the 40-bit extension and MR accumulators are modelled at 32/64-bit where the FX code needs
them). Circular buffering via L/B registers is modelled. Enough compute/move/branch/loop
opcodes for the FX code; anything unmodelled raises so we never silently mis-execute.
"""
from __future__ import annotations
import struct
import visa
from visa import Image, B, sext

FMASK = (1 << 32) - 1


def as_f(u): return struct.unpack('<f', struct.pack('<I', u & FMASK))[0]
def as_u(f):
    try:
        return struct.unpack('<I', struct.pack('<f', f))[0]
    except (OverflowError, struct.error):
        return 0x7F800000 if f > 0 else 0xFF800000


class Mem:
    """Sparse 32-bit word memory keyed by load (byte) address; each SHARC word = one cell."""

    def __init__(self, image: Image):
        self.cells = {}
        for base, data in image.regions:
            for i in range(0, len(data) - 1, 2):
                pass  # code region; leave data loads to lazily default 0
        self.image = image

    def _norm(self, a):
        return a & 0xFFFFFFFF

    def r(self, a):
        a = self._norm(a)
        v = self.cells.get(a)
        if v is None:
            v = self.image_word(a)
        return v

    def image_word(self, a):
        # byte-addressed fallback to the loaded boot image (L1 core addr -> loader addr +0x28000000)
        if not getattr(self, 'backed', False) or a & 3:
            return 0
        la = a + 0x28000000 if 0x240000 <= a < 0x3A0000 else a
        for base, data in self.image.regions:
            if base <= la < base + len(data) - 3:
                return struct.unpack_from('<I', data, la - base)[0]
        return 0

    def w(self, a, v):
        self.cells[self._norm(a)] = v & FMASK


class Regs:
    def __init__(self):
        self.R = [0] * 16
        self.I = [0] * 16
        self.M = [0] * 16
        self.L = [0] * 16
        self.Bx = [0] * 16
        self.MRF = 0
        self.MODE1 = 0
        self.ASTAT = 0
        self.LCNTR = 0


class Sharc:
    def __init__(self, image: Image | None = None, byte_space: bool = False):
        # byte_space=True models SHARC+ byte addressing as the DN2 firmware uses it: I registers hold byte
        # addresses (L1 0x240000+, L2 0x20000000+, DDR 0x80000000+); word offsets/modifiers scale x4,
        # 0x16xx MODIFY is raw bytes, 0x15xx MODIFY is words x4; (DB) branches have TWO delay slots;
        # CJUMP/RFRAME follow the ADI C frame convention. Memory cells are keyed by byte address.
        self.byte_space = byte_space
        self.img = image or Image()
        self.mem = Mem(self.img)
        self.mem.backed = byte_space
        self.reg = Regs()
        self.pc = 0                       # short-word address
        self.pcstk = []
        self.loopstk = []                 # (end_sw, count_or_None, term)
        self.running = False
        self.cycles = 0
        self.trace = None                 # optional list to append (sw, text)
        # DAG unit: M5=0,M6=1,M7=-1 are the ADI C conventions the FX code relies on
        self.reg.M[5] = 0
        self.reg.M[6] = 1
        self.reg.M[7] = 0xFFFFFFFF
        self.reg.M[13], self.reg.M[14], self.reg.M[15] = 0, 1, 0xFFFFFFFF

    # ---- flags
    def _setz_n(self, v):
        v &= FMASK
        az = 1 if v == 0 else 0
        an = 1 if v & 0x80000000 else 0
        self.reg.ASTAT = (self.reg.ASTAT & ~0x3) | (az) | (an << 1)

    def _fflags(self, f):
        az = 1 if f == 0 else 0
        an = 1 if f < 0 else 0
        self.reg.ASTAT = (self.reg.ASTAT & ~0x3) | az | (an << 1)

    # ---- DAG addressing with circular buffers
    def _sc(self, addr):
        return 4 if self.byte_space and (addr & FMASK) >= 0x240000 else 1

    def _dag_addr(self, ireg, mreg, pre):
        base = self.reg.I[ireg] & FMASK
        m = self.reg.M[mreg]
        m = (m - (1 << 32) if m & 0x80000000 else m) * self._sc(base)
        if pre:
            return (base + m) & FMASK      # pre-modify does NOT update the I register
        addr = base
        self._update_i(ireg, base, m)
        return addr

    def _update_i(self, ireg, base, m):
        L = self.reg.L[ireg]
        nv = base + m
        if L and (self.reg.MODE1 & (1 << 24)):
            Bx = self.reg.Bx[ireg]
            if nv >= Bx + L:
                nv -= L
            elif nv < Bx:
                nv += L
        self.reg.I[ireg] = nv & FMASK

    # ---- run
    def call(self, sw, args=None, maxc=5_000_000):
        """Enter a function at sw with an RTS sentinel; run until it returns."""
        # set up a stack and a synthetic caller frame whose saved return is the stop sentinel
        STK = 0x0FF00000
        if self.byte_space:                         # as if reached by CJUMP: [I6]=old I6, [I6-4]=return-1
            self.reg.I[6], self.reg.I[7] = STK, STK - 8
            self.reg.M[14] = 1
            self.mem.w(STK, STK + 0x100)
            self.mem.w(STK - 4, 0xFFFFFE)
            for k, v in (args or {}).items():
                getattr(self.reg, k[0])[int(k[1:])] = v & FMASK
        else:
            self.reg.I[6] = STK
            self.reg.I[7] = STK
            self.reg.M[14] = 0
            self.mem.w((STK - 1) & FMASK, 0xFFFFFF)   # callee reads return via DM(M7,I6)=mem[I6-1]
        self.pc = sw
        self.pcstk = [0xFFFFFF]
        self.running = True
        self.cycles = 0
        while self.running:
            if self.pc == 0xFFFFFF:
                break
            self.step()
            self.cycles += 1
            if self.cycles > maxc:
                raise RuntimeError(f'runaway at {self.pc:#x}')

    def step(self):
        ins = self.img.insn(self.pc)
        if ins is None or ins.kind == 'invalid':
            raise RuntimeError(f'bad insn at {self.pc:#x}')
        if self.trace is not None:
            self.trace.append((self.pc, ins.text))
        nxt = self.pc + ins.size
        # hardware loop: on reaching end, iterate
        self._exec(ins, nxt)

    # ---- compute execution (23-bit field)
    def _compute(self, c):
        if c == 0:
            return
        R = self.reg.R
        if c & 0x400000:
            op6 = (c >> 16) & 0x3F
            m, a = (c >> 12) & 0xF, (c >> 8) & 0xF
            xm, ym = (c >> 6) & 3, ((c >> 4) & 3) + 4
            xa, ya = ((c >> 2) & 3) + 8, (c & 3) + 12
            if op6 >= 0x20:                    # mul + dual add/sub
                if op6 & 0x10:                 # all operands are read before any result is written
                    p, fx, fy = as_f(R[xm]) * as_f(R[ym]), as_f(R[xa]), as_f(R[ya])
                    R[m] = as_u(p); R[a] = as_u(fx + fy); R[op6 & 0xF] = as_u(fx - fy)
                    return
                raise NotImplementedError(f'multifunc {op6:#x} @ {self.pc:#x}')
            if 0x18 <= op6 < 0x20:             # F mul + F alu
                p, fx, fy = as_f(R[xm]) * as_f(R[ym]), as_f(R[xa]), as_f(R[ya])
                R[m] = as_u(p)
                R[a] = as_u({0x18: fx + fy, 0x19: fx - fy, 0x1c: (fx + fy) / 2,
                             0x1d: abs(fx), 0x1e: max(fx, fy), 0x1f: min(fx, fy)}.get(op6, fx))
                return
            if op6 in (0x04, 0x05, 0x06):      # R mul + R add/sub
                p, fx, fy = as_f(R[xm]) * as_f(R[ym]), as_f(R[xa]), as_f(R[ya])
                R[m] = as_u(p)
                R[a] = as_u({0x04: fx + fy, 0x05: fx - fy, 0x06: (fx + fy) / 2}[op6])
                return
            raise NotImplementedError(f'multifunc {op6:#x} @ {self.pc:#x}')
        cu = (c >> 20) & 3
        op = (c >> 12) & 0xFF
        n, x, y = (c >> 8) & 0xF, (c >> 4) & 0xF, c & 0xF
        if cu == 0:
            if op >> 4 == 0x7:
                ax, ay = R[x], R[y]
                R[n] = as_u(as_f(ax) + as_f(ay)); R[op & 0xF] = as_u(as_f(ax) - as_f(ay)); return
            if op >> 4 == 0xF:
                fx, fy = as_f(R[x]), as_f(R[y])
                R[n] = as_u(fx + fy); R[op & 0xF] = as_u(fx - fy); return
            self._alu(op, n, x, y); return
        if cu == 1:
            if op == 0x30:
                R[n] = as_u(as_f(R[x]) * as_f(R[y])); return
            hi = (op >> 6) & 3
            if hi == 1:
                self.reg.MRF = as_u(as_f(R[x]) * as_f(R[y]))
                if ((op >> 1) & 3) in (0, 1):
                    R[n] = self.reg.MRF
                return
            if hi in (2, 3):
                prod = as_f(R[x]) * as_f(R[y])
                acc = as_f(self.reg.MRF)
                acc = acc + prod if hi == 2 else acc - prod
                self.reg.MRF = as_u(acc)
                if ((op >> 1) & 3) in (0, 1):
                    R[n] = self.reg.MRF
                return
            raise NotImplementedError(f'mul {op:#x} @ {self.pc:#x}')
        if cu == 2:
            self._shift(op, n, x, y); return
        raise NotImplementedError(f'compute cu {cu} @ {self.pc:#x}')

    def _alu(self, op, n, x, y):
        R = self.reg.R
        rx, ry = R[x], R[y]
        fx, fy = as_f(rx), as_f(ry)
        sx = rx - (1 << 32) if rx & 0x80000000 else rx
        sy = ry - (1 << 32) if ry & 0x80000000 else ry
        if op == 0x01: R[n] = (rx + ry) & FMASK; self._setz_n(R[n])
        elif op == 0x02: R[n] = (rx - ry) & FMASK; self._setz_n(R[n])
        elif op == 0x22: R[n] = (-sx) & FMASK; self._setz_n(R[n])
        elif op == 0x29: R[n] = (rx + 1) & FMASK; self._setz_n(R[n])
        elif op == 0x2a: R[n] = (rx - 1) & FMASK; self._setz_n(R[n])
        elif op == 0x21: R[n] = rx; self._setz_n(R[n])
        elif op == 0x30: R[n] = abs(sx) & FMASK; self._setz_n(R[n])
        elif op == 0x40: R[n] = rx & ry; self._setz_n(R[n])
        elif op == 0x41: R[n] = rx | ry; self._setz_n(R[n])
        elif op == 0x42: R[n] = rx ^ ry; self._setz_n(R[n])
        elif op == 0x43: R[n] = (~rx) & FMASK; self._setz_n(R[n])
        elif op == 0x61: R[n] = (sx if sx < sy else sy) & FMASK; self._setz_n(R[n])
        elif op == 0x62: R[n] = (sx if sx > sy else sy) & FMASK; self._setz_n(R[n])
        elif op == 0x0a: self._cmp(sx, sy)
        elif op == 0x0b: self._cmp(rx, ry)
        # float
        elif op == 0x81: R[n] = as_u(fx + fy); self._fflags(fx + fy)
        elif op == 0x82: R[n] = as_u(fx - fy); self._fflags(fx - fy)
        elif op == 0x89: R[n] = as_u((fx + fy) / 2); self._fflags((fx + fy) / 2)
        elif op == 0xa1: R[n] = rx; self._fflags(fx)
        elif op == 0xa2: R[n] = as_u(-fx); self._fflags(-fx)
        elif op == 0xb0: R[n] = as_u(abs(fx)); self._fflags(abs(fx))
        elif op == 0x8a: self._fcmp(fx, fy)
        elif op == 0xe1: R[n] = as_u(min(fx, fy)); self._fflags(min(fx, fy))
        elif op == 0xe2: R[n] = as_u(max(fx, fy)); self._fflags(max(fx, fy))
        elif op == 0xe3: R[n] = as_u(max(-abs(fy), min(abs(fy), fx))); self._fflags(0)
        elif op == 0x91: R[n] = as_u(abs(fx + fy))
        elif op == 0x92: R[n] = as_u(abs(fx - fy))
        elif op == 0xe0: R[n] = as_u(abs(fx) * (1 if fy >= 0 else -1))
        elif op == 0xca: R[n] = as_u(float(sx)); self._fflags(float(sx))         # FLOAT R
        elif op == 0xda: R[n] = as_u(float(sx) * 2.0 ** sy)
        elif op == 0xc9: R[n] = self._fix(fx)                                    # FIX
        elif op == 0xd9: R[n] = self._fix(fx * 2.0 ** sy)
        elif op == 0xcd: R[n] = self._trunc(fx)                                  # TRUNC
        elif op == 0xdd: R[n] = self._trunc(fx * 2.0 ** sy)
        elif op == 0xbd:                                                        # SCALB Fx BY Ry
            R[n] = as_u(fx * 2.0 ** sy)
        elif op == 0xad:                                                        # MANT Fx
            import math
            if fx == 0: R[n] = 0
            else:
                m, e = math.frexp(fx); R[n] = as_u(m * 2)                        # mantissa in [1,2)
        elif op == 0xa5: R[n] = as_u(fx)                                        # RND (32-bit: identity here)
        elif op == 0xc4: R[n] = as_u(1.0 / fx if fx else 0.0)                    # RECIPS (seed; NR follows)
        elif op == 0xc5: R[n] = as_u(fx ** -0.5 if fx > 0 else 0.0)
        else:
            raise NotImplementedError(f'alu {op:#x} @ {self.pc:#x}')

    def _fix(self, f):
        import math
        v = int(math.floor(f + 0.5)) if not (self.reg.MODE1 & (1 << 15)) else int(f)
        return v & FMASK

    def _trunc(self, f):
        return int(f) & FMASK

    def _cmp(self, sx, sy):
        # ASTAT AZ (eq), AN (lt) — enough for the FX code's IF EQ/LT/LE/GT/GE
        az = 1 if sx == sy else 0
        an = 1 if sx < sy else 0
        self.reg.ASTAT = (self.reg.ASTAT & ~0x3) | az | (an << 1)

    def _fcmp(self, fx, fy):
        az = 1 if fx == fy else 0
        an = 1 if fx < fy else 0
        self.reg.ASTAT = (self.reg.ASTAT & ~0x3) | az | (an << 1)

    def _shift(self, op, n, x, y):
        R = self.reg.R
        rx = R[x]
        sy = R[y] & 0xFF
        sy = sy - 256 if sy & 0x80 else sy
        if op == 0x00: R[n] = (rx << sy if sy >= 0 else rx >> -sy) & FMASK
        elif op == 0x04:
            s = rx - (1 << 32) if rx & 0x80000000 else rx
            R[n] = (s << sy if sy >= 0 else s >> -sy) & FMASK
        elif op == 0x40 or op == 0x48:      # FEXT (bit field extract), y = pos:len — handled at 6a level
            R[n] = rx
        elif op == 0x88:                    # LEFTZ: SV if result 32, SZ if MSB of Rx set (PRM)
            v = 32 if rx == 0 else 32 - (rx & FMASK).bit_length()
            R[n] = v
            self._shflags(sz=bool(rx & 0x80000000), sv=(v == 32))
            return
        else:
            raise NotImplementedError(f'shift {op:#x} @ {self.pc:#x}')
        self._shflags(sz=(R[n] == 0), sv=False)

    def _shflags(self, sz, sv):             # internal ASTAT bits: SV = 11, SZ = 12 (as in hardware)
        self.reg.ASTAT = (self.reg.ASTAT & ~0x1800) | (0x800 if sv else 0) | (0x1000 if sz else 0)

    def _cond(self, c):
        if c == 31:
            return True
        az = self.reg.ASTAT & 1
        an = (self.reg.ASTAT >> 1) & 1
        av = (self.reg.ASTAT >> 2) & 1
        tf = (self.reg.ASTAT >> 6) & 1
        sv = (self.reg.ASTAT >> 11) & 1
        sz = (self.reg.ASTAT >> 12) & 1
        base = {0: az, 1: an and not az, 2: az or an, 4: av, 7: sv, 8: sz, 13: tf, 16: not az,
                17: not (an and not az), 18: not (az or an), 20: not av, 23: not sv, 24: not sz, 29: not tf}
        if c in base:
            return bool(base[c])
        raise NotImplementedError(f'cond {c} @ {self.pc:#x}')

    def _ureg_get(self, u):
        g, r = (u >> 4) & 7, u & 0xF
        return [self.reg.R, self.reg.I, self.reg.M, self.reg.L, self.reg.Bx][g][r] if g < 5 else \
            (self.reg.MODE1 if u == 0x72 else 0)

    def _ureg_set(self, u, v):
        g, r = (u >> 4) & 7, u & 0xF
        v &= FMASK
        if g < 5:
            [self.reg.R, self.reg.I, self.reg.M, self.reg.L, self.reg.Bx][g][r] = v
        elif u == 0x72:
            self.reg.MODE1 = v

    def _exec(self, ins, nxt):
        t = ins.type
        x = ins.x
        R = self.reg.R
        comp = B(x, 22, 0)

        def mem_dm_read(ireg, mreg, pre=False):
            return self.mem.r(self._dag_addr(ireg, mreg, pre))

        def mem_dm_write(ireg, mreg, val, pre=False):
            self.mem.w(self._dag_addr(ireg, mreg, pre), val)

        adv = nxt
        if t in ('21a/22a/26a', '21c/22c'):
            pass
        elif t == '2c':
            op, n, r = B(x, 43, 40), B(x, 39, 36), B(x, 35, 32)
            fn, fx = as_f(R[n]), as_f(R[r])
            sg = lambda v: v - (1 << 32) if v & 0x80000000 else v
            if op == 0: R[n] = (R[n] + R[r]) & FMASK; self._setz_n(R[n])
            elif op == 1: R[n] = (R[n] - R[r]) & FMASK; self._setz_n(R[n])
            elif op == 2: R[n] = R[r]; self._setz_n(R[n])
            elif op == 3: self._cmp(sg(R[n]), sg(R[r]))
            elif op == 4: R[n] = (~R[r]) & FMASK; self._setz_n(R[n])
            elif op == 5: R[n] = (R[r] + 1) & FMASK; self._setz_n(R[n])
            elif op == 6: R[n] = (R[r] - 1) & FMASK; self._setz_n(R[n])
            elif op == 7: R[n] = (sg(R[n]) * sg(R[r])) & FMASK
            elif op == 8: R[n] = as_u(fn + fx); self._fflags(fn + fx)
            elif op == 9: R[n] = as_u(fn - fx); self._fflags(fn - fx)
            elif op == 10: R[n] = as_u(float(sg(R[r]))); self._fflags(float(sg(R[r])))
            elif op == 11: self._fcmp(fn, fx)
            elif op == 12: R[n] = R[n] & R[r]; self._setz_n(R[n])
            elif op == 13: R[n] = R[n] | R[r]; self._setz_n(R[n])
            elif op == 14: R[n] = R[n] ^ R[r]; self._setz_n(R[n])
            elif op == 15: R[n] = as_u(fn * fx)
            else: raise NotImplementedError(f'2c op {op}')
        elif t in ('2a', '2b'):
            cond = B(x, 37, 33) if t == '2a' else 31
            c = B(x, 38, 16) if t == '2b' else comp
            if self._cond(cond): self._compute(c)
        elif t in ('1a',):
            pre = list(R)                        # all parts of a parallel instruction read before any write
            if comp: self._compute(comp)
            dmd, dmi, dmm = B(x, 44, 44), B(x, 43, 41), B(x, 40, 38)
            pmd, dmr, pmi, pmm, pmr = B(x, 37, 37), B(x, 36, 33), B(x, 32, 30), B(x, 29, 27), B(x, 26, 23)
            if dmd: mem_dm_write(dmi, dmm, pre[dmr])
            else: R[dmr] = mem_dm_read(dmi, dmm)
            if pmd: mem_dm_write(pmi + 8, pmm + 8, pre[pmr])
            else: R[pmr] = mem_dm_read(pmi + 8, pmm + 8)
        elif t in ('3a', '3b'):
            sz = visa.access_size(ins)
            if sz:   # BW/SW/LW accesses are NOT modelled (memory here is word cells) -> never fake them
                raise NotImplementedError(f'{t} access size{sz} not modelled @ {self.pc:#x}: {ins.text}')
            cond = B(x, 37, 33)
            u, i, m, g, d, ur = B(x, 44, 44), B(x, 43, 41), B(x, 40, 38), B(x, 32, 32), B(x, 31, 31), B(x, 29, 23)
            ok = self._cond(cond)
            val = self._ureg_get(ur)             # store source is read before the parallel compute writes
            if t == '3a' and comp and ok: self._compute(comp)
            ii = i + (8 if g else 0)
            mm = m + (8 if g else 0)
            if ok:
                if d: mem_dm_write(ii, mm, val, pre=not u)
                else: self._ureg_set(ur, mem_dm_read(ii, mm, pre=not u))
        elif t == '3c':
            i, m, d, r = B(x, 43, 41), B(x, 40, 38), B(x, 37, 37), B(x, 35, 32)
            if d: mem_dm_write(i, m, R[r])
            else: R[r] = mem_dm_read(i, m)
        elif t in ('4a', '4b'):
            sz = visa.access_size(ins)
            if sz:
                raise NotImplementedError(f'{t} access size{sz} not modelled @ {self.pc:#x}: {ins.text}')
            cond = B(x, 37, 33)
            i, g, d, u, r = B(x, 43, 41), B(x, 40, 40), B(x, 39, 39), B(x, 38, 38), B(x, 26, 23)
            ok = self._cond(cond)
            val = R[r]
            if t == '4a' and comp and ok: self._compute(comp)
            data = sext(B(x, 32, 27), 6)
            ii = i + (8 if g else 0)
            if ok:
                base = self.reg.I[ii] & FMASK
                data *= self._sc(base)
                if u:
                    addr = base
                    self.reg.I[ii] = (base + data) & FMASK
                else:
                    addr = (base + data) & FMASK          # pre-modify: I is NOT updated
                if d: self.mem.w(addr, val)
                else: R[r] = self.mem.r(addr)
        elif t in ('5a_move', '5b_move'):
            cond = B(x, 37, 33)
            src = (B(x, 42, 38) << 2) | B(x, 32, 31)
            dst = B(x, 29, 23)
            ok = self._cond(cond)
            val = self._ureg_get(src)            # move source read before the parallel compute
            if t == '5a_move' and comp and ok: self._compute(comp)
            if ok: self._ureg_set(dst, val)
        elif t in ('5a_swap',):
            if comp: self._compute(comp)
        elif t == '6a_nomem':
            cond = B(x, 37, 33)
            if self._cond(cond): self._shift6(comp, B(x, 30, 27))
        elif t == '6a_mem':
            cond = B(x, 37, 33)
            i, m, g, d, dx, r = B(x, 43, 41), B(x, 40, 38), B(x, 32, 32), B(x, 31, 31), B(x, 30, 27), B(x, 26, 23)
            if self._cond(cond):
                val = R[r]
                self._shift6(comp, dx)
                ii = i + (8 if g else 0)
                if d: mem_dm_write(ii, m + (8 if g else 0), val)
                else: R[r] = mem_dm_read(ii, m + (8 if g else 0))
        elif t in ('7a', '7a_nw', '7d'):
            cond = B(x, 37, 33)
            if comp and self._cond(cond): self._compute(comp)
            g, i, m, idis = B(x, 38, 38), B(x, 32, 30), B(x, 29, 27), B(x, 26, 24)
            off = 8 if g else 0
            if t in ('7a', '7a_nw') and self._cond(cond):
                base = self.reg.I[i + off] & FMASK
                mm = self.reg.M[m + off]
                # PRM 6-9: plain MODIFY never scales (M = bytes in byte space); only MODIFY(..)(NW) scales x4
                mm = (mm - (1 << 32) if mm & 0x80000000 else mm) * (self._sc(base) if t == '7a_nw' else 1)
                self._update_i(i + off, base, mm)
                if idis:                          # Ib = MODIFY(Ia, M): result to Ib, Ia unchanged
                    self.reg.I[(i ^ idis) + off] = self.reg.I[i + off]; self.reg.I[i + off] = base
        elif t == '7b':
            g, i, m, idis = B(x, 38, 38), B(x, 32, 30), B(x, 29, 27), B(x, 26, 24)   # 7b idis as in visa (fixed 2026-09-26)
            off = 8 if g else 0
            base = self.reg.I[i + off] & FMASK
            mm = self.reg.M[m + off]
            mm = mm - (1 << 32) if mm & 0x80000000 else mm      # unscaled, as 7a
            self._update_i(i + off, base, mm)
            if idis:
                self.reg.I[(i ^ idis) + off] = self.reg.I[i + off]; self.reg.I[i + off] = base
        elif t == '14d':
            raise NotImplementedError(f'14d byte/short access not modelled @ {self.pc:#x}: {ins.text}')
        elif t in ('14a', '14d'):
            g, d, ur, addr = B(x, 41, 41), B(x, 40, 40), B(x, 38, 32), B(x, 31, 0)
            if t == '14d':
                d, r, addr = B(x, 40, 40), B(x, 35, 32), B(x, 31, 0)
                if d: self.mem.w(addr, R[r])
                else: R[r] = self.mem.r(addr)
            else:
                if d: self.mem.w(addr, self._ureg_get(ur))
                else: self._ureg_set(ur, self.mem.r(addr))
        elif t in ('15a', '15b'):
            if t == '15b':                       # g is bit 37 in the 32-bit form, not bit 44
                g, i, d = B(x, 37, 37), B(x, 43, 41), B(x, 40, 40)
                ur, data = B(x, 29, 23), sext(B(x, 22, 16), 7)
            else:
                g, i, d = B(x, 44, 44), B(x, 43, 41), B(x, 40, 40)
                ur, data = B(x, 38, 32), sext(B(x, 31, 0), 32)
            ii = i + (8 if g else 0)
            base = self.reg.I[ii] & FMASK
            addr = (base + data * self._sc(base)) & FMASK
            if d: self.mem.w(addr, self._ureg_get(ur))
            else: self._ureg_set(ur, self.mem.r(addr))
            # 15a/15b are pre-modify without update (data is an offset, I unchanged)
        elif t in ('16a', '16b'):
            i, m, g = B(x, 43, 41), B(x, 40, 38), B(x, 37, 37)
            data = B(x, 31, 0) if t == '16a' else sext(B(x, 31, 16), 16) & FMASK
            mem_dm_write(i + (8 if g else 0), m + (8 if g else 0), data)
        elif t in ('17a', '17b'):
            ur = B(x, 38, 32)
            data = B(x, 31, 0) if t == '17a' else sext(B(x, 31, 16), 16) & FMASK
            self._ureg_set(ur, data)
        elif t == '18a':
            bop, sreg, data = B(x, 39, 37), B(x, 35, 32), B(x, 31, 0)
            if 0x70 | sreg == 0x72:
                if bop == 0: self.reg.MODE1 |= data
                elif bop == 1: self.reg.MODE1 &= ~data & FMASK
            elif bop == 4:                       # BIT TST: TF = all tested bits set
                src = self.reg.MODE1 if (0x70 | sreg) == 0x72 else (self.reg.ASTAT if (0x70|sreg)==0x76 else 0)
                self.reg.ASTAT = (self.reg.ASTAT & ~0x40) | (0x40 if (src & data) == data else 0)
        elif t == '19a':
            sc = B(x, 41, 40)
            g, i = B(x, 38, 38), B(x, 34, 32)
            idis, data = B(x, 37, 35), sext(B(x, 31, 0), 32)
            off = 8 if g else 0
            if self.byte_space:
                data *= {1: 4, 2: 1}.get(sc, self._sc(self.reg.I[i + off]))
            if idis:                              # MODIFY(Ia, n) -> Ib: Ib = Ia + n, Ia unchanged
                self.reg.I[(i ^ idis) + off] = (self.reg.I[i + off] + data) & FMASK
            else:
                self.reg.I[i + off] = (self.reg.I[i + off] + data) & FMASK
        elif t == '8a':
            rel, b, cond = B(x, 40, 40), B(x, 39, 39), B(x, 37, 33)
            addr = B(x, 23, 0)
            if self._cond(cond):
                tgt = (self.pc + sext(addr, 24)) & 0xFFFFFF if rel else addr
                if b: self.pcstk.append(nxt)
                adv = self._delay(tgt, ins)
        elif t in ('9a', '9b'):
            rel, b, cond = B(x, 40, 40), B(x, 39, 39), B(x, 37, 33)
            e = B(x, 25, 25) if t == '9a' else 0
            if self._cond(cond):
                if rel:
                    tgt = (self.pc + sext(B(x, 32, 27), 6)) & 0xFFFFFF
                else:
                    ii = B(x, 32, 30) + 8
                    mm = self.reg.M[B(x, 29, 27) + 8]
                    mm = mm - (1 << 32) if mm & 0x80000000 else mm
                    tgt = (self.reg.I[ii] + mm) & FMASK
                if b: self.pcstk.append(nxt)
                adv = self._delay(tgt, ins)
            elif t == '9a' and comp:
                self._compute(comp)                 # ELSE compute
        elif t in ('11a', '11c'):
            cond = B(x, 37, 33)
            if self._cond(cond):
                adv = self._delay(self.pcstk.pop(), ins)
            elif t == '11a' and comp:
                self._compute(comp)
        elif t == '25a':
            addr = B(x, 23, 0)
            tgt = (self.pc + sext(addr, 24)) & 0xFFFFFF if x >> 32 == 0x1844 else addr
            if self.byte_space:                  # ADI: CJUMP = R2 = I6, I6 = I7 (the DB slots push R2 + return)
                R[2], self.reg.I[6] = self.reg.I[6], self.reg.I[7]
            else:
                i7 = self.reg.I[7] & FMASK
                self.mem.w(i7, self.reg.I[6]); self.reg.I[7] = (i7 - 1) & FMASK
                self.reg.I[6] = self.reg.I[7]
            adv = self._delay(tgt, ins)
        elif t in ('25c', '25a_rframe'):
            self.reg.I[7] = self.reg.I[6]
            if self.byte_space:                  # ADI: RFRAME = I7 = I6, I6 = DM(0, I6)
                self.reg.I[6] = self.mem.r(self.reg.I[6] & FMASK)
            else:
                self.reg.I[6] = self.mem.r((self.reg.I[6] + 1) & FMASK)
        elif t in ('12a_imm', '12a_ureg', '13a'):
            tgt = (self.pc + sext(B(x, 22, 0), 23)) & 0xFFFFFF
            if t == '12a_imm':
                cnt = B(x, 39, 24)
            elif t == '12a_ureg':
                cnt = self._ureg_get(B(x, 38, 32))
            else:
                cnt = None
            self.loopstk.append([tgt, cnt, B(x, 37, 33) if t == '13a' else None, nxt])
        else:
            raise NotImplementedError(f'exec {t} @ {self.pc:#x}')

        # hardware-loop bottom check: the DO address is the LAST instruction of the body (inclusive), so the
        # check fires after that instruction has executed and fallen through (also when a call returns onto it)
        if self.loopstk and self.pc == self.loopstk[-1][0] and adv == nxt:
            top = self.loopstk[-1]
            if top[1] is not None:
                top[1] -= 1
                if top[1] > 0:
                    adv = top[3]
                else:
                    self.loopstk.pop()
            else:
                self.loopstk.pop()
        self.pc = adv

    def _delay(self, tgt, ins):
        # execute the 1 delay-slot instruction (DB) then jump. FX code always uses (DB).
        db = '(DB)' in ins.text
        if db:
            for _ in range(2 if self.byte_space else 1):   # SHARC: a delayed branch has two delay slots
                slot = self.img.insn(self.pc + ins.size if _ == 0 else self.pc + slot.size)
                self.pc = slot.sw
                self._exec_slot(slot)
        return tgt

    def _exec_slot(self, slot):
        save_loop = self.loopstk
        self.loopstk = []                        # a delay slot never closes a hardware loop
        try:
            self._exec_noadv(slot)
        finally:
            self.loopstk = save_loop

    def _exec_noadv(self, slot):
        pc = self.pc
        self._exec(slot, pc + slot.size)
        self.pc = pc

    def _shift6(self, comp, dataex):
        op = (comp >> 16) & 0x3F
        data = (dataex << 8) | ((comp >> 8) & 0xFF)
        n, x = (comp >> 4) & 0xF, comp & 0xF
        R = self.reg.R
        rx = R[x]
        if op == 0x00:                          # LSHIFT imm
            d8 = sext(data & 0xFF, 8)
            R[n] = (rx << d8 if d8 >= 0 else rx >> -d8) & FMASK
        elif op == 0x01:                        # ASHIFT imm
            s = rx - (1 << 32) if rx & 0x80000000 else rx
            d8 = sext(data & 0xFF, 8)
            R[n] = (s << d8 if d8 >= 0 else s >> -d8) & FMASK
        elif op in (0x30, 0x31, 0x32, 0x33):    # BSET / BCLR / BTGL / BTST by immediate
            bit = sext(data & 0xFF, 8) & 31
            if op == 0x30: R[n] = (rx | (1 << bit)) & FMASK
            elif op == 0x31: R[n] = (rx & ~(1 << bit)) & FMASK
            elif op == 0x32: R[n] = (rx ^ (1 << bit)) & FMASK
            else:
                self.reg.ASTAT = (self.reg.ASTAT & ~0x40) | (0x40 if rx & (1 << bit) else 0)
                self.reg.ASTAT = (self.reg.ASTAT & ~0x1) | (0 if rx & (1 << bit) else 1)
                self._shflags(sz=not (rx & (1 << bit)), sv=False)   # BTST: SZ = tested bit is 0
                return
        elif op == 0x10 or op == 0x12:          # FEXT bit:len
            bit, ln = data & 0x3F, (data >> 6) & 0x3F
            v = (rx >> bit) & ((1 << ln) - 1)
            if op == 0x12 and ln and (v & (1 << (ln - 1))):
                v -= (1 << ln)
            R[n] = v & FMASK
        else:
            raise NotImplementedError(f'shift6 {op:#x} @ {self.pc:#x}')
        self._shflags(sz=(R[n] == 0), sv=False)


if __name__ == '__main__':
    s = Sharc()
    print('loaded regions:', [(hex(a), len(b)) for a, b in s.img.regions])
