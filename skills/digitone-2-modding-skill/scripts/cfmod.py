"""cfmod: build a ColdFire (MAIN OS, section 3) mod from C + asm sources into a signed .syx.

The pattern (proven on hardware many times):
  * your C / asm is compiled and linked at a fixed RAM address (default 0x46800000, free on 1.11) by dnfw.patch.cbuild
    (m68k GCC, -mcpu=5475 = ColdFire V4e, -Os, no libc);
  * dnfw's startup loader (a 138-byte cave) copies the appended `DNFW` area there at boot and calls your init function;
  * every hook site is checked against the stock bytes before it is overwritten -- a wrong address fails the build, it
    never ships;
  * section 3 is recompressed, the container rebuilt and re-signed by dnfw (checksums + HMAC), section 7 kept or replaced.

    import cfmod
    m = cfmod.Mod(sources=['mymod.c', 'hooks.S'], entries=['my_init', 'my_stub'], init='my_init')
    m.jsr(0x40027AC0, 'my_stub', stock='2039402a0dec')          # 6 bytes: jsr my_stub (+ nops) over a known instruction
    m.jmp(0x40134490, 'my_memcpy_stub', displaced=8)           # 8 bytes: jmp my_stub; the stub replays them (symbol
                                                              #   my_memcpy_stub_displaced must hold the same bytes)
    m.patch(0x400D7CD0, '2f0a203c', '70004e75', 'swing getter returns 0')
    m.context(0x40027AC6, '...', 'what my hook relies on')     # read, not written: asserted
    m.write('build/MY.syx')                                    # + build/MY/{section_3.bin, code.bin, symbols.txt}

Rules that cost hardware round trips (references/coldfire.md "Adding code"):
  * a stub replays exactly the bytes it displaced, and keeps d0/d1/a0/a1 when it sits mid-function;
  * no libc: a zero-initialised local struct / array becomes a memset call and the link fails;
  * every entry symbol a hook names must be in `entries` (else --gc-sections drops it);
  * appending asm to a .S file: check the last section directive (code after `.bss` lands in zeroed BSS).
"""
import hashlib
import os
import pathlib
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dn2paths as P  # noqa: E402

P.use_dnfw()
P.use_m68k()
from dnfw.cli.files import read_image  # noqa: E402
from dnfw.firmware import build as fwbuild  # noqa: E402
from dnfw.firmware.load import load  # noqa: E402
from dnfw.patch import area, cbuild, loader  # noqa: E402

BASE = 0x40000400                 # section 3 load address
CODE_VA = 0x46800000              # free RAM on 1.11 (0x46700000..0x46900000 HW-confirmed unused; dnfw's own mods use
                                  # 0x467c8000..0x467fffff -- keep clear of them if you combine)


def section(path, sid):
    return bytes(load(read_image(pathlib.Path(path))).container.find(sid).unpack())


