"""Tiny SHARC+ assembler for DN2 caves: every emitted instruction is decoded by sharcdec/visa and compared to the
text we meant, so a wrong encoding cannot reach a build. Encodings come from real firmware instances
(every encoding here was copied from a stock instance)."""
import os
import re, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import dn2paths as P; P.use_dnfw()
import visa  # noqa: E402
from dnfw.image import bootstream as bs  # noqa: E402

W = lambda *ws: b''.join(struct.pack('<H', w & 0xFFFF) for w in ws)
NOP48, NOP16 = W(0, 0, 0), W(1)
f32 = lambda v: struct.unpack('<I', struct.pack('<f', v))[0]
EQ, LT, LE, NE, GE, GT = 0, 1, 2, 16, 17, 18
COND = {EQ: 'IF EQ ', LT: 'IF LT ', LE: 'IF LE ', NE: 'IF NE ', GE: 'IF GE ', GT: 'IF GT '}


def reg(u):                                         # ureg number: R 0x0n, I 0x1n, M 0x2n
    return {'R': 0x00, 'I': 0x10, 'M': 0x20}[u[0]] | int(u[1:])


class Asm:
    def __init__(self, base):
        self.base, self.items, self.labels = base, [], {}

    def sw(self):
        return self.base + sum(len(b) for b, _, _ in self.items) // 2

    def label(self, name):
        self.labels[name] = self.sw()

    def __call__(self, bt=None, fix=None):
        if fix is not None:
            self.items.append((NOP48, None, fix))                   # all fixups are 48-bit
        else:
            self.items.append((bt[0], bt[1], None))

    def assemble(self):
        out, texts, at = b'', [], self.base
        for b, text, fix in self.items:
            if fix:
                b, text = fix(at, self.labels)
                assert len(b) == 6
            out += b; texts.append(text); at += len(b) // 2
        return out, texts


# ---- computes ----
def const(r, v):                                    # 17a  Ureg = imm32
    u = f32(v) if isinstance(v, float) else v & 0xFFFFFFFF
    return W(0x0F00 | reg(r), u >> 16, u & 0xFFFF), f'{r} = {u:#010x}'


# 2c short compute, 1 sw: Rn = Rn op Rr
C2 = {'ADD': (0x0, 'R{n} = R{n} + R{r}'), 'SUB': (0x1, 'R{n} = R{n} - R{r}'), 'PASS': (0x2, 'R{n} = PASS R{r}'),
      'COMP': (0x3, 'COMP(R{n}, R{r})'), 'INC': (0x5, 'R{n} = R{r} + 1'), 'FADD': (0x8, 'F{n} = F{n} + F{r}'),
      'FSUB': (0x9, 'F{n} = F{n} - F{r}'), 'FLOAT': (0xA, 'F{n} = FLOAT R{r}'), 'AND': (0xC, 'R{n} = R{n} AND R{r}'),
      'OR': (0xD, 'R{n} = R{n} OR R{r}'), 'FMUL': (0xF, 'F{n} = F{n} * F{r}')}


def c2(op, n, r):
    code, t = C2[op]
    return W(0xC000 | code << 8 | n << 4 | r), t.format(n=n, r=r)


# 2b compute, 2 sw: uop<<12 | n<<8 | x<<4 | y
C2B = {'FADD3': (0x081, 'F{n} = F{x} + F{y}'), 'FSUB3': (0x082, 'F{n} = F{x} - F{y}'),
       'FMUL3': (0x130, 'F{n} = F{x} * F{y}'), 'TRUNC': (0x0CD, 'R{n} = TRUNC F{x}'),
       'CLIP': (0x0E3, 'F{n} = CLIP F{x} BY F{y}'), 'FMIN': (0x0E1, 'F{n} = MIN(F{x}, F{y})'),
       'FMAX': (0x0E2, 'F{n} = MAX(F{x}, F{y})')}


def c2b(op, n, x, y=0):
    uop, t = C2B[op]
    c = uop << 12 | n << 8 | x << 4 | y
    return W(0x0180 | c >> 16, c & 0xFFFF), t.format(n=n, x=x, y=y)


# ---- memory ----
def ld15(r, k, i):                                  # 15b  Ureg = DM(k, Ii)   (I0-I7, k in -64..63 words)
    return W(0x9008 | i << 9, reg(r) << 7 | (k & 0x7F)), f'{r} = DM({k}, I{i})'


def st15(r, k, i):
    return W(0x9108 | i << 9, reg(r) << 7 | (k & 0x7F)), f'DM({k}, I{i}) = {r}'


def dm3c(r, i, m6, store):                          # 3c  Rr = DM(Ii, M5|M6) / store; 1 sw, R0-R15
    w = 0x9000 | i << 9 | 0x100 | (0x80 if m6 else 0x40) | store << 5 | 0x10 | r
    m = 'M6' if m6 else 'M5'
    return W(w), (f'DM(I{i}, {m}) = R{r}' if store else f'R{r} = DM(I{i}, {m})')


def dm3a(r, i, m6, store):                          # 48-bit 3a with empty compute (stock '5bbe 8100 0000')
    m = 'M6' if m6 else 'M5'
    return W(0x5000 | i << 9 | (0x1BE if m6 else 0x17E), store << 15 | r << 7, 0), \
        (f'DM(I{i}, {m}) = R{r}' if store else f'R{r} = DM(I{i}, {m})')


def dm3b(u, i, m, store, post):                     # 3b normal word, any ureg; stock '4bfe 003f' R0 = DM(M7, I5)
    w0 = 0x4000 | post << 12 | i << 9 | m << 6 | 0x3E
    w1 = store << 15 | reg(u) << 7 | 0x3F
    a = f'DM(I{i}, M{m})' if post else f'DM(M{m}, I{i})'
    return W(w0, w1), (f'{a} = {u}' if store else f'{u} = {a}')


