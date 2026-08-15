# cv1k-hq-audio

Toolchain for rebuilding Cave CV1000 sound ROMs (Yamaha YMZ770C).

The goal is full replacement of in-game music and sound effects with higher
quality encodes, sourced from official Cave releases. At present this project
only replaces the music phrases, sourced from official game soundtrack audio CDs.
Sound effects are currently re-encoded from the ROM's own audio samples, as well
as any music phrase that the tool cannot match to a higher-quality original.
This puts every audio phrase at the same rate, as the chip clocks a single DAC.
A ROM cannot mix rates.

Stock ROMs carry **16 kHz mono at 40–48 kbps**. Our default build is:
**32 kHz mono, 112 kbps for music and 64 kbps for sound effects**.
This puts ibara's 1141s of audio (938s of it music) at roughly 14 MB,
inside of a **16 MB ROM** (original ROMs are 8MB).

## Important: Intellectual Property Notice

**This repo ships code, never ROMs or audio.** Building a new ROM requires your own game ROM
and official soundtrack audio CD rip. We will NEVER re-distribute Cave intellectual property,
so DO NOT ASK!

## Pipeline

```
match.py   ROM phrases -> which soundtrack track, and where the loop starts
build.py   recipe -> replacement ROM
makehq.py  same, but from the ROM's own audio (proves the chain, no OST needed)
selftest.py  round-trip checks against real ROMs
```

Matching is by normalized cross-correlation against the decoded phrase, at
16 kHz mono so the CD is band-limited to what the ROM has. Scores come out
strongly bimodal: a real match sits near 0.95, anything that is not the same
recording lands near 0.04. Unmatched phrases are easy to spot and simply keep
their ROM audio.

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
libtwolame plus a header rewrite, with no Layer II implementation here. Two
things the rewrite has to get right:

- **No CRC.** AMM has no CRC field, and Layer II with the protection bit clear
  puts 16 CRC bits exactly where AMM expects band data.
- **`param_index` must match the encoder.** It selects the band allocation
  table. AMM states it outright; MP2 makes the decoder infer it from
  (channels, rate, bitrate). MPEG-2 LSF has a single table, MPEG-1 varies it.

twolame emits constant-bitrate frames, so the allocation usually leaves a byte
or two spare. Cave's data is byte-tight, so each frame is trimmed to the bits
actually used before packing.

## What actually limits quality

Not the bitrate. Measured off ibara's own audio:

| | content to | at 7.8 kHz |
| --- | --- | --- |
| ROM phrase, 48 kbps | ~7.5 kHz | −79 dB |
| soundtrack CD | 20 kHz+ | 0 dB |

The encoder is already using nearly all of the 8 kHz available to it. That wall
is **Nyquist for a 16 kHz sample rate**, not an encoder limit, so the muffled
"telephone" character cannot be fixed by spending more bits. Extra bitrate at
16 kHz buys cleaner detail below 8 kHz (less warble and grain) and nothing
else. Only a higher sample rate removes the filtered sound.

Two things follow. Re-encoding the ROM's *own* audio at a higher rate gains
nothing, since the missing band cannot come back, which is why this is built
around soundtrack sources. And the source rip would be the next ceiling: against
a 220 kbps MP3 there is little point past ~256 kbps of MP2. For a game with
ibara's runtime the 16 MB cap binds well before that, so in practice the cap
sets the bitrate, not the source.

### Sizing

**This tool caps images at 16 MB.** It reads and writes 24-bit sample offsets,
and `--rom-size` accepts 8 or 16 MB and nothing else. That is the number to
build against today.

It is probably not the chip's real limit. The YMZ770C catalog (LSI-3MZ770C50)
documents a phrase ROM of up to 32 MB on a 16-bit data bus, and every table
entry stores its start address as *bits 24-0* -- 25 bits, not 24. The phrase
table's spare byte carries `ATBL` in bits 4-6, leaving room for a 25th address
bit; FBNeo reads only the low three bytes and drops it. CV1000's sound ROM is
two devices byteswapped as 16-bit words, which looks like the 16-bit bus case.

