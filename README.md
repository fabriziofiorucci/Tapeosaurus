# Tapeosaurus

![Tapeosaurus](img/tapeosaurus.png)

Cycle-accurate tape dumper for the **Commodore 16 / Plus/4**, **Commodore 64 / 128**, **ZX Spectrum**, and **MSX** running on a single Wemos D1 Mini (ESP8266) with a 3D-printable enclosure.

Inspired by Francesco Vannini's [TrueTape64](https://github.com/francescovannini/truetape64).

Supports C16/Plus4 standard tape and Novaload turbo, Commodore 64/128 tapes, ZX Spectrum standard ROM tapes, and MSX standard tapes — producing standard `.tap` v2 / `.tzx` / `.cas` files compatible with VICE, Fuse, Tapuino, TZXDuino, openMSX, and any other TAP/TZX/CAS-capable emulator or hardware.

---

## Supported tapes

* Commodore 16 & Plus4
* Commodore 64 & 128
* Spectrum
* MSX

---

## Hardware

### What you need

| Qty | Part                                       | Note                                             |
| --- | ------------------------------------------ | ------------------------------------------------ |
| 1   | Wemos D1 Mini                              | Any revision; clones work fine                   |
| 1   | MP1584EN step-down module                  | Adjustable buck converter — set output to 5.00 V |
| 1   | MP1584EN step-down module                  | Adjustable buck converter — set output to 6.10 V |
| 1   | 10 kΩ resistor                             | READ voltage divider - in series with READ pin   |
| 1   | 20 kΩ resistor                             | READ voltage divider - shunt to GND              |
| 1   | 10 kΩ resistor                             | SENSE pull-up to 3.3 V                           |
| 1   | Green LED                                  | "Tape play" indicator                            |
| 1   | Red LED                                    | "Overflow" indicator                             |
| 1   | Yellow LED                                 | "Power on" indicator                             |
| 2   | 390 Ω resistors                            | Green and red LED current limiters               |
| 1   | 680 Ω resistor                             | Yellow LED current limiters                      |
| 1   | Passive piezo speaker                      | Tape audio feedback                              |
| 1   | 7-pin mini-DIN connector or salvaged cable | Commodore 16 Datasette connector                 |
| 1   | Mini tactile switch                        | Reset button                                     |
| 1   | DC jack barrel connector with PCB mount    | Power supply connector                           |
| 1   | >= 9V DC power supply                      | Powers both MP1584EN modules                     |

### MSX hardware notes

#### Using a Commodore tape player

MSX tapes can be dumped using a Commodore datasette, no firmware changes required.

#### Connecting an MSX tape player

MSX computers read tape via the **EAR** pin on the cassette connector (a 3.5 mm DIN jack or 8-pin DIN depending on the model). The EAR pin carries the audio signal from the tape deck at roughly 1 Vpp.

Use the same 10 kΩ / 20 kΩ voltage divider on the READ line as for Commodore tapes. No firmware changes are required — the `tapeosaurus.ino` sketch is identical; the CLI selects CHANGE edge mode automatically when `-model msx` is used.

#### MSX tape format

MSX uses a **Kansas City Standard (KCS)** variant FSK encoding:

| Baud rate | Bit 0 | Bit 1 |
|-----------|-------|-------|
| 1200 baud | 1 cycle @ 1200 Hz | 2 cycles @ 2400 Hz |
| 2400 baud | 1 cycle @ 2400 Hz | 2 cycles @ 4800 Hz |

Each byte is framed as: 1 start bit (0), 8 data bits (LSB first), 2 stop bits (1).

Output is a standard **.cas** file containing the 8-byte CAS sync header (`1F A6 DE BA CC 13 7D 74`) followed by decoded data, compatible with openMSX, fMSX, BlueMSX, CASDuino, and TZXDuino/CASduino.

### ZX Spectrum hardware notes

#### Using a Commodore tape player

