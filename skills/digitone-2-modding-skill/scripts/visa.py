"""SHARC+ VISA / ISA disassembler (ADSP-2156x, Digitone II section 7).

Sources, in order of trust:
  1. ADI "SHARC Processor Programming Reference" rev 2.4 (ADSP-214xx), ch.10 Instruction Set Opcodes
     and ch.12 Computation Type Opcodes -- plain-text bit tables (work/refs/sharc_2136x_prm.pdf).
  2. MAME src/devices/cpu/sharc/sharc_dasm.cpp (BSD-3, Ville Linde) -- 48-bit ISA field positions,
     compute-field decode.
  3. SHARC+ Core PRM figures (sharc_plus_prm.pdf) -- only for the SHARC+-only 'd' types (3d,4d,7d,14d)
     and to confirm 3c/5a/15b layouts. (Its Type2a/2b figures are defective; not used.)

Word order: code is a stream of 16-bit little-endian words; an instruction is 1..3 words,
MSW first. Every instruction is handled as a 48-bit value `x` with shorter forms left-aligned
(x = w0<<32 | w1<<16 | w2), so bit numbers match the manuals' 47..0 numbering for every size.

Addresses: `sw` = short-word address (PC units in VISA). PC-relative offsets are in short words.
"""
from __future__ import annotations
import os
import struct
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import dn2paths as P; P.use_dnfw()
from dataclasses import dataclass, field

# ---------------------------------------------------------------- registers / conditions
def ureg_name(u: int) -> str:
    g, r = (u >> 4) & 7, u & 0xF
    if g == 0: return f'R{r}'
    if g == 1: return f'I{r}'
    if g == 2: return f'M{r}'
    if g == 3: return f'L{r}'
    if g == 4: return f'B{r}'
    if g == 5: return f'S{r}'
    if g == 6:
        return ['FADDR', 'DADDR', 'u62', 'PC', 'PCSTK', 'PCSTKP', 'LADDR', 'CURLCNTR', 'LCNTR', 'EMUCLK',
                'EMUCLK2', 'PX', 'PX1', 'PX2', 'TPERIOD', 'TCOUNT'][r]
    return ['USTAT1', 'USTAT2', 'MODE1', 'MMASK', 'MODE2', 'FLAGS', 'ASTATx', 'ASTATy', 'STKYx', 'STKYy',
            'IRPTL', 'IMASK', 'IMASKP', 'LIRPTL', 'USTAT3', 'USTAT4'][r]

COND = ['EQ', 'LT', 'LE', 'AC', 'AV', 'MV', 'MS', 'SV', 'SZ', 'FLAG0', 'FLAG1', 'FLAG2', 'FLAG3', 'TF', 'BM',
        'NOT LCE', 'NE', 'GE', 'GT', 'NOT AC', 'NOT AV', 'NOT MV', 'NOT MS', 'NOT SV', 'NOT SZ', 'NOT FLAG0',
        'NOT FLAG1', 'NOT FLAG2', 'NOT FLAG3', 'NOT TF', 'NOT BM', 'TRUE']
TERM = COND[:15] + ['LCE'] + COND[16:31] + ['FOREVER']


def _if(c): return '' if c == 31 else f'IF {COND[c]} '


def sext(v, bits):
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def f32(u):
    return struct.unpack('>f', struct.pack('>I', u & 0xFFFFFFFF))[0]


# ---------------------------------------------------------------- compute field (23 bits)
_ALU = {0x01: 'R{n} = R{x} + R{y}', 0x02: 'R{n} = R{x} - R{y}', 0x05: 'R{n} = R{x} + R{y} + CI',
        0x06: 'R{n} = R{x} - R{y} + CI - 1', 0x09: 'R{n} = (R{x} + R{y})/2', 0x0a: 'COMP(R{x}, R{y})',
        0x0b: 'COMPU(R{x}, R{y})', 0x25: 'R{n} = R{x} + CI', 0x26: 'R{n} = R{x} + CI - 1', 0x29: 'R{n} = R{x} + 1',
        0x2a: 'R{n} = R{x} - 1', 0x22: 'R{n} = -R{x}', 0x30: 'R{n} = ABS R{x}', 0x21: 'R{n} = PASS R{x}',
        0x40: 'R{n} = R{x} AND R{y}', 0x41: 'R{n} = R{x} OR R{y}', 0x42: 'R{n} = R{x} XOR R{y}',
        0x43: 'R{n} = NOT R{x}', 0x61: 'R{n} = MIN(R{x}, R{y})', 0x62: 'R{n} = MAX(R{x}, R{y})',
        0x63: 'R{n} = CLIP R{x} BY R{y}',
        0x81: 'F{n} = F{x} + F{y}', 0x82: 'F{n} = F{x} - F{y}', 0x91: 'F{n} = ABS(F{x} + F{y})',
        0x92: 'F{n} = ABS(F{x} - F{y})', 0x89: 'F{n} = (F{x} + F{y})/2', 0x8a: 'COMP(F{x}, F{y})',
        0xa2: 'F{n} = -F{x}', 0xb0: 'F{n} = ABS F{x}', 0xa1: 'F{n} = PASS F{x}', 0xa5: 'F{n} = RND F{x}',
        0xbd: 'F{n} = SCALB F{x} BY R{y}', 0xad: 'R{n} = MANT F{x}', 0xc1: 'R{n} = LOGB F{x}',
        0xd9: 'R{n} = FIX F{x} BY R{y}', 0xc9: 'R{n} = FIX F{x}', 0xdd: 'R{n} = TRUNC F{x} BY R{y}',
        0xcd: 'R{n} = TRUNC F{x}', 0xda: 'F{n} = FLOAT R{x} BY R{y}', 0xca: 'F{n} = FLOAT R{x}',
        0xc4: 'F{n} = RECIPS F{x}', 0xc5: 'F{n} = RSQRTS F{x}', 0xe0: 'F{n} = F{x} COPYSIGN F{y}',
        0xe1: 'F{n} = MIN(F{x}, F{y})', 0xe2: 'F{n} = MAX(F{x}, F{y})', 0xe3: 'F{n} = CLIP F{x} BY F{y}'}
