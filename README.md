<div align="center"><img src="img/tapeosaurus.png" alt="Tapeosaurus"></div>
<br><br>

Cycle-accurate Datasette tape dumper for the Commodore 16 and Plus/4, running on a single Wemos D1 Mini (ESP8266) with a 3D-printable enclosure.

Inspired by Francesco Vannini's [TrueTape64](https://github.com/francescovannini/truetape64).

Supports C16/Plus4 standard tape and Novaload turbo producing standard `.tap` v2 files compatible with VICE, Tapuino, and any other TAP-capable emulator or hardware.

---

## Hardware

### What you need

| Qty | Part | Note |
|-----|------|------|
| 1 | Wemos D1 Mini | Any revision; clones work fine |
| 1 | MP1584EN step-down module | Adjustable buck converter — set output to 5.00 V |
| 1 | MP1584EN step-down module | Adjustable buck converter — set output to 6.10 V |
| 1 | 10 kΩ resistor | READ voltage divider - in series with READ pin |
| 1 | 20 kΩ resistor | READ voltage divider - shunt to GND |
| 1 | 10 kΩ resistor | SENSE pull-up to 3.3 V |
| 1 | Green LED | "Tape play" indicator |
| 1 | Red LED | "Overflow" indicator |
| 1 | Yellow LED | "Power on" indicator |
| 2 | 390 Ω resistors | Green and red LED current limiters |
| 1 | 680 Ω resistor | Yellow LED current limiters |
| 1 | Passive piezo speaker | Tape audio feedback |
| 1 | 7-pin mini-DIN connector or salvaged cable | Commodore 16 Datasette connector |
| 1 | Mini tactile switch | Reset button |
| 1 | DC jack barrel connector with PCB mount | Powers supply connector |
| 1 | >= 9V DC power supply | Powers both MP1584EN modules |

### Pin mapping

| Wemos Pin | GPIO | Function |
|-----------|------|----------|
| D1 | GPIO5 | Overflow LED (lit if data overflow triggered) |
| D4 | GPIO2 | Onboard LED (active LOW - lit while recording) |
| D5 | GPIO14 | SENSE from Datasette (INPUT\_PULLUP, switch to GND) |
| D6 | GPIO12 | READ from Datasette (via 10 kΩ/20 kΩ divider → 3.3 V) |
| D7 | GPIO13 | Passive piezo speaker |

### Schematics

<div align="center"><img src="schematics/schematics.png" alt="Schematics"></div>
<br><br>

> ⚠ The ESP8266 is a 3.3 V device. The Datasette READ line is 5 V TTL. The 10 kΩ / 20 kΩ divider is **mandatory** — skipping it will damage GPIO12.

### 3D-printable enclosure

Autodesk Fusion 360 files are provided [here](CAD/)

### MP1584EN Step-Down Calibration

Each module must be pre-adjusted before connecting the Datasette.

**Module U2 — 5 V (Datasette logic supply)**
Connect 12 V, measure OUT+ and adjust the trimpot to 5.00 V.

**Module U3 — 6.1 V (Datasette motor)**
Connect 12 V, measure OUT+ and adjust the trimpot to 6.10–6.15 V. Recheck with the motor spinning under load. Values above 6.5 V risk burning the motor winding.

### Quick Sanity Checks Before First Use

- [ ] MP1584EN-U2 output reads 5.00 V (Datasette logic supply)
- [ ] MP1584EN-U3 output reads 6.10–6.15 V on MOTOR line (Datasette disconnected)
- [ ] Multimeter reads ~3.3 V on Wemos 3V3 pin
- [ ] SENSE pin reads ~3.3 V at idle (no tape playing)
- [ ] SENSE pin reads ~0 V when PLAY is pressed (with cassette inserted)
- [ ] No short between 5V and GND (continuity check — no beep)
- [ ] READ pin reads ~3.3 V with tape playing (signal swings around this level)

---

## Firmware

### Requirements