Spectrum tapes can be dumped using a Commodore datasette, no firmware changes required.

#### Using a standard tape player

The Spectrum EAR socket outputs a 5 V audio signal (not TTL).  The same
**10 kΩ / 20 kΩ voltage divider** used for the Commodore READ line is suitable
here — connect the EAR tip to the divider input and the sleeve to GND.

The Spectrum has no motor-control line equivalent to SENSE; the capture starts
as soon as the CLI detects the first pulse.  Because `CAPTURE_WITHOUT_SENSE`
defaults to `0`, the firmware waits for the SENSE pin to go LOW before it
starts storing pulses.  For Spectrum use, either:

- wire a simple switch to the SENSE pin (pull it LOW when you press PLAY), **or**
- recompile the firmware with `#define CAPTURE_WITHOUT_SENSE 1` (the CLI will
  still wait for the first `CMD_PLAY_PRESSED` event, which in this mode fires
  on the first pulse rather than on the SENSE signal).

### Pin mapping

| Wemos Pin | GPIO   | Function                                              |
| --------- | ------ | ----------------------------------------------------- |
| D1        | GPIO5  | Overflow LED (lit if data overflow triggered)         |
| D4        | GPIO2  | Onboard LED (active LOW - lit while recording)        |
| D5        | GPIO14 | SENSE from Datasette (INPUT\_PULLUP, switch to GND)   |
| D6        | GPIO12 | READ from Datasette (via 10 kΩ/20 kΩ divider → 3.3 V) |
| D7        | GPIO13 | Passive piezo speaker                                 |

### Schematics

![Schematics](schematics/schematics.png)

> ⚠ The ESP8266 is a 3.3 V device. The Datasette/EAR READ line is 5 V. The 10 kΩ / 20 kΩ divider is **mandatory** — skipping it will damage GPIO12.

### 3D-printable enclosure

Autodesk Fusion 360 files are provided [here](CAD).

### MP1584EN Step-Down Calibration

Each module must be pre-adjusted before connecting the Datasette.

**Module U2 — 5 V (Datasette logic supply)** Connect 12 V, measure OUT+ and adjust the trimpot to 5.00 V.

**Module U3 — 6.1 V (Datasette motor)** Connect 12 V, measure OUT+ and adjust the trimpot to 6.10–6.15 V. Recheck with the motor spinning under load. Values above 6.5 V risk burning the motor winding.

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

| Setting       | Value                            |
| ------------- | -------------------------------- |
| Board         | LOLIN(WEMOS) D1 R2 & mini        |
| CPU Frequency | **80 MHz** ← must be 80, not 160 |
| Upload Speed  | 921600                           |

> The `CLOCK_SCALE 40` constant (80 MHz ÷ 40 = 2 MHz ticks) is hardcoded. Changing CPU frequency without updating this value will corrupt all timing.

### Tuning options

| Define                  | Default | Description                                                                              |
| ----------------------- | ------- | ---------------------------------------------------------------------------------------- |
| `CAPTURE_WITHOUT_SENSE` | `0`     | Set to `1` to capture without waiting for PLAY — useful for Spectrum or READ wiring tests |

> Edge mode (FALLING/CHANGE) is not a compile-time setting. It is controlled entirely at runtime by the Python CLI over the serial control protocol. The default on power-up is FALLING (standard KERNAL) — safe until the CLI sets it otherwise.  For `-model spectrum` the CLI automatically sends `HOST_CMD_CHANGE` before capture.

---

## Python CLI

### Install

```
pip install -r cli/requirements.txt
```

### Usage

