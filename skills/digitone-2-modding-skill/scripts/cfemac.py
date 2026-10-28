"""Python EMAC for digikit (DN2 1.11): every EMAC instruction in the MAIN OS code runs here instead of in Unicorn,
whose fractional mode drops the x2 product alignment (emactest.py). Instructions come from objdump's decoding
(`emac_rows.txt`: 'ADDR WORDS MNEMONIC OPERANDS', made by list_rows()), so no hand-decoding of opcode fields.

Model (ColdFire EMAC, MACSR bits: 7 OMC, 6 S/U, 5 F/I, 4 R/T; N Z V EV = 3..0 not modelled):
  each ACC is kept as a 48-bit raw value. Fractional: raw = ACC32 << 8 | lower-extension byte, upper extension above
  bit 40; a word product is (a*b)<<1 (Q31) << 8, a long product is (a*b)<<1 >> 24 (Q63 -> Q39); ACC32 = raw >> 8
  (+128 first if R/T). Integer: raw = ACC with the 16-bit extension above bit 32; products a*b (S/U selects signed).
  OMC saturates the 32-bit result on moves out. MASK applies to '&' addressing (ea & (0xffff0000 | MASK)).
"""
import re, struct
from unicorn import UC_HOOK_CODE
from unicorn import m68k_const as K

M32, M48 = 0xFFFFFFFF, (1 << 48) - 1
REG = {f'%d{i}': K.UC_M68K_REG_D0 + i for i in range(8)}
REG.update({f'%a{i}': K.UC_M68K_REG_A0 + i for i in range(8)})
REG['%fp'], REG['%sp'] = K.UC_M68K_REG_A6, K.UC_M68K_REG_A7


def s32(v): return v - (1 << 32) if v & 0x80000000 else v
def s16(v): return v - 0x10000 if v & 0x8000 else v
def s48(v): v &= M48; return v - (1 << 48) if v & (1 << 47) else v