_SHF = {0x00: 'R{n} = LSHIFT R{x} BY R{y}', 0x20: 'R{n} = R{n} OR LSHIFT R{x} BY R{y}',
        0x04: 'R{n} = ASHIFT R{x} BY R{y}', 0x24: 'R{n} = R{n} OR ASHIFT R{x} BY R{y}',
        0x08: 'R{n} = ROT R{x} BY R{y}', 0xc4: 'R{n} = BCLR R{x} BY R{y}', 0xc0: 'R{n} = BSET R{x} BY R{y}',
        0xc8: 'R{n} = BTGL R{x} BY R{y}', 0xcc: 'BTST R{x} BY R{y}', 0x44: 'R{n} = FDEP R{x} BY R{y}',
        0x64: 'R{n} = R{n} OR FDEP R{x} BY R{y}', 0x4c: 'R{n} = FDEP R{x} BY R{y} (SE)',
        0x6c: 'R{n} = R{n} OR FDEP R{x} BY R{y} (SE)', 0x40: 'R{n} = FEXT R{x} BY R{y}',
        0x48: 'R{n} = FEXT R{x} BY R{y} (SE)', 0x80: 'R{n} = EXP R{x}', 0x84: 'R{n} = EXP R{x} (EX)',
        0x88: 'R{n} = LEFTZ R{x}', 0x8c: 'R{n} = LEFTO R{x}', 0x90: 'R{n} = FPACK F{x}',
        0x94: 'F{n} = FUNPACK R{x}'}
_MULSRC = {0b01: 'R{x} * R{y}', 0b10: '{acc} + R{x} * R{y}', 0b11: '{acc} - R{x} * R{y}'}