```
# Standard C16/Plus4 tape (PAL)
python3 cli/tapeosaurus.py -p /dev/ttyUSB0 -model c16 output.tap

# C16/Plus4 Novaload turbo
python3 cli/tapeosaurus.py -p /dev/ttyUSB0 -model c16 --novaload output.tap

# C16/Plus4 NTSC machine
python3 cli/tapeosaurus.py -p /dev/ttyUSB0 -model c16 --ntsc output.tap

# C16/Plus4 Extract PRG files after capture
python3 cli/tapeosaurus.py -p /dev/ttyUSB0 -model c16 --prg output.tap

# C64/128 tape (PAL)
python3 cli/tapeosaurus.py -p /dev/ttyUSB0 -model c64 output.tap

# ZX Spectrum tape → TZX (default, recommended)
python3 cli/tapeosaurus.py -p /dev/ttyUSB0 -model spectrum output.tzx

# ZX Spectrum tape → raw .tap
python3 cli/tapeosaurus.py -p /dev/ttyUSB0 -model spectrum --tap output.tap

# ZX Spectrum tape → TZX + extract .bin files
python3 cli/tapeosaurus.py -p /dev/ttyUSB0 -model spectrum --prg output.tzx

# MSX tape (1200 baud, default)
python3 cli/tapeosaurus.py -p /dev/ttyUSB0 -model msx output.cas

# MSX tape (2400 baud high-speed)
python3 cli/tapeosaurus.py -p /dev/ttyUSB0 -model msx --baud 2400 output.cas

# MSX tape → CAS + extract files
python3 cli/tapeosaurus.py -p /dev/ttyUSB0 -model msx --prg output.cas
```

Passing `--novaload` is required for C16/Plus4 Novaload turbo tapes. The CLI automatically sends the correct edge mode command to the ESP before waiting for PLAY, then selects the appropriate decode path after capture. No switches, no recompile.

### What happens at startup

1. CLI connects and waits 2 seconds for the ESP to boot
2. Sends `CMD_SET_EDGE_FALLING` or `CMD_SET_EDGE_CHANGE` depending on model / flags
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
C64 PAL — scale: 0.492624
Capturing → output.tap
→  Setting edge mode: FALLING (standard)
✔  Edge mode confirmed by device: FALLING (standard)
Waiting for PLAY (LED lights up)...
```

```
ZX Spectrum — 3.5 MHz — output: .tzx
Capturing → output.tzx
→  Setting edge mode: CHANGE (Novaload/turbo)
✔  Edge mode confirmed by device: CHANGE (Novaload/turbo)
Waiting for PLAY (LED lights up)...
```

```
MSX — 1200 baud — output: .cas
Capturing → output.cas
→  Setting edge mode: CHANGE (Novaload/turbo)
✔  Edge mode confirmed by device: CHANGE (Novaload/turbo)
Waiting for PLAY (LED lights up)...
```

### Real examples

#### Dumping a C16/Plus4 Novaload tape

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

#### Dumping a C64/128 tape

```
$ python3 cli/tapeosaurus.py -p /dev/ttyUSB0 -model c64 "./Treasure Island.tap"
C64 PAL — scale: 0.492624
Capturing → ./Treasure Island.tap
→  Setting edge mode: FALLING (standard)
✔  Edge mode confirmed by device: FALLING (standard)
Waiting for PLAY (LED lights up)...
▶  RECORDING...
 … 460,000 pulses
✅ STOPPED — 461,633 pulses
✅ TAP written: ./Treasure Island.tap (461,860 B)
🔍 Decoding C64 KERNAL blocks...
  ⚠ No standard C64 KERNAL blocks found (sync not detected)
  Verify the tape is a standard (non-turbo) load, or try tapclean
💡 Full decode: wav2prg -P loaders --machine c64 --tap ./Treasure Island.tap
```

#### Dumping a ZX Spectrum tape

```
$ python3 cli/tapeosaurus.py -p /dev/ttyUSB0 -model spectrum --prg "Manic Miner.tzx"
ZX Spectrum — 3.5 MHz — output: .tzx
Capturing → Manic Miner.tzx
→  Setting edge mode: CHANGE (Novaload/turbo)
✔  Edge mode confirmed by device: CHANGE (Novaload/turbo)
Waiting for PLAY (LED lights up)...
▶  RECORDING...
  … 420,000 pulses
