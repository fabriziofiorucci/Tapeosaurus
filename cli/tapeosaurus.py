#!/usr/bin/env python3

"""
Tapeosaurus — C16/Plus4, C64 and ZX Spectrum TAP/TZX capture + extraction

Usage:
  python3 tapeosaurus.py -p /dev/ttyUSB0 out.tap
  python3 tapeosaurus.py -p /dev/ttyUSB0 -model c64 out.tap
  python3 tapeosaurus.py -p /dev/ttyUSB0 --prg out.tap
  python3 tapeosaurus.py -p /dev/ttyUSB0 -model c16 --novaload out.tap   (C16 only)
  python3 tapeosaurus.py -p /dev/ttyUSB0 -model spectrum out.tzx
  python3 tapeosaurus.py -p /dev/ttyUSB0 -model spectrum --tap out.tap

Edge mode is negotiated automatically:
  --novaload → sends HOST_CMD_CHANGE (CHANGE interrupt, both edges) — C16 only
  (default)  → sends HOST_CMD_FALLING (FALLING interrupt, standard tape)

The device ACKs with CMD_EDGE_ACK before capture begins.

Model selection:
  -model c16       (default) C16 / Plus4 standard or Novaload
  -model c64                 C64 standard KERNAL tape (non-turbo)
  -model spectrum            ZX Spectrum standard ROM tape

ZX Spectrum notes:
  The Spectrum uses CHANGE (both edges) for its ROM loader — the edge mode
  command is sent automatically; no extra flags are needed.

  Output is a .tzx file by default (TZX v1.20, block 0x11 — Speed Loading /
  Direct Recording, compatible with Fuse, SpecEmu, ZXSpin, RealSpectrum, and
  any other TZX-capable emulator or hardware player such as TZXDuino).

  Pass --tap to produce a raw .tap file instead (compatible with Fuse --tape,
  ZX-Uno, etc.).  Both formats use the standard Spectrum ROM timing constants
  (3.5 MHz clock, CHANGE-edge sampling).

  Block type 0x13 (header) and 0x16 (data) are decoded if --prg is requested,
  saving each file as <name>.bin (load address embedded in the header).

  The Spectrum ROM loader uses CHANGE (both edges) — the CLI sends
  HOST_CMD_CHANGE automatically for -model spectrum.
"""

import argparse
import struct
import sys
import time
import re

try:
    import serial
except ImportError:
    print("ERROR: pyserial is required. Install with: pip install pyserial")
    sys.exit(1)

FIRMWARE_HZ = 2_000_000
BAUD        = 250_000

# ---------------------------------------------------------------------------
# Machine clock frequencies
# ---------------------------------------------------------------------------
C16_PAL_HZ  = 886_724
C16_NTSC_HZ = 894_886

C64_PAL_HZ  = 985_248
C64_NTSC_HZ = 1_022_727

# ZX Spectrum clock — always 3.5 MHz regardless of PAL/NTSC video
SPECTRUM_HZ = 3_500_000

# ---------------------------------------------------------------------------
# TAP magic bytes
# ---------------------------------------------------------------------------
TAP_MAGIC_C16 = b"C16-TAPE-RAW"
TAP_MAGIC_C64 = b"C64-TAPE-RAW"

TAP_VERSION  = 2    # C16 Novaload / half-wave (24-byte header)
TAP_RESERVED = 0

# ---------------------------------------------------------------------------
# Device → Host control commands
# ---------------------------------------------------------------------------
CMD_PLAY_PRESSED  = 0x01
CMD_PLAY_RELEASED = 0x02
CMD_OVERFLOW      = 0x03
CMD_EDGE_ACK      = 0x04   # Device confirms edge-mode change

# ---------------------------------------------------------------------------
# Host → Device command frame: 0xFF 0xFF 0xFF <cmd>
# ---------------------------------------------------------------------------
HOST_CMD_FALLING = 0x10   # Standard tape (FALLING edge)
HOST_CMD_CHANGE  = 0x11   # Novaload turbo / Spectrum (CHANGE — both edges)

MAX_RECORDING_TIMEOUTS = 15
EDGE_ACK_TIMEOUT_S     = 3.0

# ---------------------------------------------------------------------------
# C16 decoder constants
# ---------------------------------------------------------------------------
C16_SYNC1         = 0x16
C16_SYNC2         = 0x16
C16_BIT_THRESHOLD = 8000

# ---------------------------------------------------------------------------
# C64 decoder constants
# ---------------------------------------------------------------------------
# Pulse classification thresholds in C64 machine cycles.
# Short  < C64_SHORT_MAX  (< 512)
# Medium  C64_SHORT_MAX .. C64_MEDIUM_MAX-1 (512..767)
# Long   >= C64_MEDIUM_MAX (>= 768), includes leader pulses
C64_SHORT_MAX  = 512
C64_MEDIUM_MAX = 768

# Standard KERNAL sync: bytes 0x89 down to 0x81 (9 descending bytes)
C64_SYNC_START = 0x89
C64_SYNC_END   = 0x81

# TAP machine field for C64
C64_MACHINE_ID = 0   # 0 = C64, 1 = VIC-20, 2 = C16/Plus4

# ===========================================================================
# ZX Spectrum ROM timing constants (all in 3.5 MHz T-states)
# ---------------------------------------------------------------------------
#  Pilot tone:   2168 T-states per half-pulse
#  Sync pulse 1:  667 T-states
#  Sync pulse 2:  735 T-states
#  Bit 0:         855 T-states per half-pulse  (pair = 1710)
#  Bit 1:        1710 T-states per half-pulse  (pair = 3420)
#  Pilot length: >= 8000 half-pulses for a header block (type 0x00)
#               >= 3220 half-pulses for a data  block (type 0xFF)
#
# Classification thresholds (half-pulse lengths in T-states):
#   Pilot half-pulse  : 1800 ≤ p < 2600  (centred on 2168)
#   Sync-1 half-pulse :  550 ≤ p <  750  (centred on  667)
#   Sync-2 half-pulse :  650 ≤ p <  850  (centred on  735)
#   "0"-bit half-pulse:  700 ≤ p < 1050  (centred on  855)
#   "1"-bit half-pulse: 1450 ≤ p < 1950  (centred on 1710)
#
# In practice we use a single mid-point threshold to separate 0-bits from
# 1-bits after the sync sequence has been detected:
#   half-pulse < SPECTRUM_BIT_THRESHOLD → bit 0
#   half-pulse ≥ SPECTRUM_BIT_THRESHOLD → bit 1
# ===========================================================================

SPECTRUM_PILOT_MIN    = 1800
SPECTRUM_PILOT_MAX    = 2600
SPECTRUM_SYNC1_MIN    =  550
SPECTRUM_SYNC1_MAX    =  750
SPECTRUM_SYNC2_MIN    =  650
SPECTRUM_SYNC2_MAX    =  850
SPECTRUM_BIT_THRESHOLD = 1100  # half-pulses below this → bit 0, above → bit 1
SPECTRUM_PILOT_MIN_COUNT = 100  # minimum pilot half-pulses before we declare a block

