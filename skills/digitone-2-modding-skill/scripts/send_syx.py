"""Send a .syx to a MIDI output at DIN pace (31250 baud = 3125 B/s): each SysEx message, then a wait for its bytes to leave
the DIN wire plus a margin (the bootloader has no retransmit: a dropped packet freezes the bar).
    python send_syx.py FILE.syx 'PORT NAME' [--dry]"""
import sys, time
import mido

path, port = sys.argv[1], sys.argv[2]
dry = '--dry' in sys.argv
raw = open(path, 'rb').read()
msgs, i = [], 0
while i < len(raw):
    assert raw[i] == 0xF0, (i, raw[i])
    j = raw.index(0xF7, i)
    msgs.append(raw[i + 1:j])
    i = j + 1
total = sum(len(m) + 2 for m in msgs)
print(f'{len(msgs)} messages, {total} bytes, ~{total / 3125 / 60 * 1.15:.1f} min at DIN pace', flush=True)
if dry:
    sys.exit(0)
out = mido.open_output(port)
t0 = time.time()
sent = 0
for n, m in enumerate(msgs):
    out.send(mido.Message('sysex', data=list(m)))
    sent += len(m) + 2
    due = t0 + sent / 3125 * 1.12                    # (12 % margin over the wire's own rate)
    d = due - time.time()
    if d > 0:
        time.sleep(d)
    if n % 500 == 0:
        print(f'{n}/{len(msgs)} {100 * sent / total:.1f}% {time.time() - t0:.0f}s', flush=True)
time.sleep(2)
out.close()
print(f'done: {len(msgs)} messages in {time.time() - t0:.0f}s', flush=True)
