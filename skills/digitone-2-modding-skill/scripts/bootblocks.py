"""Boot-block alignment [HW]: builds whose added L2 code blocks were 8n + 4 bytes long (assemblers pad to 4) were silent
from power-on: the ADSP-21569's L2 keeps ECC per 64-bit word, a boot DMA ending in half a word leaves it with a bad ECC,
and the I-cache fill over the block's end faults the core on the first block. The emulator copies bytes (no ECC) and
played them. dspblocks.pad_lines pads every block you add to 32-byte lines; this check fails any added block
(L2 0x20088400.., DDR code 0x80a00000.., DDR data 0x84000000..) that is not.
    python3 bootblocks.py SEC7.bin"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import dn2paths as P; P.use_dnfw()
from dnfw.image import bootstream as bs

OURS = [(0x20088400, 0x200C0000), (0x80A00000, 0x81000000), (0x84000000, 0x85000000)]
LINE = 32

w = bs.walk(open(sys.argv[1], 'rb').read())
ours = [b for b in w.blocks if b.has_payload and any(lo <= b.target < hi for lo, hi in OURS)]
bad = [b for b in ours if b.count % LINE or b.target % LINE]
for b in ours:
    print(f'  {"ok  " if b not in bad else "FAIL"} {b.target:#010x} {b.count:7d} B')
print(f'{len(ours)} of our boot blocks, {len(bad)} not whole {LINE}-byte lines {"ok" if not bad else "FAIL"}')
print('FAIL' if bad else 'OK')
sys.exit(1 if bad else 0)
