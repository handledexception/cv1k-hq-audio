"""Rebuild a CV1000 sound ROM with replaced phrase data.

The phrase table stores nothing but a 24-bit offset per phrase, so replacement
data may be any size. What the layout has to preserve:

  * sequence data stays where it is, because the sequence table's offsets are
    not rewritten (nothing here changes sequences);
  * every phrase entry keeps its index, because the SH-3 asks for phrases by
    number and the game code is untouched;
  * the ROM length stays a power of two, because the chip masks with size-1.

This module reads and writes 24-bit sample offsets, capping the image at 16 MB.
That is the tool's limit, and probably not the chip's: the YMZ770C catalog
(LSI-3MZ770C50) documents up to 32 MB on a 16-bit data bus and describes each
table entry's start address as bits 24-0, i.e. 25 bits. Where bit 24 sits is
unconfirmed and untested on hardware, so nothing here emits it. See README.

The YMZ774 in the same family goes further, taking bits 0-3 of the entry's first
byte as offset bits 24-27. The YMZ770C does not read those.
"""

import collections

from . import amm

MAX_OFFSET_24 = 0xFFFFFF

# Marker an emulator uses to tell a replacement ROM from a stock one. It sits in
# the SAC table, which the YMZ770C only reads in Simple Access mode -- a mode
# CV1000 cannot reach, because /SEL is tied high. Fields are big-endian to match
# the phrase table. Must stay in step with cv1khq_parse() in d_cv1k.cpp, on the
# cv1k-hq-audio branch of github.com/handledexception/FBNeo.
HQ_MAGIC = b"CV1KAUD\0"
HQ_HEADER = 0x800
HQ_HEADER_LEN = 0x14
HQ_VERSION = 1
HQ_FLAG_WIDE = 1        # 28-bit offsets. Never set here; bit 0 stays claimed
                        # so it cannot be reused, and cv1khq_parse() rejects any
                        # ROM that sets it rather than misreading the offsets.

VALID_RATES = (16000, 22050, 24000, 32000, 44100, 48000)


class PackError(Exception):
    pass


def _fnv1a(data):
    h = 2166136261
    for b in data:
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return h


def write_hq_header(rom, sample_rate, version=HQ_VERSION):
    """Stamp the replacement-ROM marker into a packed image."""
    if sample_rate not in VALID_RATES:
        raise PackError("sample rate %d is not encodable in AMM" % sample_rate)
    out = bytearray(rom)
    flags = 0
    hdr = bytearray(HQ_MAGIC)
    hdr += version.to_bytes(2, "big")
    hdr += flags.to_bytes(2, "big")
    hdr += len(out).to_bytes(4, "big")
    hdr += sample_rate.to_bytes(4, "big")
    assert len(hdr) == HQ_HEADER_LEN
    hdr += _fnv1a(hdr).to_bytes(4, "big")
    out[HQ_HEADER:HQ_HEADER + len(hdr)] = hdr
    return bytes(out)


def check_rate_consistency(rom, sample_rate):
    """Phrase indices whose audio is not at `sample_rate`.

    The chip clocks one DAC, and an emulator has a single output stream, so a
    ROM cannot mix rates: anything left at 16 kHz inside a 32 kHz image plays
    at double speed. Every phrase has to be re-encoded, not just the music.
    """
    bad = {}
    for i, p in amm.read_phrases(rom).items():
        if p.header is not None and p.header.sample_rate != sample_rate:
            bad[i] = p.header.sample_rate
    return bad


def read_hq_header(rom):
    """Parse the marker, or None if this looks like a stock ROM."""
    h = rom[HQ_HEADER:HQ_HEADER + HQ_HEADER_LEN + 4]
    if len(h) < HQ_HEADER_LEN + 4 or bytes(h[:8]) != HQ_MAGIC:
        return None
    if int.from_bytes(h[HQ_HEADER_LEN:HQ_HEADER_LEN + 4], "big") != _fnv1a(h[:HQ_HEADER_LEN]):
        raise PackError("replacement ROM header failed its checksum")
    return {
        "version": int.from_bytes(h[8:10], "big"),
        "flags": int.from_bytes(h[10:12], "big"),
        "rom_size": int.from_bytes(h[12:16], "big"),
        "sample_rate": int.from_bytes(h[16:20], "big"),
    }


def _is_pow2(n):
    return n > 0 and (n & (n - 1)) == 0


def read_sequences(rom):
    """{sequence index: bytes}. Lengths come from walking to the 0x0F terminator."""
    out = {}
    for i in range(amm.N_SEQS):
        off = amm.seq_offset(rom, i)
        if not off:
            continue
        _ops, _ticks, end = amm.disasm_sequence(rom, off)
        out[i] = rom[off:end]
    return out


def unpack(rom):
    """Split a ROM into {phrase index: bytes} and {sequence index: bytes}."""
    phrases = amm.read_phrases(rom)
    return ({i: rom[p.offset:p.end] for i, p in phrases.items()},
            read_sequences(rom))


