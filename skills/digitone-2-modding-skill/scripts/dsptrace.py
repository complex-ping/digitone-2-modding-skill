"""Find stock DSP state by experiment (how the filter envelope word was found).

    pypy3 -i dsptrace.py [TAG]        (TAG = a snapshot tag, default stock)

    song = voices([dict(FREQ=fq) for fq in (0, 20, 40, 64, 80, 100, 120, 127)])     # one voice / track per case
    snap = snapshot(song, 12)                                 # every written DSP word after block 12
    per_voice(snap, 8, increasing)                            # (addr, stride, values): words monotone over the voices
    hist = trace(song, 240, [2, 9, 20, 80, 200], (0x24f000, 0x26a000))   # selected blocks, a region
    like(hist, envelope)                                      # words shaped like an envelope (0, jump, decay)

Knob names: dn2synth PARAM_ROWS short names (FREQ RESO ENV ATK DEC SUS REL ...) or slot ids; frame=dict(off=value) writes
voice halfwords (e.g. {0xb4: 2}: voice v's filter machine at 0xb4 + 2 v; SYN machine 0x94 + 2 v). Words are floats unless
asked (f32 / raw). Known per-voice words: filter coefficient 0x2505b4 + 0xcc v (multimode only), filter envelope 0x2502b8 +
0x30 v (0 .. -1, every filter machine), amp envelope ~0x2547c8 + 0x2c v."""
import os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import dn2paths as P; P.use_dnfw()
import dn2synth as S, dn2frame as D

TAG = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith('-') else 'stock'
SEC7, SNAP = P.snap(TAG)
f32 = lambda w: struct.unpack('<f', struct.pack('<I', w & 0xFFFFFFFF))[0]


def voices(cases, machine='WAVETONE', note=60, vel=100, at=3, length=150, stagger=0):
    """A song: voice v = track v plays cases[v] (dict knob -> 0..127, plus frame={off: value} per-voice halfwords)."""
    s = S.Song()
    for v, c in enumerate(cases):
        snd = S.Sound(machine)
        for name, x in c.items():
            if name != 'frame':
                snd.set(name, x)
        s.assign(v, snd).track(v, v); s.note(at + stagger * v, v, note, vel, length=length)
        for off, x in c.get('frame', {}).items():
            s.put(off + 2 * v, x)
    return s


def run(song, n, each):
    f = D.Frame(D.image_from_sec7(SEC7), SNAP)
    D.render(f, song.frames(n), on_block=each)


def snapshot(song, block):
    out = {}
    run(song, block + 1, lambda b, fr: out.update(fr.s.mem.words) if b == block else None)
    return out


def trace(song, n, blocks, region=(0x24F000, 0x26A000)):
    hist = {}
    def each(b, fr):
        if b in blocks:
            for a, w in fr.s.mem.words.items():
                if region[0] <= a < region[1]:
                    hist.setdefault(a, {})[b] = w
    run(song, n, each)
    return {a: [h.get(b, 0) for b in blocks] for a, h in hist.items()}


increasing = lambda xs: all(xs[i] < xs[i + 1] for i in range(len(xs) - 1)) and xs[-1] > 2 * abs(xs[0]) + 1e-6


def per_voice(snap, nv, test=increasing, region=(0x250000, 0x270000), strides=range(4, 0x200, 4)):
    hits = []
    for a in sorted(snap):
        if not region[0] <= a < region[1]:
            continue
        for st in strides:
            ws = [snap.get(a + st * v) for v in range(nv)]
            if None in ws:
                continue
            xs = [f32(w) for w in ws]
            if all(abs(x) < 1e6 for x in xs) and test(xs):
                hits.append((hex(a), hex(st), [round(x, 5) for x in xs]))
                break
    return hits


def envelope(xs):
    ys = [abs(x) for x in xs]
    pk = max(range(1, min(5, len(ys))), key=lambda j: ys[j])
    return ((all(x >= 0 for x in xs[1:]) or all(x <= 0 for x in xs[1:])) and ys[0] < 1e-9 and ys[pk] > 0.01 and
            all(ys[j] >= ys[j + 1] for j in range(pk, len(ys) - 1)) and ys[-1] < 0.6 * ys[pk] and all(y < 1e4 for y in ys))


def like(hist, test):
    return [(hex(a), [round(f32(w), 4) for w in ws]) for a, ws in sorted(hist.items()) if test([f32(w) for w in ws])]
