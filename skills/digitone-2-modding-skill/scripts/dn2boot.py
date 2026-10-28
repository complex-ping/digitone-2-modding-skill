"""Boot the DN2 SHARC program in sim.py from its reset entry and save a snapshot where it stops.
Stops on: exception (unimplemented op), IDLE, RTS on an empty PC stack, a tight loop (<=64 sw) that keeps reading
MMRs (hardware wait), or max instructions. Usage: dn2boot.py [max_M] [out.snap.gz] [section7.bin]"""
import os
import sys, time, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import dn2paths as P; P.use_dnfw()
import sim

def main():
    maxm = float(sys.argv[1]) if len(sys.argv) > 1 else 500
    out = sys.argv[2] if len(sys.argv) > 2 else P.snap('stock')[1]
    img = None
    if len(sys.argv) > 3:
        import dn2frame
        img = dn2frame.image_from_sec7(sys.argv[3])
    s = sim.Sim(img); s.pc = 0x1C12E2
    last = collections.deque(maxlen=40)
    mmr_pcs = collections.Counter()
    orig = s.mem.r
    def r(a, size=4, sx=False):
        if s.mem.is_mmr(a & 0xFFFFFFFF): mmr_pcs[s.pc] += 1
        return orig(a, size, sx)
    s.mem.r = r
    t = time.time(); why = 'max'
    try:
        while s.icount < maxm * 1e6:
            hist = collections.Counter()
            def hook(sm, ins, hist=hist):
                last.append((sm.pc, ins.text)); hist[sm.pc >> 5] += 1
            s.on_insn = hook
            mmr_pcs.clear()
            if s.run(max_insns=1_000_000, until={0x1CA83E}) == 'until':
                why = 'reached main -> scheduler start (0x1ca83e)'; break
            (hot, n), = hist.most_common(1)
            print(f'{s.icount/1e6:7.1f}M {time.time()-t:6.0f}s pc {s.pc:#x} stk {len(s.pcstk)} loops {len(s.loopstk)} '
                  f'hot {hot << 5:#x} ({n}) mmr-reads {sum(mmr_pcs.values())}', flush=True)
            if n > 900_000 and sum(mmr_pcs.values()) > 1000:
                why = f'hardware wait loop near {hot << 5:#x} (MMR reads at {[hex(p) for p, _ in mmr_pcs.most_common(3)]})'
                break
    except sim.Stop as e:
        why = f'stop: {e}'
    except Exception as e:
        why = f'{type(e).__name__}: {e}'
    s.on_insn = None; s.mem.r = orig
    print('STOPPED:', why)
    for pc, tx in list(last)[-25:]: print(f'  {pc:#x} {tx}')
    print('pcstk', [hex(x) for x in s.pcstk], 'loops', [(hex(a) if isinstance(a, int) else a, hex(b), c) for a, b, c, _ in s.loopstk])
    sim.save_state(s, out); print('snapshot', out, f'{time.time()-t:.0f}s')

if __name__ == '__main__':
    main()
