"""Build the double_time mod: python3 build.py [-o OUT.syx]   (default $DN2_WORKSPACE/build/DN2_1.11_DOUBLE_TIME.syx)"""
import argparse, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '../../scripts'))
import cfmod, dn2paths as P

ap = argparse.ArgumentParser()
ap.add_argument('-o', default=str(P.BUILD / 'DN2_1.11_DOUBLE_TIME.syx'))
a = ap.parse_args()
m = cfmod.Mod([os.path.join(HERE, 'mod.c'), os.path.join(HERE, 'hooks.S')], entries=['dt_stub', 'dt_step', 'dt_blocks'],
              init='dt_init')
m.context(0x40027ABA, '207980005398', 'a0 = "now" right before the hook (kept by the stub)')
m.context(0x40027AD0, 'd080', 'stock doubles d0 after the hook: 2 x d0 half-units per block')
m.jsr(0x40027AC0, 'dt_stub', stock='2039402a0dec', why='audio ISR: tempo word load -> dt_stub')
m.write(a.o)
