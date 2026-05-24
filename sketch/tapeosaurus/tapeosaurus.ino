/*
 * Tapeosaurus — Wemos D1 Mini (ESP8266)
 * Commodore 16 / Plus/4 TAP capture
 * Works with tapeosaurus.py at 250000 baud.
 *
 * Pin mapping:
 *   D6 / GPIO12 → READ  from Datasette  (via 10k/20k divider → 3.3V)
 *   D5 / GPIO14 → SENSE from Datasette  (INPUT_PULLUP, switch to GND)
 *   D4 / GPIO2  → Onboard LED           (active LOW: ON while recording)
 *   D7 / GPIO13 → Passive piezo speaker (optional, safe output pin)
 *   D1 / GPIO5  → Overflow LED
 *
 * Edge mode is set at runtime via host command from tapeosaurus.py.
 * Default is FALLING (standard C16 tape).
 * Send 0xFF 0xFF 0xFF 0x11 (HOST_CMD_CHANGE)  → Novaload / turbo loaders.
 * Send 0xFF 0xFF 0xFF 0x10 (HOST_CMD_FALLING) → standard tape (default).
 * The device ACKs with 0x00 0x00 0x00 0x04 (CMD_EDGE_ACK).
 *
 * Data frame (device → host):  b0 b1 b2 (b0^b1^b2)
 * Control frame (device → host): 0x00 0x00 0x00 <cmd>
 * Command frame (host → device): 0xFF 0xFF 0xFF <cmd>
 */

#include <Arduino.h>
#include <ESP8266WiFi.h>

// Set to 1 to capture without waiting for PLAY — useful for isolating
// READ wiring issues when SENSE detection is suspected
#define CAPTURE_WITHOUT_SENSE  0

#define PIN_READ         12    // D6
#define PIN_SENSE        14    // D5
#define PIN_LED           2    // D4
#define PIN_PIEZO        13    // D7 — Safe output, not a bootstrap pin
#define PIN_LED_OVERFLOW  5    // D1

#define PIEZO_BIT  (1u << PIN_PIEZO)
#define PIEZO_TOGGLE() do { \
    if (GPO & PIEZO_BIT) GPOC = PIEZO_BIT; \
    else                 GPOS = PIEZO_BIT; \
} while (0)
#define PIEZO_OFF() (GPOC = PIEZO_BIT)

#define CLOCK_SCALE 40
#define BUF_SIZE 4096
#define BUF_MASK (BUF_SIZE - 1)

volatile uint32_t ring[BUF_SIZE];
volatile uint16_t r_head = 0;
volatile uint16_t r_tail = 0;

volatile uint32_t last_cycle = 0;
volatile bool capturing = false;
volatile bool overflow_flag = false;

#define DEBOUNCE_MS 60
static bool sense_prev = HIGH;
static unsigned long sense_ms = 0;

// ---------------------------------------------------------------------------
// Device → Host control commands
// ---------------------------------------------------------------------------
#define CMD_PLAY_PRESSED   0x01
#define CMD_PLAY_RELEASED  0x02
#define CMD_OVERFLOW       0x03
#define CMD_EDGE_ACK       0x04   // Sent after an edge-mode change is applied

// ---------------------------------------------------------------------------
// Host → Device command frame: 0xFF 0xFF 0xFF <cmd>
// ---------------------------------------------------------------------------
#define HOST_CMD_FALLING   0x10   // Switch ISR to FALLING (standard tape)
#define HOST_CMD_CHANGE    0x11   // Switch ISR to CHANGE  (Novaload / turbo)

// ---------------------------------------------------------------------------
// Runtime edge mode — default FALLING, changed by host command
// ---------------------------------------------------------------------------
static uint8_t current_edge_mode = FALLING;

