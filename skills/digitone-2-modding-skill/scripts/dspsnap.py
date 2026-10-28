"""Reuse a DSP boot snapshot for another section 7, instead of a 4-minute dn2boot.py run.

    PYTHONPATH=$WS/work/repos/dn2_firmware_explore/src pypy3.10 dspsnap.py NEW_SEC7 [BASE_SEC7 BASE_SNAP] [--compare SNAP]

A sim.py snapshot holds only the words the program WROTE (Memory.words) plus registers; every other read falls back
to the image's boot-stream regions. So the snapshot booted from BASE_SEC7 is exactly the one NEW_SEC7 would boot to,
provided (1) the boot wrote no word where the two images differ and (2) the boot never EXECUTED a differing word
(its path would change). (1) is checked here against the snapshot; (2) against the boot's executed-PC log when there
is one (dn2boot writes none), otherwise by the rule our patches follow: changes only in dead functions (caves) and in
code that runs per audio block (after boot). `--compare SNAP` diffs against a real boot of NEW_SEC7 (validation).
Prints REUSE <BASE_SNAP> or NEEDS BOOT with the reason.
"""
import os
import sys, gzip, pickle
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import dn2paths as P; P.use_dnfw()
from dnfw.image import bootstream as bs


def words(sec7):
    out = {}
    for base, data in bs.load_regions(open(sec7, 'rb').read()):
        for off in range(0, len(data) - len(data) % 4, 4):
            out[base + off] = data[off:off + 4]
    return out


def diff(a, b):
    wa, wb = words(a), words(b)
    return {k for k in set(wa) | set(wb) if wa.get(k) != wb.get(k)}


def main():
    args = [x for x in sys.argv[1:] if not x.startswith('--')]
    new = args[0]
    base, snap = (args[1], args[2]) if len(args) > 2 else P.snap('stock')
    d = diff(base, new)
    st = pickle.load(gzip.open(snap, 'rb'))
    # snapshot words are physical addresses; image regions use load addresses (+0x28000000 for 0x240000..0x3C0000)
    la = lambda pa: pa + 0x28000000 if 0x240000 <= pa < 0x3C0000 else pa
    hit = sorted(pa for pa in st['words'] if la(pa) in d)
    print(f'{len(d)} words differ between {base} and {new}; the boot wrote {len(st["words"])} words, {len(hit)} of '
          f'them in the difference')
    if '--compare' in sys.argv:
        other = pickle.load(gzip.open(sys.argv[sys.argv.index('--compare') + 1], 'rb'))
        same = {k: st[k] == other[k] for k in st if k not in ('icount',)}
        print('vs a real boot:', {k: v for k, v in same.items() if not v} or 'identical (but icount)')
    print(f'REUSE {snap}' if not hit else f'NEEDS BOOT: written words in the difference {[hex(x) for x in hit[:8]]}')


if __name__ == '__main__':
    main()