- Arduino IDE 1.8+ or 2.x
- ESP8266 Arduino core — add this URL in **File → Preferences → Additional Boards Manager URLs**:
  `https://arduino.esp8266.com/stable/package_esp8266com_index.json`

### Board settings

| Setting | Value |
|---------|-------|
| Board | LOLIN(WEMOS) D1 R2 & mini |
| CPU Frequency | **80 MHz** ← must be 80, not 160 |
| Upload Speed | 921600 |

> The `CLOCK_SCALE 40` constant (80 MHz ÷ 40 = 2 MHz ticks) is hardcoded. Changing CPU frequency without updating this value will corrupt all timing.

### Tuning options

| Define | Default | Description |
|--------|---------|-------------|
| `CAPTURE_WITHOUT_SENSE` | `0` | Set to `1` to capture without waiting for PLAY — useful for isolating READ wiring issues |

> Edge mode (FALLING/CHANGE) is not a compile-time setting. It is controlled entirely at runtime by the Python CLI over the serial control protocol. The default on power-up is FALLING (standard KERNAL) — safe until the CLI sets it otherwise.

---

## Python CLI

### Install

```bash
pip install -r cli/requirements.txt
```

### Usage

```bash
# Standard C16/Plus4 tape (PAL)
python3 cli/tapeosaurus.py -p /dev/ttyUSB0 output.tap

# Novaload turbo
python3 cli/tapeosaurus.py -p /dev/ttyUSB0 --novaload output.tap

# NTSC machine
python3 cli/tapeosaurus.py -p /dev/ttyUSB0 --ntsc output.tap

# Extract PRG files after capture
python3 cli/tapeosaurus.py -p /dev/ttyUSB0 --prg output.tap
```

Passing `--novaload` is the only configuration needed. The CLI automatically sends the correct edge mode command to the ESP before waiting for PLAY, then selects the appropriate decode path after capture. No switches, no recompile.

### What happens at startup

1. CLI connects and waits 2 seconds for the ESP to boot
2. Sends `CMD_SET_EDGE_FALLING` or `CMD_SET_EDGE_CHANGE` depending on `--novaload`
3. Waits up to 3 seconds for the ESP's confirmation echo
4. Prints the confirmed edge mode and waits for PLAY

```
C16/Plus4 PAL — scale: 0.443362
Capturing → output.tap
→  Setting edge mode: FALLING (standard)
✔  Edge mode confirmed by device: FALLING (standard)
Waiting for PLAY (LED lights up)...
```

```
C16/Plus4 Novaload PAL — scale: 0.443362
Capturing → output.tap
→  Setting edge mode: CHANGE (Novaload/turbo)
✔  Edge mode confirmed by device: CHANGE (Novaload/turbo)
Waiting for PLAY (LED lights up)...
```

### Real example — dumping a Novaload tape

```
$ python3 cli/tapeosaurus.py -p /dev/ttyUSB0 --novaload "./BMX Racers.tap"
C16/Plus4 Novaload PAL — scale: 0.443362
Capturing → ./BMX Racers.tap
→  Setting edge mode: CHANGE (Novaload/turbo)
✔  Edge mode confirmed by device: CHANGE (Novaload/turbo)
Waiting for PLAY (LED lights up)...
▶  RECORDING...
  … silence (2s)
✅ STOPPED — 313,211 pulses
✅ TAP written: ./BMX Racers.tap (313,244 B)
💡 Novaload turbo — use the raw TAP in YAPE/VICE or wav2prg
  Found: "8" ($0801–$0803, 3 B)
```

### Machine clock reference

| Machine                         | PAL clock  | NTSC clock |
|---------------------------------|------------|------------|
| Commodore 16 / Plus4 (standard) | 886 724 Hz | 894 886 Hz |
| Commodore 16 / Plus4 (Novaload) | 886 724 Hz | 894 886 Hz |

The firmware always outputs 2 MHz-equivalent ticks; the CLI scales them to the correct machine clock before writing the TAP file.

---

## How it works

### Timing

