"""cfatlas's RTTI reader on a synthetic image: no firmware needed."""
import pathlib, struct, sys, unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'skills/digitone-2-modding-skill/scripts'))
import cfatlas

BASE = 0x41000000
F1, F2, F3, MANAGER, HOST = 0x40001000, 0x40001010, 0x40001020, 0x40001030, 0x40001040


class Image:
    def __init__(self):
        self.mem = bytearray(0x400)

    def word(self, off, *vals):
        struct.pack_into(f'>{len(vals)}I', self.mem, off, *vals)
        return BASE + off

    def text(self, off, s):
        self.mem[off:off + len(s) + 1] = s.encode() + b'\0'
        return BASE + off


def image():
    i = Image()
    vptr = i.word(0x10, 0)
    base_name, sub_name, lam_name = i.text(0x200, '4Base'), i.text(0x210, '3Sub'), i.text(0x220, '*ZN3Sub4loadEvEUlvE_')
    base_ti = i.word(0x40, vptr, base_name, 0, 0)
    sub_ti = i.word(0x50, vptr, sub_name, base_ti)
    lam_ti = i.word(0x60, vptr, lam_name, 0, 0)
    i.word(0x80, 0, base_ti, F1, F2)                # Base: two slots
    i.word(0xA0, 0, sub_ti, F1, F3)                 # Sub inherits slot 0, overrides slot 1
    return i, base_ti, sub_ti, lam_ti


class Rtti(unittest.TestCase):
    def rtti(self, i):
        return cfatlas.read_rtti(bytes(i.mem), {F1, F2, F3}, base=BASE, lo=0, hi=len(i.mem))

    def test_classes_and_bases(self):
        i, base_ti, sub_ti, _ = image()
        r = self.rtti(i)
        self.assertEqual(r['infos'][sub_ti]['base'], base_ti)
        self.assertIsNone(r['infos'][base_ti]['base'])

    def test_vtables(self):
        i, _, sub_ti, _ = image()
        vts = {v['info']: v for v in self.rtti(i)['vtables']}
        self.assertEqual(vts[sub_ti]['slots'], [F1, F3])
        self.assertEqual(vts[sub_ti]['addr'], BASE + 0xA8)

    def test_slot_named_for_the_base_most_class(self):
        i, _, _, _ = image()
        auto = cfatlas.name_from_rtti(self.rtti(i), {})
        self.assertIn('Base', auto[F1][0])
        self.assertIn('Sub', auto[F3][0])

    def test_lambda_names_its_method(self):
        i, _, _, lam_ti = image()
        r = self.rtti(i)
        r['infos'][lam_ti]['name'] = 'Sub::load()::{lambda()#1}'
        auto = cfatlas.name_from_rtti(r, {lam_ti: {MANAGER}, MANAGER: {HOST}})
        self.assertEqual(auto[HOST][0], 'Sub::load()')
        self.assertTrue(auto[MANAGER][0].endswith('::manager'))

    def test_operands_read_hex_and_decimal(self):
        self.assertEqual(cfatlas.operands('lea 0x402fd000,%a2'), [0x402fd000])
        self.assertEqual(cfatlas.operands(f'movel #{0x401dd000},%d0'), [0x401dd000])
        self.assertEqual(cfatlas.operands('moveq #4,%d0'), [])


if __name__ == '__main__':
    unittest.main()
