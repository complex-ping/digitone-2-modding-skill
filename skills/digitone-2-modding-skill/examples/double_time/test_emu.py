"""Check double_time in the ColdFire emulator: the sequencer's 16th counter advances twice as fast as stock's.
    $DN2_WORKSPACE/repos/digikit/.venv-fast/bin/python test_emu.py [IMAGE.syx]
Run from anywhere; the first run per image boots the OS in the emulator (a few minutes), later runs reuse the snapshot."""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '../../scripts'))
import dn2paths as P
os.chdir(P.DIGIKIT)                     # digikit finds its own data relative to its checkout
import cfkit

SIXTEENTHS = 0x42C5A9D0                 # 16ths since PLAY (the sequencer's page counter)


def steps(sec3, out, frames=300):
    k = cfkit.Kit(sec3, str(P.STOCK), None, out)
    k.h.w(0x402A0DEC, cfkit.BE(300 * 120))        # 300 BPM (the UI resets the word: set it right before PLAY)
    k.h.tap(cfkit.PLAY)
    n0 = k.h.u32(SIXTEENTHS)
    k.h.frames(frames)
    return k.h.u32(SIXTEENTHS) - n0


img = sys.argv[1] if len(sys.argv) > 1 else str(P.BUILD / 'DN2_1.11_DOUBLE_TIME.syx')
stock = steps(str(P.SEC3_STOCK), str(P.BUILD / 'stock_emu'))
mod = steps(img[:-4] + '/section_3.bin', img[:-4] + '/emu_out')
print(f'16ths in 300 blocks at 300 BPM: stock {stock}, double_time {mod}')
ok = stock >= 3 and abs(mod - 2 * stock) <= 1
print('OK' if ok else 'FAIL')
sys.exit(0 if ok else 1)
