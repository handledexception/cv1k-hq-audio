"""Build a replacement sound ROM from a game's own audio.

    python makehq.py <dir-with-u23-u24> -o cv1khq.bin --rate 32000 --bitrate 128

This re-encodes the music already in the ROM, so it will not sound better --
the bandwidth thrown away at 16 kHz cannot come back. What it does is exercise
the whole chain end to end and produce something an emulator can load, which is
what you want before wiring soundtrack audio into it.

For a real build, replace the decode step with your own audio: same phrase
index, same duration, better source.
"""

import argparse
import os
import subprocess
import sys
import tempfile
import wave

from cv1k import amm, encode, rompack


def decode_phrase(rom, phrase, path, ffmpeg):
    """Decode one phrase to a WAV at its true rate and exact length."""
    data, _declared = amm.phrase_to_mp2(rom, phrase)
    fd, tmp = tempfile.mkstemp(suffix=".mp2")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", tmp,
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


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("romdir", help="directory holding u23 and u24")
    ap.add_argument("-o", "--out", default="cv1khq.bin")
    ap.add_argument("--rate", type=int, default=32000, choices=rompack.VALID_RATES)
    ap.add_argument("--bitrate", type=int, default=128, help="kbps")
    ap.add_argument("--stereo", action="store_true")
    ap.add_argument("--rom-size", type=lambda s: int(s, 0), default=0x2000000)
    ap.add_argument("--phrases", help="comma-separated phrase numbers (default: all music)")
    ap.add_argument("--ffmpeg", default=os.environ.get("FFMPEG", "ffmpeg"))
    args = ap.parse_args()

    rom = amm.load_rom(os.path.join(args.romdir, "u23"),
                       os.path.join(args.romdir, "u24"))
    phrases = amm.read_phrases(rom)

    source_rate = 0
    for p in phrases.values():
        if p.header is not None:
            source_rate = p.header.sample_rate
            break

    if args.phrases:
        want = [int(x) for x in args.phrases.split(",")]
    elif args.rate != source_rate:
        # One DAC, one stream, one rate: anything left at the old rate would
        # play at the wrong speed, so changing the rate means re-encoding
        # every phrase, effects included.
        want = sorted(phrases)
        print("rate changes %d -> %d Hz, so all %d phrases are re-encoded"
              % (source_rate, args.rate, len(want)))
    else:
        want = sorted(i for i in amm.classify_bgm(rom) if i in phrases)

    # Identical phrases share one offset in the table; encode each blob once.
    by_offset = {}
    replacements = {}
    channels = 2 if args.stereo else 1
    tmpdir = tempfile.mkdtemp(prefix="cv1khq_")

    print("re-encoding %d phrases at %d Hz %d kbps %s"
          % (len(want), args.rate, args.bitrate, "stereo" if args.stereo else "mono"))
    for n, i in enumerate(want, 1):
        p = phrases[i]
        if not p.frames:
            continue
        if p.offset in by_offset:
            replacements[i] = by_offset[p.offset]
            continue

        wav = os.path.join(tmpdir, "%03d.wav" % i)
        decode_phrase(rom, p, wav, args.ffmpeg)

        # Same duration, so every sequence wait chain still lines up.
        want_samples = int(round(p.seconds * args.rate))
        want_samples -= want_samples % amm.SAMPLES_PER_STEP

        blob = encode.encode_phrase(wav, args.rate, args.bitrate,
                                    channels=channels,
                                    total_samples=want_samples,
                                    ffmpeg=args.ffmpeg)
        got = encode.encoded_samples(blob)
        if got != want_samples:
            print("  phrase %3d: WARNING plays %d samples, wanted %d" % (i, got, want_samples))
        by_offset[p.offset] = blob
        replacements[i] = blob
        os.unlink(wav)
        print("  [%2d/%2d] phrase %3d  %6.2fs  %8d -> %8d B"
              % (n, len(want), i, p.seconds, p.nbytes, len(blob)))

    wide = args.rom_size > 0x1000000
    packed, used = rompack.pack(rom, replacements,
                                rom_size=args.rom_size,
                                wide_offsets=wide,
                                hq_sample_rate=args.rate)
    with open(args.out, "wb") as f:
        f.write(packed)

    bad = rompack.check_rate_consistency(packed, args.rate)
    if bad:
        shown = sorted(bad.items())[:8]
        print("\nERROR: %d phrases are not at %d Hz and would play at the wrong "
              "speed:" % (len(bad), args.rate))
        print("  " + ", ".join("phrase %d @%d Hz" % (i, r) for i, r in shown)
              + (" ..." if len(bad) > len(shown) else ""))
        print("  Re-run without --phrases so every phrase is re-encoded.")
        return 1

    hdr = rompack.read_hq_header(packed)
    print("\nwrote %s" % args.out)
    print("  %.2f / %.2f MB used, %d phrases replaced"
          % (used / 1048576.0, len(packed) / 1048576.0, len(replacements)))
    print("  header: v%d, %d Hz, wide offsets %s"
          % (hdr["version"], hdr["sample_rate"], hdr["wide_offsets"]))
    print("\nAdd it to the romset zip as cv1khq.bin.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