# Spectrum block flag byte values
SPECTRUM_FLAG_HEADER = 0x00
SPECTRUM_FLAG_DATA   = 0xFF

# Spectrum header block types (byte after flag)
SPECTRUM_HDR_PROGRAM  = 0x00
SPECTRUM_HDR_NUMARRAY = 0x01
SPECTRUM_HDR_CHARARRAY= 0x02
SPECTRUM_HDR_CODE     = 0x03

# TZX constants
TZX_SIGNATURE = b"ZXTape!"
TZX_EOF_MARKER = 0x1A
TZX_VERSION_MAJOR = 1
TZX_VERSION_MINOR = 20

# TZX block IDs used
TZX_BLOCK_STANDARD    = 0x10   # Standard Speed Data Block
TZX_BLOCK_TURBO       = 0x11   # Turbo Speed Data Block (used for raw direct-recording)
TZX_BLOCK_PURE_TONE   = 0x12   # Pure Tone
TZX_BLOCK_PULSE_SEQ   = 0x13   # Pulse Sequence
TZX_BLOCK_PURE_DATA   = 0x14   # Pure Data Block
TZX_BLOCK_DIRECT      = 0x15   # Direct Recording Block
TZX_BLOCK_TEXT_DESC   = 0x30   # Text Description (used for info comments)
TZX_BLOCK_ARCHIVE     = 0x32   # Archive Info


# ===========================================================================
# TAP header builders
# ===========================================================================

def tap_header_c16(data_len, ntsc=False, version=None):
    """
    Build a C16-TAPE-RAW TAP header.
    version=1  Full-wave (FALLING-edge, standard KERNAL). 20-byte header.
    version=2  Half-wave (CHANGE/both-edges, Novaload). 24-byte header + sample_rate.
    """
    if version is None:
        version = TAP_VERSION

    system_id   = 0   # Plus/4 (0 for bare C16/C116)
    video_id    = 1 if ntsc else 0
    sample_rate = C16_NTSC_HZ if ntsc else C16_PAL_HZ

    header = struct.pack(
        "<12sBBBBI",
        TAP_MAGIC_C16,
        version,
        system_id,
        video_id,
        TAP_RESERVED,
        data_len,
    )
    if version >= 2:
        header += struct.pack("<I", int(sample_rate))
    return header


def tap_header_c64(data_len, ntsc=False):
    """
    Build a C64-TAPE-RAW TAP header (version 1, 20 bytes).
    Version 1 supports 3-byte overflow pulses.
    Machine byte 0 = C64.  Video byte 0 = PAL, 1 = NTSC.
    """
    video_id = 1 if ntsc else 0
    return struct.pack(
        "<12sBBBBI",
        TAP_MAGIC_C64,
        1,             # TAP version 1 (overflow support)
        C64_MACHINE_ID,
        video_id,
        TAP_RESERVED,
        data_len,
    )

# ===========================================================================
# Common pulse encoder (used by C16 and C64 models)
# ===========================================================================

def encode_pulse(firmware_ticks, scale):
    machine_ticks = round(firmware_ticks * scale)
    if machine_ticks < 1:
        machine_ticks = 1
    short = machine_ticks // 8
    if 1 <= short <= 0xFE:
        return bytes([short])
    machine_ticks = min(machine_ticks, 0xFFFFFF)
    return bytes([
        0x00,
        machine_ticks & 0xFF,
        (machine_ticks >> 8) & 0xFF,
        (machine_ticks >> 16) & 0xFF,
    ])

# ===========================================================================
# C16 KERNAL block decoder  — NOT MODIFIED from original
# ===========================================================================

def pulses_to_bits(pulses, threshold):
    return [1 if p > threshold else 0 for p in pulses]


def bits_to_bytes(bits):
    result = []
    for i in range(0, len(bits) - 7, 8):
        byte = sum((bits[i + j] << (7 - j)) for j in range(8))
        result.append(byte)
    return result


def extract_c16_blocks(pulses, scale):
    machine_pulses = [round(t * scale) for t in pulses]

    pilot_end = 0
    for i, p in enumerate(machine_pulses):
        if p > 10_000:
            pilot_end = i
            break

    data_pulses = machine_pulses[pilot_end:]
    bits        = pulses_to_bits(data_pulses, C16_BIT_THRESHOLD)
    raw_bytes   = bits_to_bytes(bits)

    blocks = []
    i = 0
    while i < len(raw_bytes) - 22:
        if raw_bytes[i] == C16_SYNC1 and raw_bytes[i + 1] == C16_SYNC2:
            name_bytes = bytes(raw_bytes[i + 2 : i + 18])
            name       = name_bytes.rstrip(b"\x00").decode("ascii", errors="ignore")
            load_addr  = raw_bytes[i + 18] | (raw_bytes[i + 19] << 8)
            end_addr   = raw_bytes[i + 20] | (raw_bytes[i + 21] << 8)
            prg_len    = end_addr - load_addr + 1
            data_start = i + 22
            prg_data   = bytes(raw_bytes[data_start : data_start + prg_len])
            blocks.append((name, load_addr, prg_data))
            print(f"  Found: \"{name}\" (${load_addr:04X}–${end_addr:04X}, {prg_len} B)")
            i += 22 + prg_len
        else:
            i += 1

    return blocks, raw_bytes

# ===========================================================================
# C64 KERNAL block decoder
# ===========================================================================
#
# Standard C64 KERNAL tape format (non-turbo):
#   • Pulses classified as Short / Medium / Long by machine-cycle length.
#   • Each bit encoded as a pair of pulses:
#       Bit 0 → Short + Medium  (S M)
#       Bit 1 → Medium + Short  (M S)
#   • Each byte framed as:
#       [M+S start marker]  [8 bit-pairs, LSB first]  [parity pair]  [S+S end marker]
#   • Sync sequence: bytes 0x89 0x88 0x87 … 0x81 (9 descending bytes).
#   • Block type byte follows sync:
#       0x01 = relocatable program header
#       0x03 = non-relocatable (machine code) header
#       0x02 = data block
#       0x04 = end-of-tape / final data block
#   • Header layout (type byte already consumed):
#       load_lo, load_hi, end_lo, end_hi, filename[16] (0x20-padded)
# ===========================================================================

def _c64_classify(machine_ticks):
    """Classify a C64 pulse by duration.  Returns 'S', 'M', or 'L'."""
    if machine_ticks < C64_SHORT_MAX:
        return 'S'
    elif machine_ticks < C64_MEDIUM_MAX:
        return 'M'
    else:
        return 'L'


