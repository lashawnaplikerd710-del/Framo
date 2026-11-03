"""Original deterministic instrumental bed and cue, with no sampled recordings."""
import json
from pathlib import Path
import wave
import numpy as np


def build(root):
    root = Path(root)
    rate, beat = 24000, 60 / 104
    duration = 32 * beat
    music = np.zeros(round(duration * rate))
    rng = np.random.default_rng(413)

    def note(start, seconds, frequency, gain, kind='pluck'):
        count = round(seconds * rate)
        t = np.arange(count) / rate
        if kind == 'hat':
            tone = rng.uniform(-1, 1, count) * np.exp(-t * 65)
        elif kind == 'kick':
            tone = np.sin(2*np.pi*(48*t + 1.8*(1-np.exp(-t*25)))) * np.exp(-t*17)
        else:
            tone = (np.sin(2*np.pi*frequency*t) + .16*np.sin(4*np.pi*frequency*t))
            tone *= np.minimum(t*180, 1) * np.exp(-t*4)
        pos = round(start * rate)
        stop = min(pos+count, len(music))
        music[pos:stop] += gain * tone[:stop-pos]

    # An original restrained Cmaj7 / Am7 / Fmaj7 / G instrumental phrase.
    chords = [(60,64,67,71), (57,60,64,67), (53,57,60,64), (55,59,62,67)]
    for bar in range(8):
        chord = chords[bar % 4]
        for step in range(8):
            midi = chord[(step + bar) % 4] + 12
            note((bar*4+step*.5)*beat, beat*.9, 440*2**((midi-69)/12), .11)
            note((bar*4+step*.5)*beat, .08, 0, .018, 'hat')
        for step in (0,2):
            note((bar*4+step)*beat, .28, 0, .11, 'kick')
            note((bar*4+step)*beat, beat*1.7, 440*2**((chord[0]-12-69)/12), .07)
    fade = np.minimum(np.arange(len(music))/1200, 1) * np.minimum(np.arange(len(music))[::-1]/1200, 1)
    music *= fade
    t = np.arange(rate) / rate
    cue = .18*(np.sin(2*np.pi*880*t)+.4*np.sin(2*np.pi*1320*t))*np.exp(-t*8)*np.minimum(t*250,1)
    for relative, data, label in [('music/framo-soft-pulse.wav', music, 'Framo Soft Pulse'),
                                   ('sfx/framo-chime.wav', cue, 'Framo Chime')]:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), 'wb') as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(rate)
            out.writeframes((np.clip(data,-1,1)*32767).astype('<i2').tobytes())
        path.with_suffix('.provenance.json').write_text(json.dumps({
            'source': 'original_procedural_synthesis', 'title': label,
            'samples': 'none', 'generator': 'builtin_audio.py',
            'redistribution': 'Included for use and redistribution with Framo; original synthetic audio.'
        }, ensure_ascii=False, indent=2), encoding='utf-8')