// ---------------------------------------------------------------------------
// ISR
// ---------------------------------------------------------------------------
void IRAM_ATTR onReadISR() {
    uint32_t now = ESP.getCycleCount();

#if CAPTURE_WITHOUT_SENSE
    bool active = true;
#else
    bool active = capturing;
#endif

    if (active) PIEZO_TOGGLE();

    if (!active) {
        last_cycle = now;
        return;
    }

    if (last_cycle != 0) {
        uint32_t ticks = (now - last_cycle) / CLOCK_SCALE;
        if (ticks == 0) ticks = 1;
        if (ticks > 0x00FFFFFFul) ticks = 0x00FFFFFFul;

        uint16_t next = (r_head + 1) & BUF_MASK;
        if (next == r_tail) {
            overflow_flag = true;
        } else {
            ring[r_head] = ticks;
            r_head = next;
        }
    }

    last_cycle = now;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
static void send_control(uint8_t cmd) {
    uint8_t ctrl[4] = {0x00, 0x00, 0x00, cmd};
    Serial.write(ctrl, 4);
}

static void set_edge_mode(uint8_t mode) {
    // Re-attaching the interrupt while capturing is valid but unusual;
    // typically Python sends this command before pressing PLAY.
    detachInterrupt(digitalPinToInterrupt(PIN_READ));
    current_edge_mode = mode;
    attachInterrupt(digitalPinToInterrupt(PIN_READ), onReadISR, mode);
}

// ---------------------------------------------------------------------------
// Parse one 4-byte host-command frame from Serial (non-blocking)
// ---------------------------------------------------------------------------
static void handle_host_commands() {
    // Each host frame is exactly 4 bytes: 0xFF 0xFF 0xFF <cmd>
    // We only enter when enough bytes are waiting to avoid any blocking read.
    while (Serial.available() >= 4) {
        uint8_t buf[4];
        Serial.readBytes(buf, 4);

        if (buf[0] == 0xFF && buf[1] == 0xFF && buf[2] == 0xFF) {
            switch (buf[3]) {
                case HOST_CMD_FALLING:
                    set_edge_mode(FALLING);
                    send_control(CMD_EDGE_ACK);
                    break;
                case HOST_CMD_CHANGE:
                    set_edge_mode(CHANGE);
                    send_control(CMD_EDGE_ACK);
                    break;
                default:
                    // Unknown command — ignore silently
                    break;
            }
        }
        // Non-matching frames (e.g. noise) are discarded; we've already
        // consumed the 4 bytes so the stream stays aligned.
    }
}

// ---------------------------------------------------------------------------
// Setup / loop
// ---------------------------------------------------------------------------
void setup() {
    WiFi.persistent(false);
    WiFi.mode(WIFI_OFF);
    WiFi.forceSleepBegin();
    delay(1);

    Serial.begin(250000);

    pinMode(PIN_PIEZO, OUTPUT);
    PIEZO_OFF();

    pinMode(PIN_LED_OVERFLOW, OUTPUT);
    digitalWrite(PIN_LED_OVERFLOW, HIGH);

    pinMode(PIN_LED, OUTPUT);
    digitalWrite(PIN_LED, HIGH);

    pinMode(PIN_READ, INPUT);
    pinMode(PIN_SENSE, INPUT_PULLUP);

    sense_prev = digitalRead(PIN_SENSE);

    // Attach with the default edge mode (FALLING).
    // Python will send HOST_CMD_CHANGE before capture starts if needed.
    attachInterrupt(digitalPinToInterrupt(PIN_READ), onReadISR, current_edge_mode);
}

void loop() {
    // -----------------------------------------------------------------------
    // 1. Process any host commands arriving over Serial
    // -----------------------------------------------------------------------
    handle_host_commands();

    // -----------------------------------------------------------------------
    // 2. SENSE debounce → start / stop capture
    // -----------------------------------------------------------------------
    unsigned long now_ms = millis();
    bool sense_now = digitalRead(PIN_SENSE);

    if (sense_now != sense_prev && (now_ms - sense_ms) >= DEBOUNCE_MS) {
        sense_prev = sense_now;
        sense_ms   = now_ms;

        if (sense_now == LOW) {
            last_cycle = 0;
            r_head = r_tail = 0;
            capturing = true;
            digitalWrite(PIN_LED, LOW);
            send_control(CMD_PLAY_PRESSED);
        } else {
            capturing = false;
            PIEZO_OFF();
            digitalWrite(PIN_LED, HIGH);
            send_control(CMD_PLAY_RELEASED);
        }
    }

    // -----------------------------------------------------------------------
    // 3. Dequeue ring-buffer frames → Serial (4-byte batches)
    // -----------------------------------------------------------------------
    while (r_tail != r_head) {
        uint32_t ticks;
        // Briefly disable interrupts to avoid torn index / partial reads
        noInterrupts();
        ticks  = ring[r_tail];
        r_tail = (r_tail + 1) & BUF_MASK;
        interrupts();

        uint8_t b0 =  ticks        & 0xFF;
        uint8_t b1 = (ticks >>  8) & 0xFF;
        uint8_t b2 = (ticks >> 16) & 0xFF;
        uint8_t out[4] = { b0, b1, b2, (uint8_t)(b0 ^ b1 ^ b2) };

        Serial.write(out, 4);
    }

    // -----------------------------------------------------------------------
    // 4. Overflow notification
    // -----------------------------------------------------------------------
    if (overflow_flag) {
        overflow_flag = false;
        digitalWrite(PIN_LED_OVERFLOW, LOW);
        send_control(CMD_OVERFLOW);
    }

    yield();
}
