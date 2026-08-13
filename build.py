"""Build a replacement sound ROM from a recipe.

    python build.py recipes/ibara.json <dir-with-u23-u24> -o cv1khq.bin

Matched music comes from the soundtrack at the offset match.py found. Anything
unmatched -- unmatched music, and every sound effect -- is re-encoded from the
ROM's own audio, because there is no better source for it. All of it lands at
one sample rate, since the chip clocks a single DAC.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import wave
import zlib

import numpy as np

from cv1k import amm, align, encode, rompack


def decode_phrase_wav(rom, phrase, path, ffmpeg):
    """Decode a phrase to WAV at its own rate and exact length."""
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


def rms(x):
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2))) if len(x) else 0.0


def level_match_db(rom, phrase, track, start, ffmpeg):
    """Gain that puts the soundtrack segment at the ROM phrase's level.

    The CD is mastered louder than the game audio. Without this the replaced
    music would sit well above the sound effects, which are staying as they are.
    """
    data, _declared = amm.phrase_to_mp2(rom, phrase)
    fd, tmp = tempfile.mkstemp(suffix=".mp2")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", tmp,
             "-ar", str(align.SEARCH_RATE), "-ac", "1",
             "-f", "s16le", "-c:a", "pcm_s16le", "-"], capture_output=True)
        a = np.frombuffer(proc.stdout, dtype="<i2").astype(np.float32)
    finally:
        os.unlink(tmp)
    b = align.decode_to_mono(track, ffmpeg=ffmpeg, start=start,
                             duration=phrase.seconds)
    ra, rb = rms(a), rms(b)
    if ra <= 0 or rb <= 0:
        return 0.0
    return 20.0 * np.log10(ra / rb)


def track_duration(path, ffmpeg):
    probe = ffmpeg.replace("ffmpeg", "ffprobe")
    proc = subprocess.run([probe, "-v", "error", "-show_entries",
                           "format=duration", "-of", "csv=p=0", path],
                          capture_output=True)
    try:
        return float(proc.stdout.decode().strip())
    except ValueError:
        return 0.0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recipe")
    ap.add_argument("romdir")
    ap.add_argument("-o", "--out", default="cv1khq.bin")
    ap.add_argument("--rate", type=int, default=32000, choices=rompack.VALID_RATES)
    ap.add_argument("--bgm-bitrate", type=int, default=112)
    ap.add_argument("--sfx-bitrate", type=int, default=64)
    # 16 MB is the ceiling: sample offsets are 24 bits.
    ap.add_argument("--rom-size", type=lambda s: int(s, 0), default=0x1000000,
                    choices=(0x800000, 0x1000000))
    ap.add_argument("--no-level-match", action="store_true")
    ap.add_argument("--ffmpeg", default=os.environ.get("FFMPEG", "ffmpeg"))
    args = ap.parse_args()

    with open(args.recipe) as f:
        recipe = json.load(f)
    ostdir = recipe["ost_dir"]

    rom = amm.load_rom(os.path.join(args.romdir, "u23"),
                       os.path.join(args.romdir, "u24"))
    phrases = amm.read_phrases(rom)
    bgm = amm.classify_bgm(rom)
    by_phrase = {e["phrase"]: e for e in recipe["phrases"]}

    # Recipes name the track file, but a collection gets re-ripped: the same
    # disc may arrive later as .wav or .flac. Match on the stem so a recipe
    # built against one encoding still applies to a better one.
    available = {}
    for name in os.listdir(ostdir):
        stem, ext = os.path.splitext(name)
        if ext.lower() in (".wav", ".flac", ".mp3", ".m4a", ".ogg"):
            available.setdefault(stem.lower(), name)

    def resolve(track_name):
        stem = os.path.splitext(track_name)[0].lower()
        found = available.get(stem)
        if found is None:
            raise SystemExit("recipe wants %r but no file with that name is in %s"
                             % (track_name, ostdir))
        return os.path.join(ostdir, found)

    tmpdir = tempfile.mkdtemp(prefix="cv1kbuild_")
    replacements = {}
    done = {}
    from_ost = from_rom = 0

    for i in sorted(phrases):
        p = phrases[i]
        if not p.frames:
            continue
        if p.offset in done:
            replacements[i] = done[p.offset]
            continue

        entry = by_phrase.get(i)
        kbps = args.bgm_bitrate if i in bgm else args.sfx_bitrate

        if entry and entry.get("track"):
            path = resolve(entry["track"])
            start = entry["start"]
            dur = track_duration(path, args.ffmpeg)
            if dur and start + p.seconds > dur - 0.05:
                # A loop can match more than one iteration; taking a later one
                # risks running into the fade-out at the end of the track.
                print("  phrase %3d  cut would reach %.2fs of a %.2fs track, "
                      "backing off one loop" % (i, start + p.seconds, dur))
                start = max(0.0, start - p.seconds)
            gain = 0.0
            if not args.no_level_match:
                gain = level_match_db(rom, p, path, start, args.ffmpeg)
            want = int(round(p.seconds * args.rate))
            want -= want % amm.SAMPLES_PER_STEP
            blob = encode.encode_phrase(path, args.rate, kbps, channels=1,
                                        total_samples=want,
                                        start=start, duration=p.seconds,
                                        gain_db=gain, ffmpeg=args.ffmpeg)
            print("  phrase %3d  %6.2fs  OST  %-28s @%7.2fs  %+5.1f dB  %8d B"
                  % (i, p.seconds, entry["track"][:28], start, gain, len(blob)))
            from_ost += 1
        else:
            wav = os.path.join(tmpdir, "%03d.wav" % i)
            decode_phrase_wav(rom, p, wav, args.ffmpeg)
            want = int(round(p.seconds * args.rate))
            want -= want % amm.SAMPLES_PER_STEP
            blob = encode.encode_phrase(wav, args.rate, kbps, channels=1,
                                        total_samples=want, ffmpeg=args.ffmpeg)
            os.unlink(wav)
            print("  phrase %3d  %6.2fs  ROM  %-28s %20s %8d B"
                  % (i, p.seconds, "(%d kbps)" % kbps, "", len(blob)))
            from_rom += 1

        done[p.offset] = blob
        replacements[i] = blob

    packed, used = rompack.pack(rom, replacements, rom_size=args.rom_size,
                                hq_sample_rate=args.rate)

    bad = rompack.check_rate_consistency(packed, args.rate)
    if bad:
        print("\nERROR: %d phrases are not at %d Hz" % (len(bad), args.rate))
        return 1

    # A romset wants the two halves the board has, byteswapped, not one image.
    u23, u24 = rompack.to_u23_u24(packed)
    base = args.out
    for suffix in (".bin", ".u23", ".u24"):
        if base.lower().endswith(suffix):
            base = base[:-len(suffix)]
            break
    paths = (base + ".u23", base + ".u24")
    for path, half in zip(paths, (u23, u24)):
        with open(path, "wb") as f:
            f.write(half)

    hdr = rompack.read_hq_header(packed)
    print("\n%d phrases from the soundtrack, %d re-encoded from the ROM"
          % (from_ost, from_rom))
    print("  %.2f / %.2f MB used, %d Hz, 24-bit offsets"
          % (used / 1048576.0, len(packed) / 1048576.0, hdr["sample_rate"]))
    print("\nromset files (add both to the hack's zip):")
    for path, half in zip(paths, (u23, u24)):
        print("  %-40s %8d bytes  crc32 %08x"
              % (os.path.basename(path), len(half), zlib.crc32(half) & 0xFFFFFFFF))
    return 0


if __name__ == "__main__":
    sys.exit(main())