def compute(c: int) -> str:
    """Render a 23-bit compute field. Returns '' for 0 (no compute)."""
    if c == 0:
        return ''
    if c & 0x400000:  # multifunction (214xx PRM 12-13..12-17)
        op6 = (c >> 16) & 0x3F
        m, a = (c >> 12) & 0xF, (c >> 8) & 0xF
        xm, ym, xa, ya = (c >> 6) & 3, ((c >> 4) & 3) + 4, ((c >> 2) & 3) + 8, (c & 3) + 12
        if op6 in (0, 1):                      # MR data move: 100000 D OPCODE DREG
            mr = ['MR0F', 'MR1F', 'MR2F', '?', 'MR0B', 'MR1B', 'MR2B', '?'][(c >> 12) & 7]
            return f'{mr} = R{a}' if op6 else f'R{a} = {mr}'
        if op6 >= 0x20:                        # multiplier + dual ALU: 1 1f Rs Rm Ra
            p = 'F' if op6 & 0x10 else 'R'
            s_ = op6 & 0xF
            mul = f'F{m} = F{xm} * F{ym}' if p == 'F' else f'R{m} = R{xm} * R{ym} (SSFR)'
            return f'{mul}, {p}{a} = {p}{xa} + {p}{ya}, {p}{s_} = {p}{xa} - {p}{ya}'
        if op6 >= 0x18:
            alu = {0x18: 'F{a} = F{x} + F{y}', 0x19: 'F{a} = F{x} - F{y}', 0x1a: 'F{a} = FLOAT R{x} BY R{y}',
                   0x1b: 'R{a} = FIX F{x} BY R{y}', 0x1c: 'F{a} = (F{x} + F{y})/2', 0x1d: 'F{a} = ABS F{x}',
                   0x1e: 'F{a} = MAX(F{x}, F{y})', 0x1f: 'F{a} = MIN(F{x}, F{y})'}[op6]
            return f'F{m} = F{xm} * F{ym}, ' + alu.format(a=a, x=xa, y=ya)
        fx = {0x04: ('R{m} = R{xm} * R{ym} (SSFR)', '+'), 0x05: ('R{m} = R{xm} * R{ym} (SSFR)', '-'),
              0x06: ('R{m} = R{xm} * R{ym} (SSFR)', 'avg'), 0x08: ('MRF = MRF + R{xm} * R{ym} (SSF)', '+'),
              0x09: ('MRF = MRF + R{xm} * R{ym} (SSF)', '-'), 0x0a: ('MRF = MRF + R{xm} * R{ym} (SSF)', 'avg'),
              0x0c: ('R{m} = MRF + R{xm} * R{ym} (SSFR)', '+'), 0x0d: ('R{m} = MRF + R{xm} * R{ym} (SSFR)', '-'),
              0x0e: ('R{m} = MRF + R{xm} * R{ym} (SSFR)', 'avg'), 0x10: ('MRF = MRF - R{xm} * R{ym} (SSF)', '+'),
              0x11: ('MRF = MRF - R{xm} * R{ym} (SSF)', '-'), 0x12: ('MRF = MRF - R{xm} * R{ym} (SSF)', 'avg'),
              0x14: ('R{m} = MRF - R{xm} * R{ym} (SSFR)', '+'), 0x15: ('R{m} = MRF - R{xm} * R{ym} (SSFR)', '-'),
              0x16: ('R{m} = MRF - R{xm} * R{ym} (SSFR)', 'avg')}.get(op6)
        if not fx:
            return f'MULTIFUNCTION?({c:#08x})'
        alu = {'+': f'R{a} = R{xa} + R{ya}', '-': f'R{a} = R{xa} - R{ya}', 'avg': f'R{a} = (R{xa} + R{ya})/2'}[fx[1]]
        return fx[0].format(m=m, xm=xm, ym=ym) + ', ' + alu
    cu = (c >> 20) & 3
    op = (c >> 12) & 0xFF
    n, x, y = (c >> 8) & 0xF, (c >> 4) & 0xF, c & 0xF
    if cu == 0:
        if op >> 4 == 0x7:   # dual ALU: 000 0111 Rs Ra Rx Ry
            return f'R{n} = R{x} + R{y}, R{op & 0xF} = R{x} - R{y}'
        if op >> 4 == 0xF:
            return f'F{n} = F{x} + F{y}, F{op & 0xF} = F{x} - F{y}'
        t = _ALU.get(op)
        return t.format(n=n, x=x, y=y) if t else f'ALU?{op:#04x}(R{n},R{x},R{y})'
    if cu == 1:
        if op == 0x30:
            return f'F{n} = F{x} * F{y}'
        hi = (op >> 6) & 3
        dst = (op >> 1) & 3
        acc = 'MRB' if op & 2 else 'MRF'
        lhs = {0: f'R{n}', 1: f'R{n}', 2: 'MRF', 3: 'MRB'}[dst] if hi else None
        mod = ('S' if op & 0x20 else 'U') + ('S' if op & 0x10 else 'U') + ('F' if op & 8 else 'I') + ('R' if op & 1 else '')
        if hi:
            lhs = {0: f'R{n}', 1: f'R{n}', 2: 'MRF', 3: 'MRB'}[(op >> 1) & 3]
            accs = 'MRB' if (op >> 1) & 3 in (1, 3) else 'MRF'
            return f'{lhs} = ' + _MULSRC[hi].format(x=x, y=y, acc=accs) + f' ({mod})'
        # MR operations, PRM table 12-5: SAT 0000 f dd s | RND 0001 1 dd s | MRF=0 0x14 | MRB=0 0x16 (dd: 00 Rn=MRF,
        # 01 Rn=MRB, 10 MRF=MRF, 11 MRB=MRB; f fractional, s signed)
        if op in (0x14, 0x16):
            return 'MRF = 0' if op == 0x14 else 'MRB = 0'
        if (op >> 4) == 0 or ((op >> 4) == 1 and op & 8):
            kind = 'RND' if (op >> 4) == 1 else 'SAT'
            dd = (op >> 1) & 3
            src = 'MRB' if dd in (1, 3) else 'MRF'
            lhs = {0: f'R{n}', 1: f'R{n}', 2: 'MRF', 3: 'MRB'}[dd]
            mode = ('S' if op & 1 else 'U') + ('F' if (op & 8 or kind == 'RND') else 'I')
            return f'{lhs} = {kind} {src} ({mode})'
        return f'MUL?{op:#04x}(R{n},R{x},R{y})'
    if cu == 2:
        t = _SHF.get(op)
        return t.format(n=n, x=x, y=y) if t else f'SHIFT?{op:#04x}'
    return f'COMPUTE?{c:#08x}'


_SHORT = ['R{n} = R{n} + R{x}', 'R{n} = R{n} - R{x}', 'R{n} = PASS R{x}', 'COMP(R{n}, R{x})', 'R{n} = NOT R{x}',
          'R{n} = R{x} + 1', 'R{n} = R{x} - 1', 'R{n} = R{n} * R{x} (SSI)', 'F{n} = F{n} + F{x}',
          'F{n} = F{n} - F{x}', 'F{n} = FLOAT R{x}', 'COMP(F{n}, F{x})', 'R{n} = R{n} AND R{x}',
          'R{n} = R{n} OR R{x}', 'R{n} = R{n} XOR R{x}', 'F{n} = F{n} * F{x}']

