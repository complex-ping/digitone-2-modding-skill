"""Drive the DN2 SHARC audio frame in sim.py from the boot snapshot.

Block = what the Audio Task (0x1c9fe7) does per wake-up: CJUMP 0x1c9d6b, which reads the ColdFire link buffer
in = 0x2c29d0 + (bank << 12) (bank = DM(0x2c0450)), dispatches on its first halfword (3 = render a block) and
writes the reply to 0x2c49d0 + (bank << 12).
"""
import os, sys, time, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import dn2paths as P; P.use_dnfw()
import sim, visa

SNAP = P.snap('stock')[1]
BLOCK_FN = 0x1C9D6B
BANK = 0x2C0450
IN0, OUT0 = 0x2C29D0, 0x2C49D0
STK = 0x80F00000                        # emulator-only stack for the audio thread (DDR, far from anything)
RET = 0xFFFFF0                          # sentinel return address


def image_from_sec7(path_or_bytes=None):
    if path_or_bytes is None:
        return visa.Image()
    img = visa.Image.__new__(visa.Image)
    raw = open(path_or_bytes, 'rb').read() if isinstance(path_or_bytes, str) else bytes(path_or_bytes)
    from dnfw.image import bootstream as bs
    img.raw = raw; img.regions = bs.load_regions(raw)
    import hashlib
    img.origin = hashlib.sha256(raw).hexdigest()           # (render's prefix reuse: the image's identity)
    return img


# DN2_STATS=1: at exit, per process: frames made (and their load time), blocks run, seconds and instructions in them, split by
# whether an instruction hook (on_insn: verify_dsl's checker, tracers) was set.
STATS = {'frames': 0, 'load_s': 0.0, 'blocks': 0, 'hooked_blocks': 0, 's': 0.0, 'hooked_s': 0.0, 'insns': 0, 'hooked_insns': 0}
if os.environ.get('DN2_STATS'):
    import atexit, json as _json
    atexit.register(lambda: sys.stderr.write('DN2_STATS ' + _json.dumps(STATS) + '\n'))


def sim_class():
    """simc.CSim (the C core) unless DN2_SIM=py or it cannot be built here."""
    if os.environ.get('DN2_SIM') == 'py':
        return sim.Sim
    try:
        import simc
        return simc.CSim
    except Exception as e:                                  # (no compiler / cffi: the interpreter, slower)
        sys.stderr.write(f'dn2frame: simc unavailable ({e}); using sim.Sim\n')
        return sim.Sim


class Frame:
    def __init__(self, image=None, snap=SNAP, cls=None):
        t0 = time.time()
        self.s = sim.load_state(snap, image, cls=cls or sim_class())
        self.blocks = 0                            # blocks run (render's prefix reuse starts only on a fresh frame)
        self.origin = (getattr(image, 'origin', None), os.path.realpath(snap), os.path.getmtime(snap)) \
            if getattr(image, 'origin', None) else None
        STATS['frames'] += 1
        STATS['load_s'] += time.time() - t0
        s = self.s
        s.L[6] = s.L[7] = 0
        s.loopstk = []; s.pcstk = []; s.pending = None

    def bank(self):
        return self.s.mem.r(BANK) & 1

    def call(self, fn, args=None, max_insns=50_000_000):
        s = self.s
        s.I[6] = STK; s.I[7] = STK - 8
        s.mem.w(STK, STK + 0x100); s.mem.w(STK - 4, RET - 1)
        for k, v in (args or {}).items():
            getattr(s, k[0]) [int(k[1:])] = v if k[0] != 'R' else None
            if k[0] == 'R': s.px.R[int(k[1:])] = v
        s.pc = fn
        n0 = s.icount
        r = s.run(max_insns=max_insns, until={RET})
        if r != 'until':
            raise RuntimeError(f'{fn:#x} did not return ({r}) pc={s.pc:#x}')
        return s.icount - n0

    def block(self, packet: bytes | None = None, cmd=3):
        """Put packet (bytes, little-endian halfwords as the DSP sees them) or just `cmd` in the incoming buffer and run
        one block. Returns the instruction count."""
        s = self.s
        base = IN0 + (self.bank() << 12)
        if packet is not None:
            for k in range(0, len(packet) - 3, 4):
                s.mem.w(base + k, int.from_bytes(packet[k:k + 4], 'little'))
        else:
            s.mem.w(base, (s.mem.r(base) & ~0xFFFF) | cmd)
        t0 = time.time()
        self.blocks += 1
        n = self.call(BLOCK_FN)
        k = 'hooked_' if s.on_insn else ''
        STATS[k + 'blocks'] += 1
        STATS[k + 's'] += time.time() - t0
        STATS[k + 'insns'] += n
        return n


if __name__ == '__main__':
    f = Frame()
    writes = collections.Counter()
    orig_w = f.s.mem.w
    def w(a, v, size=4):
        writes[(a & 0xFFFFFFFF) >> 8] += 1
        return orig_w(a, v, size)
    f.s.mem.w = w
    t = time.time()
    try:
        n = f.block()
        print(f'block ran: {n} insns, {time.time() - t:.1f}s')
    except Exception as e:
        print('EXC', type(e).__name__, e, 'pc', hex(f.s.pc))
    print('written 256-byte pages:', len(writes))
    for page, c in sorted(writes.items())[:80]:
        print(f'  {page << 8:#010x} {c}')


# ---------------------------------------------------------------------------- sequences -> audio
MAIN_OUT = 0x268438                      # float L[0..31], R[32..63] after each render (ring A, -> codec)