def _c64_decode_pair(p1, p2):
    """Decode a C64 biphase bit pair.  S+M → 0,  M+S → 1,  else → None."""
    if p1 == 'S' and p2 == 'M':
        return 0
    if p1 == 'M' and p2 == 'S':
        return 1
    return None


def _c64_read_byte(ptypes, pos):
    """
    Attempt to read one C64-encoded byte starting at ptypes[pos].

    Framing:
      [M+S start marker] [8 bit-pairs LSB-first] [parity pair] [S+S end marker]

    Returns (byte_value, next_pos) on success, or (None, original_pos) on failure.
    """
    n = len(ptypes)

    # Need at least 2 (start) + 16 (8 bits) + 2 (parity) + 2 (end) = 22 pulses
    if pos + 22 > n:
        return None, pos

    # Start marker must be M+S
    if not (ptypes[pos] == 'M' and ptypes[pos + 1] == 'S'):
        return None, pos
    pos += 2

    # 8 data bits, LSB first
    byte_val = 0
    for bit_n in range(8):
        if pos + 1 >= n:
            return None, pos
        b = _c64_decode_pair(ptypes[pos], ptypes[pos + 1])
        if b is None:
            return None, pos
        byte_val |= (b << bit_n)
        pos += 2

    # Parity pair — consume without checking (tape may be worn)
    if pos + 1 < n and _c64_decode_pair(ptypes[pos], ptypes[pos + 1]) is not None:
        pos += 2

    # End marker: S+S
    if pos + 1 < n and ptypes[pos] == 'S' and ptypes[pos + 1] == 'S':
        pos += 2

    return byte_val, pos


def _c64_find_sync(ptypes, start):
    """
    Scan ptypes[start:] for the C64 KERNAL sync sequence
    (bytes 0x89 0x88 … 0x81 in descending order).

    Returns the pulse index immediately after 0x81, or -1 if not found.
    """
    n = len(ptypes)
    i = start
    while i < n - 200:
        val, next_i = _c64_read_byte(ptypes, i)
        if val == C64_SYNC_START:
            pos = next_i
            ok  = True
            for expected in range(C64_SYNC_START - 1, C64_SYNC_END - 1, -1):
                v, pos = _c64_read_byte(ptypes, pos)
                if v != expected:
                    ok = False
                    break
            if ok:
                return pos   # points just past the last sync byte (0x81)
        i += 1
    return -1


def extract_c64_blocks(pulses, scale):
    """
    Decode standard C64 KERNAL tape blocks from the raw firmware pulse stream.

    Returns (blocks, raw_bytes) where:
      blocks    = [(name, load_addr, prg_data), …]
      raw_bytes = flat list of all decoded payload bytes (for heuristics)
    """
    machine_pulses = [round(t * scale) for t in pulses]
    ptypes         = [_c64_classify(p) for p in machine_pulses]
    n              = len(ptypes)

    blocks    = []
    raw_bytes = []
    scan_pos  = 0

    while scan_pos < n:
        after_sync = _c64_find_sync(ptypes, scan_pos)
        if after_sync < 0:
            break

        pos = after_sync

        # Read block type byte
        block_type, pos = _c64_read_byte(ptypes, pos)
        if block_type is None:
            scan_pos = after_sync + 1
            continue

        # ---------------------------------------------------------------
        # Header block (0x01 = relocatable, 0x03 = non-relocatable)
        # Layout after type byte: load_lo, load_hi, end_lo, end_hi, name[16]
        # ---------------------------------------------------------------
        if block_type in (0x01, 0x03):
            lo,   pos = _c64_read_byte(ptypes, pos)
            hi,   pos = _c64_read_byte(ptypes, pos)
            if lo is None or hi is None:
                scan_pos = after_sync + 1
                continue
            load_addr = lo | (hi << 8)

            elo, pos = _c64_read_byte(ptypes, pos)
            ehi, pos = _c64_read_byte(ptypes, pos)
            if elo is None or ehi is None:
                scan_pos = after_sync + 1
                continue
            end_addr = elo | (ehi << 8)

            name_raw = []
            for _ in range(16):
                b, pos = _c64_read_byte(ptypes, pos)
                if b is None:
                    break
                name_raw.append(b)
            name    = bytes(name_raw).rstrip(b"\x20\x00").decode("ascii", errors="ignore")
            prg_len = end_addr - load_addr
            if prg_len <= 0 or prg_len > 0xC000:
                scan_pos = after_sync + 1
                continue

            print(f"  Found header: \"{name}\" (${load_addr:04X}–${end_addr:04X}, {prg_len} B)")

            # -----------------------------------------------------------
            # Look for the matching data block (type 0x02 or 0x04) after
            # the next sync sequence.  C64 tapes always store each block
            # twice; we just take the first good copy.
            # -----------------------------------------------------------
            data_scan = pos
            prg_data  = bytearray()

            while len(prg_data) < prg_len and data_scan < n:
                ds = _c64_find_sync(ptypes, data_scan)
                if ds < 0:
                    break
                dtype, ds = _c64_read_byte(ptypes, ds)
                if dtype in (0x02, 0x04):
                    while len(prg_data) < prg_len:
                        b, ds = _c64_read_byte(ptypes, ds)
                        if b is None:
                            break
                        prg_data.append(b)
                        raw_bytes.append(b)
                    data_scan = ds
                    break   # got the data block; move on
                elif dtype in (0x01, 0x03):
                    # Hit another header — data block must have been lost
                    break
                else:
                    data_scan = ds + 1

            blocks.append((name, load_addr, bytes(prg_data)))
            scan_pos = data_scan if data_scan > pos else pos

        else:
            scan_pos = after_sync + 1

    return blocks, raw_bytes

# ===========================================================================
# TAP index listing
# ===========================================================================

def print_tap_index(blocks, model='c16'):
    tape_label = (TAP_MAGIC_C64 if model == 'c64' else TAP_MAGIC_C16).decode("ascii", errors="replace")
    print()
    print(f' 0 "{tape_label}"')
    counter = 2
    for name, load_addr, prg_data in blocks:
        padded = f"{name:<10}"
        print(f' {counter} "{padded}" PRG')
        counter += 2
    print()

# ===========================================================================
# Serial frame reader
# ===========================================================================