_SHIFTIMM = {0x00: 'R{n} = LSHIFT R{x} BY {d8}', 0x08: 'R{n} = R{n} OR LSHIFT R{x} BY {d8}',
             0x01: 'R{n} = ASHIFT R{x} BY {d8}', 0x09: 'R{n} = R{n} OR ASHIFT R{x} BY {d8}',
             0x02: 'R{n} = ROT R{x} BY {d8}', 0x31: 'R{n} = BCLR R{x} BY {d8}', 0x30: 'R{n} = BSET R{x} BY {d8}',
             0x32: 'R{n} = BTGL R{x} BY {d8}', 0x33: 'BTST R{x} BY {d8}', 0x11: 'R{n} = FDEP R{x} BY {b6}:{l6}',
             0x19: 'R{n} = R{n} OR FDEP R{x} BY {b6}:{l6}', 0x13: 'R{n} = FDEP R{x} BY {b6}:{l6} (SE)',
             0x1b: 'R{n} = R{n} OR FDEP R{x} BY {b6}:{l6} (SE)', 0x10: 'R{n} = FEXT R{x} BY {b6}:{l6}',
             0x12: 'R{n} = FEXT R{x} BY {b6}:{l6} (SE)', 0x20: 'R{n} = EXP R{x}', 0x21: 'R{n} = EXP R{x} (EX)',
             0x22: 'R{n} = LEFTZ R{x}', 0x23: 'R{n} = LEFTO R{x}', 0x24: 'R{n} = FPACK F{x}',
             0x25: 'F{n} = FUNPACK R{x}'}


def shift_imm(c: int, dataex: int) -> str:
    op = (c >> 16) & 0x3F
    data = (dataex << 8) | ((c >> 8) & 0xFF)
    n, x = (c >> 4) & 0xF, c & 0xF
    t = _SHIFTIMM.get(op)
    if not t:
        return f'SHIFTIMM?{op:#x}'
    return t.format(n=n, x=x, d8=sext(data & 0xFF, 8), b6=data & 0x3F, l6=(data >> 6) & 0x3F)


# ---------------------------------------------------------------- instruction
@dataclass
class Insn:
    sw: int            # short-word address
    size: int          # words (1..3)
    type: str
    x: int             # 48-bit left-aligned value
    text: str = ''
    kind: str = 'seq'  # seq | jump | call | ret | cjump | rframe | loop | invalid
    target: int | None = None     # branch/loop target (sw)
    cond: int = 31
    imm: int | None = None        # immediate data (17a/17b/16a/16b/14a addr)
    fields: dict = field(default_factory=dict)

    @property
    def bits(self): return self.size * 16


def B(x, hi, lo): return (x >> lo) & ((1 << (hi - lo + 1)) - 1)


def length_type(w0: int, w1: int) -> tuple[int, str] | None:
    """(words, type) from the first two words, or None if not a valid VISA opcode."""
    marker = (w1 & 0x7F) == 0x3F          # bits 22:16 == 0111111 -> 32-bit 'b' form
    dmark = (w1 & 0x78) == 0x30           # bits 22:19 == 0110    -> SHARC+ 'd' form
    t3, t4, t8 = w0 >> 13, w0 >> 12, w0 >> 8
    if t3 == 0b001: return (2, '1b') if marker else (3, '1a')
    # SHARC+ adds access-size bits below the 32-bit marker: 3b = 01111 @22:18 (w17 x16),
    # 4b = 0111 @22:19 (l18 w17 x16); 'd' forms = 0110 @22:19. (PRM figs 14-7, 14-11, 14-9, 14-12)
    if t3 == 0b010: return (2, '3b') if (w1 & 0x7C) == 0x3C else (3, '3d') if dmark else (3, '3a')
    if t4 == 0b0110: return (2, '4b') if (w1 & 0x78) == 0x38 else (3, '4d') if dmark else (3, '4a')
    if t4 == 0b0111:
        sw = (w0 >> 11) & 1
        return (2, '5b_swap' if sw else '5b_move') if marker else (3, '5a_swap' if sw else '5a_move')
    if t4 == 0b1000: return (3, '6a_mem')
    if t4 == 0b1001:
        if (w0 >> 4) & 1: return (1, '3c')
        s = (w0 >> 2) & 7
        return {0: (3, '16a'), 1: (2, '16b'), 2: (2, '15b')}.get(s)
    if t3 == 0b101: return (3, '15a')
    if t4 == 0b1100: return (1, '2c')
    if t4 >= 0b1101: return None
    # t3 == 000
    b39 = (w0 >> 7) & 1
    if t8 == 0x00: return (1, '21c/22c') if w0 & 1 else (3, '21a/22a/26a')
    if t8 == 0x01: return (2, '2b') if b39 else (3, '2a')
    if t8 == 0x02: return (3, '6a_nomem')
    if t8 == 0x04:   # b39 & !b23: MODIFY ... (NW) (scaled 7a; 216x `MODIFY(I7,M7)(NW)` stack pad before C-call args)
        if b39: return (3, '7d') if (w1 >> 7) & 1 else (3, '7a_nw')
        return (2, '7b') if marker else (3, '7a')
    if t8 in (0x06, 0x07): return (3, '8a')
    if t8 in (0x08, 0x09): return (2, '9b') if marker else (3, '9a')
    if t8 in (0x0A, 0x0B): return (1, '11c') if b39 else (3, '11a')
    if t8 == 0x0C: return (3, '12a_imm')
    if t8 == 0x0D: return (3, '12a_ureg')
    if t8 == 0x0E: return (3, '13a')
    if t8 == 0x0F: return (2, '17b') if b39 else (3, '17a')
    if 0x10 <= t8 <= 0x13: return (3, '14a')
    if t8 == 0x14: return (3, '18a')
    if t8 in (0x15, 0x16): return (3, '19a')   # SHARC+: 000101 sc[41:40] (0x15 = sc01, 0x16 = sc10 classic)
    if t8 == 0x17: return (3, '20a')
    if w0 in (0x1804, 0x1844): return (3, '25a')
    if w0 == 0x1900: return (3, '25a_rframe')
    if w0 == 0x1901: return (1, '25c')
    if t8 in (0x1A, 0x1B): return (3, '14d')
    return None


