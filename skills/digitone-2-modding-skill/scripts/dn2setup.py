"""Set up (or check) a DN2 modding workspace from YOUR copy of Elektron's Digitone II OS 1.11.

    python3 dn2setup.py                 check + extract sections, parameter table, ColdFire disassembly, EMAC rows
    python3 dn2setup.py --boot          + boot the stock DSP in the SHARC emulator once (snap/boot_stock.snap.gz, ~2-5 min)
    python3 dn2setup.py --template      + capture real OS frames in the ColdFire emulator (data/frame_template.bin; digikit)

Workspace = $DN2_WORKSPACE (default ~/dn2-workspace); layout in dn2paths.py. Nothing here downloads anything: it prints
what to clone / install when something is missing. No firmware is ever written outside your workspace.
"""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dn2paths as P  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OK, MISS = '[ok]  ', '[--]  '


def say(ok, what, hint=''):
    print((OK if ok else MISS) + what + ('' if ok or not hint else f'\n        -> {hint}'))
    return ok


def pypy():
    return next((shutil.which(n) for n in ('pypy3', 'pypy3.11', 'pypy3.10') if shutil.which(n)), None)


def digikit_python():
    for v in ('.venv-fast', '.venv'):
        p = P.DIGIKIT / v / 'bin/python'
        if p.exists():
            return str(p)
    return None


