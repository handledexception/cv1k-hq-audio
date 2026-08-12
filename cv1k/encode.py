"""Encode audio into AMM phrases for the YMZ770C.

AMM is MPEG Layer II with a repacked 32-bit header, so the encoder is ffmpeg's
libtwolame plus a header rewrite. Nothing here re-implements Layer II.

Two constraints the rewrite has to respect:

  * AMM has no CRC field. Layer II with the protection bit clear puts 16 CRC
    bits right where AMM expects band data, so CRC must be off.
  * `param_index` selects the band allocation table and must match what the
    encoder actually used. It is derived from the MP2 bitrate/samplerate via
    the same table the decoder uses (LAYER2_PARAM_INDEX).
"""

import os
import struct
import subprocess
import tempfile

from . import amm

# MPEG-1 / MPEG-2 LSF Layer II both carry 1152 samples per frame.
SAMPLES_PER_FRAME = amm.SAMPLES_PER_FRAME

# Rates reachable through each AMM sync variant.
VARIANT_LSF = 2          # sync 0xFFF4: 22050 / 24000 / 16000
VARIANT_MPEG1 = 6        # sync 0xFFFC: 44100 / 48000 / 32000

RATE_TO_VARIANT_INDEX = {
    22050: (VARIANT_LSF, 0), 24000: (VARIANT_LSF, 1), 16000: (VARIANT_LSF, 2),
    44100: (VARIANT_MPEG1, 0), 48000: (VARIANT_MPEG1, 1), 32000: (VARIANT_MPEG1, 2),
}

STEREO_MODE_STEREO = 0
STEREO_MODE_JOINT = 1
STEREO_MODE_MONO = 3


class EncodeError(Exception):
    pass


# ---------------------------------------------------------------------------
# MP2 parsing
# ---------------------------------------------------------------------------

class Mp2Frame(object):
    __slots__ = ("offset", "size", "variant", "prot", "bitrate_index",
                 "srate_index", "padding", "stereo_mode", "stereo_mode_ext")

    @property
    def is_lsf(self):
        return self.variant == VARIANT_LSF

    @property
    def sample_rate(self):
        return amm.SAMPLE_RATES[self.srate_index + (4 if self.is_lsf else 0)]

    @property
    def channels(self):
        return 1 if self.stereo_mode == STEREO_MODE_MONO else 2

    @property
    def bitrate(self):
        table = amm.BITRATES_LSF if self.is_lsf else amm.BITRATES_MPEG1
        return table[self.bitrate_index]

    @property
    def param_index(self):
        # MPEG-2 LSF has a single allocation table; only MPEG-1 varies it by
        # bitrate. Going through LAYER2_PARAM_INDEX for LSF would both index
        # the wrong row and return the wrong table.
        pi = amm.inferred_param_index(self.variant, self.channels,
                                      self.srate_index, self.bitrate_index)
        if pi < 0:
            raise EncodeError(
                "no Layer II band table for %d kbps / %d Hz / %dch -- pick another bitrate"
                % (self.bitrate, self.sample_rate, self.channels))
        return pi


def parse_mp2(data):
    """Split an MP2 bytestream into frames. Raises on anything unexpected."""
    frames = []
    p = 0
    n = len(data)
    while p + 4 <= n:
        if data[p] != 0xFF or (data[p + 1] & 0xF0) != 0xF0:
            raise EncodeError("lost MP2 sync at offset 0x%x" % p)
        f = Mp2Frame()
        f.offset = p
        f.variant = (data[p + 1] >> 1) & 7
        f.prot = data[p + 1] & 1
        f.bitrate_index = data[p + 2] >> 4
        f.srate_index = (data[p + 2] >> 2) & 3
        f.padding = (data[p + 2] >> 1) & 1
        f.stereo_mode = data[p + 3] >> 6
        f.stereo_mode_ext = (data[p + 3] >> 4) & 3

        if f.variant not in (VARIANT_LSF, VARIANT_MPEG1):
            raise EncodeError("frame at 0x%x is not Layer II (variant %d)" % (p, f.variant))
        if not f.prot:
            raise EncodeError(
                "frame at 0x%x has a CRC; AMM has no CRC field. Encode with CRC off." % p)
        if not f.bitrate or not f.sample_rate:
            raise EncodeError("frame at 0x%x has a free/reserved bitrate or rate" % p)

        f.size = amm.mp2_frame_size(f.bitrate, f.sample_rate) + f.padding
        if p + f.size > n:
            break                       # trailing partial frame from ffmpeg
        frames.append(f)
        p += f.size
    if not frames:
        raise EncodeError("no MP2 frames found")
    return frames


