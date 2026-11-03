"""Original procedural source audio; no video decoding or video encoding."""
import math
from pathlib import Path
import wave


def synthesize(output, duration_us, theme):
    import numpy as np
    output = Path(output); output.mkdir(exist_ok=True, parents=True)
    sr = 44100
    def save(name, data):
        data = np.asarray(data)
        if data.ndim == 1: data = np.column_stack((data, data))
        peak = float(np.max(np.abs(data)))
        if not 0 < peak < 1: raise ValueError('Invalid generated source audio peak')
        path = output/(name+'.wav')
        with wave.open(str(path), 'wb') as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(sr)
            w.writeframes((data*32767).astype('<i2').tobytes())
        return str(path)
    def tone(midi, length, amp):
        t = np.arange(round(length*sr))/sr
        f = 440*2**((midi-69)/12)
        envelope = (1-np.exp(-t*90))*np.exp(-t*4)*np.minimum(1, (length-t)*30)
        return amp*(np.sin(2*np.pi*f*t)+.2*np.sin(4*np.pi*f*t))*envelope
    # Extra source handle absorbs MediaInfo's millisecond duration rounding.
    # The native music clip still ends at the exact project duration.
    n = math.ceil(duration_us*sr/1_000_000)+sr//10
    music = np.zeros((n, 2)); beat = 60/(104 if theme == 'light' else 84)
    chords = [(53,57,60,65), (48,52,55,60), (55,59,62,67), (57,60,64,69)]
    rng = np.random.default_rng(927)
    for b in range(math.ceil(n/sr/beat)):
        k = round(b*beat*sr); c = chords[(b//4)%4]
        sound = tone(c[b%4]+12, .65, .22)
        count = min(len(sound), n-k)
        if count > 0: music[k:k+count] += sound[:count, None]*np.array([.85,1] if b%2 else [1,.85])
        if b%2 == 0:
            bass = tone(c[0]-12, .8, .14); count = min(len(bass), n-k)
            if count > 0: music[k:k+count] += bass[:count,None]
    tick = tone(79, .18, .3)
    chime = tone(84, .7, .25)+tone(91, .7, .15)
    t = np.arange(round(.4*sr))/sr
    whoosh = np.convolve(rng.normal(0,1,len(t)), np.ones(9)/9, 'same')*.3*np.sin(np.pi*t/.4)**2
    return {'music': save('original-'+theme, music), 'tick': save('original-tick', tick),
            'chime': save('original-chime', chime), 'whoosh': save('original-whoosh', whoosh)}
