"""48-bit (ISA) instruction builder + packer for SHARC+ hardware-loop bodies.

WHY: SHARC+ PRM 4-42 — the last 11 instructions of a hardware loop (all of a short loop) must be legacy 48-bit ISA
instructions. Stock 1.11 obeys this in every loop, so the compressed 16/32-bit VISA forms sasm used are forbidden inside
loops. (A same-DAG post-modify load is the other classic silent-unit cause: M3() refuses it.)

48-bit ISA instructions can carry a compute AND a data access, so a loop body written as a sequence of simple ops is
packed: each output instruction holds at most one compute (type 2a / the compute field of 3a/4a) and one access
(type 3a: any ureg, DM(Ia,Mb) post-modify or DM(Mb,Ia) pre-modify; type 4a: Dreg, DM(k,Ia) with k in -32..31).
Packing is list scheduling that only uses the parallel read-before-write semantics that make it equal to the
sequential program: a RAW or WAW dependency forces a later instruction, a WAR allows the same one, and data accesses
keep their program order. Every packed instruction is decoded by sharcdec/visa and compared with the intended text.
"""
import re
from sasm import W, visa, f32

M32 = 0xFFFFFFFF


def ureg(name):                                     # R 0x0n, I 0x1n, M 0x2n (visa ureg_name)
    return {'R': 0x00, 'F': 0x00, 'I': 0x10, 'M': 0x20}[name[0]] | int(name[1:])


def rname(name):                                    # canonical register name for dependencies (F = R)
    return 'R' + name[1:] if name[0] == 'F' else name


class Op:
    """kind 'c' (compute: field c) or 'm' (access: fields for 3a/4a). reads/writes: register names."""
    def __init__(self, kind, text, reads, writes, **f):
        self.kind, self.text, self.reads, self.writes, self.f = kind, text, set(reads), set(writes), f


# ---------------------------------------------------------------- computes (23-bit field, PRM ch. 12)
ALU = {'ADD': (0, 0x01, 'R{n} = R{x} + R{y}', 'xy'), 'SUB': (0, 0x02, 'R{n} = R{x} - R{y}', 'xy'),
       'PASS': (0, 0x21, 'R{n} = PASS R{x}', 'x'), 'COMP': (0, 0x0a, 'COMP(R{x}, R{y})', 'xy'),
       'INC': (0, 0x29, 'R{n} = R{x} + 1', 'x'), 'AND': (0, 0x40, 'R{n} = R{x} AND R{y}', 'xy'),
       'OR': (0, 0x41, 'R{n} = R{x} OR R{y}', 'xy'), 'FADD': (0, 0x81, 'F{n} = F{x} + F{y}', 'xy'),
       'FSUB': (0, 0x82, 'F{n} = F{x} - F{y}', 'xy'), 'FLOAT': (0, 0xca, 'F{n} = FLOAT R{x}', 'x'),
       'TRUNC': (0, 0xcd, 'R{n} = TRUNC F{x}', 'x'), 'FCLIP': (0, 0xe3, 'F{n} = CLIP F{x} BY F{y}', 'xy'),
       'FMIN': (0, 0xe1, 'F{n} = MIN(F{x}, F{y})', 'xy'), 'FMAX': (0, 0xe2, 'F{n} = MAX(F{x}, F{y})', 'xy'),
       'FPASS': (0, 0xa1, 'F{n} = PASS F{x}', 'x'), 'FMUL': (1, 0x30, 'F{n} = F{x} * F{y}', 'xy')}


def C(op, n, x, y=0):
    cu, code, t, uses = ALU[op]
    c = cu << 20 | code << 12 | n << 8 | x << 4 | y
    reads = {f'R{x}'} | ({f'R{y}'} if 'y' in uses else set())
    writes = set() if op == 'COMP' else {f'R{n}'}
    if op == 'COMP':
        writes = {'ASTAT'}
    return Op('c', t.format(n=n, x=x, y=y), reads, writes, c=c)


# ---------------------------------------------------------------- accesses
def same_dag(reg, i):
    """True if reg is a DAG register (I/M/L/B) of the same DAG as index register Ii."""
    return reg[0] in 'IMLB' and (int(reg[1:]) >= 8) == (i >= 8)