1. A GPIO ISR fires on every pulse boundary (`FALLING` for standard, `CHANGE` for Novaload).
2. `ESP.getCycleCount()` is sampled inside the ISR — an 80 MHz hardware counter.
3. The delta between consecutive calls is divided by 40 to produce a 2 MHz tick unit.
4. Ticks are clamped to 24 bits and pushed into a 4096-entry ring buffer.
5. `loop()` drains the buffer to serial at 250 000 baud.

Wi-Fi is completely disabled at boot to eliminate scheduler jitter.

### Control protocol

All serial frames are exactly 4 bytes, in both directions. The same `[00][00][00][CMD]` structure is used for host→device commands and device→host events.

Device is Wemos D1 ESP, Host is the Linux host where `tapeosaurus.py` is run.

**Device → Host:**

| Frame | Bytes | Meaning |
|-------|-------|---------|
| Pulse data | `[B0][B1][B2][B0^B1^B2]` | 24-bit tick count, 2 MHz clock, LSB first |
| PLAY pressed | `[00][00][00][01]` | Tape started — begin recording |
| PLAY released | `[00][00][00][02]` | Tape stopped — end recording |
| Overflow | `[00][00][00][03]` | Ring buffer full, data lost |
| Edge mode change | `[00][00][00][04]` | Confirmation: edge-mode changed |

**Host → Device:**

| Frame | Bytes | Meaning |
|-------|-------|---------|
| Set FALLING | `[FF][FF][FF][10]` | Switch to FALLING edge (standard KERNAL) |
| Set CHANGE | `[FF][FF][FF][11]` | Switch to CHANGE edge (Novaload / turbo) |

Command byte ranges are non-overlapping: `0x01–0x05` device-to-host, `0x10–0x11` host-to-device. A stray echo of a SET command can never be mistaken for a PLAY event.

Control frames are unambiguous from pulse data: the only data frame that starts with three zero bytes would have checksum `0x00`, which is never a valid CMD byte.

### Edge mode negotiation

When the ESP receives a SET command it immediately reattaches the interrupt with the new edge polarity and echoes back `CMD_EDGE_FALLING` or `CMD_EDGE_CHANGE` as confirmation. The CLI waits up to 3 seconds for this echo before proceeding.

The ESP also re-echoes the active mode on every PLAY press — immediately before `CMD_PLAY_PRESSED` — so the host always knows the active mode before any pulse data arrives, and the terminal shows a per-tape confirmation.

The 4-byte receive state machine in the ESP's `loop()` re-aligns naturally after a lost byte — the next valid `[FF][FF][FF][CMD]` frame will be caught after at most 3 bytes of drift, with no explicit framing header needed.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `⚠  No echo from device` at startup | ESP not ready or baud mismatch | Check baud is 250 000 in both CLI and Arduino IDE; power-cycle the Wemos |
| 0 pulses captured | READ wiring or wrong edge mode | Set `CAPTURE_WITHOUT_SENSE 1` to test READ independently; verify `--novaload` flag matches tape type |
| Novaload tape loads garbled | Edge mode mismatch | Ensure `--novaload` is passed for turbo tapes |
| Frame error: timeout before tape starts | Normal — motor spin-up gap | CLI tolerates 30 s of silence before stopping |
| Motor doesn't spin / spins wrong speed | MP1584EN-U3 not calibrated | Set to 6.10–6.15 V with a multimeter before connecting |
| Overflow LED lights up | Buffer draining too slowly | Check baud is 250 000; close other apps using the port |
| Garbled timing | CPU not at 80 MHz | Check board settings in Arduino IDE |
| Datasette not powering on | MP1584EN-U2 not calibrated | Verify 5 V output before connecting Datasette |

---

## Tested tapes

Here's a list of successfully [tested tapes](/TAPES.md).

---

## Credits & license

Inspired by [TrueTape64](https://github.com/francescovannini/truetape64) by Francesco Vannini.

**License:** GNU General Public License v3.0

---

*Made for the Commodore 16 preservation community · Long live the Datasette 🖤*
