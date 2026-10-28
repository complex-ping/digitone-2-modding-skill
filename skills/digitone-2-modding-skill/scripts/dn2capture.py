"""Record the ColdFire->SHARC frames the real Digitone II 1.11 OS sends while a note is played (digikit emulator).

Run with digikit's venv (from the digikit checkout, which holds its boot snapshots under out/):
    cd $DN2_WORKSPACE/repos/digikit && DT2_SECTIONS=out/sections/dn2-1.11 .venv-fast/bin/python \
        PATH/TO/scripts/dn2capture.py out/snapshots/dn2-1.11/boot400M.snap OUTDIR [--frames 120] [--press 10] [--release 70]

Every call of the SPI driver 0x400cf7be is recorded (both senders: the vector-191 frame handler and the
command-2 sender at 0x400d0fec), not just the frames we trigger. Between frames the OS runs `--step` instructions
with its timers (spin), so the sequencer/UI advance. Trig key 1 = panel code 25 = channel 3 bit 0.
Output: OUTDIR/frames.bin (N x 2688 bytes, as sent = big-endian halfwords), OUTDIR/frames.json (per frame: sender,
command word), PNGs of the screen.
"""
import argparse, json, os, struct, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dn2paths as P  # noqa: E402
DIGIKIT = str(P.DIGIKIT)
sys.path.insert(0, DIGIKIT); sys.path.insert(0, os.path.join(DIGIKIT, 'tools'))

from unicorn import UC_HOOK_CODE  # noqa: E402
from unicorn.m68k_const import (UC_M68K_REG_A2, UC_M68K_REG_A3, UC_M68K_REG_A7, UC_M68K_REG_D0,  # noqa: E402
                                 UC_M68K_REG_D1, UC_M68K_REG_D2, UC_M68K_REG_PC)
import emucheck  # noqa: E402
from emu import panel, panelin  # noqa: E402
from emu.longrun import spin  # noqa: E402