class Emac:
    def __init__(self, uc):
        self.uc, self.macsr, self.mask, self.acc = uc, 0x20, 0xFFFF, [0, 0, 0, 0]
        self.hits = {}

    # ---- helpers
    def r(self, name): return self.uc.reg_read(REG[name]) & M32
    def w(self, name, v): self.uc.reg_write(REG[name], v & M32)
    def frac(self): return bool(self.macsr & 0x20)

    def acc32(self, k):
        raw = s48(self.acc[k])
        if self.frac():
            v = (raw + (128 if self.macsr & 0x10 else 0)) >> 8
        else:
            v = raw
        if self.macsr & 0x80:                                   # OMC: saturate
            if self.frac() or not self.macsr & 0x40:
                v = max(-(1 << 31), min((1 << 31) - 1, v))
            else:
                v = max(0, min(M32, v))
        return v & M32

    def set_acc32(self, k, v):
        self.acc[k] = ((s32(v) << 8) if self.frac() else s32(v)) & M48

    def operand(self, tok):                                     # '%d6u' / '%d0l' / '%d1'
        m = re.match(r'(%[ad]\d|%fp|%sp)([ul]?)$', tok)
        v = self.r(m.group(1))
        if m.group(2) == 'u': v >>= 16
        if m.group(2):
            v &= 0xFFFF
            return s16(v) if (self.frac() or not self.macsr & 0x40) else v
        return s32(v) if (self.frac() or not self.macsr & 0x40) else v

    def product(self, a, b, long_):
        p = a * b
        if self.frac():
            p <<= 1
            return p >> 24 if long_ else p << 8
        return p

    def ea(self, tok):                                          # '%a1@', '%a1@(4)', '%a0@+', '%a4@-', trailing '&'
        masked = tok.endswith('&')
        tok = tok.rstrip('&')
        m = re.match(r'(%a\d|%fp|%sp)@(\+|-|\((-?\d+)\))?$', tok)
        an, mode = m.group(1), m.group(2)
        base = self.r(an)
        if mode == '-':
            base = (base - 4) & M32; self.w(an, base)
        addr = (base + int(m.group(3))) & M32 if (mode and mode.startswith('(')) else base
        if masked: addr &= 0xFFFF0000 | self.mask
        if mode == '+': self.w(an, base + 4)
        return addr

    # ---- execution
    def execute(self, mn, ops):
        t = [o.strip() for o in re.split(r',(?![^(]*\))', ops)]
        if mn in ('macl', 'msacl', 'macw', 'msacw'):
            long_, sub = mn.endswith('l'), mn.startswith('msac')
            ry, rx = self.operand(t[0]), self.operand(t[1])
            rest, shift = t[2:], 0
            if rest and rest[0] in ('<<', '>>'):
                shift = 1 if rest[0] == '<<' else -1; rest = rest[1:]
            acc = int(rest[-1][4])
            load = None
            if len(rest) == 3:                                  # <ea>, Rw, ACCx
                addr = self.ea(rest[0])
                load = (rest[1], struct.unpack('>I', bytes(self.uc.mem_read(addr, 4)))[0])
            p = self.product(ry, rx, long_)
            if shift == 1: p <<= 1
            elif shift == -1: p >>= 1
            self.acc[acc] = (self.acc[acc] + (-p if sub else p)) & M48
            if load: self.w(load[0], load[1])
            return
        if mn == 'movclrl':                                     # ACCx,Rn
            k = int(t[0][4]); self.w(t[1], self.acc32(k)); self.acc[k] = 0; return
        if mn == 'movel':
            a, b = t
            if b == '%macsr':
                self.macsr = (int(a[1:]) if a.startswith('#') else self.r(a)) & 0xFFF; return
            if a == '%macsr': self.w(b, self.macsr); return
            if b == '%mask': self.mask = self.r(a) & 0xFFFF; return
            if a == '%mask': self.w(b, 0xFFFF0000 | self.mask); return
            if re.match(r'%acc\d$', b): self.set_acc32(int(b[4]), int(a[1:]) if a.startswith('#') else self.r(a)); return
            if re.match(r'%acc\d$', a): self.w(b, self.acc32(int(a[4]))); return
            if b.startswith('%accext'): self.set_ext(int(b[7]), self.r(a)); return
            if a.startswith('%accext'): self.w(b, self.get_ext(int(a[7]))); return
        raise NotImplementedError(f'EMAC {mn} {ops}')

    def get_ext(self, k):                                       # ACCEXT01 / ACCEXT23
        out = 0
        for j, acc in enumerate((k, k + 1)):
            raw = self.acc[acc]
            e = (((raw >> 40) & 0xFF) << 8 | (raw & 0xFF)) if self.frac() else (raw >> 32) & 0xFFFF
            out |= e << (16 * j)
        return out

    def set_ext(self, k, v):
        for j, acc in enumerate((k, k + 1)):
            e = (v >> (16 * j)) & 0xFFFF
            raw = self.acc[acc]
            if self.frac():
                raw = (raw & ~((0xFF << 40) | 0xFF)) | ((e >> 8) << 40) | (e & 0xFF)
            else:
                raw = (raw & M32) | (e << 32)
            self.acc[acc] = raw & M48

    def install(self, rows):
        for addr, size, mn, ops in rows:
            def hook(uc_, a, sz, user, mn=mn, ops=ops, nxt=addr + size):
                self.execute(mn, ops)
                self.hits[a] = self.hits.get(a, 0) + 1
                uc_.reg_write(K.UC_M68K_REG_PC, nxt)
            self.uc.hook_add(UC_HOOK_CODE, hook, begin=addr, end=addr)


def load_rows(path):
    rows = []
    for line in open(path):
        a, words, mn, *ops = line.split(None, 3)
        rows.append((int(a, 16), len(words) // 2, mn, ops[0].strip() if ops else ''))
    return rows


def list_rows(dis_path, out_path, lo=0x40000400, hi=0x401D0000):
    """From `m68k-elf-objdump -D` of section 3: every EMAC instruction in the real code range."""
    out = []
    for line in open(dis_path):
        m = re.match(r'([0-9a-f]{8}):\s+((?:[0-9a-f]{4} ?)+)\s+(\S+)\s*(.*)', line)
        if not m: continue
        a = int(m.group(1), 16)
        mn, ops = m.group(3), m.group(4).strip()
        if lo <= a < hi and (mn.startswith(('mac', 'msac', 'movclr')) or
                             (mn == 'movel' and re.search(r'%(macsr|acc\d|accext\d\d|mask)\b', ops))):
            out.append(f'{a:#x} {"".join(m.group(2).split())} {mn} {ops}')
    open(out_path, 'w').write('\n'.join(out) + '\n')
    return len(out)
