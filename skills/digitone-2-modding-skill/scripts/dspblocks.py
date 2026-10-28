"""dspblocks: add your own code / data blocks to the SHARC boot stream (section 7) and hook them into the stock program.

Section 7 is an ADI boot stream: 16-byte block headers (code AD xx xx 01, target, count, argument; the 16 bytes XOR to 0
via HDRCHK in byte 2) + payloads, ending in a FINAL block whose target is the entry point. The stock image loads L2 code
only up to 0x2008823c; above it, up to 0x200B0000, is free and HW-proven (references/sharc.md "Memory"):

    L2_CODE 0x20090000 (VISA short-word 0xBC8000) .. 0x20098000      your code
    L2_DATA 0x20098000 .. 0x200A0000                                  your state (zero-filled at boot by a FILL block)
    0x200A0000 .. 0x200B0000                                          more (HW-proven); NOT above 0x200B0000 (boot ROM,
                                                                      DMA buffers: the unit no longer starts)

    import dspblocks as B, sasm
    a = sasm.Asm(B.L2_CODE_SW); ...; code, texts = a.assemble()
    s7 = B.add_blocks(stock_s7, [(B.L2_CODE, code), (B.L2_DATA, None, 0x1000)])   # payload, or None + a FILL length
    s7 = B.call_hook(s7, 0x1C1FEF, sasm.W(0x1002, 0x0025, 0x7980), entry_sw)      # CALL over one stock 48-bit instr
    s7 = B.pad_lines(s7)                                                          # every block of yours: whole 32 B
Hardware rules that never show in the emulator (each one silenced a unit once): whole 32-byte lines per block (L2 ECC),
stock encodings only (encodecheck.py), the PRM's loop / DAG / branch rules (hwrules.py, sasm.lint, shapecheck.py),
section 7 under 1 MiB (the ColdFire's DSP loader limit).
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dn2paths as P  # noqa: E402

P.use_dnfw()
from dnfw.image import bootstream as bs  # noqa: E402
import visa  # noqa: E402

L2_CODE, L2_DATA, L2_END = 0x20090000, 0x20098000, 0x200A0000
L2_CODE_SW = visa.Image.load_to_sw(L2_CODE)           # 0xBC8000
OURS = [(0x20088400, 0x200B0000), (0x80A00000, 0x81000000)]   # where your blocks may live (L2 above stock, DDR code)
LINE = 32
NOP16 = struct.pack('<H', 0x0001)


def _xor(h):
    h[2] = 0
    x = 0
    for c in h:
        x ^= c
    h[2] = x
    return h


def header(flags, target, count, argument=0):
    return bytes(_xor(bytearray(struct.pack('<IIII', 0xAD000000 | flags << 8 | 0x01, target, count, argument))))


def _fix_first(out, firsts):
    for k, at in enumerate(firsts):
        nxt = firsts[k + 1] if k + 1 < len(firsts) else len(out)
        h = bytearray(out[at:at + 16])
        struct.pack_into('<I', h, 12, nxt - at - 16)          # a FIRST block's argument = the bytes up to the next FIRST
        out[at:at + 16] = _xor(h)


def add_blocks(s7, blocks):
    """Insert blocks before the FINAL block: (target, payload) or (target, None, fill_bytes) (zero-filled at boot)."""
    raw = bytes(s7)
    w = bs.walk(raw)
    final = w.blocks[-1]
    assert final.flags & bs.FLAG_FINAL and w.complete
    ins = b''
    for b in blocks:
        if b[1] is None:
            ins += header(bs.FLAG_FILL, b[0], b[2])
        else:
            ins += header(0, b[0], len(b[1])) + bytes(b[1])
    out = bytearray(raw[:final.offset] + ins + raw[final.offset:])
    _fix_first(out, [b.offset + (len(ins) if b.offset >= final.offset else 0)
                     for b in w.blocks if b.flags & bs.FLAG_FIRST])
    assert bs.walk(bytes(out)).complete
    return out


def pad_lines(s7, ours=OURS, code=OURS):
    """Every payload block whose target lies in `ours` padded to whole 32-byte lines (NOP parcels in code, zeros in data).
    A block of 8n + 4 bytes boots in the emulator and leaves the unit silent from power-on (L2 ECC per 64-bit word)."""
    raw = bytes(s7)
    w = bs.walk(raw)
    assert w.complete
    spans = [(b.target, b.target + b.count) for b in w.blocks if b.count]
    out, firsts = bytearray(), []
    for b in w.blocks:
        hdr = bytearray(raw[b.offset:b.offset + 16])
        payload = bytes(raw[b.payload_at:b.payload_at + b.count]) if b.has_payload else b''
        if b.flags & bs.FLAG_FIRST:
            firsts.append(len(out))
        if b.has_payload and any(lo <= b.target < hi for lo, hi in ours) and len(payload) % LINE:
            n = -len(payload) % LINE
            end = b.target + len(payload)
            assert not any(lo < end + n and end < hi for lo, hi in spans if (lo, hi) != (b.target, end)), hex(b.target)
            is_code = any(lo <= b.target < hi for lo, hi in code)
            payload += (NOP16 * (n // 2)) if is_code else bytes(n)
            struct.pack_into('<I', hdr, 8, len(payload))
            _xor(hdr)
        out += hdr + payload
    _fix_first(out, firsts)
    w2 = bs.walk(bytes(out))
    assert w2.complete and len(w2.blocks) == len(w.blocks)
    return out


def call_hook(s7, site_sw, stock, entry_sw):
    """Replace one stock 48-bit instruction at `site_sw` (its bytes `stock`, asserted) by `CALL entry_sw` (non-delayed).
    Your routine must replay the displaced instruction and keep every register / flag the code after the site reads."""
    out = bytearray(s7)
    at = visa.Image.sw_to_load(site_sw)
    have = bs.read_span(bytes(out), at, 6)
    if have != bytes(stock):
        raise SystemExit(f'hook {site_sw:#x}: holds {have.hex()}, not {bytes(stock).hex()}')
    bs.write_span(out, at, struct.pack('<3H', 0x06BE, entry_sw >> 16, entry_sw & 0xFFFF))
    return out


def image(s7):
    """visa.Image of a section 7 in memory (for .insn(sw).text checks)."""
    img = visa.Image.__new__(visa.Image)
    img.raw = bytes(s7)
    img.regions = bs.load_regions(img.raw)
    return img