✅ STOPPED — 421,044 pulses
🔍 Decoding ZX Spectrum ROM blocks...

  Block 0: Header  ✔
    Type    : Bytes
    Name    : "Manic Min"
    Length  : 49152 bytes
    Load    : $8000
  Block 1: Data    ✔  (49152 bytes payload)

✅ TZX written: Manic Miner.tzx (98,340 B)
  ✅ BIN: Manic_Min.bin (49152 B, load $8000)
💡 Spectrum TZX compatible with: Fuse, SpecEmu, ZXSpin, TZXDuino
💡 Full decode: tzxtools --info Manic Miner.tzx
```

#### Dumping an MSX tape

```
$ python3 cli/tapeosaurus.py -p /dev/ttyUSB0 -model msx --prg "Knightmare.cas"
MSX — 1200 baud — output: .cas
Capturing → Knightmare.cas
→  Setting edge mode: CHANGE (Novaload/turbo)
✔  Edge mode confirmed by device: CHANGE (Novaload/turbo)
Waiting for PLAY (LED lights up)...
▶  RECORDING...
  … 580,000 pulses
✅ STOPPED — 581,290 pulses
🔍 Decoding MSX tape blocks...

  Block 0: Binary    "KNIGHT"  (16406 bytes)  load=$E000 end=$FFFF entry=$E000

✅ CAS written: Knightmare.cas (16,422 B)
  ✅ BIN: KNIGHT.bin (16384 B @ $E000)