def swap16(raw: bytes) -> bytes:
    """Frames as the ColdFire sends them (big-endian halfwords) -> the DSP's view (little-endian halfwords)."""
    return b''.join(raw[i:i + 2][::-1] for i in range(0, len(raw) - 1, 2))


def load_frames(path):
    raw = open(path, 'rb').read()
    return [swap16(raw[k:k + 2688]) for k in range(0, len(raw) - 2687, 2688)]


# Prefix reuse (2026-10-22, the suite's profile: 98 % of the DSP tests' time is blocks; the K parts render 8-9 sweeps that share
# their first 100 / 300 blocks): a fresh frame's render keeps its state at these block counts, keyed by the image, the
# snapshot and every packet so far; a later render in the same process with the same start resumes from the longest one.
# Only for an unhooked frame (no on_insn) and no on_block; bit-identical by construction (the same state, the same packets).
CHECKPOINTS = (64, 100, 200, 300)
_PREFIX = {}                                     # key -> (blocks, pickled state, L, R, counts)
_PREFIX_MAX = 12


def _state_of(s):
    import pickle
    return pickle.dumps(dict(words=s.mem.words, mmr=s.mem.mmr, px=vars(s.px), py=vars(s.py), I=s.I, M=s.M, L=s.L, Bx=s.Bx,
                             sysr=s.sysr, PX1=s.PX1, PX2=s.PX2, pc=s.pc, pcstk=s.pcstk, loopstk=s.loopstk, ststk=s.ststk,
                             pending=s.pending, icount=s.icount, lcntr=getattr(s, 'lcntr_next', 0)), protocol=4)


def _restore(s, blob):
    import pickle
    st = pickle.loads(blob)
    s.mem.words = st['words']; s.mem.mmr = st['mmr']
    s.px.__dict__.update(st['px']); s.py.__dict__.update(st['py'])
    s.I, s.M, s.L, s.Bx = st['I'], st['M'], st['L'], st['Bx']
    s.sysr = st['sysr']; s.PX1, s.PX2 = st['PX1'], st['PX2']
    s.pc, s.pcstk, s.loopstk, s.ststk = st['pc'], st['pcstk'], st['loopstk'], st['ststk']
    s.pending, s.icount, s.lcntr_next = st['pending'], st['icount'], st['lcntr']


def render(frame: 'Frame', packets, repeat_last=0, on_block=None):
    """Run the packets (DSP view) block by block, then repeat the last one `repeat_last` times.
    -> (left, right, insn_counts)."""
    import hashlib
    L, R, counts = [], [], []
    seq = list(packets) + [packets[-1]] * repeat_last
    reuse = frame.origin is not None and frame.blocks == 0 and on_block is None and frame.s.on_insn is None
    start = 0
    if reuse:
        h = hashlib.sha256(repr(frame.origin).encode())
        keys = {}
        for k, p in enumerate(seq[:max(CHECKPOINTS)]):
            h.update(p)
            if k + 1 in CHECKPOINTS:
                keys[k + 1] = h.hexdigest()
        for n in sorted(keys, reverse=True):
            hit = _PREFIX.get(keys[n])
            if hit:
                _restore(frame.s, hit[1])
                L, R, counts = list(hit[2]), list(hit[3]), list(hit[4])
                frame.blocks = start = n
                STATS['resumed_blocks'] = STATS.get('resumed_blocks', 0) + n
                break
    for k in range(start, len(seq)):
        counts.append(frame.block(seq[k]))
        m = frame.s.mem
        L += [m.rf(MAIN_OUT + 4 * i) for i in range(32)]
        R += [m.rf(MAIN_OUT + 128 + 4 * i) for i in range(32)]
        if on_block: on_block(k, frame)
        if reuse and k + 1 in keys and keys[k + 1] not in _PREFIX:
            if len(_PREFIX) >= _PREFIX_MAX:
                _PREFIX.pop(next(iter(_PREFIX)))
            _PREFIX[keys[k + 1]] = (k + 1, _state_of(frame.s), tuple(L), tuple(R), tuple(counts))
    return L, R, counts


def write_wav(path, L, R, fs=48000):
    import wave
    with wave.open(path, 'wb') as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(fs)
        clip = lambda v: max(-32767, min(32767, int(round(v * 32767))))
        w.writeframes(b''.join(struct.pack('<hh', clip(a), clip(b)) for a, b in zip(L, R)))


import struct  # noqa: E402


# ---------------------------------------------------------------------------- injection harness
VOICE_RET = 0x1C304D                     # render: right after the voice-render CJUMP returns (before FXINS/mixer)
VOICEBUF = 0x257EE8                      # voice v stereo buffer: L words 0..31, R 32..63 (256 B per voice)
CSEND = 0x257A24                         # per-voice chorus-send gain (float), read by the FX mixer


def inject(frame: 'Frame', gen, csend=None):
    """Install a hook that, every block, overwrites the voice buffers right after the real voice render with
    gen(block, voice) -> (L[32], R[32]) or None (leave that voice), and optionally forces csend[v]."""
    st = {'b': 0}
    def hook(s, ins):
        if s.pc == VOICE_RET and s.pending is None:
            for v in range(16):
                g = gen(st['b'], v)
                if g is not None:
                    L, R = g
                    for k in range(32):
                        s.mem.wf(VOICEBUF + 256 * v + 4 * k, L[k]); s.mem.wf(VOICEBUF + 256 * v + 128 + 4 * k, R[k])
                if csend is not None and csend[v] is not None:
                    s.mem.wf(CSEND + 4 * v, csend[v])
            st['b'] += 1
    frame.s.on_insn = hook
    frame.s.on_insn_pcs = {VOICE_RET}
    return st
