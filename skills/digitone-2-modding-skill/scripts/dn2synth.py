"""Virtual Digitone II front end: build the ColdFire->SHARC render frames (command 3) from sounds and note events,
so the emulated DSP plays real patches without digikit's ColdFire (whose captured parameter values are still
ramping from 0 after load — ~18 s of emulation per frame, never converged).

Frame layout (big-endian halfwords as sent; DSP view = swap16), derived from the DSP's reader 0x1c2877..0x1c2b9x:
  +0x00           command (3 = render)
  +0x02 + 2v      note of voice v (MIDI note << 8)          +0x22 / +0x26   note-on bitmask (bit v), two copies
  +0x24 / +0x28   note-off bitmask                          +0x34 + 2v      velocity << 8
  +0x54 + 2v      velocity curve 0..4 (captured: 3)          +0x74 + 2v      voice level (track volume; ~squared)
  +0x94 + 2v      machine type (0 FM TONE, 1 WAVETONE, 2 FM DRUM, 3 SWARMER)
  +0xd8           tempo * 120
  +0xda + 146v    voice v's sound record: slot s at +{2(s-25) s<=65; 82+2(s-66) s<=79; 110+2(s-80) s<=92; 136+2(s-95)}
  +0x9c8 + 2*id   FX / global parameter id (25..69: chorus, delay, reverb, ext-in, master)
  +0xa58          master tune * 10 (4400 = 440 Hz)
Values are the parameter's stored raw value (knob << 8 | fine), which the DSP converts as raw/32768 (or /32512).
Voices are what the DSP renders; the ColdFire allocates them and copies the track's sound into the voice record.
"""
import re, struct, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dn2paths as P
import dn2frame as D

PARAMS = str(P.DATA / 'params_1.11.txt')            # `dnfw params` of your stock image (dn2setup.py)
TEMPLATE = str(P.DATA / 'frame_template.bin')       # real OS frames (ColdFire emulator capture, dn2setup.py --template)
MACHINES = {'FMTONE': 0, 'WAVETONE': 1, 'FMDRUM': 2, 'SWARMER': 3}
SOUND_GROUPS = (5, 11, 13, 14, 15)          # filter, amp, filter env, portamento, FX sends
FX_GROUPS = (16, 17, 18, 19, 20, 21)         # chorus, reverb, delay, master, compressor, ext-in


def load_params():
    rows = []
    for line in open(PARAMS):
        m = re.match(r'0x\w+\s+\d+\s+(\S+)\s+(\S+)\s+(\S+)?\s+(.+?)\s{2,}(\S+(?: \S+)?)\s+([0-9a-f]+)\s+([0-9a-f]+)', line)
        if not m or m.group(1) == '--' or m.group(2) == '--':
            continue
        rows.append(dict(grp=int(m.group(1)), id=int(m.group(2)), page=m.group(3), name=m.group(4).strip(),
                         short=m.group(5), range=int(m.group(6), 16), dflt=int(m.group(7), 16)))
    return rows


PARAM_ROWS = load_params()


def slot_off(s):
    if 25 <= s <= 65: return 2 * (s - 25)
    if 66 <= s <= 79: return 82 + 2 * (s - 66)
    if 80 <= s <= 92: return 110 + 2 * (s - 80)
    if 95 <= s <= 99: return 136 + 2 * (s - 95)
    return None


class Sound:
    """A sound = machine + raw slot values. set(name_or_short, value): value 0..127 (knob) or raw (>=0x100)."""
    def __init__(self, machine='WAVETONE', **knobs):
        self.machine = machine
        grp = MACHINES[machine]
        self.slots, self.names = {}, {}
        for r in PARAM_ROWS:
            if r['grp'] == grp or r['grp'] in SOUND_GROUPS:
                if slot_off(r['id']) is not None:
                    self.slots.setdefault(r['id'], r['dflt'])
                    self.names.setdefault(f"{r['page']}.{r['short']}".upper(), r['id'])   # e.g. AMP.ATK, SYN.LEV1
                    self.names.setdefault(r['short'].upper(), r['id'])
                    self.names.setdefault(r['name'].upper(), r['id'])
        for k, v in knobs.items():
            self.set(k, v)

    def set(self, name, value):
        sid = name if isinstance(name, int) else self.names[name.upper().replace('_', ' ')]
        self.slots[sid] = value << 8 if value < 0x100 else value
        return self