💡 CAS compatible with: openMSX, fMSX, BlueMSX, CASDuino, TZXDuino
💡 Full decode: cas2wav Knightmare.cas output.wav
```

### Machine clock reference

| Machine                         | Clock      | Notes                       |
| ------------------------------- | ---------- | --------------------------- |
| Commodore 16 / Plus4 (standard) | 886 724 Hz PAL / 894 886 Hz NTSC | |
| Commodore 16 / Plus4 (Novaload) | 886 724 Hz PAL / 894 886 Hz NTSC | |
| Commodore 64 / 128              | 985 248 Hz PAL / 1 022 727 Hz NTSC | |
| ZX Spectrum (all models)        | 3 500 000 Hz | Fixed; --ntsc has no effect |
| MSX (all models)                | FSK / KCS — 1200 or 2400 baud | Decoded from pulse widths; --ntsc has no effect |


The firmware always outputs 2 MHz-equivalent ticks; the CLI scales them to the correct machine clock before writing the TAP/TZX file.

---

## ZX Spectrum — Technical Details

### Timing constants (3.5 MHz T-states)

| Signal            | T-states   |
| ----------------- | ---------- |
| Pilot half-pulse  | 2168       |
| Sync-1 half-pulse | 667        |
| Sync-2 half-pulse | 735        |
| Bit-0 half-pulse  | 855 × 2    |
| Bit-1 half-pulse  | 1710 × 2   |

### Block types

| Flag byte | Meaning                            |
| --------- | ---------------------------------- |
| 0x00      | Header block (19 bytes payload)    |
| 0xFF      | Data block (variable payload)      |

### Header block layout (flag = 0x00, 17 payload bytes)

| Offset | Size | Field    | Notes                                         |
| ------ | ---- | -------- | --------------------------------------------- |
| 0      | 1    | type     | 0=Program 1=NumArray 2=CharArray 3=Code/Bytes |
| 1      | 10   | filename | space-padded ASCII                            |
| 11     | 2    | length   | data block payload length                     |
| 13     | 2    | param1   | Program: LINE; Code: load address            |
| 15     | 2    | param2   | Program: BASIC length; Code: 0x8000          |

### Output formats

**TZX (default, `-model spectrum`)** — Standard Speed Data blocks (0x10) for
each decoded ROM block.  If no ROM blocks are decoded (e.g. custom/turbo
loader), a Direct Recording block (0x15) is written at 2 MHz sample rate so
the raw waveform is preserved.  Compatible with Fuse, SpecEmu, ZXSpin,
RealSpectrum, TZXDuino, and PZX2TZX tools.

**TAP (`--tap`)** — Raw `.tap` block stream, compatible with Fuse `--tape`,
ZX-Uno, and RetroVirtualMachine.  Only available when ROM blocks are
successfully decoded; custom loaders should use TZX.

---

## MSX — Technical Details

### Tape encoding (Kansas City Standard variant)

MSX uses **Frequency Shift Keying (FSK)** based on the Kansas City Standard:

| Baud rate | Bit 0 | Bit 1 | Header tone |
| --------- | ----- | ----- | ----------- |
| 1200 baud | 1 cycle @ 1200 Hz (833 µs) | 2 cycles @ 2400 Hz (417 µs) | 2400 Hz × 16 000 half-pulses (~6.7 s) |
| 2400 baud | 1 cycle @ 2400 Hz (417 µs) | 2 cycles @ 4800 Hz (208 µs) | 4800 Hz × 32 000 half-pulses (~6.7 s) |

A **short header** (~1.7 s) separates the file header record from the data body.

Each byte is framed as: **1 start bit (0) + 8 data bits LSB-first + 2 stop bits (1)** — 11 bits total.

The signal is captured in **CHANGE edge mode** (both edges), identical to the Spectrum path.

### CAS sync word and file type markers

Every logical block on tape begins with the 8-byte sync word, followed by 10 repeating type-marker bytes:

| Sync word (hex)               | `1F A6 DE BA CC 13 7D 74` |
| ----------------------------- | ------------------------- |
| BASIC tokenised marker        | `D3 D3 D3 D3 D3 D3 D3 D3 D3 D3` |
| ASCII / text marker           | `EA EA EA EA EA EA EA EA EA EA` |
| Binary / machine code marker  | `D0 D0 D0 D0 D0 D0 D0 D0 D0 D0` |

### File header record layout

The 6-byte filename (space-padded) follows immediately after the 10 type-marker bytes.  For binary (BSAVE) files, three 2-byte little-endian words follow the filename:

| Offset | Size | Field         | Notes                  |
| ------ | ---- | ------------- | ---------------------- |
| 10     | 6    | filename      | space-padded ASCII     |
| 16     | 2    | start address | load address           |
| 18     | 2    | end address   | inclusive              |
| 20     | 2    | entry address | execution entry point  |

### Output format

**CAS (`.cas`)** — Standard MSX cassette image.  Each block is stored as the 8-byte CAS sync word followed by the raw decoded data (type markers + filename + payload).  Compatible with openMSX (`-cassetteplayer`), fMSX, BlueMSX, CASDuino, TZXDuino/CASduino, and `cas2wav`.

Pass `--prg` to additionally extract individual files:
- Binary (BSAVE) blocks → `.bin` (raw payload, load address shown in output)
- BASIC tokenised blocks → `.bas` (raw tokenised BASIC, load with openMSX BASIC `BLOAD`)
- ASCII / text blocks → `.asc`


---

## How it works

### Timing

1. A GPIO ISR fires on every pulse boundary (`FALLING` for standard, `CHANGE` for Novaload/Spectrum).
2. `ESP.getCycleCount()` is sampled inside the ISR — an 80 MHz hardware counter.
3. The delta between consecutive calls is divided by 40 to produce a 2 MHz tick unit.
4. Ticks are clamped to 24 bits and pushed into a 4096-entry ring buffer.
5. `loop()` drains the buffer to serial at 250 000 baud.

Wi-Fi is completely disabled at boot to eliminate scheduler jitter.

### Control protocol

All serial frames are exactly 4 bytes, in both directions.
The `[FF][FF][FF][CMD]` structure is used for host→device commands and `[00][00][00][CMD]` device→host events.

Device is Wemos D1 ESP, Host is the Linux host where `tapeosaurus.py` is run.

**Device → Host:**

| Frame            | Bytes                    | Meaning                                   |
| ---------------- | ------------------------ | ----------------------------------------- |
| Pulse data       | `[B0][B1][B2][B0^B1^B2]` | 24-bit tick count, 2 MHz clock, LSB first |
| PLAY pressed     | `[00][00][00][01]`       | Tape started — begin recording            |
| PLAY released    | `[00][00][00][02]`       | Tape stopped — end recording              |
| Overflow         | `[00][00][00][03]`       | Ring buffer full, data lost               |
| Edge mode change | `[00][00][00][04]`       | Confirmation: edge-mode changed           |

**Host → Device:**

| Frame       | Bytes              | Meaning                                  |
| ----------- | ------------------ | ---------------------------------------- |
| Set FALLING | `[FF][FF][FF][10]` | Switch to FALLING edge (standard KERNAL) |
| Set CHANGE  | `[FF][FF][FF][11]` | Switch to CHANGE edge (Novaload / Spectrum / turbo) |

---

## Troubleshooting

| Symptom                                 | Likely cause                   | Fix                                                                                                  |
| --------------------------------------- | ------------------------------ | ---------------------------------------------------------------------------------------------------- |
| `⚠ No echo from device` at startup     | ESP not ready or baud mismatch | Check baud is 250 000 in both CLI and Arduino IDE; power-cycle the Wemos                             |
| 0 pulses captured                       | READ wiring or wrong edge mode | Set `CAPTURE_WITHOUT_SENSE 1` to test READ independently; verify `--novaload` flag matches tape type |
| Novaload tape loads garbled             | Edge mode mismatch             | Ensure `--novaload` is passed for turbo tapes                                                        |
| Frame error: timeout before tape starts | Normal — motor spin-up gap     | CLI tolerates 30 s of silence before stopping                                                        |
| Motor doesn't spin / spins wrong speed  | MP1584EN-U3 not calibrated     | Set to 6.10–6.15 V with a multimeter before connecting                                               |
| Overflow LED lights up                  | Buffer draining too slowly     | Check baud is 250 000; close other apps using the port                                               |
| Garbled timing                          | CPU not at 80 MHz              | Check board settings in Arduino IDE                                                                  |
| Datasette not powering on               | MP1584EN-U2 not calibrated     | Verify 5 V output before connecting Datasette                                                        |
| Spectrum: no blocks decoded             | Custom/turbo loader            | TZX Direct Recording block is still written — use tzxtools or PlayTZX to play it back               |
| Spectrum: bad checksum warnings         | Worn tape or level mismatch    | Try adjusting the EAR volume on the source device; increase divider tolerance                        |
| Spectrum: SENSE never fires             | No SENSE wiring                | Compile with `CAPTURE_WITHOUT_SENSE 1` for Spectrum use                                              |
| MSX: no blocks decoded                  | Wrong baud rate                | Try `--baud 2400`; most commercial game tapes are 1200 baud but some publishers used 2400            |
| MSX: garbled data                       | Signal level too high/low (not applicable when Commodore datasette is used)      | Adjust tape deck volume; verify 10 kΩ/20 kΩ divider is fitted on the EAR line                       |
| MSX: SENSE never fires                  | No SENSE wiring (not applicable when Commodore datasette is used)                | Compile with `CAPTURE_WITHOUT_SENSE 1`; MSX has no motor-control equivalent to Datasette SENSE       |

---

## Tested tapes

Here's a list of successfully [tested tapes](TAPES.md).

---

## Credits & license

Inspired by [TrueTape64](https://github.com/francescovannini/truetape64) by Francesco Vannini.

**License:** GNU General Public License v3.0

---

*Made for the Commodore, Spectrum and MSX preservation community · Long live the Datasette 🖤*
