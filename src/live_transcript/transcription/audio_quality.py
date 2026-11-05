"""Observe decoded PCM quality without modifying archival or ASR input."""

import math
import struct


def measure(pcm, sample_rate=16000):
    if len(pcm) % 2 or sample_rate <= 0:
        raise ValueError("expected aligned signed 16-bit PCM")
    values = [v[0] for v in struct.iter_unpack("<h", pcm)]
    count = len(values)
    if not count:
        return dict(seconds=0, rms=0, dbfs=None, peak=0, clipped_fraction=0, flags=["empty"])
    rms = math.sqrt(sum(v * v for v in values) / count)
    peak = max(abs(v) for v in values)
    clipped = sum(abs(v) >= 32760 for v in values) / count
    flags = []
    if peak == 0:
        flags.append("digital_silence")
    elif rms < 220:
        flags.append("quiet_or_silence")
    if clipped >= 0.001:
        flags.append("clipping")
    # Level is not speech detection or an SNR estimate. Noise and overlap need
    # reference-based tests or a separate acoustic model, never invented labels.
    return dict(
        seconds=round(count / sample_rate, 3),
        rms=round(rms, 2),
        dbfs=round(20 * math.log10(rms / 32768), 2) if rms else None,
        peak=peak,
        clipped_fraction=round(clipped, 6),
        flags=flags,
    )
