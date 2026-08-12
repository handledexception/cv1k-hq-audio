"""Parsing library for the CV1000 sound ROM (U23+U24, Yamaha YMZ770C "AMMS-A").

Samples are stored in Yamaha's AMM format, which is plain MPEG Layer II audio
behind a repacked 32-bit header. The payload is bit-identical to Layer II, so
rewriting the header is enough to turn a phrase into a playable .mp2.

Unlike a fixed-length scan, the frame walker here reproduces the decoder's exact
bit consumption (see frame_bits), so frame boundaries are found rather than
guessed. That matters because 0xFFFx byte pairs occur inside frame payloads by
chance, and because not every game uses the same frame size.

Tables are transcribed from MAME's mpeg_audio.cpp.

Vendored from cv1k_research/Audio_ExtractData/amm.py -- keep the two in sync.
The copy lives here so this repo runs standalone; the canonical version belongs
next to the extraction tooling.
"""

# ---------------------------------------------------------------------------
# ROM layout
# ---------------------------------------------------------------------------

PHRASE_TABLE = 0x000    # 256 x 4 bytes: [atbl<<4][24-bit offset]
SEQ_TABLE    = 0x400    # 256 x 4 bytes: [??][24-bit offset]
SAC_TABLE    = 0x800    # 256 x 4 bytes, unused on CV1000 (/SEL is NC)
DATA_START   = 0xC00

N_PHRASES = 256
N_SEQS    = 256


def load_rom(*paths):
    """Load U23(+U24) and byteswap, giving the byte order the YMZ770C sees.

    Equivalent to FBNeo's BurnByteswap() on the concatenated ROMs, and to
    running U4_Utils/swap.py over each file first.
    """
    data = bytearray()
    for p in paths:
        with open(p, "rb") as f:
            data += f.read()
    data[0::2], data[1::2] = data[1::2], data[0::2]
    return bytes(data)


def phrase_offset(rom, i):
    e = PHRASE_TABLE + 4 * i
    return rom[e + 1] << 16 | rom[e + 2] << 8 | rom[e + 3]


def phrase_atbl(rom, i):
    return (rom[PHRASE_TABLE + 4 * i] >> 4) & 7


def seq_offset(rom, i):
    e = SEQ_TABLE + 4 * i
    return rom[e + 1] << 16 | rom[e + 2] << 8 | rom[e + 3]


# ---------------------------------------------------------------------------
# Bit reader
# ---------------------------------------------------------------------------

class BitReader(object):
    def __init__(self, data, bitpos=0):
        self.d = data
        self.p = bitpos

    def gb(self, n):
        v = 0
        for _ in range(n):
            v = (v << 1) | ((self.d[self.p >> 3] >> (7 - (self.p & 7))) & 1)
            self.p += 1
        return v


# ---------------------------------------------------------------------------
# Layer II tables (mpeg_audio.cpp)
# ---------------------------------------------------------------------------

SAMPLE_RATES = [44100, 48000, 32000, 0, 22050, 24000, 16000, 0]

TOTAL_BAND_COUNTS = [27, 30, 8, 12, 30]
JOINT_BAND_COUNTS = [4, 8, 12, 16]

