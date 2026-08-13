"""Match a game's music phrases against soundtrack tracks, writing a recipe.

    python match.py <dir-with-u23-u24> <ost-dir> -o recipes/ibara.json

The recipe records, per phrase, which track it came from and where the loop
starts in it. Nothing but paths and offsets -- no audio.

Scores are strongly bimodal in practice: a real match sits near 0.95+, anything
that is not the same recording lands near the noise floor around 0.04. Phrases
below the threshold are written out unmatched so they keep their ROM audio.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

import numpy as np

from cv1k import amm, align

# How well a phrase must correlate to count as found. The absolute score
# varies a lot by game -- ibara's matches land near 0.95, ddpsdoj's near 0.41,
# because that game's audio is not a straight downmix of the CD stereo mix. The
# margin over the next best track is far steadier: a real match beats the
# runner-up by 20x, a false one by nothing. So require both a floor and a gap.
DEFAULT_THRESHOLD = 0.08
DEFAULT_MARGIN = 3.0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("romdir")
    ap.add_argument("ostdir")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help="minimum correlation score")
    ap.add_argument("--margin", type=float, default=DEFAULT_MARGIN,
                    help="how many times the runner-up the best must be")
    ap.add_argument("--exclude", default="Arranged,Voice,Arrange",
                    help="skip tracks whose names contain these (comma separated)")
    ap.add_argument("--ffmpeg", default=os.environ.get("FFMPEG", "ffmpeg"))
    args = ap.parse_args()

    rom = amm.load_rom(os.path.join(args.romdir, "u23"),
                       os.path.join(args.romdir, "u24"))
    phrases = amm.read_phrases(rom)
    bgm = sorted(i for i in amm.classify_bgm(rom) if i in phrases and phrases[i].frames)

    skip = [s.strip().lower() for s in args.exclude.split(",") if s.strip()]
    tracks = [t for t in align.list_tracks(args.ostdir)
              if not any(s in os.path.basename(t).lower() for s in skip)]
    print("%d music phrases, %d candidate tracks" % (len(bgm), len(tracks)))

    cache = {}
    entries = []
    matched = 0
    seen = {}
    for i in bgm:
        p = phrases[i]
        if p.offset in seen:                    # duplicate table entries
            entry = dict(seen[p.offset], phrase=i)
            entries.append(entry)
            continue
        ref = align.decode_phrase(rom, p, ffmpeg=args.ffmpeg)
        results = align.match_phrase(ref, tracks, ffmpeg=args.ffmpeg, cache=cache)
        best = results[0] if results else (0.0, 0.0, None)
        score, start, path = best
        runner = results[1][0] if len(results) > 1 else 0.0

        entry = {
            "phrase": i,
            "seconds": round(p.seconds, 6),
            "samples": p.samples,
            "score": round(score, 4),
            "runner_up": round(runner, 4),
        }
        ratio = score / runner if runner > 0 else float("inf")
        if score >= args.threshold and ratio >= args.margin:
            entry["track"] = os.path.basename(path)
            entry["start"] = round(start, 6)
            matched += 1
            print("  phrase %3d  %6.2fs  %.3f (%4.1fx)  @%7.2fs  %s"
                  % (i, p.seconds, score, ratio, start, os.path.basename(path)))
        else:
            print("  phrase %3d  %6.2fs  %.3f (%4.1fx)  no match (keeps ROM audio)"
                  % (i, p.seconds, score, ratio))
        seen[p.offset] = entry
        entries.append(entry)

    recipe = {
        "rom": os.path.basename(os.path.normpath(args.romdir)),
        "ost_dir": os.path.abspath(args.ostdir),
        "threshold": args.threshold,
        "margin": args.margin,
        "phrases": entries,
    }
    outdir = os.path.dirname(args.out)
    if outdir and not os.path.isdir(outdir):
        os.makedirs(outdir)
    with open(args.out, "w") as f:
        json.dump(recipe, f, indent=2)
    print("\nmatched %d/%d phrases -> %s" % (matched, len(entries), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
