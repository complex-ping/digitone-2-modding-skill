"""Where everything lives. Every script imports this instead of hard-coding paths.

    DN2_WORKSPACE   your workspace (default ~/dn2-workspace), laid out by `dn2setup.py`:
        Digitone_II_OS1.11.syx      your own copy of Elektron's OS 1.11 (never shipped with these tools)
        repos/dnfw                  github.com/angellinares/dn2_firmware_explore  (container, sign, cbuild)
        repos/digikit               github.com/m-dwyer/digikit                    (ColdFire emulator)
        extracted/section_3.bin     MAIN OS (ColdFire), unpacked, base 0x40000400
        extracted/section_7.bin     the SHARC boot stream, unpacked
        extracted/section_3.dis     cached ColdFire disassembly (cfdis.sh)
        snap/                       DSP boot snapshots (dn2boot.py): boot_<tag>.snap.gz + sec7_<tag>.bin
        data/                       tables made from YOUR image (params_1.11.txt, frame_template.bin)
        build/                      your images and their emulator snapshots
    DN2_STOCK       override the stock image path
    DN2_M68K_BIN    directory holding m68k-linux-gnu-gcc / -objdump (else they must be on PATH)
"""
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
WS = pathlib.Path(os.environ.get('DN2_WORKSPACE', '~/dn2-workspace')).expanduser()
STOCK = pathlib.Path(os.environ.get('DN2_STOCK', WS / 'Digitone_II_OS1.11.syx')).expanduser()
DNFW = WS / 'repos/dnfw'
DIGIKIT = WS / 'repos/digikit'
EXTRACTED = WS / 'extracted'
SEC3_STOCK = EXTRACTED / 'section_3.bin'
SEC7_STOCK = EXTRACTED / 'section_7.bin'
SNAP = WS / 'snap'
DATA = WS / 'data'
BUILD = WS / 'build'

# sha256 of Elektron's published Digitone II OS 1.11 .syx: every address in these notes and tools is this image's.
STOCK_SHA256 = '2af43e65e3d8390b41c9f66222620f8cce027d73ed87db00c80b440f628472e0'


def use_dnfw():
    """Make `import dnfw` work (the dnfw checkout's src/)."""
    p = str(DNFW / 'src')
    if p not in sys.path:
        sys.path.insert(0, p)


def use_m68k():
    """Put the m68k cross toolchain on PATH (dnfw.patch.cbuild looks for m68k-linux-gnu-gcc / m68k-elf-gcc there)."""
    b = os.environ.get('DN2_M68K_BIN', str(WS / 'm68k/bin'))
    if os.path.isdir(b) and b not in os.environ['PATH'].split(os.pathsep):
        os.environ['PATH'] = b + os.pathsep + os.environ['PATH']


def snap(tag):
    """(section 7, boot snapshot) of a tag: `stock` after dn2setup, your own after dn2boot.py."""
    return str(SNAP / f'sec7_{tag}.bin'), str(SNAP / f'boot_{tag}.snap.gz')
