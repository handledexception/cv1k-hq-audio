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
import tempfile

import numpy as np

from . import amm

SEARCH_RATE = 16000


class AlignError(Exception):
    pass


def decode_phrase(rom, phrase, ffmpeg="ffmpeg", rate=SEARCH_RATE):
    """Decode a ROM phrase to a float32 mono array at `rate`.

    The .mp2 a phrase exports to may declare a different sample rate than the
    phrase really uses; that is how param_index survives the re-header when no
    LSF header can express it, as in deathsml and ddpsdoj. Asking ffmpeg for an
    output rate would make it resample from the declared rate and stretch the
    audio, so decode at whatever it claims, relabel to the real rate, and
    resample from there.
    """
    data, _declared = amm.phrase_to_mp2(rom, phrase)
    fd, tmp = tempfile.mkstemp(suffix=".mp2")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", tmp,
             "-ac", "1", "-f", "s16le", "-c:a", "pcm_s16le", "-"],
            capture_output=True)
        if proc.returncode != 0:
            raise AlignError(proc.stderr.decode("utf-8", "replace").strip())
    finally:
        os.unlink(tmp)

    x = np.frombuffer(proc.stdout, dtype="<i2").astype(np.float32)
    x = x[:phrase.samples]                  # drop the padded final frame
    real = phrase.header.sample_rate if phrase.header else rate
    if real != rate and len(x) > 1:
        n = int(round(len(x) * rate / float(real)))
        x = np.interp(np.linspace(0, len(x) - 1, n),
                      np.arange(len(x)), x).astype(np.float32)
    return x


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