class FrameReader:
    def __init__(self, port):
        self.port = port

    def read_exact(self, n):
        data = b""
        while len(data) < n:
            chunk = self.port.read(n - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def read_frame(self):
        raw = self.read_exact(4)
        if raw is None:
            return ("timeout", None)
        b0, b1, b2, ck = raw

        # Control / ACK frame from device (0x00 0x00 0x00 <cmd>)
        if b0 == 0 and b1 == 0 and b2 == 0:
            if ck in (CMD_PLAY_PRESSED, CMD_PLAY_RELEASED,
                      CMD_OVERFLOW, CMD_EDGE_ACK):
                return ("control", ck)
            return ("data", 0)

        # Data frame — verify checksum
        if (b0 ^ b1 ^ b2) != ck:
            return ("error", "checksum")
        return ("data", b0 | (b1 << 8) | (b2 << 16))

# ===========================================================================
# Edge-mode negotiation
# ===========================================================================

def send_edge_command(ser, use_change: bool) -> bool:
    """
    Send an edge-mode command and wait for CMD_EDGE_ACK.
    Returns True if the device acknowledged, False on timeout.
    """
    cmd       = HOST_CMD_CHANGE if use_change else HOST_CMD_FALLING
    mode_name = "CHANGE (Novaload/turbo)" if use_change else "FALLING (standard)"
    print(f"→  Setting edge mode: {mode_name}")
    ser.write(bytes([0xFF, 0xFF, 0xFF, cmd]))
    ser.flush()

    deadline = time.monotonic() + EDGE_ACK_TIMEOUT_S
    buf = b""
    while time.monotonic() < deadline:
        chunk = ser.read(ser.in_waiting or 1)
        if chunk:
            buf += chunk
            while len(buf) >= 4:
                if buf[0] == 0 and buf[1] == 0 and buf[2] == 0 and buf[3] == CMD_EDGE_ACK:
                    print(f"✔  Edge mode confirmed by device: {mode_name}")
                    return True
                buf = buf[1:]

    print(f"⚠  No ACK received for edge-mode command (timeout {EDGE_ACK_TIMEOUT_S}s) — continuing")
    return False

# ===========================================================================
# Heuristic filename extraction (relaxed)
# ===========================================================================

PRINTABLE_RE = re.compile(rb"[A-Z0-9 _\-]{2,10}")


def heuristic_name_from_raw(raw_bytes):
    sample  = bytes(raw_bytes[:512])
    matches = PRINTABLE_RE.findall(sample)
    if not matches:
        return None
    for m in matches:
        s = m.strip().decode("ascii", errors="ignore")
        if 1 <= len(s) <= 8:
            return s
    return matches[0].strip().decode("ascii", errors="ignore")

# ===========================================================================
# ZX Spectrum — pulse-level decoder
# ===========================================================================
#
# The Spectrum ROM loader samples both edges (CHANGE mode).  Each half-pulse
# is one firmware tick interval from the ISR.  We convert firmware ticks to
# Spectrum T-states using:
#
#   t_states = firmware_ticks * (SPECTRUM_HZ / FIRMWARE_HZ)
#            = firmware_ticks * (3_500_000 / 2_000_000)
#            = firmware_ticks * 1.75
#
# State machine:
#   PILOT  — consume half-pulses in the pilot-tone range until count drops out
#   SYNC1  — expect a short sync-1 half-pulse (~667 T)
#   SYNC2  — expect a slightly longer sync-2 half-pulse (~735 T)
#   DATA   — pairs of half-pulses per bit (both half-pulses same width):
#               width < SPECTRUM_BIT_THRESHOLD → bit 0
#               width ≥ SPECTRUM_BIT_THRESHOLD → bit 1
#             Each byte is 8 bits MSB-first, followed by a parity bit.
#             The block ends when we have read the expected number of bytes
#             (taken from the header if available, otherwise we stop when
#             the pilot tone of the next block begins).
#
# A complete standard ROM block is:
#   flag byte (0x00 = header, 0xFF = data)
#   payload bytes
#   checksum byte  (XOR of flag + all payload bytes)
#
# Standard header payload (17 bytes):
#   type       [0]      0x00=Program 0x01=NumArray 0x02=CharArray 0x03=Code
#   filename   [1..10]  10 ASCII chars, space-padded
#   length     [11..12] little-endian word
#   param1     [13..14] for Program: LINE autostart (or 0x8000 if none)
#                       for Code: load address
#   param2     [15..16] for Program: length of BASIC area
#                       for Code: 0x8000
# ===========================================================================

def _spectrum_ticks_to_tstates(firmware_ticks):
    """Convert firmware 2 MHz tick count to Spectrum 3.5 MHz T-states."""
    return round(firmware_ticks * SPECTRUM_HZ / FIRMWARE_HZ)


def _spectrum_classify_half_pulse(tstates):
    """Return a string label for a Spectrum half-pulse by T-state length."""
    if SPECTRUM_PILOT_MIN <= tstates <= SPECTRUM_PILOT_MAX:
        return 'P'   # pilot
    elif SPECTRUM_SYNC1_MIN <= tstates <= SPECTRUM_SYNC1_MAX:
        return 'S1'  # sync-1
    elif SPECTRUM_SYNC2_MIN <= tstates <= SPECTRUM_SYNC2_MAX:
        return 'S2'  # sync-2
    elif tstates < SPECTRUM_BIT_THRESHOLD:
        return 'B0'  # data bit 0 half-pulse
    elif tstates < SPECTRUM_PILOT_MIN:
        return 'B1'  # data bit 1 half-pulse
    else:
        return 'X'   # unknown / gap


def _spectrum_read_block(half_pulses, start_idx):
    """
    Attempt to read one complete Spectrum ROM block starting at start_idx.
    The pilot and both sync pulses must already be consumed before start_idx.

    Returns (flag, payload_bytes, checksum_ok, next_idx) or None on failure.

    Bit encoding: each bit is encoded as two consecutive half-pulses of the
    same width.  Width < SPECTRUM_BIT_THRESHOLD → 0, else → 1.  MSB first.
    """
    n   = len(half_pulses)
    idx = start_idx
    raw = []

    while idx + 1 < n:
        hp1 = half_pulses[idx]
        hp2 = half_pulses[idx + 1]

        # Both half-pulses of a bit should be similar width.
        # Tolerate up to 30% difference to handle worn tapes.
        if hp1 == 0 or hp2 == 0:
            break
        ratio = max(hp1, hp2) / min(hp1, hp2)
        if ratio > 1.6:
            # Significant mismatch — we've drifted out of data territory
            break

        bit = 0 if (hp1 + hp2) / 2 < SPECTRUM_BIT_THRESHOLD * 2 else 1
        raw.append(bit)
        idx += 2

        if len(raw) % 8 == 0:
            byte_val = 0
            for b in raw[-8:]:
                byte_val = (byte_val << 1) | b
            # Once we've accumulated enough bytes (flag + at least 1 payload),
            # stop collecting when the next pulse indicates a new pilot/gap.
            # Practical termination: we stop when next pulses look like pilot.
            if idx < n and half_pulses[idx] >= SPECTRUM_PILOT_MIN:
                break

    if len(raw) < 16:   # too short to be a valid block
        return None

    # Pack bits into bytes (MSB first)
    nbytes = len(raw) // 8
    decoded = []
    for i in range(nbytes):
        byte_val = 0
        for b in raw[i * 8:(i + 1) * 8]:
            byte_val = (byte_val << 1) | b
        decoded.append(byte_val)

    if len(decoded) < 2:
        return None

    flag     = decoded[0]
    payload  = decoded[1:-1]
    checksum = decoded[-1]

    # Verify checksum: XOR of flag + all payload bytes should equal checksum
    xor = flag
    for b in payload:
        xor ^= b
    checksum_ok = (xor == checksum)

    return flag, bytes(payload), checksum_ok, idx


def _spectrum_find_block(half_pulses, scan_from):
    """
    Scan half_pulses[scan_from:] for a pilot tone followed by sync pulses.

    Returns (block_start_idx_after_sync, pilot_count) or (-1, 0) if not found.
    """
    n = len(half_pulses)
    i = scan_from

    while i < n:
        # Look for the start of a pilot tone
        if not (SPECTRUM_PILOT_MIN <= half_pulses[i] <= SPECTRUM_PILOT_MAX):
            i += 1
            continue

        # Count pilot half-pulses
        pilot_count = 0
        j = i
        while j < n and SPECTRUM_PILOT_MIN <= half_pulses[j] <= SPECTRUM_PILOT_MAX:
            pilot_count += 1
            j += 1

        if pilot_count < SPECTRUM_PILOT_MIN_COUNT:
            i = j + 1
            continue

        # Expect sync-1 half-pulse
        if j >= n or not (SPECTRUM_SYNC1_MIN <= half_pulses[j] <= SPECTRUM_SYNC1_MAX):
            i = j + 1
            continue
        j += 1

        # Expect sync-2 half-pulse
        if j >= n or not (SPECTRUM_SYNC2_MIN <= half_pulses[j] <= SPECTRUM_SYNC2_MAX):
            i = j + 1
            continue
        j += 1

        return j, pilot_count  # j now points to the first data half-pulse

    return -1, 0


def _spectrum_parse_header(payload):
    """
    Parse a standard Spectrum ROM header payload (17 bytes).
    Returns a dict with decoded fields or None if payload is too short.
    """
    if len(payload) < 17:
        return None

    block_type = payload[0]
    filename   = payload[1:11].decode("ascii", errors="replace").rstrip()
    length     = struct.unpack_from("<H", payload, 11)[0]
    param1     = struct.unpack_from("<H", payload, 13)[0]
    param2     = struct.unpack_from("<H", payload, 15)[0]

    type_names = {
        SPECTRUM_HDR_PROGRAM:   "Program",
        SPECTRUM_HDR_NUMARRAY:  "Number array",
        SPECTRUM_HDR_CHARARRAY: "Character array",
        SPECTRUM_HDR_CODE:      "Bytes",
    }
    type_str = type_names.get(block_type, f"Unknown({block_type:#04x})")

    load_addr = None
    if block_type == SPECTRUM_HDR_CODE:
        load_addr = param1
    elif block_type == SPECTRUM_HDR_PROGRAM:
        load_addr = 0x5CCB   # conventional BASIC area start

    return {
        "block_type": block_type,
        "type_str":   type_str,
        "filename":   filename,
        "length":     length,
        "param1":     param1,
        "param2":     param2,
        "load_addr":  load_addr,
    }


def extract_spectrum_blocks(pulses):
    """
    Decode standard Spectrum ROM tape blocks from the raw firmware pulse stream.
    Pulses are in firmware 2 MHz ticks (CHANGE mode — both edges).

    Returns a list of dicts:
        {
            "flag":         int,       0x00=header, 0xFF=data
            "payload":      bytes,     raw payload (excluding flag and checksum)
            "checksum_ok":  bool,
            "header_info":  dict|None, parsed header fields (flag==0x00 only)
            "pilot_count":  int,       number of pilot half-pulses seen
        }
    """
    # Convert to T-states
    half_pulses = [_spectrum_ticks_to_tstates(t) for t in pulses]

    blocks   = []
    scan_pos = 0

    while scan_pos < len(half_pulses):
        data_start, pilot_count = _spectrum_find_block(half_pulses, scan_pos)
        if data_start < 0:
            break

        result = _spectrum_read_block(half_pulses, data_start)
        if result is None:
            scan_pos = data_start + 1
            continue

        flag, payload, checksum_ok, next_idx = result

        header_info = None
        if flag == SPECTRUM_FLAG_HEADER:
            header_info = _spectrum_parse_header(payload)

        blocks.append({
            "flag":        flag,
            "payload":     payload,
            "checksum_ok": checksum_ok,
            "header_info": header_info,
            "pilot_count": pilot_count,
        })

        scan_pos = next_idx

    return blocks


def print_spectrum_blocks(blocks):
    """Pretty-print decoded Spectrum blocks."""
    print()
    for i, blk in enumerate(blocks):
        flag = blk["flag"]
        ok   = "✔" if blk["checksum_ok"] else "✘ bad checksum"
        plen = len(blk["payload"])

        if flag == SPECTRUM_FLAG_HEADER and blk["header_info"]:
            h = blk["header_info"]
            la_str = f"${h['load_addr']:04X}" if h["load_addr"] is not None else "N/A"
            print(f"  Block {i}: Header  {ok}")
            print(f"    Type    : {h['type_str']}")
            print(f"    Name    : \"{h['filename']}\"")
            print(f"    Length  : {h['length']} bytes")
            print(f"    Load    : {la_str}")
        elif flag == SPECTRUM_FLAG_DATA:
            print(f"  Block {i}: Data    {ok}  ({plen} bytes payload)")
        else:
            print(f"  Block {i}: Flag={flag:#04x}  {ok}  ({plen} bytes payload)")
    print()


# ===========================================================================
# ZX Spectrum — TZX file builder
# ===========================================================================
#
# We write TZX Standard Speed Data blocks (0x10) for each decoded ROM block.
# If the decoded block list is empty (turbo / custom loader), we fall back to
# a TZX Direct Recording block (0x15) containing the raw half-pulse widths
# sampled at the firmware resolution (500 ns per sample, 2 MHz).
#
# TZX Standard Speed Data block (0x10) layout:
#   1 byte  block ID = 0x10
#   2 bytes pause after block in ms (little-endian)  — 1000 ms default
#   2 bytes data length in bytes (little-endian)
#   N bytes data  (flag byte + payload + checksum)
#
# TZX Direct Recording block (0x15) layout:
#   1 byte  block ID = 0x15
#   2 bytes T-states per sample (little-endian)  — we use 500 (= 2 MHz @ 3.5 MHz base)
#   2 bytes pause after block in ms
#   1 byte  used bits in last byte (1-8)
#   3 bytes data length in bytes (little-endian, 24-bit)
#   N bytes packed pulse data (1 bit per sample, MSB first)
# ===========================================================================

def _tzx_header():
    """Return the 10-byte TZX file header."""
    return (
        TZX_SIGNATURE
        + bytes([TZX_EOF_MARKER, TZX_VERSION_MAJOR, TZX_VERSION_MINOR])
    )


def _tzx_standard_block(flag, payload, checksum, pause_ms=1000):
    """
    Build a TZX Standard Speed Data block (0x10).
    The block data is: flag byte + payload bytes + checksum byte.
    """
    block_data = bytes([flag]) + payload + bytes([checksum])
    return (
        bytes([TZX_BLOCK_STANDARD])
        + struct.pack("<H", pause_ms)
        + struct.pack("<H", len(block_data))
        + block_data
    )


def _tzx_text_description(text):
    """Build a TZX Text Description block (0x30)."""
    encoded = text.encode("ascii", errors="replace")
    return bytes([TZX_BLOCK_TEXT_DESC, len(encoded)]) + encoded


def _tzx_archive_info(title=None, author=None, comment=None):
    """Build a TZX Archive Info block (0x32)."""
    strings = []
    if title:
        strings.append((0x00, title))
    if author:
        strings.append((0x01, author))
    if comment:
        strings.append((0xFF, comment))
    if not strings:
        return b""

    body = bytes([len(strings)])
    for code, text in strings:
        enc = text.encode("ascii", errors="replace")
        body += bytes([code, len(enc)]) + enc

    return bytes([TZX_BLOCK_ARCHIVE]) + struct.pack("<H", len(body)) + body


def _tzx_direct_recording_block(pulses, pause_ms=1000):
    """
    Build a TZX Direct Recording block (0x15) from raw firmware tick pulses.

    Each firmware tick is 500 ns (2 MHz).  TZX direct recording uses a fixed
    sample rate expressed as T-states per sample; at 3.5 MHz base clock:
        T-states per sample = 3_500_000 / 2_000_000 = 1.75 ≈ 2 (rounded up)

    We store one bit per half-pulse: the bit alternates 0/1 for each pulse
    boundary, packed MSB-first.  This is equivalent to a 2 MHz 1-bit stream.

    Actually, TZX direct recording stores the actual signal level as a
    sequence of bits at the given sample rate.  We reconstruct the waveform
    from the half-pulse durations: each half-pulse contributes (duration_in_ticks)
    samples of the same level.
    """
    TSTATES_PER_SAMPLE = 2   # closest integer to 1.75 for 2 MHz firmware

    # Reconstruct binary waveform from half-pulse lengths
    # Level alternates starting at HIGH (1)
    bits = []
    level = 1
    for fw_ticks in pulses:
        sample_count = max(1, round(fw_ticks * TSTATES_PER_SAMPLE / TSTATES_PER_SAMPLE))
        # Each firmware tick = one sample at the firmware rate
        bits.extend([level] * fw_ticks)
        level ^= 1

    # Pack bits MSB-first into bytes
    used_bits_last = len(bits) % 8 or 8
    while len(bits) % 8 != 0:
        bits.append(0)

    data = bytearray()
    for i in range(0, len(bits), 8):
        byte_val = 0
        for j in range(8):
            byte_val = (byte_val << 1) | bits[i + j]
        data.append(byte_val)

    data_len = len(data)
    return (
        bytes([TZX_BLOCK_DIRECT])
        + struct.pack("<H", TSTATES_PER_SAMPLE)
        + struct.pack("<H", pause_ms)
        + bytes([used_bits_last])
        + struct.pack("<I", data_len)[:3]   # 24-bit little-endian length
        + bytes(data)
    )


def build_tzx(decoded_blocks, pulses=None, source_info=None):
    """
    Build a complete TZX byte string.

    If decoded_blocks is non-empty, write Standard Speed Data blocks (0x10)
    for each decoded ROM block.

    If decoded_blocks is empty and pulses is provided, write a single Direct
    Recording block (0x15) with the raw waveform — useful for turbo loaders.

    source_info: optional string written as a TZX archive info comment.
    """
    tzx = bytearray(_tzx_header())

    if source_info:
        tzx += _tzx_archive_info(
            title="Tapeosaurus capture",
            comment=f"Captured by Tapeosaurus. {source_info}"
        )

    if decoded_blocks:
        for blk in decoded_blocks:
            flag     = blk["flag"]
            payload  = blk["payload"]
            # Recompute checksum (in case of minor bit errors we still store
            # the checksum that was actually on the tape so emulators decide)
            xor = flag
            for b in payload:
                xor ^= b
            tzx += _tzx_standard_block(flag, payload, checksum=xor)
    elif pulses:
        tzx += _tzx_direct_recording_block(pulses)

    return bytes(tzx)


def build_spectrum_tap(decoded_blocks, pulses=None):
    """
    Build a ZX Spectrum .tap byte string.

    Each .tap block is:  length_lo length_hi  flag  payload...  checksum
    (length covers flag + payload + checksum, i.e. payload_len + 2)

    If no decoded blocks are available, we cannot write a valid .tap and
    return an empty bytes object (the caller should warn the user).
    """
    tap = bytearray()
    for blk in decoded_blocks:
        flag    = blk["flag"]
        payload = blk["payload"]
        xor = flag
        for b in payload:
            xor ^= b
        block_data = bytes([flag]) + payload + bytes([xor])
        tap += struct.pack("<H", len(block_data)) + block_data
    return bytes(tap)


# ===========================================================================
# Spectrum post-capture output
# ===========================================================================

def write_spectrum_output(args, decoded_blocks, pulses, output_path):
    """
    Write the Spectrum output file (.tzx or .tap) and optionally extract
    binary files when --prg is requested.
    """
    if args.spectrum_tap:
        # .tap output
        tap_bytes = build_spectrum_tap(decoded_blocks, pulses)
        if tap_bytes:
            with open(output_path, "wb") as f:
                f.write(tap_bytes)
            print(f"✅ TAP written: {output_path} ({len(tap_bytes):,} B)")
        else:
            print("⚠  No standard ROM blocks decoded — .tap output requires decodable blocks.")
            print("   Rerun without --tap to get a Direct Recording TZX instead.")
    else:
        # .tzx output (default)
        source = f"Model: ZX Spectrum. Pulses: {len(pulses):,}."
        tzx_bytes = build_tzx(decoded_blocks, pulses=pulses, source_info=source)
        with open(output_path, "wb") as f:
            f.write(tzx_bytes)
        print(f"✅ TZX written: {output_path} ({len(tzx_bytes):,} B)")

    # --prg: extract binary files
    if args.prg and decoded_blocks:
        # Pair header blocks with the data block that follows them
        i = 0
        while i < len(decoded_blocks):
            blk = decoded_blocks[i]
            if blk["flag"] == SPECTRUM_FLAG_HEADER and blk["header_info"]:
                hdr = blk["header_info"]
                # Look for the next data block
                if i + 1 < len(decoded_blocks) and decoded_blocks[i + 1]["flag"] == SPECTRUM_FLAG_DATA:
                    data_payload = decoded_blocks[i + 1]["payload"]
                    safe_name    = re.sub(r'[^A-Za-z0-9_\-]', '_', hdr["filename"].strip())[:10] or "NONAME"
                    bin_fn       = f"{safe_name}.bin"
                    with open(bin_fn, "wb") as f:
                        f.write(data_payload)
                    la = hdr["load_addr"]
                    la_str = f"${la:04X}" if la is not None else "N/A"
                    print(f"  ✅ BIN: {bin_fn} ({len(data_payload)} B, load {la_str})")
                    i += 2
                    continue
            i += 1


# ===========================================================================
# Main
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Tapeosaurus — C16/Plus4, C64 and ZX Spectrum TAP/TZX capture"
    )
    parser.add_argument("-p", "--port",  required=True, help="Serial port")
    parser.add_argument("output",        nargs="?", default=None,
                        help="Output file (.tap for C16/C64, .tzx or .tap for Spectrum)")
    parser.add_argument("--novaload",    action="store_true",
                        help="Novaload turbo mode (CHANGE edge + skip KERNAL decoder) — C16 only")
    parser.add_argument("--ntsc", "-v",  action="store_true", help="NTSC clock instead of PAL (C16/C64 only)")
    parser.add_argument("--prg",         action="store_true",
                        help="Extract PRG/BIN files from decoded blocks")
    parser.add_argument("-model",        choices=["c16", "c64", "spectrum"], default="c16",
                        dest="model",
                        help="Target computer: c16 (default), c64, or spectrum")
    parser.add_argument("--tap",         action="store_true", dest="spectrum_tap",
                        help="Write .tap instead of .tzx (Spectrum model only)")
    args = parser.parse_args()

    model = args.model

    # --novaload is meaningless for C64 standard tapes
    if args.novaload and model == "c64":
        print("⚠  --novaload is not supported for -model c64; ignoring")
        args.novaload = False

    # --novaload is meaningless for Spectrum
    if args.novaload and model == "spectrum":
        print("⚠  --novaload is not supported for -model spectrum; ignoring")
        args.novaload = False

    # --ntsc is meaningless for Spectrum (fixed 3.5 MHz clock)
    if args.ntsc and model == "spectrum":
        print("⚠  --ntsc has no effect for -model spectrum (Spectrum clock is always 3.5 MHz)")

    # --spectrum-tap outside spectrum model
    if args.spectrum_tap and model != "spectrum":
        print("⚠  --tap is only meaningful for -model spectrum; ignoring")
        args.spectrum_tap = False

    # Default output filename
    if args.output is None:
        if model == "spectrum" and not args.spectrum_tap:
            args.output = "out.tzx"
        else:
            args.output = "out.tap"

    # Select machine clock and scale
    if model == "spectrum":
        machine_hz = SPECTRUM_HZ
        scale      = machine_hz / FIRMWARE_HZ   # for TAP encoding only
        video_str  = ""
    elif model == "c64":
        machine_hz = C64_NTSC_HZ if args.ntsc else C64_PAL_HZ
        scale      = machine_hz / FIRMWARE_HZ
        video_str  = "NTSC" if args.ntsc else "PAL"
    else:
        machine_hz = C16_NTSC_HZ if args.ntsc else C16_PAL_HZ
        scale      = machine_hz / FIRMWARE_HZ
        video_str  = "NTSC" if args.ntsc else "PAL"

    mode_label = {
        "c64":      "C64",
        "c16":      "C16/Plus4" + (" Novaload" if args.novaload else ""),
        "spectrum": "ZX Spectrum",
    }[model]

    if model == "spectrum":
        out_fmt = ".tap" if args.spectrum_tap else ".tzx"
        print(f"{mode_label} — {machine_hz / 1e6:.1f} MHz — output: {out_fmt}")
    else:
        print(f"{mode_label} {video_str} — scale: {scale:.6f}")
    print(f"Capturing → {args.output}")

    try:
        ser = serial.Serial(args.port, BAUD, timeout=2)
    except serial.SerialException as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    # Allow the ESP8266 to boot/reset, then flush garbage from reset sequence
    time.sleep(2)
    ser.reset_input_buffer()

    # Edge mode:
    #   C16 standard → FALLING
    #   C16 Novaload → CHANGE
    #   C64          → FALLING
    #   Spectrum     → CHANGE (ROM loader samples both edges)
    use_change = (model == "c16" and args.novaload) or (model == "spectrum")
    send_edge_command(ser, use_change=use_change)

    reader   = FrameReader(ser)
    tap_data = bytearray()   # TAP-encoded pulse stream (C16/C64)
    pulses   = []            # raw firmware ticks (all models)
    overflow = False

    print("Waiting for PLAY (LED lights up)...")
    try:
        while True:
            ftype, val = reader.read_frame()
            if ftype == "control" and val == CMD_PLAY_PRESSED:
                print("▶  RECORDING...")
                break
            if ftype == "control" and val == CMD_OVERFLOW:
                print("⚠  Overflow before start")
                overflow = True
            if ftype == "control" and val == CMD_EDGE_ACK:
                pass  # Late ACK — device already applied the mode

        pulse_count    = 0
        timeout_streak = 0

        while True:
            ftype, val = reader.read_frame()
            if ftype == "data":
                timeout_streak = 0
                if val > 0:
                    pulses.append(val)
                    if model != "spectrum":
                        tap_data += encode_pulse(val, scale)
                    pulse_count += 1
                    if pulse_count % 5000 == 0:
                        print(f"\r … {pulse_count:,} pulses", end="", flush=True)
            elif ftype == "control":
                timeout_streak = 0
                if val == CMD_PLAY_RELEASED:
                    print(f"\n✅ STOPPED — {pulse_count:,} pulses")
                    break
                if val == CMD_OVERFLOW:
                    overflow = True
                    print("\n⚠  Buffer overflow")
                # CMD_EDGE_ACK during recording is ignored
            elif ftype == "timeout":
                timeout_streak += 1
                print(f"\r … silence ({timeout_streak * 2}s)", end="", flush=True)
                if timeout_streak >= MAX_RECORDING_TIMEOUTS:
                    print(f"\n⏹  Stopped after {timeout_streak * 2}s silence")
                    break
            elif ftype == "error":
                print("\n⚠  Frame error — continuing")

    except KeyboardInterrupt:
        print("\nAborted by user.")
    finally:
        ser.close()

    if not pulses:
        print("❌ No data captured")
        sys.exit(1)

    # -----------------------------------------------------------------------
    # Model-specific output
    # -----------------------------------------------------------------------

    if model == "spectrum":
        # -------------------------------------------------------------------
        # ZX Spectrum path
        # -------------------------------------------------------------------
        print("🔍 Decoding ZX Spectrum ROM blocks...")
        decoded_blocks = extract_spectrum_blocks(pulses)

        if decoded_blocks:
            print_spectrum_blocks(decoded_blocks)
        else:
            print("  ⚠  No standard ROM blocks decoded.")
            if not args.spectrum_tap:
                print("  Falling back to Direct Recording TZX (raw waveform).")
            else:
                print("  Cannot produce .tap without decoded ROM blocks.")
                print("  Rerun without --tap for a Direct Recording TZX.")

        write_spectrum_output(args, decoded_blocks, pulses, args.output)

        if overflow:
            print("⚠  Buffer overflow — data may be incomplete")

        print(f"💡 Spectrum TZX compatible with: Fuse, SpecEmu, ZXSpin, TZXDuino")
        print(f"💡 Full decode: tzxtools --info {args.output}")

    elif model == "c64":
        # -------------------------------------------------------------------
        # C64 standard KERNAL path — NOT MODIFIED from original
        # -------------------------------------------------------------------
        if not tap_data:
            print("❌ No data captured")
            sys.exit(1)

        header = tap_header_c64(len(tap_data), ntsc=args.ntsc)
        with open(args.output, "wb") as f:
            f.write(header)
            f.write(tap_data)
        print(f"✅ TAP written: {args.output} ({len(header) + len(tap_data):,} B)")

        if overflow:
            print("⚠  Buffer overflow — data may be incomplete")

        if pulses:
            print("🔍 Decoding C64 KERNAL blocks...")
            blocks, raw_bytes = extract_c64_blocks(pulses, scale)

            if blocks:
                print_tap_index(blocks, model='c64')
                if args.prg:
                    for name, load_addr, prg_data in blocks:
                        safe_name = name.strip().replace(" ", "_")[:8] or "NONAME"
                        prg_fn    = f"{safe_name}.prg"
                        with open(prg_fn, "wb") as f:
                            f.write(struct.pack("<H", load_addr))
                            f.write(prg_data)
                        print(f"  ✅ PRG: {prg_fn} ({len(prg_data)} B @ ${load_addr:04X})")
            else:
                print("  ⚠  No standard C64 KERNAL blocks found (sync not detected)")
                print("  Verify the tape is a standard (non-turbo) load, or try tapclean")

            print(f"💡 Full decode: wav2prg -P loaders --machine c64 --tap {args.output}")

    else:
        # -------------------------------------------------------------------
        # C16 / Novaload path — NOT MODIFIED from original
        # -------------------------------------------------------------------
        if not tap_data:
            print("❌ No data captured")
            sys.exit(1)

        tap_ver = TAP_VERSION if args.novaload else 1
        header  = tap_header_c16(len(tap_data), ntsc=args.ntsc, version=tap_ver)
        with open(args.output, "wb") as f:
            f.write(header)
            f.write(tap_data)
        print(f"✅ TAP written: {args.output} ({len(header) + len(tap_data):,} B)")

        if overflow:
            print("⚠  Buffer overflow — data may be incomplete")

        blocks    = []
        raw_bytes = b""

        if not args.novaload and pulses:
            print("🔍 Decoding C16 KERNAL blocks...")
            blocks, raw_bytes = extract_c16_blocks(pulses, scale)

            if blocks:
                print_tap_index(blocks, model='c16')
                if args.prg:
                    for name, load_addr, prg_data in blocks:
                        safe_name = name.strip().replace(" ", "_")[:8] or "NONAME"
                        prg_fn    = f"{safe_name}.prg"
                        with open(prg_fn, "wb") as f:
                            f.write(struct.pack("<H", load_addr))
                            f.write(prg_data)
                        print(f"  ✅ PRG: {prg_fn} ({len(prg_data)} B @ ${load_addr:04X})")
            else:
                print("  ⚠  No standard KERNAL blocks found (pilot/sync not detected)")
                print("  Try wav2prg if this is a turbo/custom loader")

            # Detect turbo loader started via "SYS 1536" (0x0600) as a heuristic
            turbo_detected = False
            try:
                if raw_bytes:
                    if b"\x00\x06" in bytes(raw_bytes):
                        turbo_detected = True
            except Exception:
                turbo_detected = False

            if turbo_detected:
                print("  ⚡ Turbo loader (SYS 1536) detected — attempting filename heuristic")
                found_name      = heuristic_name_from_raw(tap_data) if tap_data else None
                dummy_name      = found_name or "TURBO_PROG"
                dummy_load_addr = 0x0600
                dummy_prg_data  = b'\x00' * 3
                dummy_len       = len(dummy_prg_data)
                dummy_header    = struct.pack(
                    "<12sBBBBI",
                    TAP_MAGIC_C16,
                    TAP_VERSION,
                    0,
                    1 if args.ntsc else 0,
                    TAP_RESERVED,
                    dummy_len,
                )
                dummy_header += struct.pack("<I", int(C16_NTSC_HZ if args.ntsc else C16_PAL_HZ))
                print(f'  Found: "{dummy_name}" (${dummy_load_addr:04X}–${dummy_load_addr + dummy_len - 1:04X}, {dummy_len} B)')
                with open(args.output, "ab") as f:
                    f.write(dummy_header)
                    f.write(dummy_prg_data)

        elif args.novaload:
            print("💡 Novaload turbo — use the raw TAP in YAPE/VICE or wav2prg")

            # Try to detect a filename
            found_name     = None
            decoded_blocks = []
            if pulses:
                decoded_blocks, raw_bytes = extract_c16_blocks(pulses, scale)
                if decoded_blocks:
                    for nm, _, _ in decoded_blocks:
                        if nm and nm.strip():
                            found_name = nm
                            break
                    if found_name is None:
                        found_name = decoded_blocks[0][0]
                else:
                    candidate = heuristic_name_from_raw(tap_data)
                    if candidate:
                        found_name = candidate

            dummy_name      = found_name or "DUMMY_PROG"
            dummy_load_addr = 0x0801
            dummy_prg_data  = b'\x00' * 3
            dummy_len       = len(dummy_prg_data)
            dummy_header    = struct.pack(
                "<12sBBBBI",
                TAP_MAGIC_C16,
                TAP_VERSION,
                1,
                0,
                TAP_RESERVED,
                dummy_len,
            )
            dummy_header += struct.pack("<I", int(C16_NTSC_HZ if args.ntsc else C16_PAL_HZ))
            print(f'  Found: "{dummy_name}" (${dummy_load_addr:04X}–${dummy_load_addr + dummy_len - 1:04X}, {dummy_len} B)')
            with open(args.output, "ab") as f:
                f.write(dummy_header)
                f.write(dummy_prg_data)

            if decoded_blocks:
                for name, load_addr, prg_data in decoded_blocks:
                    safe_name2 = name.strip().replace(" ", "_")[:8] or "NONAME"
                    prg_fn2    = f"{safe_name2}.prg"
                    with open(prg_fn2, "wb") as f:
                        f.write(struct.pack("<H", load_addr))
                        f.write(prg_data)
                    print(f"  ✅ PRG: {prg_fn2} ({len(prg_data)} B @ ${load_addr:04X})")

            print(f"💡 Full decode: wav2prg -P loaders --machine c16 --tap {args.output}")


if __name__ == "__main__":
    main()