# ---------------------------------------------------------------------------
# MP2 -> AMM
# ---------------------------------------------------------------------------

def amm_header(variant, steps, srate_index, stereo_mode, stereo_mode_ext, param_index):
    """Build the 32-bit AMM header.

    steps is the number of 32-sample groups this frame decodes to, capped at 36;
    it is split into full_packets/last_id as steps = 3*full_packets + last_id.
    """
    if not 0 < steps <= amm.MAX_STEPS:
        raise EncodeError("steps %d out of range 1..%d" % (steps, amm.MAX_STEPS))
    full_packets, last_id = divmod(steps, 3)
    bits = 0
    for value, width in ((0xFFF, 12), (variant, 3), (0, 1),
                         (full_packets, 4), (srate_index, 2), (last_id, 2),
                         (stereo_mode, 2), (stereo_mode_ext, 2),
                         (param_index, 3), (0, 1)):
        bits = (bits << width) | value
    return struct.pack(">I", bits)


def mp2_to_amm(data, total_samples=None):
    """Convert an MP2 bytestream into AMM frames plus the 0xFFF0 terminator.

    If total_samples is given, the last frame declares only as many 32-sample
    steps as are really wanted, so the phrase ends at an exact length. The
    residue is rounded up to a whole step -- 32 samples is the finest the
    format can express.
    """
    frames = parse_mp2(data)
    out = bytearray()

    if total_samples is None:
        last_steps = amm.MAX_STEPS
    else:
        capacity = len(frames) * SAMPLES_PER_FRAME
        if total_samples > capacity:
            raise EncodeError("asked for %d samples but only %d encoded"
                              % (total_samples, capacity))
        residue = total_samples - (len(frames) - 1) * SAMPLES_PER_FRAME
        if residue <= 0:
            raise EncodeError("total_samples %d leaves no data in the last frame"
                              % total_samples)
        last_steps = min(amm.MAX_STEPS, -(-residue // amm.SAMPLES_PER_STEP))

    for i, f in enumerate(frames):
        steps = last_steps if i == len(frames) - 1 else amm.MAX_STEPS
        frame = (amm_header(f.variant, steps, f.srate_index, f.stereo_mode,
                            f.stereo_mode_ext, f.param_index)
                 + data[f.offset + 4:f.offset + f.size])
        # twolame emits constant-bitrate frames, so the bit allocation usually
        # leaves a byte or two spare. Cave's data is byte-tight, and the chip
        # finds the next frame by consuming bits, so trim to what is really used.
        out += frame[:amm.frame_length(frame, 0)]

    out += b"\xFF\xF0"
    return bytes(out)


def encoded_samples(amm_data):
    """Sample count a phrase built by mp2_to_amm will actually play."""
    p = amm.walk_phrase(amm_data, 0)
    return p.samples


# ---------------------------------------------------------------------------
# ffmpeg front end
# ---------------------------------------------------------------------------

def encode_file(path, sample_rate, bitrate, channels=1, joint_stereo=True,
                start=None, duration=None, ffmpeg="ffmpeg", gain_db=None):
    """Encode any ffmpeg-readable file to MP2 bytes at the given settings."""
    if sample_rate not in RATE_TO_VARIANT_INDEX:
        raise EncodeError("sample rate %d is not reachable in AMM; pick one of %s"
                          % (sample_rate, sorted(RATE_TO_VARIANT_INDEX)))

    fd, tmp = tempfile.mkstemp(suffix=".mp2")
    os.close(fd)
    try:
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        if start is not None:
            cmd += ["-ss", "%.6f" % start]
        cmd += ["-i", path]
        if duration is not None:
            cmd += ["-t", "%.6f" % duration]
        if gain_db:
            cmd += ["-af", "volume=%.2fdB" % gain_db]
        cmd += ["-c:a", "libtwolame",
                "-ar", str(sample_rate),
                "-ac", str(channels),
                "-b:a", "%dk" % bitrate,
                # AMM has nowhere to put a CRC.
                "-psymodel", "4",
                "-f", "mp2", tmp]
        if channels == 2:
            cmd[-3:-3] = ["-mode", "joint_stereo" if joint_stereo else "stereo"]

        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            raise EncodeError("ffmpeg failed: %s"
                              % proc.stderr.decode("utf-8", "replace").strip())
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def encode_phrase(path, sample_rate, bitrate, channels=1, total_samples=None,
                  **kwargs):
    """Encode a file straight to an AMM phrase blob."""
    mp2 = encode_file(path, sample_rate, bitrate, channels=channels, **kwargs)
    return mp2_to_amm(mp2, total_samples=total_samples)
