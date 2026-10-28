"""Encoding census: the hardware decodes some fields wider than visa.py does. A hand-coded `R2 = LSHIFT R1 BY -10` with its
middle parcel 0x0000 (where every stock negative shift has 0x7800 -- the shift amount's sign bits) runs as -10 in the
emulator and stops the DSP on the unit. For each instruction in the given code ranges, its shape (text with register numbers
and constants blanked, the sign of constants kept) is looked up in stock's real code; the bits that are the same in every
stock instance of that shape must be the same in yours. A mismatch is an encoding our decoder accepts but stock never makes.
    python3 encodecheck.py SEC7.bin [lo_sw hi_sw ...]        (default: the free L2 code 0xbc4200..0xbe0000 and DDR code 0x800000..)
    python3 encodecheck.py --build MODULE SEC7.bin            (MODULE.build() is run with sasm.Asm hooked: every instruction your
        assemblers emit is checked at its exact address -- a linear sweep loses sync on data in code. --write-baseline stores
        the shapes your last hardware-good build already had in data/encode_baseline.json; a new shape FAILs)"""
import os
import re, sys, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import dn2paths as P; P.use_dnfw()
import dn2frame as D

REAL = [(0xB80000, 0xB8F440), (0x1C0000, 0x1C12E0), (0x1C12E2, 0x1CDA7E)]


def norm(t):
    t = t.split('  ;')[0]
    t = re.sub(r'\b([RFSIMLB])(\d+)\b', lambda m: m.group(1) + '#', t)
    return re.sub(r'(-?)(0x[0-9a-f]+|\b\d+\b)', lambda m: m.group(1) + 'N', t)


def bits(img, i):
    v = 0
    for k in range(i.size):
        w = img.word(i.sw + k)
        if w is None:
            return None
        v = v << 16 | w
    return v


def stock_masks(st):
    acc = {}
    for lo, hi in REAL:
        for i in st.linear(lo, end=hi, stop_invalid=False):
            if i.kind == 'invalid':
                continue
            k = (i.size, norm(i.text))
            b = bits(st, i)
            if b is None:
                continue
            a = acc.get(k)
            acc[k] = (b, b, 1) if a is None else (a[0] & b, a[1] | b, a[2] + 1)
    return acc


def check(img, ranges, acc):
    out = []
    for lo, hi in ranges:
        for i in img.linear(lo, end=hi, stop_invalid=False):
            if i.kind == 'invalid':
                continue
            k = (i.size, norm(i.text))
            if k not in acc:
                continue
            a, o, n = acc[k]
            const = ~(a ^ o) & ((1 << 16 * i.size) - 1)
            b = bits(img, i)
            if b is not None and n >= 30 and (b & const) != (a & const):
                out.append((i.sw, i.text, hex(b), hex(a & const), hex(const), n))
    return out


BASELINE = str(P.DATA / 'encode_baseline.json')   # your last HW-good build's accepted deviations (--write-baseline)


def emitted(module):
    """The sw address of every instruction the build's sasm.Asm assemblers emit (build() run once)."""
    import sasm
    addrs = set()
    _asm = sasm.Asm.assemble

    def assemble(self):
        at = self.base
        for b, text, fix in self.items:
            addrs.add(at)
            at += len(b) // 2
        return _asm(self)
    sasm.Asm.assemble = assemble
    __import__(module).build()
    sasm.Asm.assemble = _asm
    return sorted(addrs)


def check_addrs(img, addrs, acc):
    """(mismatching instructions, their shapes) over exact instruction addresses."""
    bad = []
    for a in addrs:
        i = img.insn(a)
        if i is None or i.kind == 'invalid':
            continue
        k = (i.size, norm(i.text))
        if k not in acc or acc[k][2] < 30:
            continue
        x, o, n = acc[k]
        const = ~(x ^ o) & ((1 << 16 * i.size) - 1)
        b = bits(img, i)
        if b is not None and (b & const) != (x & const):
            bad.append((a, i.text, hex(b), k[1]))
    return bad


if __name__ == '__main__' and '--build' in sys.argv:
    import json
    module = sys.argv[sys.argv.index('--build') + 1]
    sec7 = [a for a in sys.argv[1:] if a.endswith('.bin')][0]
    img = D.image_from_sec7(sec7)
    acc = stock_masks(D.image_from_sec7(P.snap('stock')[0]))
    bad = check_addrs(img, emitted(module), acc)
    shapes = sorted({s_ for *_, s_ in bad})
    if '--write-baseline' in sys.argv:
        json.dump(shapes, open(BASELINE, 'w'), indent=0)
        print(f'baseline: {len(shapes)} shapes written')
        sys.exit(0)
    base = set(json.load(open(BASELINE))) if os.path.exists(BASELINE) else set()
    new = [b for b in bad if b[3] not in base]
    print(f'{len(bad)} emitted instructions off stock\'s fixed bits, {len(new)} of a shape your baseline does not have '
          f'{"ok" if not new else "FAIL"}')
    for a, t, b, s_ in new[:20]:
        print(f'  FAIL {a:#08x} {t}  ({b})')
    print('FAIL' if new else 'OK')
    sys.exit(1 if new else 0)

if __name__ == '__main__':
    img = D.image_from_sec7(sys.argv[1])
    st = D.image_from_sec7(P.snap('stock')[0])
    r = [int(x, 0) for x in sys.argv[2:]]
    ranges = list(zip(r[::2], r[1::2])) or [(0xBC4200, 0xBE0000), (0x800000, 0x840000)]
    bad = check(img, ranges, stock_masks(st))
    print(f'{len(bad)} instructions whose fixed bits differ from every stock instance of their shape')
    for sw, t, b, want, c, n in bad[:40]:
        print(f'  {sw:#08x} {t:40s} ours {b}  stock fixed {want} (mask {c}, {n} stock instances)')
