# cv1k_audio

Toolchain for rebuilding the audio in Cave CV1000 sound ROMs (Yamaha YMZ770C).

The goal is replacing the game music with higher quality encodes from the
official soundtrack CDs. Stock ROMs carry **16 kHz mono at 40–48 kbps**; the
format itself reaches far past that.

**This repo ships code, never audio.** Building a ROM needs your own game ROM
and your own soundtrack rip. Nothing here redistributes either.

## Status

Working: decode, re-encode, repack, and a self test that proves the round trip.
Not written yet: OST alignment, recipe-driven builds, and the FBNeo changes that
let an emulator load the result.

```
$ python selftest.py roms/ibara
ibara (8 MB, 256 phrases, 32 BGM)
    ok    repack identity: 256 phrases, 7.45 MB used, 576309 B slack
    ok    stock 16k/48k mono: phrase 2 6.00s, 36232 -> 36230 B (-0.0%), ROM 7.5/8.0 MB
    ok    HQ 32k/128k stereo: phrase 2 6.00s, 36232 -> 96131 B (+165.3%), ROM 7.5/32.0 MB
```

Re-encoding a phrase at stock settings lands within 2 bytes of Cave's own
encoder, which is the check that the header rewrite and frame trimming really
match the format.

## How it works

AMM is MPEG Layer II with a repacked 32-bit header, so encoding is ffmpeg's
libtwolame plus a header rewrite — no Layer II implementation here. Two things
the rewrite has to get right:

- **No CRC.** AMM has no CRC field, and Layer II with the protection bit clear
  puts 16 CRC bits exactly where AMM expects band data.
- **`param_index` must match the encoder.** It selects the band allocation
  table. AMM states it outright; MP2 makes the decoder infer it from
  (channels, rate, bitrate). MPEG-2 LSF has a single table, MPEG-1 varies it.

twolame emits constant-bitrate frames, so the allocation usually leaves a byte
or two spare. Cave's data is byte-tight, so each frame is trimmed to the bits
actually used before packing.

## Headroom

Measured ceilings from the decoder's own tables, against ~15 min of music:

| config | audio bandwidth | size | ROM |
| --- | --- | --- | --- |
| 16 kHz mono 48 kbps (stock) | 8 kHz | 5.4 MB | 8 MB |
| 32 kHz mono 112 kbps | 16 kHz | 12.6 MB | 16 MB |
| 44.1 kHz stereo 192 kbps | 22 kHz | 21.6 MB | 32 MB |

Sample rate is the bigger lever: 16 kHz caps the audio at 8 kHz of bandwidth no
matter how many bits are spent. Note that re-encoding the *existing* ROM audio
at a higher rate gains nothing — the missing band cannot be recovered, which is
why this is built around soundtrack sources.

Stock ROMs address samples with 24-bit offsets, capping the image at 16 MB.
`wide_offsets` takes bits 0-3 of each table entry's first byte as offset bits
24-27, reaching 256 MB. The YMZ774 in the same chip family already reads that
nibble, so it is a natural extension — but it is an emulator-side one, and no
real YMZ770C is known to honour it.

## Layout

```
cv1k/amm.py        ROM parsing, AMM frames, phrase/sequence tables
cv1k/encode.py     audio -> MP2 -> AMM phrases
cv1k/rompack.py    rebuild a ROM image, rewriting both tables
selftest.py        end-to-end checks against real ROMs
recipes/           per-game build recipes (phrase -> track, cut points)
patches/           FBNeo changes needed to load a wider/HQ ROM
```

`cv1k/amm.py` is vendored from
[cv1k_research](https://github.com/buffis/cv1k_research)`/Audio_ExtractData/` —
keep the two in sync. That repo is also where the extraction and inspection
tooling lives (`cv1k_audio.py`: phrase listings, BGM/SFX classification,
sequence disassembly).

## Repacking notes

Both the phrase and sequence tables are rewritten, so nothing depends on how
the source ROM arranged its data. What must hold:

- **Phrase indices keep their meaning.** The SH-3 asks for phrases by number
  and the game code is never touched.
- **ROM size stays a power of two.** The chip masks addresses with size-1.
- **Music phrase *durations* should be preserved.** A sequence's wait chain is
  tuned to its phrase's length, so changing a track's duration means retuning
  it. Changing only the byte size is free.

## Requirements

Python 3, and ffmpeg built with libtwolame (`ffmpeg -encoders | grep twolame`).
Set `FFMPEG` if it is not on `PATH`.
