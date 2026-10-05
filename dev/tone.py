"""Write a 440 Hz stereo test tone at Bragi's wire format (44.1 kHz, 16-bit)."""
import math
import struct
import sys
import wave

path, seconds = sys.argv[1], float(sys.argv[2])
with wave.open(path, "wb") as out:
    out.setnchannels(2)
    out.setsampwidth(2)
    out.setframerate(44100)
    frames = bytearray()
    for i in range(int(44100 * seconds)):
        sample = int(0.3 * 32767 * math.sin(2 * math.pi * 440 * i / 44100))
        frames += struct.pack("<hh", sample, sample)
    out.writeframes(frames)
