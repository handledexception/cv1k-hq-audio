"""Locate each ROM phrase inside a soundtrack track.

An OST track is not the in-game loop: it is usually the loop played through
twice with a fade, sometimes a different master, and the disc order does not
follow phrase order. But the ROM already holds the answer -- the phrase *is*
the loop, so decoding it gives a reference to search the CD audio for.

Both signals are brought to 16 kHz mono, which also band-limits the CD to the
8 kHz the ROM has, so the two are directly comparable. Normalized cross-
correlation then gives an offset and a score; a low score means that track is
not the same recording, which happens because these discs mix original and
arranged versions.
"""

import os
import subprocess

import numpy as np

SEARCH_RATE = 16000


class AlignError(Exception):
    pass


def decode_to_mono(path, rate=SEARCH_RATE, ffmpeg="ffmpeg", start=None, duration=None):
    """Any ffmpeg-readable file to a float32 mono array at `rate`."""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", "%.6f" % start]
    cmd += ["-i", path]
    if duration is not None:
        cmd += ["-t", "%.6f" % duration]
    cmd += ["-ac", "1", "-ar", str(rate), "-f", "s16le", "-c:a", "pcm_s16le", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise AlignError("ffmpeg failed on %s: %s"
                         % (path, proc.stderr.decode("utf-8", "replace").strip()))
    return np.frombuffer(proc.stdout, dtype="<i2").astype(np.float32)


def normalized_xcorr(haystack, needle):
    """Best (score, offset) of `needle` inside `haystack`.

    Pearson correlation at every lag: the FFT gives the dot products, and a
    running sum of squares gives each window's energy, so amplitude and
    mastering differences between the CD and the ROM do not skew the match.
    """
    n, m = len(haystack), len(needle)
    if m > n:
        return 0.0, 0
    x = haystack - haystack.mean()
    y = needle - needle.mean()

    size = 1 << int(np.ceil(np.log2(n + m)))
    corr = np.fft.irfft(np.fft.rfft(x, size) * np.conj(np.fft.rfft(y, size)), size)
    corr = corr[:n - m + 1]

    # sliding window energy of the haystack
    csum = np.concatenate([[0.0], np.cumsum(x.astype(np.float64) ** 2)])
    win_energy = csum[m:] - csum[:-m]
    win_energy = win_energy[:len(corr)]

    denom = np.sqrt(win_energy * float(np.dot(y, y)))
    denom[denom <= 0] = np.inf
    ncc = corr / denom

    best = int(np.argmax(ncc))
    return float(ncc[best]), best


def match_phrase(reference, tracks, ffmpeg="ffmpeg", cache=None):
    """Best-matching track for one decoded phrase.

    reference: float32 mono at SEARCH_RATE (the decoded ROM phrase)
    tracks:    list of file paths
    Returns a list of (score, seconds, path) sorted best first.
    """
    out = []
    for path in tracks:
        if cache is not None and path in cache:
            audio = cache[path]
        else:
            audio = decode_to_mono(path, ffmpeg=ffmpeg)
            if cache is not None:
                cache[path] = audio
        if len(audio) < len(reference):
            continue
        score, offset = normalized_xcorr(audio, reference)
        out.append((score, offset / float(SEARCH_RATE), path))
    out.sort(key=lambda r: -r[0])
    return out


def list_tracks(directory, exts=(".mp3", ".flac", ".wav", ".m4a", ".ogg")):
    return sorted(os.path.join(directory, f) for f in os.listdir(directory)
                  if f.lower().endswith(exts))