# band_parameter_index_bits_count[5][32]
BAND_PARAM_BITS = [
    [4,4,4,4,4,4,4,4,4,4,4,3,3,3,3,3,3,3,3,3,3,3,3,2,2,2,2,0,0,0,0,0],
    [4,4,4,4,4,4,4,4,4,4,4,3,3,3,3,3,3,3,3,3,3,3,3,2,2,2,2,2,2,2,0,0],
    [4,4,3,3,3,3,3,3,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
    [4,4,3,3,3,3,3,3,3,3,3,3,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
    [4,4,4,4,3,3,3,3,3,3,3,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,0,0],
]

# band_parameter_indexed_values[5][32][17] -- only 8 distinct rows exist
_R_A = [0,1,3,5,6,7,8,9,10,11,12,13,14,15,16,17,-1]
_R_B = [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,17,-1]
_R_C = [0,1,2,3,4,5,6,17] + [-1] * 9
_R_D = [0,1,2,17] + [-1] * 13
_R_E = [0] + [-1] * 16
_R_F = [0,1,2,4,5,6,7,8,9,10,11,12,13,14,15,16,-1]
_R_G = [0,1,2,4,5,6,7,8] + [-1] * 9
_R_H = [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,-1]
_R_I = [0,1,2,4] + [-1] * 13

BAND_PARAM_VALUES = [
    [_R_A] * 3 + [_R_B] * 8 + [_R_C] * 12 + [_R_D] * 4 + [_R_E] * 5,
    [_R_A] * 3 + [_R_B] * 8 + [_R_C] * 12 + [_R_D] * 7 + [_R_E] * 2,
    [_R_F] * 2 + [_R_G] * 6 + [_R_E] * 24,
    [_R_F] * 2 + [_R_G] * 10 + [_R_E] * 20,
    [_R_H] * 4 + [_R_G] * 7 + [_R_I] * 19 + [_R_E] * 2,
]

# band_infos[18], columns "bits" and "cube_bits"
BAND_BITS = [0,2,3,3,4,4,5,6,7,8,9,10,11,12,13,14,15,16]
BAND_CUBE = [0,5,7,9,10,12,15,18,21,24,27,30,33,36,39,42,45,48]

# layer2_param_index[channels-1][srate_index][bitrate_index]
LAYER2_PARAM_INDEX = [
    [
        [ 1, 2, 2, 0, 0, 0, 1, 1, 1, 1, 1,-1,-1,-1,-1,-1],
        [ 0, 2, 2, 0, 0, 0, 0, 0, 0, 0, 0,-1,-1,-1,-1,-1],
        [ 1, 3, 3, 0, 0, 0, 1, 1, 1, 1, 1,-1,-1,-1,-1,-1],
        [-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1],
    ],
    [
        [ 1,-1,-1,-1, 2,-1, 2, 0, 0, 0, 1, 1, 1, 1, 1,-1],
        [ 0,-1,-1,-1, 2,-1, 2, 0, 0, 0, 0, 0, 0, 0, 0,-1],
        [ 1,-1,-1,-1, 3,-1, 3, 0, 0, 0, 1, 1, 1, 1, 1,-1],
        [-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1],
    ],
]

# kbps by bitrate_index, for MPEG-1 and MPEG-2 LSF Layer II
BITRATES_MPEG1 = [0,32,48,56,64,80,96,112,128,160,192,224,256,320,384,0]
BITRATES_LSF   = [0, 8,16,24,32,40,48,56,64,80,96,112,128,144,160,0]

SAMPLES_PER_FRAME = 1152
SAMPLES_PER_STEP = 32       # decode_mpeg2 emits 32 samples per frame_number
MAX_STEPS = 36              # 3 upper x 4 middle x 3 lower


# ---------------------------------------------------------------------------
# AMM frames
# ---------------------------------------------------------------------------

class AmmHeader(object):
    """32-bit AMM frame header.

    Field-for-field it is an MPEG Layer II header with {bitrate} replaced by
    {full_packets}, {padding,private} by {last_id}, and {copyright, original,
    emphasis} by {param_index, reserved}.
    """

    __slots__ = ("variant", "full_packets", "srate_index", "last_id",
                 "stereo_mode", "stereo_mode_ext", "param_index", "reserved")

    def __init__(self, rom, off):
        br = BitReader(rom, off * 8)
        if br.gb(12) != 0xFFF:
            raise ValueError("no sync at 0x%x" % off)
        self.variant = br.gb(3)
        br.gb(1)                            # unused
        self.full_packets = br.gb(4)
        self.srate_index = br.gb(2)
        self.last_id = br.gb(2)
        self.stereo_mode = br.gb(2)
        self.stereo_mode_ext = br.gb(2)
        self.param_index = br.gb(3)
        self.reserved = br.gb(1)

    @property
    def is_lsf(self):
        """variant 2 selects the MPEG-2 LSF rate table, variant 6 the MPEG-1 one."""
        return self.variant == 2

    @property
    def sample_rate(self):
        return SAMPLE_RATES[self.srate_index + (4 if self.is_lsf else 0)]

    @property
    def channels(self):
        return 1 if self.stereo_mode == 3 else 2

    @property
    def steps(self):
        """Number of 32-sample steps this frame decodes to (capped at 36)."""
        return min(3 * self.full_packets + self.last_id, MAX_STEPS)

    @property
    def samples(self):
        return self.steps * SAMPLES_PER_STEP

    def __repr__(self):
        return ("<AMM v%d %dHz %s param%d %d smp>"
                % (self.variant, self.sample_rate,
                   "mono" if self.channels == 1 else "stereo",
                   self.param_index, self.samples))


def is_sync(rom, off):
    return off + 1 < len(rom) and rom[off] == 0xFF and (rom[off + 1] & 0xF0) == 0xF0


def is_terminator(rom, off):
    """0xFFF0 ends a phrase (variant 0 is not a valid layer)."""
    return is_sync(rom, off) and (rom[off + 1] & 0x0E) == 0


def frame_bits(rom, off):
    """Exact number of bits the decoder consumes for the frame at `off`.

    Mirrors read_header_amm + read_data_mpeg2 + 12 x build_next_segments.
    Verified against real ROMs: the next frame always begins at the very next
    byte boundary, i.e. AMM carries no inter-frame padding.
    """
    h = AmmHeader(rom, off)
    br = BitReader(rom, off * 8 + 32)

    pidx = h.param_index
    if pidx >= len(TOTAL_BAND_COUNTS):
        raise ValueError("param_index %d out of range at 0x%x" % (pidx, off))

    chans = h.channels
    total = TOTAL_BAND_COUNTS[pidx]
    joint = total
    if h.stereo_mode == 1:                                  # joint stereo
        joint = min(JOINT_BAND_COUNTS[h.stereo_mode_ext], total)

    bits_tab = BAND_PARAM_BITS[pidx]
    vals_tab = BAND_PARAM_VALUES[pidx]

    def read_band_param(band):
        return vals_tab[band][br.gb(bits_tab[band])]

    # read_band_params
    band_param = [[0] * 32, [0] * 32]
    band = 0
    while band < joint:
        for c in range(chans):
            band_param[c][band] = read_band_param(band)
        band += 1
    while band < total:
        v = read_band_param(band)
        band_param[0][band] = band_param[1][band] = v
        band += 1

    # read_scfci
    scfsi = [[0] * 32, [0] * 32]
    for b in range(total):
        for c in range(chans):
            if band_param[c][b]:
                scfsi[c][b] = br.gb(2)

    # read_band_amplitude_params: 3, 2, 1 or 2 scalefactors of 6 bits
    for b in range(total):
        for c in range(chans):
            if band_param[c][b]:
                s = scfsi[c][b]
                br.gb(18 if s == 0 else 6 if s == 2 else 12)

    def triplet(c, b):
        idx = band_param[c][b]
        if idx == 0:
            return
        if idx in (1, 2, 4):
            br.gb(BAND_CUBE[idx])           # 3 values packed into one codeword
        else:
            br.gb(BAND_BITS[idx] * 3)

    for _ in range(12):                     # 3 upper_step x 4 middle_step
        band = 0
        while band < joint:
            for c in range(chans):
                triplet(c, band)
            band += 1
        while band < total:                 # shared bands above the joint limit
            triplet(0, band)
            band += 1

    return br.p - off * 8


def frame_length(rom, off):
    """Byte length of the frame at `off`, from its bit consumption."""
    return (frame_bits(rom, off) + 7) // 8


class Phrase(object):
    __slots__ = ("index", "offset", "end", "frames", "samples", "header")

    def __init__(self, index, offset, end, frames, samples, header):
        self.index = index
        self.offset = offset
        self.end = end
        self.frames = frames
        self.samples = samples
        self.header = header

    @property
    def nbytes(self):
        return self.end - self.offset

    @property
    def seconds(self):
        sr = self.header.sample_rate if self.header else 16000
        return self.samples / float(sr) if sr else 0.0

    @property
    def kbps(self):
        return self.nbytes * 8 / self.seconds / 1000.0 if self.seconds else 0.0


RESYNC_WINDOW = 64      # bytes of slack tolerated between frames


def walk_phrase(rom, offset, index=-1, max_frames=100000):
    """Walk a phrase from `offset`, returning a Phrase.

    Playback ends on the first frame decoding to fewer than 1152 samples, which
    is how the YMZ770C detects the end of a phrase -- the 0xFFF0 marker that
    follows is not what stops it.

    Cave's data is byte-tight, so a frame normally ends exactly where the next
    begins. The chip finds the next frame by scanning for a sync, though, so a
    small gap is tolerated here too: constant-bitrate encoders leave a byte or
    two of slack after the bits they actually used.
    """
    frames = []
    samples = 0
    first = None
    p = offset

    while len(frames) < max_frames:
        if not is_sync(rom, p) or is_terminator(rom, p):
            break
        h = AmmHeader(rom, p)
        if h.steps == 0:
            # A null phrase: header with no data. Cave points unused phrase
            # table entries at one of these.
            p += 4
            break
        if first is None:
            first = h
        n = frame_length(rom, p)
        frames.append((p, n, h))
        samples += h.samples
        p += n
        if h.samples < SAMPLES_PER_FRAME:   # last block
            break
        if not is_sync(rom, p):
            for skip in range(1, RESYNC_WINDOW):
                if is_sync(rom, p + skip):
                    p += skip
                    break
            else:
                break

    for skip in range(RESYNC_WINDOW):
        if is_terminator(rom, p + skip):
            p += skip + 2
            break
    return Phrase(index, offset, p, frames, samples, first)


def read_phrases(rom):
    """All 256 phrase-table entries as Phrase objects (index -> Phrase)."""
    out = {}
    cache = {}
    for i in range(N_PHRASES):
        off = phrase_offset(rom, i)
        if not off:
            continue
        if off not in cache:
            cache[off] = walk_phrase(rom, off, i)
        p = cache[off]
        out[i] = Phrase(i, p.offset, p.end, p.frames, p.samples, p.header)
    return out


# ---------------------------------------------------------------------------
# Sequences
# ---------------------------------------------------------------------------

SUBREG = ["PHRASE", "VOL", "PAN", "KON"]


def disasm_sequence(rom, offset, max_ops=400000):
    """Decode a sequence into (ops, ticks).

    Each reg/data pair costs one sample tick; register 0x0E adds 31 more, so a
    single 0x0E is a 32-sample wait. Register 0x0F ends the sequence.
    """
    ops = []
    ticks = 0
    p = offset
    while len(ops) < max_ops and p + 1 < len(rom):
        reg, data = rom[p], rom[p + 1]
        p += 2
        ticks += 1
        if reg == 0x0F:
            ops.append(("END", data, reg))
            break
        if reg == 0x0E:
            ticks += 31
            ops.append(("WAIT32", data, reg))
        elif reg < 0x40:
            ops.append(("GLOBAL%02X" % reg, data, reg))
        elif reg < 0x60:
            ops.append(("CH%d.%s" % ((reg >> 2) & 7, SUBREG[reg & 3]), data, reg))
        else:
            ops.append(("SEQ%02X" % reg, data, reg))
    return ops, ticks, p


def sequence_phrases(ops):
    """Phrase numbers keyed on by a sequence, in trigger order.

    A channel is keyed on when KON bits are 10 or 11; the phrase number is
    whatever was last written to that channel's PHRASE register.
    """
    cur = {}
    out = []
    for name, data, _reg in ops:
        if name.endswith(".PHRASE"):
            cur[name[:3]] = data
        elif name.endswith(".KON") and (data & 6) in (2, 6):
            ph = cur.get(name[:3])
            if ph is not None:
                out.append((ph, name[:3], bool(data & 1)))
    return out


def classify_bgm(rom):
    """Phrase indices used as music.

    The sequencer is the BGM player -- every phrase a sequence keys on is a
    music track, and sound effects are triggered by the SH-3 writing the chip's
    registers directly. Cross-checks: BGM phrases are long and loop, SFX are
    short one-shots.
    """
    bgm = set()
    for off in set(seq_offset(rom, i) for i in range(N_SEQS)) - {0}:
        ops, _ticks, _end = disasm_sequence(rom, off)
        for ph, _ch, _loop in sequence_phrases(ops):
            bgm.add(ph)
    return bgm


# ---------------------------------------------------------------------------
# AMM -> MP2
# ---------------------------------------------------------------------------

def mp2_frame_size(bitrate_kbps, sample_rate):
    return 144 * bitrate_kbps * 1000 // sample_rate


LSF_PARAM_INDEX = 4     # MPEG-2 LSF Layer II defines a single allocation table


def inferred_param_index(variant, channels, srate_index, bitrate_index):
    """Band table a standard Layer II decoder picks for these header fields.

    AMM states param_index outright; MP2 does not carry it, so re-headering has
    to land on fields the decoder will read back as the same table.
    """
    if variant == 2:                                    # MPEG-2 LSF
        return LSF_PARAM_INDEX
    return LAYER2_PARAM_INDEX[channels - 1][srate_index][bitrate_index]


def pick_mp2_header(header, nbytes):
    """MP2 header fields carrying `nbytes` of this frame under its own band table.

    Games do not all agree with what a Layer II decoder would infer. ibara and
    futaribl use param_index 4 at LSF rates, which matches; deathsml uses
    param_index 0 at an LSF rate, which no LSF header can express -- decoding
    that as LSF yields noise. An MPEG-1 header with the right bitrate/rate pair
    selects the correct table, at the cost of declaring a different sample rate,
    so the true rate is returned for the caller to apply.

    Returns (variant, srate_index, bitrate_index, frame_size, declared_rate).
    """
    want = header.param_index
    channels = header.channels
    best = None
    for variant, table in ((2, BITRATES_LSF), (6, BITRATES_MPEG1)):
        for srate_index in range(3):
            rate = SAMPLE_RATES[srate_index + (4 if variant == 2 else 0)]
            if not rate:
                continue
            for bitrate_index in range(1, 15):
                kbps = table[bitrate_index]
                if not kbps:
                    continue
                if inferred_param_index(variant, channels, srate_index, bitrate_index) != want:
                    continue
                size = mp2_frame_size(kbps, rate)
                if size < nbytes:
                    continue
                # Prefer keeping the real sample rate, then the tightest frame.
                native = variant == header.variant and srate_index == header.srate_index
                cand = (0 if native else 1, size, variant, srate_index, bitrate_index, rate)
                if best is None or cand < best:
                    best = cand
    if best is None:
        raise ValueError("no Layer II header expresses param_index %d for a %d byte frame"
                         % (want, nbytes))
    _rank, size, variant, srate_index, bitrate_index, rate = best
    return variant, srate_index, bitrate_index, size, rate


def amm_frame_to_mp2(rom, off, nbytes, header):
    """Re-header one AMM frame as a standard MPEG Layer II frame.

    AMM frames are variable length; MP2 frames are not, so the payload is padded
    up to the chosen frame size. The decoder stops at the bits it needs.
    """
    variant, srate_index, bitrate_index, size, _rate = pick_mp2_header(header, nbytes)
    b1 = 0xF0 | (variant << 1) | 1                 # sync tail, ID, layer, no CRC
    b2 = (bitrate_index << 4) | (srate_index << 2)  # bitrate, srate, no padding
    b3 = ((header.stereo_mode << 6) | (header.stereo_mode_ext << 4) | 0b0100)
    out = bytes([0xFF, b1, b2, b3]) + rom[off + 4:off + nbytes]
    return out + b"\x00" * (size - nbytes)


def phrase_to_mp2(rom, phrase):
    """Whole phrase as a playable .mp2 bytestring.

    Returns (data, declared_rate). declared_rate differs from the phrase's real
    sample rate when the band table could only be expressed by another rate; the
    samples are correct either way, they just need relabelling.
    """
    data = b"".join(amm_frame_to_mp2(rom, off, n, h) for off, n, h in phrase.frames)
    _v, _s, _b, _size, rate = pick_mp2_header(phrase.frames[0][2], phrase.frames[0][1])
    return data, rate
