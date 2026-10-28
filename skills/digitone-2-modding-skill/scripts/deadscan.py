"""List unreferenced SHARC functions in a section-7 image, merging adjacent dead chunks."""
import os
import re, struct, sys, bisect
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import dn2paths as P; P.use_dnfw()
import visa
from dnfw.image import bootstream as bs

CODE = [(0x20000000, 0x1e880), (0x2001e888, 0x699b4), (0x28380000, 0x25c0), (0x283825c4, 0x19a38)]
CACHE_TAILS = [(0x2839C000, 0x283A0000), (0x282EC000, 0x282F0000), (0x2831C000, 0x28320000)]


def scan(img):
    insns, refs = [], set()
    for base, n in CODE:
        for ins in img.linear(visa.Image.load_to_sw(base), end=visa.Image.load_to_sw(base + n), stop_invalid=False):
            insns.append(ins)
            for h in re.findall(r'0x[0-9a-f]+', ins.text):
                refs.add(int(h, 16))
    for base, data in img.regions:
        for o in range(0, len(data) - 3, 2):
            refs.add(struct.unpack_from('<I', data, o)[0])
    refs = sorted(refs)
    chunks, start = [], None
    for k, ins in enumerate(insns):
        if start is None: start = ins.sw
        if ins.text in ('RFRAME', 'RTS', 'RTI') or ins.text.startswith('RTS') or ins.text.startswith('RTI'):
            chunks.append((start, ins.sw + ins.size)); start = None
    dead = []
    for a, b in chunks:
        i = bisect.bisect_left(refs, a)
        if i < len(refs) and refs[i] < b: continue
        ld = visa.Image.sw_to_load(a)
        if any(lo <= ld < hi for lo, hi in CACHE_TAILS): continue
        dead.append((a, b))
    merged = []
    for a, b in dead:
        if merged and merged[-1][1] == a: merged[-1] = (merged[-1][0], b, merged[-1][2] + 1)
        else: merged.append((a, b, 1))
    return dead, merged


if __name__ == '__main__':
    img = visa.Image()
    dead, merged = scan(img)
    print(len(dead), 'dead chunks')
    for a, b, n in sorted(merged, key=lambda t: t[0] - t[1])[:25]:
        print(f'{a:#x}..{b:#x}  {b - a:4d} sw  ({n} chunk{"s" if n > 1 else ""})  first: {img.insn(a).text}')


def scan_transitive(img, exclude=((0x1C0A6E, 0x1C0B5B),)):
    """scan() + functions referenced ONLY from dead functions (iterated to a fixed point): a helper whose every caller
    is dead is dead too. Data words stay references from live 'code'. Same return shape as scan(). `exclude`
    keeps the interrupt trampoline and what follows it out (0x1c0b2f.. is only referenced from the trampoline)."""
    insns = []
    for base, n in CODE:
        insns += list(img.linear(visa.Image.load_to_sw(base), end=visa.Image.load_to_sw(base + n), stop_invalid=False))
    data = set()
    for base, d in img.regions:
        for o in range(0, len(d) - 3, 2):
            data.add(struct.unpack_from('<I', d, o)[0])
    chunks, start, own = [], None, set()
    for ins in insns:
        if start is None:
            start, own = ins.sw, set()
        own |= {int(h, 16) for h in re.findall(r'0x[0-9a-f]+', ins.text)}
        if ins.text in ('RFRAME', 'RTS', 'RTI') or ins.text.startswith('RTS') or ins.text.startswith('RTI'):
            chunks.append((start, ins.sw + ins.size, own)); start = None
    dead = set()
    while True:
        refs = set(data)
        for a, b, own in chunks:
            if (a, b) not in dead:
                refs |= own
        refs = sorted(refs)
        new = set()
        for a, b, _ in chunks:
            if (a, b) in dead or any(lo <= a < hi for lo, hi in exclude):
                continue
            i = bisect.bisect_left(refs, a)
            if i < len(refs) and refs[i] < b:
                continue
            if any(lo <= visa.Image.sw_to_load(a) < hi for lo, hi in CACHE_TAILS):
                continue
            new.add((a, b))
        if not new:
            break
        dead |= new
    dead = sorted(dead)
    merged = []
    for a, b in dead:
        if merged and merged[-1][1] == a: merged[-1] = (merged[-1][0], b, merged[-1][2] + 1)
        else: merged.append((a, b, 1))
    return dead, merged