# Access-size encodings, SHARC+ PRM "ACCESS/BH/BHSE Encode Table" for Type 3b and 4b, keyed (l, x, w).
# NOTE the normal-word encoding differs per type: 3b = (0,1,1), 4b = (1,1,1).  (0,0,0) is a BYTE access in both.
SIZE_3B = {(0, 1, 1): '', (1, 1, 1): ' (LW)', (0, 0, 0): ' (BW)', (0, 1, 0): ' (BWSE)',
           (1, 0, 0): ' (SW)', (1, 1, 0): ' (SWSE)'}
SIZE_4B = {(1, 1, 1): '', (0, 0, 0): ' (BW)', (0, 1, 0): ' (BWSE)', (1, 0, 0): ' (SW)', (1, 1, 0): ' (SWSE)'}
NW_3B, NW_4B = (0, 1, 1), (1, 1, 1)


def access_size(ins):
    """'' for a normal-word access, else the size suffix; None if the type has no size field."""
    x = ins.x
    if ins.type == '3b':
        return SIZE_3B.get((B(x, 30, 30), B(x, 16, 16), B(x, 17, 17)), ' (SIZE?)')
    if ins.type == '4b':
        return SIZE_4B.get((B(x, 18, 18), B(x, 16, 16), B(x, 17, 17)), ' (SIZE?)')
    if ins.type == '3a':
        return ' (LW)' if B(x, 30, 30) else ''
    return None


def _dm(g, i, m, pre=False):
    I = f'I{i + 8 if g else i}'
    M = f'M{m + 8 if g else m}'
    mem = 'PM' if g else 'DM'
    return f'{mem}({M}, {I})' if pre else f'{mem}({I}, {M})'


