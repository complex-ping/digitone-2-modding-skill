"""Instruction-shape census: every instruction a build changes, normalised (register numbers and constants blanked),
compared with the shapes that occur in stock REAL code. A shape stock never uses is where the interpreter is most
likely to be wrong about the hardware (it finds e.g. a same-DAG `M4 = DM(I0, M6)`, which silences the unit).
Usage: python3 shapecheck.py SEC7.bin [--l2]  -> lists novel / rare shapes (not an error by itself: justify each from the PRM)."""
import os
import re, sys, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import dn2paths as P; P.use_dnfw()
import visa, dn2frame as D

REAL = [(0xB80000, 0xB8F440), (0x1C0000, 0x1C12E0), (0x1C12E2, 0x1CDA7E)]   # code regions (hwrules.CODE); 0xb8f444.. = data


def norm(t):
    t = t.split('  ;')[0]
    t = re.sub(r'\b[RFSIMLB](\d+)\b', lambda m: m.group(0)[0] + '#', t)
    return re.sub(r'-?0x[0-9a-f]+|-?\b\d+\b', 'N', t)


def shapes(img):
    """Shape counts over stock code. A linear sweep loses sync after embedded data, so also decode from every
    branch/call/loop target it found (each instruction address counted once)."""
    seen, targets = {}, set()
    for a, e in REAL:
        for i in img.linear(a, end=e, stop_invalid=False):
            seen[i.sw] = i
            if i.target is not None and any(lo <= i.target < hi for lo, hi in REAL):
                targets.add(i.target)
    for t in targets:
        for n, i in enumerate(img.linear(t, count=400, stop_invalid=True)):
            if i.sw in seen and seen[i.sw].size == i.size and seen[i.sw].text == i.text:
                break                                   # back in sync with the sweep
            seen[i.sw] = i
    c = collections.Counter()
    for i in seen.values():
        c[(i.size, i.type, norm(i.text))] += 1
    return c


def changed(img, stock):
    rs = []
    for (ba, da), (bb, db) in zip(stock.regions, img.regions):
        if ba != bb or len(da) != len(db):
            continue
        i = 0
        while i < len(da):
            if da[i] != db[i]:
                j = i
                while j < len(da) and any(da[k] != db[k] for k in range(j, min(j + 8, len(da)))):
                    j += 1
                rs.append((visa.Image.load_to_sw(ba + i), visa.Image.load_to_sw(ba + j) + 1)); i = j
            else:
                i += 1
    return rs


def census(img, stock, rare=3):
    st = shapes(stock)
    out, seen = [], set()
    bad = lambda ins: sum(i.kind == 'invalid' or '?' in i.text for i in ins)
    for a, e in changed(img, stock):
        if not any(lo <= a < hi for lo, hi in REAL):
            continue
        # the change may start mid-instruction: take the nearest start at or before it that decodes cleanly
        votes = collections.Counter()                   # stock boundary at/just before a: decode stock from several
        for back in (45, 60, 75, 90, 120):              # points further back; VISA streams resync within a few insns
            last = None
            for i in stock.linear(a - back, end=a + 1, stop_invalid=False):
                if i.sw <= a: last = i.sw
            votes[last] += 1
        s0 = votes.most_common(1)[0][0]
        ins = list(img.linear(s0, end=e, stop_invalid=False))
        if bad(ins):                                    # a cave's own code: start exactly at the change
            ins = list(img.linear(a, end=e, stop_invalid=False))
        for i in ins:
            if i.sw in seen or i.sw + i.size <= a:
                continue
            seen.add(i.sw)
            n = st[(i.size, i.type, norm(i.text))]
            if n < rare:
                out.append((n, i.sw, i.type, i.text))
    return out


def census_l2(img, stock, rare=3, n_sw=0x4000, start=0xBC8000):
    """The L2 code block (0x20090000 = sw 0xbc8000): new code, not a change of stock bytes -- every
    instruction from its start up to the first invalid word (the NOP / data padding ends it)."""
    st = shapes(stock)
    out = []
    for i in img.linear(start, end=start + n_sw, stop_invalid=True):
        n = st[(i.size, i.type, norm(i.text))]
        if n < rare:
            out.append((n, i.sw, i.type, i.text))
    return out


if __name__ == '__main__':
    img = D.image_from_sec7(sys.argv[1])
    stock = D.image_from_sec7(P.snap('stock')[0])
    res = census(img, stock)
    if '--l2' in sys.argv:
        res += census_l2(img, stock)
        res += census_l2(img, stock, n_sw=0x1000, start=0xBC4200)    # the L2 space below it (0x20088400..)
    for n, sw, t, text in res:
        print(f'{"NOVEL" if n == 0 else f"rare({n})":8s} {sw:#x} {t:8s} {text}')
    print(f'{sum(1 for r in res if r[0] == 0)} novel, {sum(1 for r in res if r[0]) } rare shapes')
