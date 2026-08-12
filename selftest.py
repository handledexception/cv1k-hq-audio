"""End-to-end checks against real ROMs.

    python selftest.py <dir-with-u23-u24> [...]

Each ROM directory is exercised three ways:

  1. repack unchanged   -- the packer must be a no-op on stock data
  2. re-encode one BGM phrase at stock settings and pack into 8 MB
  3. same, but at a higher rate/bitrate into a wider ROM

(2) is the interesting one: it validates the encoder and packer together
without needing any emulator change, because the result is a stock-format ROM.
"""

import os
import subprocess
import sys
import tempfile
import wave

from cv1k import amm, encode, rompack

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")


def _fail(msg):
    print("    FAIL  " + msg)
    return False


def _ok(msg):
    print("    ok    " + msg)
    return True


def decode_phrase_to_wav(rom, phrase, path):
    data, _declared = amm.phrase_to_mp2(rom, phrase)
    fd, tmp = tempfile.mkstemp(suffix=".mp2")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        proc = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", tmp,
             "-f", "s16le", "-c:a", "pcm_s16le", "-"], capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode("utf-8", "replace"))
        ch = phrase.header.channels
        with wave.open(path, "wb") as w:
            w.setnchannels(ch)
            w.setsampwidth(2)
            w.setframerate(phrase.header.sample_rate)
            w.writeframes(proc.stdout[:phrase.samples * ch * 2])
    finally:
        os.unlink(tmp)


def test_identity(rom, name):
    """Repacking without replacements must preserve every phrase exactly."""
    packed, used = rompack.pack(rom, rom_size=len(rom))
    before = amm.read_phrases(rom)
    after = amm.read_phrases(packed)
    if set(before) != set(after):
        return _fail("phrase set changed: %d -> %d" % (len(before), len(after)))
    for i in before:
        a, b = before[i], after[i]
        if rom[a.offset:a.end] != packed[b.offset:b.end]:
            return _fail("phrase %d data changed" % i)
        if a.samples != b.samples:
            return _fail("phrase %d length %d -> %d" % (i, a.samples, b.samples))
    return _ok("repack identity: %d phrases, %.2f MB used, %d B slack"
               % (len(before), used / 1048576.0, len(rom) - used))


def test_reencode(rom, name, phrase_idx, sample_rate, bitrate, channels,
                  rom_size, wide, label):
    phrases = amm.read_phrases(rom)
    src = phrases[phrase_idx]

    wav = os.path.join(tempfile.gettempdir(), "cv1k_%s_%d.wav" % (name, phrase_idx))
    decode_phrase_to_wav(rom, src, wav)

    # Keep the same duration so sequence wait chains still line up.
    want_samples = int(round(src.seconds * sample_rate))
    want_samples -= want_samples % amm.SAMPLES_PER_STEP

    blob = encode.encode_phrase(wav, sample_rate, bitrate, channels=channels,
                                total_samples=want_samples, ffmpeg=FFMPEG)

    got = encode.encoded_samples(blob)
    if got != want_samples:
        return _fail("%s: asked %d samples, phrase plays %d" % (label, want_samples, got))

    packed, used = rompack.pack(rom, {phrase_idx: blob},
                                rom_size=rom_size, wide_offsets=wide)
    out = amm.read_phrases(packed)[phrase_idx]
    if out.samples != want_samples:
        return _fail("%s: after packing, phrase plays %d samples" % (label, out.samples))
    h = out.header
    if h.sample_rate != sample_rate or h.channels != channels:
        return _fail("%s: packed phrase is %d Hz %dch" % (label, h.sample_rate, h.channels))

    # Everything else must survive untouched.
    after = amm.read_phrases(packed)
    for i, before in phrases.items():
        if i == phrase_idx:
            continue
        if rom[before.offset:before.end] != packed[after[i].offset:after[i].end]:
            return _fail("%s: phrase %d was disturbed" % (label, i))

    delta = out.nbytes - src.nbytes
    return _ok("%s: phrase %d %.2fs, %d -> %d B (%+.1f%%), ROM %.1f/%.1f MB"
               % (label, phrase_idx, out.seconds, src.nbytes, out.nbytes,
                  100.0 * delta / src.nbytes, used / 1048576.0, rom_size / 1048576.0))


def run(path):
    u23, u24 = os.path.join(path, "u23"), os.path.join(path, "u24")
    if not (os.path.exists(u23) and os.path.exists(u24)):
        print("%s: no u23/u24, skipped" % path)
        return True
    name = os.path.basename(os.path.normpath(path))
    rom = amm.load_rom(u23, u24)
    bgm = sorted(amm.classify_bgm(rom))
    phrases = amm.read_phrases(rom)
    # A mid-sized music phrase: long enough to be real, short enough to be quick.
    cands = [i for i in bgm if i in phrases and 5.0 < phrases[i].seconds < 20.0]
    idx = cands[0] if cands else bgm[0]

    print("%s (%d MB, %d phrases, %d BGM)" % (name, len(rom) // 1048576, len(phrases), len(bgm)))
    results = [
        test_identity(rom, name),
        test_reencode(rom, name, idx, 16000, 48, 1, len(rom), False, "stock 16k/48k mono"),
        test_reencode(rom, name, idx, 32000, 128, 2, 0x2000000, True, "HQ 32k/128k stereo"),
    ]
    print()
    return all(results)


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    return 0 if all(run(p) for p in argv[1:]) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
