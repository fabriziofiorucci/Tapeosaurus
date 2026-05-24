#!/usr/bin/env python3
"""
Tapeosaurus — C16/Plus4 TAP capture + PRG extraction

Usage:
  python3 tapeosaurus.py -p /dev/ttyUSB0 out.tap
  python3 tapeosaurus.py -p /dev/ttyUSB0 --prg out.tap
  python3 tapeosaurus.py -p /dev/ttyUSB0 --novaload out.tap

Edge mode is negotiated automatically:
  --novaload  →  sends HOST_CMD_CHANGE  (CHANGE  interrupt, both edges)
  (default)   →  sends HOST_CMD_FALLING (FALLING interrupt, standard tape)
The device ACKs with CMD_EDGE_ACK before capture begins.
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

C16_PAL_HZ  = 886_724
C16_NTSC_HZ = 894_886

TAP_MAGIC     = b"C16-TAPE-RAW"
TAP_VERSION   = 2
TAP_RESERVED  = 0

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
HOST_CMD_FALLING  = 0x10   # Standard tape  (FALLING edge)
HOST_CMD_CHANGE   = 0x11   # Novaload turbo (CHANGE  — both edges)

MAX_RECORDING_TIMEOUTS = 15
EDGE_ACK_TIMEOUT_S     = 3.0   # How long to wait for CMD_EDGE_ACK

C16_SYNC1         = 0x16
C16_SYNC2         = 0x16
C16_BIT_THRESHOLD = 8000   # tune if needed


# ---------------------------------------------------------------------------
# TAP helpers
# ---------------------------------------------------------------------------

def tap_header(data_len, ntsc=False, version=None):
    """
    Build a C16-TAPE-RAW TAP header.

    version=1  Full-wave (FALLING-edge capture, standard KERNAL tapes).
               20-byte header; no sample_rate extension.
    version=2  Half-wave (CHANGE/both-edges capture, Novaload/turbo tapes).
               24-byte header; 32-bit sample_rate appended.

    Defaults to TAP_VERSION (2) when not supplied so existing callers
    (the Novaload path) are unaffected.
    """
    if version is None:
        version = TAP_VERSION

    system_id   = 0               # Plus/4  (use 0 for bare C16/C116)
    video_id    = 1 if ntsc else 0
    sample_rate = C16_NTSC_HZ if ntsc else C16_PAL_HZ

    header = struct.pack(
        "<12sBBBBI",
        TAP_MAGIC,
        version,
        system_id,
        video_id,
        TAP_RESERVED,
        data_len,
    )
    if version >= 2:
        header += struct.pack("<I", int(sample_rate))
    return header


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


# ---------------------------------------------------------------------------
# C16 KERNAL block decoder
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# TAP index listing
# ---------------------------------------------------------------------------

def print_tap_index(blocks):
    tape_label = TAP_MAGIC.decode("ascii", errors="replace")
    print()
    print(f' 0 "{tape_label}"')

    counter = 2
    for name, load_addr, prg_data in blocks:
        padded = f"{name:<10}"
        print(f' {counter} "{padded}" PRG')
        counter += 2

    print()


# ---------------------------------------------------------------------------
# Serial frame reader
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Edge-mode negotiation
# ---------------------------------------------------------------------------

def send_edge_command(ser, use_change: bool) -> bool:
    """
    Send an edge-mode command to the device and wait for CMD_EDGE_ACK.

    Returns True if the device acknowledged, False on timeout.
    The device will still have applied the command even if we don't see the
    ACK (e.g. the byte arrived late), so a False return is non-fatal.
    """
    cmd       = HOST_CMD_CHANGE if use_change else HOST_CMD_FALLING
    mode_name = "CHANGE (Novaload/turbo)" if use_change else "FALLING (standard)"

    print(f"→  Setting edge mode: {mode_name}")
    ser.write(bytes([0xFF, 0xFF, 0xFF, cmd]))
    ser.flush()

    # Drain any stale bytes, then look for the ACK within the timeout window.
    deadline = time.monotonic() + EDGE_ACK_TIMEOUT_S
    buf = b""
    while time.monotonic() < deadline:
        chunk = ser.read(ser.in_waiting or 1)
        if chunk:
            buf += chunk
            # Scan for a 4-byte control frame  0x00 0x00 0x00 CMD_EDGE_ACK
            while len(buf) >= 4:
                if buf[0] == 0 and buf[1] == 0 and buf[2] == 0 and buf[3] == CMD_EDGE_ACK:
                    print(f"✔  Edge mode confirmed by device: {mode_name}")
                    return True
                buf = buf[1:]   # slide forward one byte and retry
    print(f"⚠  No ACK received for edge-mode command (timeout {EDGE_ACK_TIMEOUT_S}s) — continuing")
    return False


# ---------------------------------------------------------------------------
# Heuristic filename extraction (relaxed)
# ---------------------------------------------------------------------------

PRINTABLE_RE = re.compile(rb"[A-Z0-9 _\-]{2,10}")

def heuristic_name_from_raw(raw_bytes):
    # Look at the first 512 bytes for a printable token (uppercase common for file labels)
    sample = bytes(raw_bytes[:512])
    matches = PRINTABLE_RE.findall(sample)
    if not matches:
        return None
    # Prefer short matches of length 1-8 (trim later)
    for m in matches:
        s = m.strip().decode("ascii", errors="ignore")
        if 1 <= len(s) <= 8:
            return s
    # Otherwise return first match truncated
    return matches[0].strip().decode("ascii", errors="ignore")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Tapeosaurus — C16/Plus4 TAP capture"
    )
    parser.add_argument("-p", "--port",     required=True, help="Serial port")
    parser.add_argument("output",           nargs="?", default="out.tap", help="TAP output file")
    parser.add_argument("--novaload",       action="store_true", help="Novaload turbo mode (CHANGE edge + skips KERNAL decoder)")
    parser.add_argument("--ntsc",   "-v",   action="store_true", help="NTSC clock instead of PAL")
    parser.add_argument("--prg",            action="store_true", help="Extract PRG files from decoded blocks")
    args = parser.parse_args()

    machine_hz   = C16_NTSC_HZ if args.ntsc else C16_PAL_HZ
    video_id_str = "NTSC" if args.ntsc else "PAL"
    scale        = machine_hz / FIRMWARE_HZ

    mode_label = "C16/Plus4" + (" Novaload" if args.novaload else "")
    print(f"{mode_label} {video_id_str} — scale: {scale:.6f}")
    print(f"Capturing → {args.output}")

    try:
        ser = serial.Serial(args.port, BAUD, timeout=2)
    except serial.SerialException as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    # Allow the ESP8266 to boot / reset after the port opens, then flush
    # any garbage from the reset sequence before sending commands.
    time.sleep(2)
    ser.reset_input_buffer()

    # -----------------------------------------------------------------------
    # Negotiate edge mode with the device before any capture activity.
    # --novaload → CHANGE (both edges), default → FALLING.
    # -----------------------------------------------------------------------
    send_edge_command(ser, use_change=args.novaload)

    reader   = FrameReader(ser)
    tap_data = bytearray()
    pulses   = []   # raw firmware ticks, kept for KERNAL decoder
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
                # Late ACK that arrived after send_edge_command's window —
                # already applied on the device, just acknowledge quietly.
                pass

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
                        print(f"\r  … {pulse_count:,} pulses", end="", flush=True)

            elif ftype == "control":
                timeout_streak = 0
                if val == CMD_PLAY_RELEASED:
                    print(f"\n✅ STOPPED — {pulse_count:,} pulses")
                    break
                if val == CMD_OVERFLOW:
                    overflow = True
                    print("\n⚠  Buffer overflow")
                # CMD_EDGE_ACK during recording is ignored (shouldn't happen)

            elif ftype == "timeout":
                timeout_streak += 1
                print(f"\r  … silence ({timeout_streak * 2}s)", end="", flush=True)
                if timeout_streak >= MAX_RECORDING_TIMEOUTS:
                    print(f"\n⏹  Stopped after {timeout_streak * 2}s silence")
                    break

            elif ftype == "error":
                print("\n⚠  Frame error — continuing")

    except KeyboardInterrupt:
        print("\nAborted by user.")
    finally:
        ser.close()

    if not tap_data:
        print("❌ No data captured")
        sys.exit(1)

    # Write TAP file.
    # Standard tape (FALLING mode) produces full-wave values -> TAP v1 (20-byte header).
    # Novaload (CHANGE mode) produces half-wave values -> TAP v2 (24-byte header + sample_rate).
    # Using v2 for standard tape was the bug: xplus4 expected half-wave data, got
    # full-wave, KERNAL timing never matched, 'FOUND FILENAME' was never shown.
    tap_ver = TAP_VERSION if args.novaload else 1
    header = tap_header(len(tap_data), ntsc=args.ntsc, version=tap_ver)
    with open(args.output, "wb") as f:
        f.write(header)
        f.write(tap_data)

    print(f"✅ TAP written: {args.output} ({len(header) + len(tap_data):,} B)")

    if overflow:
        print("⚠  Buffer overflow — data may be incomplete")

    # Decode KERNAL blocks (standard tapes only, not Novaload)
    blocks = []
    raw_bytes = b""
    if not args.novaload and pulses:
        print("🔍 Decoding C16 KERNAL blocks...")
        blocks, raw_bytes = extract_c16_blocks(pulses, scale)

        if blocks:
            print_tap_index(blocks)

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
            print("     Try wav2prg if this is a turbo/custom loader")
            # Detect turbo loader started via "SYS 1536" (0x0600) as a heuristic:
            turbo_detected = False
            try:
                if raw_bytes:
                    if b"\x00\x06" in bytes(raw_bytes):
                        turbo_detected = True
            except Exception:
                turbo_detected = False

            if turbo_detected:
                print("  ⚡ Turbo loader (SYS 1536) detected — attempting filename heuristic")
                found_name = heuristic_name_from_raw(tap_data) if tap_data else None
                dummy_name = found_name or "TURBO_PROG"
                safe_name = dummy_name.strip().replace(" ", "_")[:8] or "NONAME"
                dummy_load_addr = 0x0600
                dummy_prg_data = b'\x00' * 3
                dummy_len = len(dummy_prg_data)

                dummy_header = struct.pack(
                    "<12sBBBBI",
                    TAP_MAGIC,
                    TAP_VERSION,
                    0,  # System ID (bare C16)
                    1 if args.ntsc else 0,
                    TAP_RESERVED,
                    dummy_len
                )
                dummy_header += struct.pack("<I", int(C16_NTSC_HZ if args.ntsc else C16_PAL_HZ))

                print(f'  Found: "{dummy_name}" (${dummy_load_addr:04X}–${dummy_load_addr + dummy_len - 1:04X}, {dummy_len} B)')
                with open(args.output, "ab") as f:
                    f.write(dummy_header)
                    f.write(dummy_prg_data)

    elif args.novaload:
        print("💡 Novaload turbo — use the raw TAP in YAPE/VICE or wav2prg")

        # Try to detect a filename:
        found_name = None
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

        dummy_name = found_name or "DUMMY_PROG"
        safe_name = dummy_name.strip().replace(" ", "_")[:8] or "NONAME"
        dummy_load_addr = 0x0801
        dummy_prg_data = b'\x00' * 3
        dummy_len = len(dummy_prg_data)

        dummy_header = struct.pack(
            "<12sBBBBI",
            TAP_MAGIC,
            TAP_VERSION,
            1,  # System ID (for Plus/4)
            0,  # Video ID (0 for PAL)
            TAP_RESERVED,
            dummy_len
        )
        dummy_header += struct.pack("<I", int(C16_NTSC_HZ if args.ntsc else C16_PAL_HZ))

        print(f'  Found: "{dummy_name}" (${dummy_load_addr:04X}–${dummy_load_addr + dummy_len - 1:04X}, {dummy_len} B)')

        with open(args.output, "ab") as f:
            f.write(dummy_header)
            f.write(dummy_prg_data)

        if decoded_blocks:
            for name, load_addr, prg_data in decoded_blocks:
                safe_name2 = name.strip().replace(" ", "_")[:8] or "NONAME"
                prg_fn2 = f"{safe_name2}.prg"
                with open(prg_fn2, "wb") as f:
                    f.write(struct.pack("<H", load_addr))
                    f.write(prg_data)
                print(f"  ✅ PRG: {prg_fn2} ({len(prg_data)} B @ ${load_addr:04X})")

    print(f"💡 Full decode: wav2prg -P loaders --machine c16 --tap {args.output}")


if __name__ == "__main__":
    main()