**Untested.** Nothing here has been run on hardware, and where bit 24 physically
sits is a guess from the address map. Confirming it would double the budget, and
unlike the YMZ774-style 28-bit offsets this repo tried and dropped, 25-bit
offsets would be documented chip behaviour rather than an emulator extension.
The replacement-ROM header keeps a reserved wide flag so an emulator can
recognise a 28-bit image and reject it instead of misreading it.

So the budget is what fits in 16 MB: pick the bitrate that spends it on the
music without overrunning, which is where the 112/64 kbps default comes from.

### Real hardware

The catalog says fs is *"set to the fs specified by the AMM data during
playback"*, so the rate is not a register or a build-time constant -- the
decoder reads it out of the frame header. The crystal only picks which pair of
rates is reachable:

| fs | XI |
| --- | --- |
| 48 / 24 kHz | 18.432 MHz |
| 44.1 / 22.05 kHz | 16.9344 MHz |
| **32 / 16 kHz** | **16.384 MHz** |

Stock CV1000 audio is 16 kHz, which per that table needs a 16.384 MHz XI, and
that same crystal covers 32 kHz. So a 32 kHz ROM should play on an unmodified
board: no oscillator swap, no ROM hack. The reset default is even fs = XI/512 =
32 kHz, dropping to 16 kHz once a 16 kHz phrase starts.

**What has not been verified.** All of the below wants a real cart:

- **Sequencer tempo.** Wait chains are tuned to a 16 kHz stream. If the
  sequencer counts fs samples, a 32 kHz ROM plays its music at double tempo and
  the sequence data would have to be retimed; if it counts off the fixed XI
  clock, tempo is unaffected. The catalog lists the `TMRH`/`TMRL` wait timers
  but never documents their units, and the emulator patch assumes the second
  case. Both cannot be right.
- **Fitting 16 MB onto a cart**, which is a board question, not a format one.
- Whether a 770C behaves as the catalog describes once every phrase in a ROM is
  at 32 kHz, rather than one at a time.

Until someone burns one, treat hardware support as plausible and unproven.

## Layout

```
cv1k/amm.py        ROM parsing, AMM frames, phrase/sequence tables
cv1k/encode.py     audio -> MP2 -> AMM phrases
cv1k/rompack.py    rebuild a ROM image, rewriting both tables
selftest.py        end-to-end checks against real ROMs
recipes/           per-game build recipes (phrase -> track, cut points)
```

`cv1k/amm.py` is vendored from
[cv1k_research](https://github.com/buffis/cv1k_research)`/Audio_ExtractData/`,
so keep the two in sync. That repo is also where the extraction and inspection
tooling lives (`cv1k_audio.py`: phrase listings, BGM/SFX classification,
sequence disassembly).

## Emulator support

A replacement ROM needs an emulator that knows to read the marker and clock its
output at the ROM's rate. That work lives on the `cv1k-hq-audio` branch of
[handledexception/FBNeo](https://github.com/handledexception/FBNeo/tree/cv1k-hq-audio),
which touches three files:

- `src/burn/snd/ymz770.cpp`, `.h` give `ymz770_init` an optional sample rate and
  give the sequencer its own 16 kHz clock, so a 32 kHz ROM does not play its
  music at double tempo. At 16 kHz that clock ticks once per sample, so stock
  ROMs stay bit-identical.

  Note this is not what the chip does. FBNeo opens the stream at a fixed rate
  and discards the sample rate `decode_buffer()` returns, which was harmless
  while every ROM was 16 kHz. Hardware follows the rate in the frame header
  instead, so the faithful fix is to honour the decoded rate per phrase, which
  would leave the header's `sample_rate` field redundant.
- `src/burn/drv/cave/d_cv1k.cpp` adds `cv1khq_parse()` and the HQ romsets. Keep
  it in step with the header constants in `cv1k/rompack.py`.

Stock romsets are matched by content and are unaffected. With no replacement
present, none of it runs.

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