def decode(words: list[int], sw: int) -> Insn:
    w0 = words[0]
    w1 = words[1] if len(words) > 1 else 0
    w2 = words[2] if len(words) > 2 else 0
    lt = length_type(w0, w1)
    if lt is None:
        return Insn(sw, 1, 'invalid', w0 << 32, f'.word {w0:#06x}', kind='invalid')
    size, t = lt
    x = (w0 << 32) | ((w1 << 16) if size >= 2 else 0) | (w2 if size == 3 else 0)
    ins = Insn(sw, size, t, x)
    cond = B(x, 37, 33)
    comp = B(x, 22, 0)
    cs = compute(comp) if size == 3 else ''

    def join(*parts):
        return ', '.join(p for p in parts if p)

    if t in ('1a', '1b'):
        dmd, dmi, dmm, pmd, dmr, pmi, pmm, pmr = B(x, 44, 44), B(x, 43, 41), B(x, 40, 38), B(x, 37, 37), B(x, 36, 33), B(x, 32, 30), B(x, 29, 27), B(x, 26, 23)
        a = f'DM(I{dmi}, M{dmm}) = R{dmr}' if dmd else f'R{dmr} = DM(I{dmi}, M{dmm})'
        b = f'PM(I{pmi + 8}, M{pmm + 8}) = R{pmr}' if pmd else f'R{pmr} = PM(I{pmi + 8}, M{pmm + 8})'
        ins.text = join(cs if t == '1a' else '', a, b)
    elif t in ('2a', '2b'):
        c = B(x, 38, 16) if t == '2b' else comp
        ins.text = (_if(cond) if t == '2a' else '') + compute(c)
        ins.cond = cond if t == '2a' else 31
    elif t == '2c':
        op, n, r = B(x, 43, 40), B(x, 39, 36), B(x, 35, 32)
        ins.text = _SHORT[op].format(n=n, x=r)
    elif t in ('3a', '3b', '3d'):
        u, i, m, g, d, l, ur = B(x, 44, 44), B(x, 43, 41), B(x, 40, 38), B(x, 32, 32), B(x, 31, 31), B(x, 30, 30), B(x, 29, 23)
        mem = _dm(g, i, m, pre=not u)
        mv = f'{mem} = {ureg_name(ur)}' if d else f'{ureg_name(ur)} = {mem}'
        if t == '3a' and l: mv += ' (LW)'
        if t == '3d':
            mv += f' [3d ex={B(x,18,18)} w={B(x,17,17)} x={B(x,16,16)}]'
        if t == '3b':
            mv += SIZE_3B.get((l, B(x, 16, 16), B(x, 17, 17)), ' (SIZE?)')
        ins.text = _if(cond) + join(cs if t == '3a' else '', mv)
        ins.cond = cond
    elif t == '3c':
        i, m, d, r = B(x, 43, 41), B(x, 40, 38), B(x, 37, 37), B(x, 35, 32)
        ins.text = f'DM(I{i}, M{m}) = R{r}' if d else f'R{r} = DM(I{i}, M{m})'
    elif t in ('4a', '4b', '4d'):
        i, g, d, u, r = B(x, 43, 41), B(x, 40, 40), B(x, 39, 39), B(x, 38, 38), B(x, 26, 23)
        data = sext(B(x, 32, 27), 6)
        I = f'I{i + 8 if g else i}'
        mem = ('PM' if g else 'DM') + (f'({I}, {data})' if u else f'({data}, {I})')
        mv = f'{mem} = R{r}' if d else f'R{r} = {mem}'
        if t == '4b':
            mv += SIZE_4B.get((B(x, 18, 18), B(x, 16, 16), B(x, 17, 17)), ' (SIZE?)')
        if t == '4d':
            mv += f' [4d l={B(x,18,18)} w={B(x,17,17)} x={B(x,16,16)}]'
        ins.text = _if(cond) + join(cs if t == '4a' else '', mv)
        ins.cond = cond
        ins.fields = dict(i=i, g=g, d=d, u=u, data=data, dreg=r)
    elif t in ('5a_move', '5b_move'):
        src = (B(x, 42, 38) << 2) | B(x, 32, 31)
        dst = B(x, 29, 23)
        ins.text = _if(cond) + join(cs if t == '5a_move' else '', f'{ureg_name(dst)} = {ureg_name(src)}')
        ins.cond = cond
    elif t in ('5a_swap', '5b_swap'):
        ins.text = _if(cond) + join(cs if t == '5a_swap' else '', f'R{B(x,26,23)} <-> S{B(x,41,38)}')
        ins.cond = cond
    elif t == '6a_mem':
        i, m, g, d, dx, r = B(x, 43, 41), B(x, 40, 38), B(x, 32, 32), B(x, 31, 31), B(x, 30, 27), B(x, 26, 23)
        mem = _dm(g, i, m)
        ins.text = _if(cond) + join(shift_imm(comp, dx), f'{mem} = R{r}' if d else f'R{r} = {mem}')
        ins.cond = cond
    elif t == '6a_nomem':
        ins.text = _if(cond) + shift_imm(comp, B(x, 30, 27))
        ins.cond = cond
    elif t in ('7a', '7a_nw', '7b', '7d'):
        g, i, m, idis = B(x, 38, 38), B(x, 32, 30), B(x, 29, 27), B(x, 26, 24)
        off = 8 if g else 0
        if t == '7d':                                  # ACONV: byte<->word pointer convert
            breg, toby = B(x, 28, 28), B(x, 27, 27)
            reg = f'B{i + off}' if breg else f'I{i + off}'
            mod = f'{reg} = {"w2b" if toby else "b2w"}({reg})'
        elif idis == 0:
            mod = f'MODIFY(I{i + off}, M{m + off})'
        else:
            mod = f'I{(i ^ idis) + off} = MODIFY(I{i + off}, M{m + off})'
        if t == '7a_nw':
            mod += ' (NW)'
        elif t == '7a' and B(x, 23, 23):               # BH (w=0, l=1) = (SW): PRM Type 7a BH encode table
            mod += ' (SW)'
        ins.text = _if(cond) + join(cs if t != '7b' else '', mod)
        ins.cond = cond
    elif t == '8a':
        rel, b, a, j, ci = B(x, 40, 40), B(x, 39, 39), B(x, 38, 38), B(x, 26, 26), B(x, 24, 24)
        addr = B(x, 23, 0)
        tgt = (sw + sext(addr, 24)) & 0xFFFFFF if rel else addr
        ins.kind, ins.target, ins.cond = ('call' if b else 'jump'), tgt, cond
        ins.text = _if(cond) + ('CALL' if b else 'JUMP') + f' {tgt:#08x}' + (' (DB)' if j else '') + (' (LA)' if a else '') + (' (CI)' if ci else '')
    elif t in ('9a', '9b'):
        rel, b, a = B(x, 40, 40), B(x, 39, 39), B(x, 38, 38)
        j, ci = B(x, 26, 26), B(x, 24, 24)
        e = B(x, 25, 25) if t == '9a' else 0
        if rel:
            tgt = (sw + sext(B(x, 32, 27), 6)) & 0xFFFFFF
            dest = f'{tgt:#08x}'
            ins.target = tgt
        else:
            dest = f'(M{B(x,29,27)+8}, I{B(x,32,30)+8})'
        ins.kind, ins.cond = ('call' if b else 'jump'), cond
        s = _if(cond) + ('CALL ' if b else 'JUMP ') + dest + (' (DB)' if j else '') + (' (LA)' if a else '') + (' (CI)' if ci else '')
        if t == '9a' and comp:
            s += (', ELSE ' if e else ', ') + cs
        ins.text = s
    elif t in ('11a', '11c'):
        rti = B(x, 40, 40)
        if t == '11a':
            j, e, lr = B(x, 26, 26), B(x, 25, 25), B(x, 24, 24)
        else:
            j, e, lr = B(x, 38, 38), 0, B(x, 32, 32)
        s = _if(cond) + ('RTI' if rti else 'RTS') + (' (DB)' if j else '') + (' (LR)' if lr else '')
        if t == '11a' and comp:
            s += (', ELSE ' if e else ', ') + cs
        ins.text, ins.kind, ins.cond = s, 'ret', cond
    elif t in ('12a_imm', '12a_ureg', '13a'):
        tgt = (sw + sext(B(x, 22, 0), 23)) & 0xFFFFFF
        ins.target, ins.kind = tgt, 'loop'
        if t == '12a_imm':
            ins.text = f'LCNTR = {B(x,39,24)}, DO {tgt:#08x} UNTIL LCE'
        elif t == '12a_ureg':
            ins.text = f'LCNTR = {ureg_name(B(x,38,32))}, DO {tgt:#08x} UNTIL LCE'
        else:
            ins.text = f'DO {tgt:#08x} UNTIL {TERM[cond]}'
    elif t == '14a':
        g, d, l, ur, addr = B(x, 41, 41), B(x, 40, 40), B(x, 39, 39), B(x, 38, 32), B(x, 31, 0)
        mem = f'{"PM" if g else "DM"}({addr:#010x})'
        ins.text = (f'{mem} = {ureg_name(ur)}' if d else f'{ureg_name(ur)} = {mem}') + (' (LW)' if l else '')
        ins.imm = addr
        ins.fields = dict(g=g, d=d, ureg=ur, addr=addr)
    elif t == '14d':
        # PRM 16-4..16-6 (Type 14d): w=1 -> normal word (+LW if l), w=0 -> sub-word: l selects SW (1) / BW (0),
        # x sign-extends loads. Every 14d in real 1.11 code has w=0 (the tempo read @0x1c3005 is SW, the mute flags
        # 0x2578c8+v are BW). Earlier decoding (w = byte, x = short, l = LW) was wrong.
        d, ex, l, w, xx, r, addr = B(x, 40, 40), B(x, 39, 39), B(x, 38, 38), B(x, 37, 37), B(x, 36, 36), B(x, 35, 32), B(x, 31, 0)
        if w:
            size_s = ' (LW)' if l else ''
        else:
            size_s = (' (SW' if l else ' (BW') + ('SE)' if xx and not d else ')')
        mem = f'DM({addr:#010x})'
        ins.text = (f'{mem} = R{r}' if d else f'R{r} = {mem}') + size_s + (' (EX)' if ex else '')
        ins.imm = addr
    elif t == '15a':
        g, i, d, l, ur, data = B(x, 44, 44), B(x, 43, 41), B(x, 40, 40), B(x, 39, 39), B(x, 38, 32), B(x, 31, 0)
        I = f'I{i + 8 if g else i}'
        mem = f'{"PM" if g else "DM"}({sext(data,32):#x}, {I})'
        ins.text = (f'{mem} = {ureg_name(ur)}' if d else f'{ureg_name(ur)} = {mem}') + (' (LW)' if l else '')
        ins.fields = dict(g=g, i=i, d=d, ureg=ur, off=sext(data, 32))
    elif t == '15b':
        i, d, l, g, ur, data = B(x, 43, 41), B(x, 40, 40), B(x, 39, 39), B(x, 37, 37), B(x, 29, 23), sext(B(x, 22, 16), 7)
        I = f'I{i + 8 if g else i}'
        mem = f'{"PM" if g else "DM"}({data}, {I})'
        ins.text = (f'{mem} = {ureg_name(ur)}' if d else f'{ureg_name(ur)} = {mem}') + (' (LW)' if l else '')
        ins.fields = dict(g=g, i=i, d=d, ureg=ur, off=data)
    elif t in ('16a', '16b'):
        i, m, g = B(x, 43, 41), B(x, 40, 38), B(x, 37, 37)
        data = B(x, 31, 0) if t == '16a' else sext(B(x, 31, 16), 16) & 0xFFFFFFFF
        ins.imm = data
        ins.text = f'{_dm(g, i, m)} = {data:#010x}'
    elif t in ('17a', '17b'):
        ur = B(x, 38, 32)
        data = B(x, 31, 0) if t == '17a' else sext(B(x, 31, 16), 16) & 0xFFFFFFFF
        ins.imm = data
        ins.fields = dict(ureg=ur)
        fl = f'  ; ={f32(data):g}' if t == '17a' and 0x30000000 <= (data & 0x7FFFFFFF) <= 0x48000000 else ''
        ins.text = f'{ureg_name(ur)} = {data:#010x}{fl}'
    elif t == '18a':
        bop = ['SET', 'CLR', 'TGL', '?', 'TST', 'XOR', '?', '?'][B(x, 39, 37)]
        sreg = ureg_name(0x70 | B(x, 35, 32))
        data = B(x, 31, 0)
        MODE1 = {1 << 15: 'TRUNC', 1 << 16: 'RND32', 1 << 13: 'ALUSAT', 1 << 21: 'PEYEN',
                 1 << 24: 'CBUFEN', 1 << 23: 'BDCST1', 1 << 22: 'BDCST9'}
        note = ''
        if sreg == 'MODE1' and data in MODE1:
            note = f'  ; {MODE1[data]}'
        ins.text = f'BIT {bop} {sreg} {data:#010x}{note}'
    elif t == '19a':
        sc = B(x, 41, 40)
        br, g, idis, i = B(x, 39, 39) if sc == 2 else 0, B(x, 38, 38), B(x, 37, 35), B(x, 34, 32)
        off = 8 if g else 0
        ins.fields = dict(sc=sc, w=B(x, 39, 39), g=g, i=i, idis=idis, data=sext(B(x, 31, 0), 32))
        ins.text = f'{"BITREV" if br else "MODIFY"}(I{i + off}, {sext(B(x,31,0),32)})' + (f' -> I{(i ^ idis) + off}' if idis else '')
        if sc == 1:                                    # BH: sc=01 w=0 (SW) x2, w=1 (NW) x4 (PRM Type 19a BH table)
            ins.text += ' (NW)' if B(x, 39, 39) else ' (SW)'
    elif t == '20a':
        names = [(39, 'PUSH LOOP'), (38, 'POP LOOP'), (37, 'PUSH STS'), (36, 'POP STS'), (35, 'PUSH PCSTK'), (34, 'POP PCSTK'), (33, 'FLUSH CACHE')]
        ins.text = ', '.join(n for b_, n in names if B(x, b_, b_)) or f'20a({x:#014x})'
    elif t in ('21a/22a/26a', '21c/22c'):
        if B(x, 39, 39): ins.text = 'IDLE'
        elif B(x, 38, 38): ins.text = 'EMUIDLE/SYNC'
        else: ins.text = 'NOP'
    elif t == '25a':
        addr = B(x, 23, 0)
        tgt = (sw + sext(addr, 24)) & 0xFFFFFF if w0 == 0x1844 else addr
        ins.kind, ins.target = 'cjump', tgt
        ins.text = f'CJUMP {tgt:#08x} (DB)'
    elif t in ('25a_rframe', '25c'):
        ins.kind = 'rframe'
        ins.text = 'RFRAME'
    return ins


