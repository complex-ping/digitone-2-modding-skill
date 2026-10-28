"""Flash an OS .syx into the running Digitone II over USB the way Elektron Transfer / Elektroid do: the MIDI RPC
OsUpgradeStart (0x50: u32 size BIG-endian, "sysex", auto-commit byte) + OsUpgradeWrite (0x51) 2 KB blocks with crc32 +
OsUpgradeEnd (0x52: 1 commit / 0 cancel).
    python rpc_flash.py FILE.syx [--quiet 90]      (pip install mido python-rtmidi; Transfer must be closed)
    python rpc_flash.py --ping | --cancel | --commit
OS 1.11's RPC handlers read every u32 big-endian. With auto-commit the OS posts "upload complete" to the upgrade view as the
last block is parsed -- on the first upload after a boot that view is not open yet and the event is lost (the unit sits on
"OS UPGRADE / Receiving"), so this sends Start WITHOUT auto-commit, all blocks, waits a few seconds, then End(1): the End
handler checks every byte arrived and posts the event itself. The stock OS then validates the container and asks
UPGRADE OS NOW? on its screen: press YES on the unit. Keep the USB silent while it writes flash and reboots (--quiet), then
this pings until the unit answers again. One upload, no retries: if it times out, look at the screen first.
Python + mido can hang at exit in CoreMIDI: ends with os._exit."""
import argparse, os, struct, sys, time, zlib
import mido

PORT = 'Elektron Digitone II'
HDR = bytes([0x00, 0x20, 0x3C, 0x10, 0x00])
BLOCK = 0x800


def enc87(s):
    o = bytearray()
    for i in range(0, len(s), 7):
        c = s[i:i + 7]
        o.append(sum(((b >> 7) & 1) << (6 - k) for k, b in enumerate(c)))
        o += bytes(b & 0x7F for b in c)
    return bytes(o)


def dec87(s):
    o = bytearray()
    for i in range(0, len(s), 8):
        hi, c = s[i], s[i + 1:i + 8]
        o += bytes(b | (((hi >> (6 - k)) & 1) << 7) for k, b in enumerate(c))
    return bytes(o)


class Rpc:
    def __init__(self):
        self.out = mido.open_output(PORT)
        self.inp = mido.open_input(PORT)
        self.seq = 0
        while self.inp.poll():
            pass

    def call(self, body, timeout=5.0):
        seq = self.seq
        self.seq = (self.seq + 1) & 0xFFFF
        msg = struct.pack('>H', seq) + b'\0\0' + body
        self.out.send(mido.Message('sysex', data=list(HDR + enc87(msg))))
        t = time.time() + timeout
        while time.time() < t:
            m = self.inp.poll()
            if m is None:
                time.sleep(0.001)
                continue
            if m.type != 'sysex' or bytes(m.data[:5]) != HDR:
                continue
            r = dec87(bytes(m.data[5:]))
            if len(r) >= 5 and struct.unpack('>H', r[2:4])[0] == seq:
                if r[4] != (body[0] | 0x80):
                    raise SystemExit(f'reply type {r[4]:#x} to {body[0]:#x}: {r.hex()}')
                return r
        raise SystemExit(f'no reply to {body[0]:#x} (seq {seq})')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('syx', nargs='?')
    ap.add_argument('--ping', action='store_true')
    ap.add_argument('--quiet', type=float, default=90, help='seconds of USB silence after the commit before the first ping')
    ap.add_argument('--no-wait', action='store_true', help='return right after the upload')
    ap.add_argument('--settle', type=float, default=3, help='seconds between Start and the first block')
    ap.add_argument('--cancel', action='store_true', help='only cancel a pending upload (End 0) and exit')
    ap.add_argument('--commit', action='store_true', help='only commit a complete pending upload (End 1) and exit')
    ap.add_argument('--commit-after', type=float, default=3, help='seconds between the last block and End(commit)')
    a = ap.parse_args()
    rpc = Rpc()
    r = rpc.call(b'\x01')
    print('ping:', r[5:].split(b'\0')[0][-24:], flush=True)
    if a.cancel or a.commit:
        e = rpc.call(b'\x52\x01' if a.commit else b'\x52\x00', timeout=10)
        print(f'end({"commit" if a.commit else "cancel"}): {e[5:].hex()}', flush=True)
        sys.exit(0)
    if a.ping or not a.syx:
        sys.exit(0)
    raw = open(a.syx, 'rb').read()
    for attempt in (0, 1):
        r = rpc.call(b'\x50' + struct.pack('>I', len(raw)) + b'sysex\0\x00')      # (no auto-commit: End(1) below)
        print(f'start ({len(raw)} B): status {r[5]} {r[6:].split(chr(0).encode())[0]!r}', flush=True)
        if not r[5]:
            break
        if attempt:
            sys.exit('OsUpgradeStart refused')
        e = rpc.call(b'\x52\x00', timeout=10)                # (an old upload pending: cancel it)
        print(f'end(cancel) of the pending upload: {e[5:].hex()}', flush=True)
        time.sleep(2)
    time.sleep(a.settle)               # (the upgrade view opens on the UI task after Start: its events must find it open)
    off, t0 = 0, time.time()
    while off < len(raw):
        n = min(BLOCK, len(raw) - off)
        c = raw[off:off + n]
        crc = zlib.crc32(c, 0xFFFFFFFF)                    # (elektroid: zlib crc32(0xffffffff, ...))
        r = rpc.call(b'\x51' + struct.pack('>III', crc, n, off) + c, timeout=20)
        off += n
        st = r[9] if len(r) > 9 else 255
        if (off // BLOCK) % 64 == 0 or st:
            print(f'  {off}/{len(raw)} {100 * off / len(raw):.1f}% {time.time() - t0:.0f}s status {st}', flush=True)
        if st == 1:
            break
        if st > 1:
            sys.exit(f'write refused at {off}: status {st} {r[10:].split(chr(0).encode())[0]!r} ({r.hex()[:80]})')
    print(f'sent in {time.time() - t0:.0f}s, last status {st}', flush=True)
    if st != 1:
        sys.exit('the unit did not report the upload complete')
    time.sleep(a.commit_after)         # (the upgrade view opens once the UI runs again: the commit's event 20 must find it)
    r = rpc.call(b'\x52\x01', timeout=30)
    print(f'end(commit): status {r[5]} {r[6:].split(chr(0).encode())[0]!r}', flush=True)
    if r[5]:
        sys.exit('the OS refused the commit')
    del rpc
    if a.no_wait:
        sys.exit(0)
    print('committed: confirm UPGRADE OS NOW? on the unit if it asks', flush=True)
    time.sleep(a.quiet)                # (the USB silent while it validates, writes flash and reboots)
    t2 = time.time()
    while time.time() - t2 < 180:
        try:
            r = Rpc().call(b'\x01', timeout=3)
        except (SystemExit, OSError, IOError):          # (no port yet, or no reply: still rebooting)
            time.sleep(10)
            continue
        name = r[5:].split(b'\0')[0][-24:]
        print(f'answers again after {a.quiet + time.time() - t2:.0f}s: {name}', flush=True)
        sys.exit(0)
    sys.exit('the unit did not answer after the upload: look at its screen')


try:                                  # (os._exit: the interpreter's exit can hang in CoreMIDI's teardown)
    main()
    code = 0
except SystemExit as e:
    code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    if isinstance(e.code, str):
        print(e.code, file=sys.stderr)
sys.stdout.flush(); sys.stderr.flush()
os._exit(code)
