"""Summarise a recording: duration, peak, RMS, and how much of it is silent.

Parses RIFF directly because roc-recv may write 32-bit float WAV, which the
standard library's wave module refuses.
"""
import array
import math
import struct
import sys

data = open(sys.argv[1], "rb").read()
pos, fmt, samples = 12, None, None
while pos + 8 <= len(data):
    tag, size = struct.unpack_from("<4sI", data, pos)
    body = data[pos + 8:pos + 8 + size]
    if tag == b"fmt ":
        fmt = struct.unpack_from("<HHIIHH", body)
    elif tag == b"data":
        samples = body
    pos += 8 + size + (size & 1)
code, channels, rate, _, _, bits = fmt
if code == 0xFFFE:  # WAVE_FORMAT_EXTENSIBLE: the real code leads the subformat GUID.
    code = struct.unpack_from("<H", data, data.index(b"fmt ") + 8 + 24)[0]
if code == 3:
    values = array.array("f", samples[: len(samples) // 4 * 4])
elif bits == 16:
    values = [s / 32768 for s in array.array("h", samples[: len(samples) // 2 * 2])]
else:
    values = [s / 2 ** 31 for s in array.array("i", samples[: len(samples) // 4 * 4])]

chunk = rate * channels // 10  # 100 ms
chunks = [values[i:i + chunk] for i in range(0, len(values), chunk)]
silent = sum(1 for c in chunks if max(map(abs, c), default=0) < 0.001)
peak = max(map(abs, values), default=0)
rms = math.sqrt(sum(v * v for v in values) / max(len(values), 1))
print(f"{len(values) / channels / rate:.1f}s, {'float' if code == 3 else bits}-bit {channels}ch {rate} Hz, "
      f"peak {peak:.3f}, rms {rms:.3f}, silent 100 ms chunks {silent}/{len(chunks)}")