def emac_rows(dis, out, lo=0x40000400, hi=0x401D0000):
    """Every EMAC instruction of the MAIN OS code (cfemac runs them in Python: Unicorn's fractional EMAC is wrong)."""
    rows = []
    for line in open(dis):
        m = re.match(r'([0-9a-f]{8}):\s+((?:[0-9a-f]{4} ?)+)\s+(\S+)\s*(.*)', line)
        if not m:
            continue
        a, mn, ops = int(m.group(1), 16), m.group(3), m.group(4).strip()
        if lo <= a < hi and (mn.startswith(('mac', 'msac', 'movclr')) or
                             (mn == 'movel' and re.search(r'%(macsr|acc\d|accext\d\d|mask)\b', ops))):
            rows.append(f'{a:#x} {"".join(m.group(2).split())} {mn} {ops}')
    open(out, 'w').write('\n'.join(rows) + '\n')
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--boot', action='store_true', help='boot the stock DSP once (SHARC emulator snapshot)')
    ap.add_argument('--template', action='store_true', help='capture real OS frames (ColdFire emulator)')
    a = ap.parse_args()
    print(f'workspace {P.WS}')
    for d in (P.EXTRACTED, P.SNAP, P.DATA, P.BUILD):
        d.mkdir(parents=True, exist_ok=True)

    if not say(P.STOCK.exists(), f'stock image {P.STOCK}',
               "download Digitone II OS 1.11 from Elektron's site yourself, put the .syx here (or set DN2_STOCK)"):
        sys.exit(1)
    sha = hashlib.sha256(P.STOCK.read_bytes()).hexdigest()
    if not say(sha == P.STOCK_SHA256, f'stock image is OS 1.11 (sha256 {sha[:16]}...)',
               f'expected {P.STOCK_SHA256}: every address in these tools is 1.11\'s'):
        sys.exit(1)
    if not say((P.DNFW / 'src/dnfw').is_dir(), f'dnfw at {P.DNFW}',
               f'git clone https://github.com/angellinares/dn2_firmware_explore {P.DNFW}'):
        sys.exit(1)
    P.use_dnfw()
    from dnfw.cli.files import read_image
    from dnfw.firmware.load import load
    fw = load(read_image(P.STOCK))
    for sid, path in ((3, P.SEC3_STOCK), (7, P.SEC7_STOCK)):
        if not path.exists():
            path.write_bytes(bytes(fw.container.find(sid).unpack()))
        say(True, f'section {sid} -> {path} ({path.stat().st_size} B)')
    s7 = P.SNAP / 'sec7_stock.bin'
    if not s7.exists():
        shutil.copy(P.SEC7_STOCK, s7)
    params = P.DATA / 'params_1.11.txt'
    if not params.exists():
        r = subprocess.run([sys.executable, '-m', 'dnfw.cli.main', 'params', str(P.STOCK)], capture_output=True, text=True,
                           env=dict(os.environ, PYTHONPATH=str(P.DNFW / 'src')))
        if r.returncode == 0:
            params.write_text(r.stdout)
    say(params.exists() and params.stat().st_size > 1000, f'parameter table -> {params}')

    P.use_m68k()
    objdump = shutil.which('m68k-linux-gnu-objdump') or shutil.which('m68k-elf-objdump')
    gcc = shutil.which('m68k-linux-gnu-gcc') or shutil.which('m68k-elf-gcc')
    say(bool(gcc), 'm68k GCC (ColdFire C mods)', 'Linux / WSL: apt install gcc-m68k-linux-gnu binutils-m68k-linux-gnu; '
        'macOS: build GCC 13 + binutils for m68k-linux-gnu (references/setup.md), put it in $DN2_WORKSPACE/m68k/bin')
    dis = P.EXTRACTED / 'section_3.dis'
    if objdump and not (dis.exists() and dis.stat().st_size):
        with open(dis, 'w') as f:
            subprocess.run([os.path.join(HERE, 'cfdis.sh'), '0x40000400', '0x402e0000'], stdout=f, check=True)
    say(dis.exists() and dis.stat().st_size > 0, f'ColdFire disassembly -> {dis}', 'needs m68k objdump (as above)')
    rows = P.DATA / 'emac_rows_1.11.txt'
    if dis.exists() and dis.stat().st_size and not rows.exists():
        emac_rows(dis, rows)
    say(rows.exists(), f'EMAC rows -> {rows}')

    pp = pypy()
    say(bool(pp), 'PyPy (SHARC emulator ~8x faster in Python mode)', 'optional: brew install pypy3 / apt install pypy3')
    say(bool(shutil.which('cc') or shutil.which('clang')), 'C compiler (the SHARC emulator C core, fastblock)',
        'install Xcode command line tools / build-essential')
    dk = digikit_python()
    say(bool(dk), f'digikit + its venv at {P.DIGIKIT} (ColdFire emulator)',
        f'git clone https://github.com/m-dwyer/digikit {P.DIGIKIT}, follow its README for DN2 1.11, then '
        f'git -C {P.DIGIKIT} apply {os.path.join(HERE, "digikit-local.patch")}; faster + correct EMAC: '
        'digiemu\'s patched Unicorn (references/setup.md)')

    boot = P.SNAP / 'boot_stock.snap.gz'
    if a.boot and not boot.exists():
        print('booting the stock DSP in the emulator (a few minutes)...', flush=True)
        subprocess.run([pp or sys.executable, os.path.join(HERE, 'dn2boot.py'), '700', str(boot), str(s7)], check=True)
    say(boot.exists(), f'DSP boot snapshot -> {boot}', 'python3 dn2setup.py --boot')

    tpl = P.DATA / 'frame_template.bin'
    if a.template and not tpl.exists() and dk:
        print('capturing OS frames in the ColdFire emulator (first run boots the OS: a few minutes)...', flush=True)
        code = ('import sys; sys.path.insert(0, %r); import cfkit, dn2paths as P; '
                'k = cfkit.Kit(str(P.SEC3_STOCK), str(P.STOCK), None, str(P.BUILD / "stock_emu")); '
                'fr = k.h.frames(90); open(%r, "wb").write(b"".join(f[:2688] for f in fr))') % (HERE, str(tpl))
        subprocess.run([dk, '-c', code], check=True, cwd=str(P.DIGIKIT))
    say(tpl.exists(), f'frame template -> {tpl}', 'python3 dn2setup.py --template (needs digikit)')


if __name__ == '__main__':
    main()