# ---------------------------------------------------------------- image access
class Image:
    """Short-word addressed view of the loaded SHARC program (section 7 boot stream)."""

    def __init__(self, sec7_path=None):
        from dnfw.image import bootstream as bs
        sec7_path = sec7_path or str(P.SEC7_STOCK)
        self.raw = open(sec7_path, 'rb').read()
        self.regions = bs.load_regions(self.raw)

    # byte (load) address <-> short word
    @staticmethod
    def sw_to_load(sw):
        if 0xB80000 <= sw < 0xC00000:
            return 0x20000000 + 2 * (sw - 0xB80000)
        if 0x800000 <= sw < 0xB00000:                  # DDR: VISA 0x800000..0xAFFFFF = 0x80A00000..0x80FFFFFF
            return 0x80A00000 + 2 * (sw - 0x800000)    # (ADSP-2156x data sheet Rev. D, Table 6)
        return 0x28000000 + 2 * sw

    @staticmethod
    def load_to_sw(a):
        if 0x20000000 <= a < 0x28000000:
            return 0xB80000 + (a - 0x20000000) // 2
        if 0x80A00000 <= a < 0x81000000:
            return 0x800000 + (a - 0x80A00000) // 2
        return (a - 0x28000000) // 2

    def region_of(self, load):
        for base, data in self.regions:
            if base <= load < base + len(data):
                return base, data
        return None

    def word(self, sw):
        a = self.sw_to_load(sw)
        r = self.region_of(a)
        if not r or a + 2 > r[0] + len(r[1]):
            return None
        return struct.unpack_from('<H', r[1], a - r[0])[0]

    def insn(self, sw):
        ws = []
        for k in range(3):
            w = self.word(sw + k)
            if w is None:
                break
            ws.append(w)
        if not ws:
            return None
        return decode(ws, sw)

    def linear(self, sw, count=None, end=None, stop_invalid=True):
        n = 0
        while (count is None or n < count) and (end is None or sw < end):
            ins = self.insn(sw)
            if ins is None:
                return
            yield ins
            if ins.kind == 'invalid' and stop_invalid:
                return
            sw += ins.size
            n += 1


def fmt(ins: Insn, img: Image | None = None) -> str:
    raw = ' '.join(f'{(ins.x >> (32 - 16 * k)) & 0xFFFF:04x}' for k in range(ins.size))
    la = f'{Image.sw_to_load(ins.sw):#010x}'
    return f'{ins.sw:#08x} {la}  {raw:<15s} {ins.type:<9s} {ins.text}'


if __name__ == '__main__':
    import sys
    img = Image()
    a = int(sys.argv[1], 16)
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    sw = Image.load_to_sw(a) if a >= 0x20000000 else a
    for ins in img.linear(sw, n, stop_invalid=False):
        print(fmt(ins))
