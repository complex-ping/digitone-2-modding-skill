"""Check track_trim in the SHARC emulator: a note on track 1 comes out at half the level of stock; track 2 unchanged.
    python3 test_emu.py [SECTION_7.bin]      (needs dn2setup.py --boot --template; ~1 min)"""
import math, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '../../scripts'))
import dn2paths as P
import dn2frame as D, dn2synth as S

mod7 = sys.argv[1] if len(sys.argv) > 1 else str(P.BUILD / 'DN2_1.11_TRACK_TRIM/section_7.bin')
sec7, snap = P.snap('stock')


def rms(path, track, n=60):
    song = S.Song().assign(0, S.Sound('WAVETONE')).track(0, track).note(5, 0, 60, 110, length=n)
    L, R, _ = D.render(D.Frame(D.image_from_sec7(path), snap), song.frames(n))
    tail = L[32 * 30:] + R[32 * 30:]                      # after the attack
    return math.sqrt(sum(x * x for x in tail) / len(tail))


ok = True
for track, want in ((0, 0.5), (1, 1.0)):
    a, b = rms(sec7, track), rms(mod7, track)
    r = b / a if a else float('nan')
    good = a > 1e-4 and abs(r - want) < 0.02
    ok &= good
    print(f'track {track + 1}: stock rms {a:.5f}, mod {b:.5f}, ratio {r:.4f} (want {want}) {"ok" if good else "FAIL"}')
print('OK' if ok else 'FAIL')
sys.exit(0 if ok else 1)
