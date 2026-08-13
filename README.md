# cv1k_audio

Toolchain for rebuilding the audio in Cave CV1000 sound ROMs (Yamaha YMZ770C).

The goal is replacing the game music with higher quality encodes from the
official soundtrack CDs. Stock ROMs carry **16 kHz mono at 40–48 kbps**; the
format itself reaches far past that.

**This repo ships code, never audio.** Building a ROM needs your own game ROM
and your own soundtrack rip. Nothing here redistributes either.

## Pipeline

```
match.py   ROM phrases -> which soundtrack track, and where the loop starts
build.py   recipe -> replacement ROM
makehq.py  same, but from the ROM's own audio (proves the chain, no OST needed)
selftest.py  round-trip checks against real ROMs
```

Matching is by normalized cross-correlation against the decoded phrase, at
16 kHz mono so the CD is band-limited to what the ROM has. Scores come out
strongly bimodal — a real match sits near 0.95, anything that is not the same
recording lands near 0.04 — so unmatched phrases are easy to spot and simply
keep their ROM audio.

That also recovers the structure: each track is an intro phrase plus a loop
phrase, and the loop matches more than one iteration of the CD version, so
`build.py` backs off a loop when a cut would reach the fade-out.

Soundtrack audio is level-matched to the phrase it replaces. CDs are mastered
far louder than the game, and the sound effects are not being replaced.

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

## What actually limits quality

Not the ROM size. Measured off ibara's own audio:

| | content to | at 7.8 kHz |
| --- | --- | --- |
| ROM phrase, 48 kbps | ~7.5 kHz | −79 dB |
| soundtrack CD | 20 kHz+ | 0 dB |

The encoder is already using nearly all of the 8 kHz available to it. That wall
is **Nyquist for a 16 kHz sample rate**, not an encoder limit, so the muffled
"telephone" character cannot be fixed by spending more bits. Extra bitrate at
16 kHz buys cleaner detail below 8 kHz — less warble and grain — and nothing
else. Only a higher sample rate removes the filtered sound.

Two things follow. Re-encoding the ROM's *own* audio at a higher rate gains
nothing, since the missing band cannot come back, which is why this is built
around soundtrack sources. And a soundtrack rip is usually the real ceiling:
against a 220 kbps MP3 there is little point past ~256 kbps of MP2.

### Sizing

For ibara's 1141s of audio, storage is not the constraint — even uncompressed
16-bit stereo at 44.1 kHz is 192 MB, inside what the format can address. The
limits that do exist:

- **24-bit sample offsets → 16 MB.** What the real chip reads.
- **28-bit offsets → 256 MB**, via `wide_offsets`, taking bits 0-3 of a table
  entry's first byte the way the YMZ774 in the same family already does. This
  is an emulator-side extension; a stock YMZ770C will not follow it.
- **255 MB** regardless, because `channel.pptr` is an INT32 holding `8*offset`.

So pick a bitrate for transparency to the source and let the size fall out,
unless real hardware is the target — then 16 MB and 24-bit offsets are hard.

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