class Song:
    """Per-block frame builder. Voices are assigned explicitly (voice index = what the DSP renders)."""
    def __init__(self, bpm=120.0, fx=None, template_frame=-1):
        raw = open(TEMPLATE, 'rb').read()
        template_frame %= len(raw) // 2688
        self.base = bytearray(raw[2688 * template_frame:2688 * (template_frame + 1)])
        self.put(0x00, 3)
        self.put(0xd8, int(round(bpm * 120)))
        for k in range(16):                             # no stale notes/flags from the capture
            self.put(0x02 + 2 * k, 0); self.put(0x34 + 2 * k, 0)
        for o in (0x22, 0x24, 0x26, 0x28):
            self.put(o, 0)
        for r in PARAM_ROWS:                            # FX/global block from the parameter defaults
            if r['grp'] in FX_GROUPS and 25 <= r['id'] <= 69:
                self.put(0x9c8 + 2 * r['id'], r['dflt'])
        for k, v in (fx or {}).items():
            self.set_fx(k, v)
        self.voices = [None] * 16
        self.events = {}                                # block -> list of ('on'|'off', voice, note, vel)

    def put(self, off, v, buf=None):
        struct.pack_into('>H', self.base if buf is None else buf, off, v & 0xFFFF)

    def set_fx(self, name, value):
        for r in PARAM_ROWS:
            if r['grp'] in FX_GROUPS and name.upper() in (r['name'].upper(), f"{r['page'].upper()}.{r['short'].upper()}"):
                self.put(0x9c8 + 2 * r['id'], value << 8 if value < 0x100 else value)
                return self
        raise KeyError(name)

    def assign(self, voice, sound, level=100):
        self.voices[voice] = sound
        self.put(0x74 + 2 * voice, level << 8)          # 0x2ea in the capture = not yet ramped up after load
        self.put(0x94 + 2 * voice, MACHINES[sound.machine])
        rec = 0xda + 146 * voice
        for sid, v in sound.slots.items():
            self.put(rec + slot_off(sid), v)
        self.put(0x54 + 2 * voice, 3)
        return self

    def track(self, voice, t):
        """Voice -> track (frame +0xa5a + 2v; the DSP copies it to its map 0x257898 + 2v)."""
        self.put(0xA5A + 2 * voice, t)
        return self

    def note(self, block, voice, note, vel=100, length=None):
        self.events.setdefault(block, []).append(('on', voice, note, vel))
        if length:
            self.events.setdefault(block + length, []).append(('off', voice, note, vel))
        return self

    def frames(self, n_blocks):
        """-> list of n DSP-view packets."""
        out = []
        for b in range(n_blocks):
            f = bytearray(self.base)
            on = off = 0
            for kind, v, note, vel in self.events.get(b, []):
                if kind == 'on':
                    on |= 1 << v
                    self.put(0x02 + 2 * v, note << 8, f); self.put(0x34 + 2 * v, vel << 8, f)
                    self.put(0x02 + 2 * v, note << 8); self.put(0x34 + 2 * v, vel << 8)   # held for later blocks
                else:
                    off |= 1 << v
            self.put(0x22, on, f); self.put(0x26, on, f); self.put(0x24, off, f); self.put(0x28, off, f)
            out.append(D.swap16(bytes(f)))
        return out


def blocks(seconds):
    return int(seconds * 48000 / 32)


if __name__ == '__main__':
    s = Sound('WAVETONE')
    print(len(s.slots), 'slots;', sorted(s.names)[:12])
    song = Song().assign(0, s).note(2, 0, 60, 100, length=blocks(0.5))
    fr = song.frames(10)
    print(len(fr), len(fr[0]))


def side_packet(words):
    """Your own ColdFire -> DSP data: the DMA moves 30 halfwords after the 2688-byte frame that the stock driver never
    writes (references/sharc.md "Side channel"). Returns the 60 bytes in the DSP's view (little-endian halfwords) to
    append to a frame: frames = [f + side_packet(w) for f, w in zip(song.frames(n), words_per_block)]."""
    hw = list(words)[:30] + [0] * (30 - len(words))
    return b''.join((v & 0xFFFF).to_bytes(2, 'little') for v in hw)
