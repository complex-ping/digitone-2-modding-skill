"""What the SHARC tools can show without firmware: name records survive, and the fractional multiply rule."""
import os, pathlib, sys, tempfile, types, unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent / 'skills/digitone-2-modding-skill/scripts'
sys.path.insert(0, str(ROOT))
os.environ['DN2_WORKSPACE'] = tempfile.mkdtemp()
import sharcatlas
import sim


class Names(unittest.TestCase):
    def test_round_trip(self):
        sharcatlas.OUT.mkdir(parents=True, exist_ok=True)
        sharcatlas.write_names({0x1c1018: ('cosf', 'verified', 'ran it')})
        self.assertEqual(sharcatlas.read_names(), {0x1c1018: ('cosf', 'verified', 'ran it')})

    def test_compact(self):
        ins = [types.SimpleNamespace(sw=0x100, text='R2 = 0x40000000  ; =2'), types.SimpleNamespace(sw=0x102, text='NOP'),
               types.SimpleNamespace(sw=0x103, text='CALL 0x200')]
        self.assertEqual(sharcatlas._compact(ins, {0x200: 'powf'}, {0x103}), ['R2 = 2', '103:', 'CALL powf'])


class FractionalMultiply(unittest.TestCase):
    def mul(self, op, x, y):
        pe = types.SimpleNamespace(R=[0, x, y, 0], MRF=0, MRB=0)
        me = types.SimpleNamespace(_mr_result=lambda acc, frac, rnd: sim.Sim._mr_result(None, acc, frac, rnd))
        sim.Sim._mul(me, pe, op, 3, 1, 2)
        return pe.R[3]

    def test_unsigned_is_not_shifted(self):                 # 0x48 = (UUF); stock's 64 bit rand needs the plain high word
        self.assertEqual(self.mul(0x48, 0x80000000, 0x80000000), 0x40000000)

    def test_signed_is_shifted(self):                       # 0x78 = (SSF): 0.5 * 0.5 = 0.25 in 1.31
        self.assertEqual(self.mul(0x78, 0x40000000, 0x40000000), 0x20000000)


if __name__ == '__main__':
    unittest.main()