class _Arena(object):
    """Append-only blob store that reuses identical blobs, as stock ROMs do."""

    def __init__(self, base):
        self.base = base
        self.data = bytearray()
        self._seen = {}

    def add(self, blob):
        blob = bytes(blob)
        key = (len(blob), hash(blob))
        for off in self._seen.get(key, ()):
            if self.data[off:off + len(blob)] == blob:
                return self.base + off
        off = len(self.data)
        self._seen.setdefault(key, []).append(off)
        self.data += blob
        return self.base + off

    def __len__(self):
        return len(self.data)


def pack(rom, replacements=None, rom_size=None, sequences=None,
         hq_sample_rate=None):
    """Build a new ROM image.

    replacements maps phrase index -> new AMM blob; sequences maps sequence
    index -> new sequence bytes. Anything left out keeps its original data.

    Both tables are rewritten, so nothing depends on how the source ROM laid
    its sequence and phrase data out. Sequences are placed first, keeping them
    at low offsets the way stock ROMs do.
    """
    replacements = dict(replacements or {})
    seq_replacements = dict(sequences or {})

    old_phrases = amm.read_phrases(rom)
    old_seqs = read_sequences(rom)

    arena = _Arena(amm.DATA_START)

    seq_offsets = {}
    for i in sorted(set(old_seqs) | set(seq_replacements)):
        blob = seq_replacements.get(i, old_seqs.get(i))
        if blob:
            seq_offsets[i] = arena.add(blob)

    phrase_blobs = {}
    for i in sorted(set(old_phrases) | set(replacements)):
        if i in replacements:
            blob = bytes(replacements[i])
        else:
            p = old_phrases[i]
            blob = rom[p.offset:p.end]
        if blob:
            phrase_blobs[i] = blob

    # Largest first, so the big tracks sit together and any offset-limit
    # failure shows up on the item that actually caused it.
    phrase_offsets = {}
    for i in sorted(phrase_blobs, key=lambda k: (-len(phrase_blobs[k]), k)):
        phrase_offsets[i] = arena.add(phrase_blobs[i])

    offsets = phrase_offsets
    total = amm.DATA_START + len(arena)
    if rom_size is None:
        rom_size = 1 << (total - 1).bit_length()
        rom_size = max(rom_size, len(rom))
    if not _is_pow2(rom_size):
        raise PackError("ROM size 0x%x is not a power of two; the chip masks with size-1"
                        % rom_size)
    if total > rom_size:
        raise PackError("content needs %.2f MB but ROM size is %.2f MB"
                        % (total / 1048576.0, rom_size / 1048576.0))

    for what, table in (("phrase", phrase_offsets), ("sequence", seq_offsets)):
        for i, off in table.items():
            if off > MAX_OFFSET_24:
                raise PackError(
                    "%s %d lands at 0x%x, past the 24-bit offset limit; "
                    "16 MB is all the chip can address" % (what, i, off))

    out = bytearray(rom_size)
    out[:amm.DATA_START] = rom[:amm.DATA_START]     # tables, rewritten below
    out[amm.DATA_START:total] = arena.data

    def write_entry(base, i, off, keep_high):
        e = base + 4 * i
        out[e] = keep_high
        out[e + 1] = (off >> 16) & 0xFF
        out[e + 2] = (off >> 8) & 0xFF
        out[e + 3] = off & 0xFF

    for i in range(amm.N_PHRASES):
        write_entry(amm.PHRASE_TABLE, i, phrase_offsets.get(i, 0),
                    rom[amm.PHRASE_TABLE + 4 * i] & 0x70)    # keep atbl
    for i in range(amm.N_SEQS):
        write_entry(amm.SEQ_TABLE, i, seq_offsets.get(i, 0),
                    rom[amm.SEQ_TABLE + 4 * i] & 0x70)

    result = bytes(out)
    if hq_sample_rate is not None:
        result = write_hq_header(result, hq_sample_rate)
    return result, total


def to_u23_u24(rom):
    """Split a packed image into byteswapped U23/U24 halves.

    This is the form a romset wants: the sample ROM is two parts on the board,
    stored byteswapped relative to what the chip sees, and an emulator loads
    both halves and swaps. Writing replacement sets the same way means they are
    the same shape as a real dump -- 8 MB parts instead of 4 MB ones -- rather
    than a single file needing its own load path.
    """
    d = bytearray(rom)
    d[0::2], d[1::2] = d[1::2], d[0::2]
    half = len(d) // 2
    return bytes(d[:half]), bytes(d[half:])


def summarize(rom, total_used):
    """Occupancy of a packed image, for reporting."""
    phrases = amm.read_phrases(rom)
    uniq = {}
    for i, p in phrases.items():
        uniq.setdefault(p.offset, p)
    bgm = amm.classify_bgm(rom)
    stat = collections.OrderedDict()
    stat["rom_size"] = len(rom)
    stat["used"] = total_used
    stat["free"] = len(rom) - total_used
    stat["phrases"] = len(uniq)
    stat["bgm_bytes"] = sum(p.nbytes for o, p in uniq.items() if p.index in bgm)
    stat["sfx_bytes"] = sum(p.nbytes for o, p in uniq.items() if p.index not in bgm)
    return stat