def M3(reg, i, m, store, post):
    """Type 3a access: ureg <-> DM(Ii, Mm) (post-modify, updates Ii) or DM(Mm, Ii) (pre-modify, no update).
    PRM type 3: 'the Ureg may not be from the same DAG as Ia/Mb'. Stock only breaks that with pre-modify LOADS; a
    same-DAG store or post-modify load (`M4 = DM(I0, M6)`: silent on HW) is refused — load a data
    register and use MV() instead."""
    assert not (same_dag(reg, i) and (store or post)), f'same-DAG {"store" if store else "post-modify load"} {reg}/I{i}'
    a = f'DM(I{i}, M{m})' if post else f'DM(M{m}, I{i})'
    t = f'{a} = {reg}' if store else f'{reg} = {a}'
    reads = {f'I{i}', f'M{m}'} | ({rname(reg)} if store else set())
    writes = ({f'I{i}'} if post else set()) | (set() if store else {rname(reg)})
    return Op('m', t, reads, writes, t3='3a', u=post, i=i, m=m, d=store, ur=ureg(reg))


def M4(r, k, i, store, post=False):
    """Type 4a access: Rr <-> DM(k, Ii) (pre, no update) or DM(Ii, k) (post, Ii += k words). k in -32..31."""
    assert -32 <= k <= 31, k
    a = f'DM(I{i}, {k})' if post else f'DM({k}, I{i})'
    t = f'{a} = R{r}' if store else f'R{r} = {a}'
    reads = {f'I{i}'} | ({f'R{r}'} if store else set())
    writes = ({f'I{i}'} if post else set()) | (set() if store else {f'R{r}'})
    return Op('m', t, reads, writes, t3='4a', u=post, i=i, d=store, r=r, k=k)


def MV(dst, src):
    """Type 5a register move `ureg = ureg` (carries a compute; no memory access in the same instruction). Stock:
    0x1ca898 'F12 = F0 * F12, M4 = R1' (703e 9213 0c0c)."""
    return Op('v', f'{dst} = {src}', {rname(src)}, {rname(dst)}, t3='5a', src=ureg(src), dst=ureg(dst))


def encode(cop, mop, cond=31):
    """One 48-bit instruction from an optional compute op and an optional access (3a/4a) or move (5a) op."""
    c = cop.f['c'] if cop else 0
    if mop is None:
        x = 0x01 << 40 | cond << 33 | c                                  # type 2a
        text = cop.text
    elif mop.f['t3'] == '5a':
        s = mop.f['src']
        x = 0b0111 << 44 | (s >> 2) << 38 | cond << 33 | (s & 3) << 31 | mop.f['dst'] << 23 | c
        text = ', '.join(t for t in (cop.text if cop else '', mop.text) if t)
    elif mop.f['t3'] == '3a':
        f = mop.f
        x = (0b010 << 45 | f['u'] << 44 | f['i'] << 41 | f['m'] << 38 | cond << 33 | 0 << 32 | f['d'] << 31
             | 0 << 30 | f['ur'] << 23 | c)
        text = ', '.join(t for t in (cop.text if cop else '', mop.text) if t)
    else:
        f = mop.f
        x = (0b0110 << 44 | f['i'] << 41 | 0 << 40 | f['d'] << 39 | f['u'] << 38 | cond << 33
             | (f['k'] & 0x3F) << 27 | f['r'] << 23 | c)
        text = ', '.join(t for t in (cop.text if cop else '', mop.text) if t)
    b = W(x >> 32, (x >> 16) & 0xFFFF, x & 0xFFFF)
    got = visa.decode([x >> 32, (x >> 16) & 0xFFFF, x & 0xFFFF], 0)
    assert got.size == 3 and got.text == text, (got.type, got.text, text)
    return b, text


def schedule(ops):
    """Pack ops (program order) into 48-bit instructions; returns [(compute op|None, access op|None)]."""
    slots = []                                     # per instruction: {'c': op, 'm': op}
    placed = []                                    # (op, cycle)
    last_mem = -1
    for op in ops:
        lo = 0
        for p, cyc in placed:
            if p.writes & op.reads or p.writes & op.writes:
                lo = max(lo, cyc + 1)              # RAW / WAW: strictly later
            elif p.reads & op.writes:
                lo = max(lo, cyc)                  # WAR: same instruction reads the old value
        if op.kind == 'm':
            lo = max(lo, last_mem + 1)             # accesses keep program order
        slot = 'c' if op.kind == 'c' else 'm'      # a 5a move ('v') takes the access slot of its instruction
        cyc = lo
        while cyc < len(slots) and slot in slots[cyc]:
            cyc += 1
        while cyc >= len(slots):
            slots.append({})
        slots[cyc][slot] = op
        placed.append((op, cyc))
        if op.kind == 'm':
            last_mem = cyc
    return [(s.get('c'), s.get('m')) for s in slots]


def emit(a, ops):
    """Append the packed 48-bit instructions for ops to sasm.Asm a; returns the instruction count."""
    packed = schedule(ops)
    for cop, mop in packed:
        a(encode(cop, mop))
    return len(packed)


def const_f(v):
    return f32(v) if isinstance(v, float) else v & M32
