#!/usr/bin/env python3

"""
Tapeosaurus — C16/Plus4 and C64 TAP capture + PRG extraction

Usage:
  python3 tapeosaurus.py -p /dev/ttyUSB0 out.tap
  python3 tapeosaurus.py -p /dev/ttyUSB0 -model c64 out.tap
  python3 tapeosaurus.py -p /dev/ttyUSB0 --prg out.tap
  python3 tapeosaurus.py -p /dev/ttyUSB0 -model c16 --novaload out.tap   (C16 only)

Edge mode is negotiated automatically:
  --novaload → sends HOST_CMD_CHANGE (CHANGE interrupt, both edges) — C16 only
  (default)  → sends HOST_CMD_FALLING (FALLING interrupt, standard tape)

The device ACKs with CMD_EDGE_ACK before capture begins.

Model selection:
  -model c16  (default) C16 / Plus4 standard or Novaload
  -model c64            C64 standard KERNAL tape (non-turbo)
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
HOST_CMD_CHANGE  = 0x11   # Novaload turbo (CHANGE — both edges)

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
# Common pulse encoder (used by both models)
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
    print(f"→ Setting edge mode: {mode_name}")
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
                    print(f"✔ Edge mode confirmed by device: {mode_name}")
                    return True
                buf = buf[1:]

    print(f"⚠ No ACK received for edge-mode command (timeout {EDGE_ACK_TIMEOUT_S}s) — continuing")
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
# Main
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Tapeosaurus — C16/Plus4 and C64 TAP capture + PRG extraction"
    )
    parser.add_argument("-p", "--port",  required=True, help="Serial port")
    parser.add_argument("output",        nargs="?", default="out.tap", help="TAP output file")
    parser.add_argument("--novaload",    action="store_true",
                        help="Novaload turbo mode (CHANGE edge + skip KERNAL decoder) — C16 only")
    parser.add_argument("--ntsc", "-v",  action="store_true", help="NTSC clock instead of PAL")
    parser.add_argument("--prg",         action="store_true", help="Extract PRG files from decoded blocks")
    parser.add_argument("-model",        choices=["c16", "c64"], default="c16",
                        dest="model",
                        help="Target computer: c16 (default) or c64")
    args = parser.parse_args()

    model = args.model   # 'c16' or 'c64'

    # --novaload is meaningless for C64 standard tapes
    if args.novaload and model == "c64":
        print("⚠ --novaload is not supported for -model c64; ignoring")
        args.novaload = False

    # Select machine clock
    if model == "c64":
        machine_hz = C64_NTSC_HZ if args.ntsc else C64_PAL_HZ
    else:
        machine_hz = C16_NTSC_HZ if args.ntsc else C16_PAL_HZ

    video_id_str = "NTSC" if args.ntsc else "PAL"
    scale        = machine_hz / FIRMWARE_HZ

    mode_label = {
        "c64": "C64",
        "c16": "C16/Plus4" + (" Novaload" if args.novaload else ""),
    }[model]

    print(f"{mode_label} {video_id_str} — scale: {scale:.6f}")
    print(f"Capturing → {args.output}")

    try:
        ser = serial.Serial(args.port, BAUD, timeout=2)
    except serial.SerialException as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    # Allow the ESP8266 to boot/reset, then flush garbage from reset sequence
    time.sleep(2)
    ser.reset_input_buffer()

    # Edge mode: C64 standard tape always uses FALLING;
    # C16 Novaload uses CHANGE (both edges).
    use_change = (model == "c16" and args.novaload)
    send_edge_command(ser, use_change=use_change)

    reader   = FrameReader(ser)
    tap_data = bytearray()
    pulses   = []   # raw firmware ticks, kept for block decoder
    overflow = False

    print("Waiting for PLAY (LED lights up)...")
    try:
        while True:
            ftype, val = reader.read_frame()
            if ftype == "control" and val == CMD_PLAY_PRESSED:
                print("▶ RECORDING...")
                break
            if ftype == "control" and val == CMD_OVERFLOW:
                print("⚠ Overflow before start")
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
                    tap_data += encode_pulse(val, scale)
                    pulses.append(val)
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
                    print("\n⚠ Buffer overflow")
                # CMD_EDGE_ACK during recording is ignored
            elif ftype == "timeout":
                timeout_streak += 1
                print(f"\r … silence ({timeout_streak * 2}s)", end="", flush=True)
                if timeout_streak >= MAX_RECORDING_TIMEOUTS:
                    print(f"\n⏹ Stopped after {timeout_streak * 2}s silence")
                    break
            elif ftype == "error":
                print("\n⚠ Frame error — continuing")

    except KeyboardInterrupt:
        print("\nAborted by user.")
    finally:
        ser.close()

    if not tap_data:
        print("❌ No data captured")
        sys.exit(1)

    # -----------------------------------------------------------------------
    # Write TAP file
    # -----------------------------------------------------------------------
    if model == "c64":
        # C64 TAP v1 — 20-byte header, no sample_rate extension
        header = tap_header_c64(len(tap_data), ntsc=args.ntsc)
    else:
        # C16: v1 = full-wave (standard), v2 = half-wave (Novaload)
        tap_ver = TAP_VERSION if args.novaload else 1
        header  = tap_header_c16(len(tap_data), ntsc=args.ntsc, version=tap_ver)

    with open(args.output, "wb") as f:
        f.write(header)
        f.write(tap_data)
    print(f"✅ TAP written: {args.output} ({len(header) + len(tap_data):,} B)")

    if overflow:
        print("⚠ Buffer overflow — data may be incomplete")

    # -----------------------------------------------------------------------
    # Post-capture decode
    # -----------------------------------------------------------------------
    blocks    = []
    raw_bytes = b""

    if model == "c64":
        # -------------------------------------------------------------------
        # C64 standard KERNAL decoder
        # -------------------------------------------------------------------
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
                print("  ⚠ No standard C64 KERNAL blocks found (sync not detected)")
                print("  Verify the tape is a standard (non-turbo) load, or try tapclean")

            print(f"💡 Full decode: wav2prg -P loaders --machine c64 --tap {args.output}")

    else:
        # -------------------------------------------------------------------
        # C16 / Novaload decoder — original logic, completely untouched
        # -------------------------------------------------------------------
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
                print("  ⚠ No standard KERNAL blocks found (pilot/sync not detected)")
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