def ld_abs(r, a):                                   # 14a  Rr = DM(addr)
    return W(0x1000 | r, a >> 16, a & 0xFFFF), f'R{r} = DM({a:#010x})'


def st_abs(r, a):
    return W(0x1100 | r, a >> 16, a & 0xFFFF), f'DM({a:#010x}) = R{r}'


def modify(i, n):                                   # 19a MODIFY(Ii, n) — byte offset (unscaled)
    return W(0x1600 | i, (n >> 16) & 0xFFFF, n & 0xFFFF), f'MODIFY(I{i}, {n})'


R2_FROM = {'I0': W(0x713E, 0x013F), 'I1': W(0x713E, 0x813F), 'I2': W(0x713F, 0x013F), 'I3': W(0x713F, 0x813F),
           'I5': W(0x717E, 0x813F)}                  # 5b 'R2 = In' (stock encodings)


def mv_i_from_r2(i):                                # 5b 'In = R2'  (7000|(s>>2)<<6|3E|(s>>1&1), (s&1)<<15|d<<7|3F)
    s, d = 0x02, 0x10 | i                           # source ureg in w0, destination in w1 (stock '703f 0a3f' I4 = R2)
    return W(0x7000 | (s >> 2) << 6 | 0x3E | (s >> 1 & 1), (s & 1) << 15 | d << 7 | 0x3F), f'I{i} = R2'


# ---- flow ----
def jump_abs(addr):                                 # 8a direct, unconditional, non-delayed
    return W(0x063E, (addr >> 16) & 0xFF, addr & 0xFFFF), f'JUMP {addr:#08x}'


def jrel(cond, label):                              # 8a relative, non-delayed
    def fix(at, labels):
        off = (labels[label] - at) & 0xFFFFFF
        return W(0x0700 | cond << 1, off >> 16, off & 0xFFFF), f'{COND.get(cond, "")}JUMP {labels[label]:#x}'
    return fix


def loop(count, end_label):                         # 12a LCNTR = count, DO end UNTIL LCE (end = last body instr)
    def fix(at, labels):
        return W(0x0C00 | count >> 8, (count & 0xFF) << 8 | 0x80, labels[end_label] - at), \
            f'LCNTR = {count}, DO {labels[end_label]:#x} UNTIL LCE'
    return fix


# ---- image helpers ----
def image_of(raw):
    img = visa.Image.__new__(visa.Image)
    img.raw = bytes(raw); img.regions = bs.load_regions(img.raw)
    return img


def texts(img, sw, n):
    return [i.text.split('  ;')[0] for i in img.linear(sw, n)]


FLOW = ('jump', 'call', 'cjump', 'ret')


def lint(img, at, n, allowed, subword_ok=()):
    """HW rules the emulator cannot check (references/sharc.md): NW accesses only, branch spacing, 12x48-bit valid
    prefetch after every branch, no same-DAG I/M store, register whitelist, stock loop shape."""
    ins = [i for i in img.linear(at, n) if i.sw < at + n]       # (the whole range: checking only a prefix once let
                                                                  # violations through)
    prev = None
    for i in ins:
        assert i.kind != 'invalid', hex(i.sw)
        assert visa.access_size(i) in (None, '') or i.text in subword_ok, f'non-NW access {i.sw:#x}: {i.text}'
        if i.kind in FLOW:
            if prev is not None:
                assert i.sw - (prev.sw + prev.size) >= 2, f'branches too close {prev.sw:#x} {i.sw:#x}'
            prev = i
            sw_, after = i.sw + i.size, 0
            while after < 36:
                nx = img.insn(sw_); assert nx is not None and nx.kind != 'invalid', f'prefetch after {i.sw:#x}'
                after += nx.size; sw_ += nx.size
        m = re.search(r'DM\(-?\w+, I(\d+)\) = ([IM])(\d+)', i.text)
        assert not (m and (int(m.group(1)) < 8) == (int(m.group(3)) < 8)), f'same-DAG store {i.sw:#x}: {i.text}'
        # PRM types 3/15: no same-DAG store and no post-modify same-DAG load (`M4 = DM(I0, M6)` silences the unit)
        import hwrules
        assert not hwrules.same_dag(i.text), f'DAG-SAME {i.sw:#x}: {i.text}'
        for dst in re.findall(r'(?:^|, |IF \w+ )([RFIMS]\d+) =', i.text):
            d = 'R' + dst[1:] if dst[0] == 'F' else dst
            assert d in allowed, (hex(i.sw), i.text)
    ends = []
    for i in ins:
        m = re.match(r'LCNTR = \d+, DO (0x[0-9a-f]+)', i.text)
        if m:
            end = int(m.group(1), 16); ends.append(end)
            body = [j for j in ins if i.sw < j.sw <= end]
            assert body and body[-1].sw + body[-1].size - 1 >= end, f'loop end mid-instruction {i.sw:#x}'
            assert all(j.kind not in FLOW for j in body[-3:]), hex(i.sw)
            assert body[-1].size == 3 and end + 3 - body[0].sw >= 3, f'loop {i.sw:#x}: must end on 48-bit (stock)'
            # SHARC+ PRM 4-42: the last 11 instructions of a hardware loop (all of a shorter loop) must be 48-bit ISA
            # encodings — silent builds broke this. Build loop bodies with sisa.py.
            short = [j for j in body[-11:] if j.size != 3]
            assert not short, f'loop {i.sw:#x}: VISA (16/32-bit) instruction in the last 11: {short[0].sw:#x} {short[0].text}'
    assert len(ends) == len(set(ends))
