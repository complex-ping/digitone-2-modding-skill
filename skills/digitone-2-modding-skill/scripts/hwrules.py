"""Static SHARC+ hardware-rule checker for a section-7 image (rules the interpreter cannot see; SHARC+ PRM ch. 4).

  LOOP-ISA   "The last 11 instructions of a hardware loop must be encoded as legacy ISA (48-bit) instructions ...
             In cases of short loops (body shorter than 11 instructions) ... all the instructions in the loop."
             (PRM 4-42, VISA-Related Restrictions on Hardware Loops). Stock 1.11 obeys it in all 286 loops. (A loop that
             obeys it can still silence the unit: see DAG-SAME below.)
  LOOP-END   nested loops must not share an end address (PRM 4-44).
  LOOP-LCE   no `IF NOT LCE` in the last 11 instructions of a counter loop (PRM 4-41).
  LOOP-CALL  no CALL in the last 5 instructions of a loop (PRM 4-41, SHARC+ LR rule).
  BRANCH-12  after every branch at least 12 valid 48-bit instructions' worth of code (PRM 4-14, branch predictor).
  BRANCH-WIN two branches must not share a 48-bit (3 short-word) window (PRM 4-14, VISA).
  DAG-SAME   no same-DAG DAG-register store, no post-modify same-DAG DAG-register load (PRM types 3a/3b/15:
             "the Ureg may not be from the same DAG as Ia/Mb"). The root cause of silent units: a `M4 = DM(I0, M6)` in a
             sample loop, or a same-DAG store -- the emulator runs both "correctly". Stock
             real code has 0 of either (its same-DAG loads are all pre-modify / immediate-offset, no index update).

Usage: python3 hwrules.py SEC7.bin [--baseline STOCK_SEC7.bin]   -> lists violations not present in the baseline.
"""
import os
import re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import dn2paths as P; P.use_dnfw()
import visa

CODE = [(0x20000000, 0x1e880), (0x2001e888, 0x699b4), (0x28380000, 0x25c0), (0x283825c4, 0x19a38)]
FLOW = ('jump', 'call', 'cjump', 'ret')
MAX_BODY = 3000                          # longer "loops" are data decoded as DO instructions


def code_insns(img):
    for base, n in CODE:
        yield from img.linear(visa.Image.load_to_sw(base), end=visa.Image.load_to_sw(base + n), stop_invalid=False)


def loop_of(ins):
    """(end address) for a DO-UNTIL instruction, else None."""
    if 'UNTIL' not in ins.text:
        return None
    m = re.search(r'DO (0x[0-9a-f]+)', ins.text)
    if not m:
        return None
    end = int(m.group(1), 16)
    return end if 0 < end - ins.sw < MAX_BODY else None


def loop_violations(img, do_ins, end):
    """Rule violations for one hardware loop (list of (rule, sw, text))."""
    body = [b for b in img.linear(do_ins.sw + do_ins.size, end=end + 1, stop_invalid=False) if b.sw <= end]
    out = []
    if not body or body[-1].sw != end:
        out.append(('LOOP-SHAPE', do_ins.sw, f'end {end:#x} is not an instruction start'))
        return out
    for b in body[-11:]:
        if b.size != 3:
            out.append(('LOOP-ISA', b.sw, b.text))
        if 'LCNTR' in do_ins.text and 'NOT LCE' in b.text:
            out.append(('LOOP-LCE', b.sw, b.text))
    for b in body[-5:]:
        if b.kind == 'call':
            out.append(('LOOP-CALL', b.sw, b.text))
    return out


def _dag(r):
    m = re.match(r'([IMLB])(\d+)$', r)
    return (1 if int(m.group(2)) < 8 else 2) if m else None


def same_dag(text):
    """DAG-SAME: PRM types 3/15 'the Ureg may not be from the same DAG as Ia/Mb'. Stock only uses same-DAG LOADS
    that do not update the index (pre-modify or immediate offset); a same-DAG store or post-modify load is flagged."""
    out = []
    for m in re.finditer(r'(?:^|, |IF [\w ]+? )([IMLB]\d+) = (?:DM|PM)\(([^)]*)\)', text):
        dst, args = m.group(1), [a.strip() for a in m.group(2).split(',')]
        if len(args) == 2 and re.match(r'I\d+$', args[0]) and re.match(r'M\d+$', args[1]) and _dag(dst) == _dag(args[0]):
            out.append(f'post-modify load of {dst}')
    for m in re.finditer(r'(?:DM|PM)\(([^)]*)\) = ([IMLB]\d+)', text):
        idx = [a.strip() for a in m.group(1).split(',') if re.match(r'\s*I\d+$', a)]
        if idx and _dag(m.group(2)) == _dag(idx[0]):
            out.append(f'store of {m.group(2)}')
    return out


def check(img, ranges=None):
    """All violations in the image (or only in the given sw ranges: then only those are decoded -- every rule below is
    already restricted to instructions in the ranges, and a range starts at an instruction, e.g. a cave or a block)."""
    if ranges:
        insns = [i for lo, hi in ranges for i in img.linear(lo, end=hi, stop_invalid=False) if i.sw < hi]
    else:
        insns = list(code_insns(img))
    viol, ends = [], {}
    by_sw = {i.sw: i for i in insns}
    inr = (lambda a: any(lo <= a < hi for lo, hi in ranges)) if ranges else (lambda a: True)
    for ins in insns:
        if inr(ins.sw) and ins.kind != 'invalid':
            viol += [('DAG-SAME', ins.sw, ins.sw, f'{w}: {ins.text}') for w in same_dag(ins.text)]
    for ins in insns:
        end = loop_of(ins)
        if end is None or not inr(ins.sw):
            continue
        viol += [(r, ins.sw, a, t) for r, a, t in loop_violations(img, ins, end)]
        ends.setdefault(end, []).append(ins.sw)
    for end, starts in ends.items():
        if len(starts) > 1:
            viol += [('LOOP-END', s, end, 'nested loops share an end address') for s in starts]
    prev = None
    for ins in insns:
        if ins.kind not in FLOW or ins.kind == 'invalid' or not inr(ins.sw):
            continue
        if prev is not None and 0 < ins.sw - prev.sw < 3:        # stock places 48-bit branches back to back
            viol.append(('BRANCH-WIN', prev.sw, ins.sw, f'{prev.text} / {ins.text}'))
        prev = ins
        sw_, after = ins.sw + ins.size, 0
        while after < 36:
            nx = by_sw.get(sw_) or img.insn(sw_)
            if nx is None or nx.kind == 'invalid':
                viol.append(('BRANCH-12', ins.sw, sw_, ins.text)); break
            after += nx.size; sw_ += nx.size
    return viol


def new_violations(img, base_img, ranges=None):
    """Violations in img that the baseline image does not have (compared by rule, address and text)."""
    base = {(r, a, t) for r, a, _, t in check(base_img, ranges)}
    return [v for v in check(img, ranges) if (v[0], v[1], v[3]) not in base]


if __name__ == '__main__':
    import dn2frame as D
    img = D.image_from_sec7(sys.argv[1])
    if '--baseline' in sys.argv:
        vs = new_violations(img, D.image_from_sec7(sys.argv[sys.argv.index('--baseline') + 1]))
    else:
        vs = check(img)
    loopish = [v for v in vs if v[0].startswith('LOOP')]
    print(f'{len(vs)} violations ({len(loopish)} loop rules)')
    for r, a, b, t in vs[:60]:
        print(f'  {r:10s} {a:#08x} {b if isinstance(b, str) else hex(b)}  {t}')