class Mod:
    def __init__(self, sources, entries=(), init=None, code_va=CODE_VA, include=(), defines=None, stock=None):
        self.sources = [pathlib.Path(s) for s in sources]
        self.entries = list(entries) + ([init] if init and init not in entries else [])
        self.init, self.code_va = init, code_va
        self.include = [pathlib.Path(i) for i in include]
        self.defines = defines or {}
        self.stock = pathlib.Path(stock or P.STOCK)
        self.ops = []                 # (kind, va, ...)
        self.checks = []

    # -- what to change ---------------------------------------------------------------------------------------------
    def jsr(self, va, stub, stock, why=''):
        """jsr stub over `stock` (hex, >= 6 bytes, padded with nops). The stub does the displaced work itself, or
        replays it, then rts."""
        self.ops.append(('call', va, '4eb9', stub, bytes.fromhex(stock), why or f'jsr {stub}'))

    def jmp(self, va, stub, displaced, why=''):
        """jmp stub over `displaced` bytes; the stub's `<stub>_displaced` symbol holds those bytes (replayed) and it jumps
        back to va + displaced. The build asserts the stub's copy equals the image's bytes."""
        self.ops.append(('jmp', va, '4ef9', stub, displaced, why or f'jmp {stub}'))

    def patch(self, va, stock, new, why=''):
        self.ops.append(('patch', va, bytes.fromhex(stock), bytes.fromhex(new), why))

    def pointer(self, va, stock_value, symbol, why=''):
        """A 32-bit word (a vtable slot, a table entry) -> one of your symbols; the old value is asserted."""
        self.ops.append(('ptr', va, stock_value, symbol, why))

    def context(self, va, stock, why=''):
        self.checks.append((va, bytes.fromhex(stock), why))

    # -- build ------------------------------------------------------------------------------------------------------
    def compose(self, base_3=None):
        base_3 = base_3 if base_3 is not None else section(self.stock, 3)
        inc = self.include + [P.DNFW / 'csrc/include'] + sorted({s.parent for s in self.sources})
        code = cbuild.build(self.sources, base=self.code_va, include=inc, entries=self.entries, defines=self.defines)
        chunk = area.CodeChunk(self.code_va, code.image, code.bss, code[self.init] if self.init else 0)
        content = loader.install(base_3, [(area.CODE, chunk.pack())])
        print(f'C: {len(code.image)} B + {code.bss} B BSS at {self.code_va:#x}; MAIN OS {len(base_3)} -> {len(content)} B')
        img = lambda sym, n: code.image[code[sym] - self.code_va:code[sym] - self.code_va + n]
        for va, want, why in self.checks:
            have = bytes(content[va - BASE:va - BASE + len(want)])
            if have != want:
                raise SystemExit(f'context {va:#010x} ({why}): {have.hex()}, not {want.hex()}')
        for op in self.ops:
            kind, va = op[0], op[1]
            if kind == 'call':
                _, _, opc, stub, stock, why = op
                put(content, va, stock, pad(bytes.fromhex(opc) + struct.pack('>I', code[stub]), len(stock)), why)
            elif kind == 'jmp':
                _, _, opc, stub, n, why = op
                displaced = img(stub + '_displaced', n)
                put(content, va, displaced, pad(bytes.fromhex(opc) + struct.pack('>I', code[stub]), n), why)
            elif kind == 'patch':
                _, _, stock, new, why = op
                put(content, va, stock, new, why)
            elif kind == 'ptr':
                _, _, old, sym, why = op
                put(content, va, struct.pack('>I', old), struct.pack('>I', code[sym]), why or f'-> {sym}')
        return content, code

    def write(self, out, sec7=None):
        """out.syx (+ out/ with section_3.bin, code.bin, symbols.txt). sec7: a section 7 file to ship instead of stock's."""
        content, code = self.compose()
        firmware = load(read_image(self.stock))
        s3, s7 = firmware.container.find(3), firmware.container.find(7)
        sections = {3: (s3.id, s3.dest, bytes(content))}
        if sec7:
            sections[7] = (s7.id, s7.dest, pathlib.Path(sec7).read_bytes())
        out_bytes = build_image(firmware, sections)
        out = pathlib.Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(out_bytes)
        d = out.with_suffix('')
        d.mkdir(exist_ok=True)
        (d / 'section_3.bin').write_bytes(bytes(content))
        (d / 'code.bin').write_bytes(code.image)
        (d / 'symbols.txt').write_text(''.join(f'{v:#010x} {k}\n' for k, v in sorted(code.symbols.items(),
                                                                                        key=lambda kv: kv[1])))
        print(f'{out}\nsha256 {hashlib.sha256(out_bytes).hexdigest()}')
        flash_check(out_bytes)
        return out_bytes


def flash_check(syx_bytes, margin=12_288):
    """The running OS receives an upgrade into a 2 MiB buffer (2,097,152 B: preamble + container, padded to 101-byte
    packets). Over it the upload stalls and the unit keeps its old OS. Measure, never estimate: aPLib is nonlinear."""
    from dnfw.syx import transport
    got = len(transport.decode(bytes(syx_bytes)).stream)
    print(f'flash size: {got:,} of 2,097,152 B received ({2_097_152 - got:,} free)')
    if got > 2_097_152:
        raise SystemExit('over the 2 MiB OS upgrade buffer: this image cannot be installed')
    if got > 2_097_152 - margin:
        print(f'WARNING: less than {margin:,} B left under the 2 MiB buffer')
    return got


def build_image(firmware, sections):
    """{sid: (id, dest, unpacked bytes)} -> the signed .syx bytes (zcache: aPLib with a deeper match search, cached)."""
    import zcache
    return fwbuild.build(firmware, zcache.compress_all(sections))


def pad(b, n):
    assert len(b) <= n and (n - len(b)) % 2 == 0, (len(b), n)
    return b + b'\x4e\x71' * ((n - len(b)) // 2)


def put(content, va, stock, new, what):
    at = va - BASE
    have = bytes(content[at:at + len(stock)])
    if have != stock:
        raise SystemExit(f'{what}: {va:#010x} holds {have.hex()}, not {stock.hex()}')
    content[at:at + len(new)] = new
    print(f'  {va:#010x}  {what}')
