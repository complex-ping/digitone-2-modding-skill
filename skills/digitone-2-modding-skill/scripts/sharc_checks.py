"""Behaviour checks of stock SHARC routines: call them in the emulator and compare with what their names claim. Reading alone
got some of these wrong (sinf and cosf swapped, floorf taken for truncf). Needs the stock snapshot (dn2setup.py --boot).

    python3 sharc_checks.py [OUT.tsv]
Every line must say ok. Add a line when a mod comes to rely on a routine.
"""
import math, os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DN2_SIM', 'py')
import dn2frame as D
f = D.Frame()
s = f.s
fl = lambda x: struct.unpack('<I', struct.pack('<f', x))[0]
un = lambda w: struct.unpack('<f', struct.pack('<I', w & 0xffffffff))[0]
res = []


def callf(fn, *a, rts=False):
    for r, v in zip((4, 8, 12), a):
        s.px.R[r] = v
    if rts:
        s.pcstk.append(D.RET)
    f.call(fn)
    return un(s.px.R[0])


def show(name, addr, xs, want, rts=False):
    got = []
    for x in xs:
        try:
            got.append(callf(addr, *[fl(v) for v in (x if isinstance(x, tuple) else (x,))], rts=rts))
        except Exception as e:
            got.append(f'{e!r}'[:40])
    ok = all(isinstance(g, float) and abs(g - w) < 1e-2 * max(1, abs(w)) for g, w in zip(got, want))
    print(('ok  ' if ok else 'BAD ') + f'{addr:#x} {name}: {[round(g, 4) if isinstance(g, float) else g for g in got]} want {[round(w, 4) for w in want]}', flush=True)
    res.append((addr, name, ok, got))


show('cosf', 0x1c1018, [0.0, 0.5, 1.0, 2.0], [math.cos(x) for x in (0.0, 0.5, 1.0, 2.0)])
show('sinf', 0x1c10e4, [0.0, 0.5, 1.0, 2.0], [math.sin(x) for x in (0.0, 0.5, 1.0, 2.0)], rts=True)
show('tanf', 0x1c125e, [0.1, 0.5, 1.0], [math.tan(x) for x in (0.1, 0.5, 1.0)])
show('log10f', 0x1c0b60, [1.0, 10.0, 100.0, 0.5], [math.log10(x) for x in (1.0, 10.0, 100.0, 0.5)])
show('powf', 0x1c0c76, [(2.0, 3.0), (10.0, 2.0), (2.0, 0.5)], [8.0, 100.0, math.sqrt(2)])
show('float_div', 0x1c06ba, [(1.0, 2.0), (3.0, 4.0)], [0.5, 0.75], rts=True)
xs = (0.0, 0.25, 2.0, 100.0)
show('sqrtf', 0x1c122e, xs, [math.sqrt(x) for x in xs], rts=True)
xs = (0.0, 1.0, -1.0, 2.5)
show('expf', 0x1c11a5, xs, [math.exp(x) for x in xs], rts=True)
xs = (0.0, 1.5, -1.5, 3.999)
show('floorf', 0x1c07cc, xs, [float(math.floor(x)) for x in xs], rts=True)      # FIX with MODE1.TRUNC: toward -inf


def calli(fn, *a):
    """Integer call: the routines below return through the frame or with RTS; retries with RTS on a stray return."""
    for rts in (False, True):
        s.pcstk = []; s.loopstk = []; s.pending = None
        for r, v in zip((4, 8, 12), a):
            s.px.R[r] = v & 0xffffffff
        if rts:
            s.pcstk.append(D.RET)
        try:
            f.call(fn, max_insns=2_000_000)
            return s.px.R[0] & 0xffffffff
        except Exception:
            pass
    raise RuntimeError(f'{fn:#x} did not return')


def check(name, addr, got, want):
    ok = got == want
    print(('ok  ' if ok else 'BAD ') + f'{addr:#x} {name}: {got} want {want}', flush=True)
    res.append((addr, name, ok, got))


m = s.mem
BUF = 0x26f800                            # scratch; a Frame is throwaway
rb = lambda a, n: [m.r(a + i, 1) & 0xff for i in range(n)]
for i in range(32):
    m.w(BUF + i, 0x11, 1)
calli(0x1c0bde, BUF + 3, 0x5a, 9)
check('memset', 0x1c0bde, rb(BUF, 14), [0x11] * 3 + [0x5a] * 9 + [0x11] * 2)
for i in range(32):
    m.w(BUF + i, i, 1)
    m.w(BUF + 0x40 + i, 0xee, 1)
calli(0x1c0756, BUF + 0x41, BUF + 2, 7)
check('memcpy_bytes', 0x1c0756, rb(BUF + 0x40, 10), [0xee] + list(range(2, 9)) + [0xee] * 2)
# udiv32 clears RND32 and divides in 40 bit floats; the emulator's floats are 32 bit, so quotients above 2^24 come out rounded
check('udiv32', 0xb8b4dc, [calli(0xb8b4dc, *x) for x in ((100, 7), (1000000, 3), (5, 9), (0x80000000, 2))],
      [100 // 7, 1000000 // 3, 0, 0x40000000])
m.w(0x241228, 0x12345678); m.w(0x24122c, 0x9abcdef0)
x = 0x9abcdef012345678
x = (x * 6364136223846793005 + 1442695040888963407) & (2 ** 64 - 1)
check('rand', 0x1c0df6, [calli(0x1c0df6), m.r(0x241228), m.r(0x24122c)], [(x >> 32) & 0x7fffffff, x & 0xffffffff, x >> 32])
check('Irq_checkSecId', 0xb8b598, [calli(0xb8b598, v) for v in (0, 0x10, 0xc0, 0xc1, 0x1000)], [0, 0, 0, 0xffffffff, 0xffffffff])
m.w(BUF, 6); m.w(BUF + 4, 2); m.w(BUF + 8, 16)
check('prvBytesInBuffer', 0xb86f08, calli(0xb86f08, BUF), (16 + 2 - 6) % 16)
L = BUF + 0x40
calli(0xb85ad5, L)
items = [(L + 0x40, 5), (L + 0x80, 1), (L + 0xc0, 3), (L + 0x100, 0xffffffff)]
for a, v in items:
    calli(0xb85acc, a)
    check('vListInitialiseItem', 0xb85acc, m.r(a + 16), 0)
    m.w(a, v)
    calli(0xb85a45, L, a)
order = []
it = m.r(L + 12)
for _ in range(5):
    if it == L + 8:
        break
    order.append(m.r(it))
    it = m.r(it + 4)
check('vListInitialise', 0xb85ad5, m.r(L + 8), 0xffffffff)
check('vListInsert', 0xb85a45, (m.r(L), order), (4, [1, 3, 5, 0xffffffff]))
if len(sys.argv) > 1:
    with open(sys.argv[1], 'w') as o:
        for a, n, ok, got in res:
            o.write(f'{a:x}\t{n}\t{"ok" if ok else "BAD"}\t{got}\n')
sys.exit(0 if all(r[2] for r in res) else 1)
