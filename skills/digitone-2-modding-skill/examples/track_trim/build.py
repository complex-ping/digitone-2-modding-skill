"""track_trim: track 1 plays 6 dB quieter in the main mix (a minimal SHARC DSP mod).

Where: the main mix (0x1c1f1e) first sums every voice into 16 stereo track buses (0x258f68 + 256 t: L 32 floats, R 32
floats), then mixes the buses on. Right after the voice sum, at 0x1c1fef, stock runs `R2 = DM(0x00257980)` (a 48-bit
instruction, no hardware loop active). We put `CALL trim` there; trim saves what it touches, runs an fxdsl kernel over
track 1's bus, restores, replays the displaced instruction and returns.
What: the kernel multiplies the 64 floats by GAIN, a word in our L2 data block (0.5 = -6 dB).
The send FX (delay / reverb / chorus) take the voice buffers earlier, so the sends keep their level: only the dry path
changes.

    python3 build.py [-o OUT.syx]        -> OUT.syx + OUT/section_7.bin
"""
import argparse, os, struct, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '../../scripts'))
import dn2paths as P
import cfmod, dspblocks as B, fxdsl, sasm, hwrules
from sasm import W, NOP48, const, ld_abs, st_abs, mv_i_from_r2

TRACK = 0                                       # 0 = track 1
BUS = 0x258F68 + 256 * TRACK                    # byte address of the track's stereo bus
HOOK, HOOK_STOCK = 0x1C1FEF, W(0x1002, 0x0025, 0x7980)          # R2 = DM(0x00257980)
GAIN = B.L2_DATA                                # float
SAVE = B.L2_DATA + 0x100                        # 21 words: R0..R15, I0..I4
R2_FROM = dict(sasm.R2_FROM, I4=W(0x717E, 0x013F))               # 5b 'R2 = In' (stock encodings)
RTS = (W(0x0ABE), 'RTS')


def kernel():
    k = fxdsl.Kernel('trim_bus', inputs={'bus': 'I1'})
    src = k['bus']
    dst = k.ptr(k.int_of(src))
    g = k.ld_abs(GAIN)
    with k.loop(64):                            # L 32 + R 32 floats, contiguous
        k.st_post(dst, k.ld_post(src) * g)
    return k


def trim(a, entry):
    for r in range(16):
        a(st_abs(r, SAVE + 4 * r))
    for n, i in enumerate(('I0', 'I1', 'I2', 'I3', 'I4')):
        a((R2_FROM[i], f'R2 = {i}')); a(st_abs(2, SAVE + 4 * (16 + n)))
    a(const('R2', BUS)); a(mv_i_from_r2(1))
    a((NOP48, 'NOP'))
    a((W(0x06BE, entry >> 16, entry & 0xFFFF), f'CALL {entry:#x}'))  # (branches >= 2 sw apart: NOPs around)
    a((NOP48, 'NOP'))
    for n, i in enumerate((0, 1, 2, 3, 4)):
        a(ld_abs(2, SAVE + 4 * (16 + n))); a(mv_i_from_r2(i))
    for r in range(16):
        a(ld_abs(r, SAVE + 4 * r))
    a((HOOK_STOCK, 'R2 = DM(0x00257980)'))      # the displaced instruction
    a(RTS)


def assemble():
    for _ in range(2):                          # pass 1 finds the kernel's address, pass 2 uses it
        a = sasm.Asm(B.L2_CODE_SW)
        entry = getattr(assemble, 'kentry', B.L2_CODE_SW)
        a.label('trim')
        trim(a, entry)
        assemble.kentry = a.sw()
        kernel().emit(a)
        for _ in range(12):                     # >= 12 valid 48-bit instructions after the last branch (prefetch)
            a((NOP48, 'NOP'))
        code, texts = a.assemble()
    while len(code) % 4:
        code += W(0x0001)
    return code


def build():
    s7 = P.SEC7_STOCK.read_bytes()
    stock = B.image(s7)
    code = assemble()
    data = struct.pack('<f', 0.5) + bytes(0x200 - 4)
    s7 = B.add_blocks(s7, [(B.L2_CODE, code), (B.L2_DATA, data)])
    s7 = B.call_hook(s7, HOOK, HOOK_STOCK, B.L2_CODE_SW)
    s7 = B.pad_lines(s7)
    img = B.image(s7)
    assert img.insn(HOOK).text.startswith('CALL'), img.insn(HOOK).text
    n = len(code) // 2
    sasm.lint(img, B.L2_CODE_SW, n, allowed=[f'R{i}' for i in range(16)] + ['I0', 'I1', 'I2', 'I3', 'I4', 'M4'])
    bad = hwrules.check(img, [(B.L2_CODE_SW, B.L2_CODE_SW + n), (HOOK - 12, HOOK + 12)])
    base = hwrules.check(stock, [(HOOK - 12, HOOK + 12)])
    assert len(bad) <= len(base), bad[:5]
    for i in img.linear(B.L2_CODE_SW, 12):
        print(f'  {i.sw:#08x}  {i.text}')
    print(f'code {len(code)} B at {B.L2_CODE:#x}, section 7 {len(s7)} B')
    return bytes(s7)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('-o', default=str(P.BUILD / 'DN2_1.11_TRACK_TRIM.syx'))
    out = ap.parse_args().o
    s7 = build()
    d = out[:-4]
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, 'section_7.bin'), 'wb').write(s7)
    # the ColdFire side stays stock: rebuild the container with section 7 replaced
    from dnfw.cli.files import read_image
    from dnfw.firmware.load import load
    import hashlib, pathlib
    fw = load(read_image(P.STOCK))
    s = fw.container.find(7)
    raw = cfmod.build_image(fw, {7: (s.id, s.dest, s7)})
    pathlib.Path(out).write_bytes(raw)
    print(out, '\nsha256', hashlib.sha256(raw).hexdigest())
    cfmod.flash_check(raw)
