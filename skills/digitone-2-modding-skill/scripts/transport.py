"""Start / stop the Digitone II sequencer over USB MIDI (System Real-Time Start 0xFA / Stop 0xFC; the unit's
SETTINGS > MIDI CONFIG > SYNC > TRANSPORT RECEIVE must be on).
    python transport.py start|stop|cont
(the unit: TRANSPORT RECEIVE on, CLOCK RECEIVE off; the pattern loaded on the unit plays)"""
import sys
import mido
out = mido.open_output('Elektron Digitone II')
out.send(mido.Message({'start': 'start', 'stop': 'stop', 'cont': 'continue'}[sys.argv[1]]))
out.close()
