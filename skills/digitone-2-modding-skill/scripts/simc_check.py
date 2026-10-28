"""Lockstep check of simc.CSim (the C core) against sim.Sim: the same snapshot and packets, the full machine state compared
after every audio block (memory words, registers, stacks, MODE1 / sysregs, icount, pc). Also times both. Run it after any
change to sim.py or simc_core.c (they must change together).

    pypy3 simc_check.py [--blocks N] [--song idle|synth] [--tag TAG] [--speed]
Songs: idle (no notes: the boot frame), synth (16 voices of the four stock machines).
"""
import argparse, hashlib, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import dn2paths as P; P.use_dnfw()
import sim, simc
import dn2frame as D


def state_hash(s):
    h = hashlib.sha256()
    for k, v in sorted(s.mem.words.items()):
        h.update(k.to_bytes(8, 'little')); h.update((v & 0xFFFFFFFF).to_bytes(4, 'little'))
    h.update(repr(sorted(s.mem.mmr.items())).encode())
    h.update(repr((s.px.R, s.py.R, s.px.ast, s.py.ast, s.px.sticky, s.py.sticky, s.px.MRF, s.px.MRB, s.py.MRF, s.py.MRB,
                   s.I, s.M, s.L, s.Bx, sorted(s.sysr.items()), s.PX1, s.PX2, s.pc, s.pcstk, s.loopstk, s.ststk,
                   s.pending, s.icount)).encode())
    return h.hexdigest()


def frames(song, n):
    import dn2synth as S
    if song == 'idle':
        return [None] * n
    if song == 'synth':
        s = S.Song()
        for v in range(16):
            s.assign(v, S.Sound(('WAVETONE', 'FMTONE', 'SWARMER', 'FMDRUM')[v % 4])).track(v, v)
            s.note(2 + v % 5, v, 36 + 3 * v, 110, length=n)
        return s.frames(n)
    raise SystemExit(f'song {song}?')


def make(cls):
    return D.Frame(D.image_from_sec7(SEC7), SNAP, cls=cls)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--blocks', type=int, default=40); ap.add_argument('--song', default='synth')
    ap.add_argument('--tag', default='stock'); ap.add_argument('--speed', action='store_true')
    ap.add_argument('--both', action='store_true', help='with --speed: time the interpreter too')
    a = ap.parse_args()
    TAG = a.tag
    SEC7, SNAP = P.snap(TAG)
    fr = frames(a.song, a.blocks)
    ref, csim = make(sim.Sim), make(simc.CSim)
    t_ref = t_c = 0.0
    for b, p in enumerate(fr):
        if not a.speed or b == 0 or a.both:
            t0 = time.time(); ref.block(p); t_ref += time.time() - t0
        t0 = time.time(); csim.block(p); t_c += time.time() - t0
        if a.speed and (b + 1) % 100 == 0:
            print(f'  {b + 1}: csim {(b + 1) / t_c:.2f} blocks/s' + (f', ref {(b + 1) / t_ref:.2f}' if a.both else ''), flush=True)
        if not a.speed:
            h1, h2 = state_hash(ref.s), state_hash(csim.s)
            if h1 != h2:
                print(f'block {b}: STATE DIFFERS (icount ref {ref.s.icount} csim {csim.s.icount}, pc {ref.s.pc:#x} {csim.s.pc:#x})')
                for nm in ('I', 'M', 'L', 'Bx', 'pcstk', 'loopstk'):
                    if getattr(ref.s, nm) != getattr(csim.s, nm):
                        print(' ', nm, getattr(ref.s, nm), getattr(csim.s, nm))
                if ref.s.px.R != csim.s.px.R: print('  R', ref.s.px.R, csim.s.px.R)
                diff = [k for k in set(ref.s.mem.words) | set(csim.s.mem.words) if ref.s.mem.words.get(k) != csim.s.mem.words.get(k)]
                print('  words differ:', len(diff), [hex(k) for k in sorted(diff)[:8]])
                sys.exit(1)
    n = len(fr)
    print(f'{a.song}: {n} blocks {"(speed only)" if a.speed else "identical state after every block"}; '
          f'ref {t_ref:.1f}s{"" if a.speed else f" ({n / t_ref:.2f} blocks/s)"}, csim {t_c:.1f}s ({n / t_c:.2f} blocks/s), '
          f'C {csim.s.c_insns} insns, Python {csim.s.py_insns}')
    print('OK')