PROF = dict(vector=191, handler=0x40025E36, driver=0x400CF7BE, counter=0x402876F8, gate=0x4058E54C)
REPORTER = 0x4011EA6A


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('snapshot'); ap.add_argument('out')
    ap.add_argument('--syx', default=str(P.STOCK))
    ap.add_argument('--frames', type=int, default=120)
    ap.add_argument('--step', type=int, default=150_000, help='ColdFire instructions between frames')
    ap.add_argument('--press', type=int, default=10)
    ap.add_argument('--release', type=int, default=70)
    ap.add_argument('--settle-max', type=int, default=600_000_000)
    ap.add_argument('--no-wait-screen', action='store_true')
    ap.add_argument('--save-ui', help='save a digikit snapshot at the main screen')
    ap.add_argument('--key', default='3:0', help='panel channel:bit to press (TRIG1 3:0, PLAY 2:3)')
    ap.add_argument('--no-frames-before', type=int, default=0, help='do not raise frames before this index')
    ap.add_argument('--frame-count', type=int, default=20_000_000, help='max ColdFire instructions per raised frame')
    ap.add_argument('--debug-frames', action='store_true')
    ap.add_argument('--patch-sec3', help='section-3 image of a build: its byte differences to the stock section 3 '
                    'are written into the running OS (code/consts; equivalent to booting that build)')
    ap.add_argument('--ext-out', action='store_true', help='run the driver\'s real TX-list builder (0x400cf906..'
                    '0x400cf94c) and save frames_ext.bin: the data halfwords of all list entries = the bytes the SPI '
                    'DMA clocks out (2748 per frame: frame + 60 padding bytes: the side channel)')
    ap.add_argument('--emac', choices=('python', 'smoother', 'unicorn'), default='python',
                    help='python: all EMAC instructions in Python (default); smoother: only the parameter smoother '
                         'loop; unicorn: raw digikit behaviour (fractional products halved)')
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()
    log = lambda s: print(f'[{time.time() - t0:6.1f}s] {s}', flush=True)

    m, ev, st, pc, inq, at, pits, profile = emucheck.setup(a.snapshot, a.syx)
    uc = m.uc
    log(f'setup pc={pc:#x}')
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    if a.emac == 'smoother':                     # Unicorn EMAC fractional bug -> params at 2.91 % (cfhooks.py)
        import cfhooks
        cfhooks.install_emac_smoother(uc)
    elif a.emac == 'python':                     # every EMAC instruction of the OS in Python (cfemac.py)
        import cfemac
        rows_path = str(P.DATA / 'emac_rows_1.11.txt')
        emac = cfemac.Emac(uc); emac.install(cfemac.load_rows(rows_path))
        log(f'python EMAC: {len(emac.hits) or len(cfemac.load_rows(rows_path))} instructions hooked')

    if a.patch_sec3:
        stock3 = open(P.SEC3_STOCK, 'rb').read()
        new3 = open(a.patch_sec3, 'rb').read()
        assert len(stock3) == len(new3)
        k, runs = 0, 0
        while k < len(new3):
            if new3[k] != stock3[k]:
                j = k
                while j < len(new3) and new3[j] != stock3[j]: j += 1
                uc.mem_write(0x40000400 + k, new3[k:j]); runs += 1; k = j
            else:
                k += 1
        log(f'patched section 3 into the running OS: {runs} byte runs')
    extra = []

    sends = []                       # (frame_index, caller, tx_len, bytes)
    cur = {'i': -1}

    TXLIST, BUILD, BUILT = 0x80002510, 0x400CF906, 0x400CF950   # driver's TX DMA list; its list builder
    pend = {}

    def ret_from_driver(sp, ret):
        uc.reg_write(UC_M68K_REG_D0, 0)
        uc.reg_write(UC_M68K_REG_A7, sp + 4)
        uc.reg_write(UC_M68K_REG_PC, ret)

    def driver_hook(uc_, addr, size, user):
        sp = uc.reg_read(UC_M68K_REG_A7)
        ret, tx_len, tx, rx_len, rx = struct.unpack('>IIIII', bytes(uc.mem_read(sp, 20)))
        data = bytes(uc.mem_read(tx, tx_len)) if tx and tx_len else b''
        sends.append((cur['i'], ret, tx_len, data))
        n = struct.unpack('>I', bytes(uc.mem_read(0x42440958, 4)))[0]    # SPI transfer length (2748)
        if not (a.ext_out and n and 0 < tx_len <= 2800):
            extra.append(None); ret_from_driver(sp, ret); return
        # run the real list builder (DMA/DSPI register writes before and after it are skipped)
        pend.update(sp=sp, ret=ret, n=min(n, 2800), tx=data,
                    regs=[uc.reg_read(r) for r in (UC_M68K_REG_D2, UC_M68K_REG_A2, UC_M68K_REG_A3)])
        uc.reg_write(UC_M68K_REG_D0, tx_len); uc.reg_write(UC_M68K_REG_D1, pend['n'])
        uc.reg_write(UC_M68K_REG_A2, tx_len); uc.reg_write(UC_M68K_REG_A3, tx)
        uc.reg_write(UC_M68K_REG_PC, BUILD)

    def built_hook(uc_, addr, size, user):
        k = pend['n'] // 2
        lst = bytes(uc.mem_read(TXLIST, 4 * k))
        sent = b''.join(lst[4 * j + 2:4 * j + 4] for j in range(k))     # data halfwords = what reaches the DSP
        if sent[:len(pend['tx'])] != pend['tx']:
            bad = [j for j in range(0, len(pend['tx']), 2) if sent[j:j + 2] != pend['tx'][j:j + 2]]
            log(f'  !! frame {cur["i"]}: {len(bad)} halfwords of the SENT frame differ from the frame buffer '
                f'(first at byte {bad[0]})')
        extra.append(sent)
        for r, v in zip((UC_M68K_REG_D2, UC_M68K_REG_A2, UC_M68K_REG_A3), pend['regs']):
            uc.reg_write(r, v)
        ret_from_driver(pend['sp'], pend['ret'])
    uc.hook_add(UC_HOOK_CODE, driver_hook, begin=PROF['driver'], end=PROF['driver'])
    uc.hook_add(UC_HOOK_CODE, built_hook, begin=BUILT, end=BUILT)
    key = tuple(int(x) for x in a.key.split(':'))
    from unicorn.m68k_const import UC_M68K_REG_D0 as D0
    def rep_hook(uc_, addr, size, user):
        sp = uc.reg_read(UC_M68K_REG_A7)
        stk = struct.unpack('>16I', bytes(uc.mem_read(sp, 64)))
        log('EXCEPTION REPORTER entered: sp=%#x stack=%s' % (sp, ' '.join('%08x' % w for w in stk)))
    uc.hook_add(UC_HOOK_CODE, rep_hook, begin=REPORTER, end=REPORTER)

    def png(name):
        buf = panel.read(m, profile.fb_front)
        if buf is not None:
            panel.write_png(buf, os.path.join(a.out, name))

    if not a.no_wait_screen:
        done_total = 0
        while done_total < a.settle_max:
            pc, done, stop = spin(m, pc, 15_000_000, pits=pits)
            done_total += done
            buf = panel.read(m, profile.fb_front)
            frac = emucheck.frame_on_fraction(buf) if buf else None
            if stop != 'limit':
                log(f'spin stopped: {stop}'); break
            if frac is not None and frac >= emucheck.MAIN_SCREEN_ON_FRACTION:
                log(f'main screen at +{done_total // 1_000_000}M'); break
        png('0_screen.png')
        if a.save_ui:
            from emu import snapshot
            uc.reg_write(UC_M68K_REG_PC, pc)
            info = snapshot.save(m, a.save_ui); log(f'saved UI snapshot {a.save_ui} {info}')

    def frame():
        uc.mem_write(PROF['gate'], bytes(4))
        uc.mem_write(PROF['counter'], bytes(4))
        resume = uc.reg_read(UC_M68K_REG_PC)
        m.halt_vec = None
        if not m.raise_vector(PROF['vector']):
            raise SystemExit('vector 191 has no handler')
        t0 = time.time()
        uc.emu_start(uc.reg_read(UC_M68K_REG_PC), resume, count=a.frame_count)
        endpc = uc.reg_read(UC_M68K_REG_PC)
        if a.debug_frames:
            log(f'  frame: {time.time() - t0:.2f}s, stopped at {endpc:#x} (resume {resume:#x}) sends so far {len(sends)}')
        return endpc

    held = 0
    for i in range(a.frames):
        cur['i'] = i
        if i == a.press:
            held = 1 << key[1]; pc = panelin.buttons(m, profile, key[0], held); log(f'frame {i}: press {key}')
        if i == a.release:
            held = 0; pc = panelin.buttons(m, profile, key[0], held); log(f'frame {i}: release {key}')
        pc, done, stop = spin(m, pc, a.step, pits=pits)
        if stop != 'limit':
            log(f'spin stopped at frame {i}: {stop}')
        if i >= a.no_frames_before:
            pc = frame()
        if i in (a.press + 2, a.release + 2):
            png(f'1_frame{i}.png')

    with open(os.path.join(a.out, 'frames.bin'), 'wb') as f:
        for _, _, n, d in sends:
            f.write(d.ljust(2688, b'\0')[:2688])
    if a.ext_out:
        with open(os.path.join(a.out, 'frames_ext.bin'), 'wb') as f:
            for (_, _, n, d), x in zip(sends, extra):
                if x is not None:
                    f.write(x.ljust(2748, b'\0')[:2748])
    meta = [dict(frame=i, caller=f'{ret:#x}', tx_len=n, cmd=(d[0] << 8 | d[1]) if len(d) > 1 else None,
                 nonzero=sum(1 for b in d if b)) for i, ret, n, d in sends]
    json.dump(meta, open(os.path.join(a.out, 'frames.json'), 'w'), indent=1)
    log(f'{len(sends)} sends recorded -> {a.out}')
    from collections import Counter
    log(f'senders: {Counter(x["caller"] for x in meta)}  cmds: {Counter(x["cmd"] for x in meta)}')


if __name__ == '__main__':
    main()
