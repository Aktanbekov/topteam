/*
 * AI Driving Test Coach - alert sketch (UNO Q microcontroller side)
 *
 * Drives three things from one alert level sent by the Linux side:
 *
 *   built-in 8x13 LED matrix   the strike count, and a big X on a critical error
 *   Modulino Pixels (0x6C)     8 RGB LEDs: colour = severity, lit = strikes
 *   Modulino Vibro  (0x70)     a buzz you feel without looking
 *
 * Both Modulinos plug into the QWIIC connector and daisy-chain to each other.
 * No other wiring, no resistors, no transistor - the Vibro has its own MOSFET.
 *
 *   0 = driving fine      green,  no buzz
 *   1 = heads up          amber,  no buzz      (see "why level 1 is silent")
 *   2 = minor mistake     red,    one buzz,    strike count +1
 *   3 = critical mistake  red X,  three buzzes
 *
 * Nothing here blocks. delay() would stall the bridge and make the board miss
 * the next alert, so every animation is driven off millis().
 */

#include "Arduino_RouterBridge.h"
#include "Arduino_LED_Matrix.h"
#include "Modulino.h"

// ---------------------------------------------------------------- hardware
const int COLS = 13;
const int ROWS = 8;

ArduinoLEDMatrix matrix;
ModulinoPixels strip;
ModulinoVibro vibro;

uint8_t frame[ROWS * COLS];

// ------------------------------------------------------------------ state
// Set from the Linux side, read by loop(). The count lives here rather than on
// the laptop so the existing alert(level) protocol did not have to change.
volatile int currentLevel = 0;
int strikes = 0;
int lastLevel = 0;

// How many minor mistakes before the strip is full. The brief allows 15 DMV
// minors, but 8 LEDs read better as the simplified "3 strikes" practice mode.
const int MAX_STRIKES = 3;

// ------------------------------------------------------------- animations
// A buzz is a burst of short pulses. We queue them and let loop() play them
// out, so alert() returns immediately and the bridge stays responsive.
int pulsesLeft = 0;
unsigned long nextPulseAt = 0;
const unsigned long PULSE_MS = 120;
const unsigned long PULSE_GAP_MS = 180;

// After a new strike the matrix shows the number for a moment, then goes back
// to the calm bar. Reading digits while driving is a bad idea; a brief
// confirmation right after the event is not.
unsigned long showCountUntil = 0;
const unsigned long COUNT_MS = 2000;

unsigned long lastBlinkAt = 0;
bool blinkOn = false;

// ------------------------------------------------------------------ digits
// 3x5 font, one byte per row, low 3 bits are the pixels. Written out rather
// than pulled from the library's fonts.h so you can see exactly what it draws.
const uint8_t DIGITS[10][5] = {
  {0b111, 0b101, 0b101, 0b101, 0b111},  // 0
  {0b010, 0b110, 0b010, 0b010, 0b111},  // 1
  {0b111, 0b001, 0b111, 0b100, 0b111},  // 2
  {0b111, 0b001, 0b111, 0b001, 0b111},  // 3
  {0b101, 0b101, 0b111, 0b001, 0b001},  // 4
  {0b111, 0b100, 0b111, 0b001, 0b111},  // 5
  {0b111, 0b100, 0b111, 0b101, 0b111},  // 6
  {0b111, 0b001, 0b001, 0b001, 0b001},  // 7
  {0b111, 0b101, 0b111, 0b101, 0b111},  // 8
  {0b111, 0b101, 0b111, 0b001, 0b111},  // 9
};

void clearFrame() {
  memset(frame, 0, sizeof(frame));
}

void setPixel(int row, int col) {
  if (row < 0 || row >= ROWS || col < 0 || col >= COLS) {
    return;
  }
  frame[row * COLS + col] = 1;
}

void drawDigit(int value, int col) {
  if (value < 0 || value > 9) {
    return;
  }
  for (int r = 0; r < 5; r++) {
    for (int c = 0; c < 3; c++) {
      if (DIGITS[value][r] & (1 << (2 - c))) {
        setPixel(r + 2, col + c);  // rows 2-6 centres the digit vertically
      }
    }
  }
}

void drawNumber(int value) {
  if (value < 10) {
    drawDigit(value, 5);            // one digit, centred
  } else {
    drawDigit((value / 10) % 10, 3);  // two digits with a gap
    drawDigit(value % 10, 7);
  }
}

// A calm bar: how much of the strike budget is used. No reading required,
// which is the whole point while the car is moving.
void drawBar(int used, int total) {
  int filled = (total <= 0) ? 0 : (used * COLS) / total;
  for (int c = 0; c < COLS; c++) {
    setPixel(3, c);                 // the empty track
    if (c < filled) {
      setPixel(2, c);               // thicken what is used
      setPixel(4, c);
    }
  }
}

void drawCross() {
  for (int i = 0; i < ROWS; i++) {
    setPixel(i, i + 2);
    setPixel(i, COLS - 3 - i);
  }
}

// ------------------------------------------------------------------ strip
// Colour carries severity, the number lit carries the strike count. One glance,
// two facts, no counting.
void updateStrip(int level) {
  strip.clear();

  const uint8_t dim = 12;  // bright enough at night, not dazzling

  if (level >= 3) {
    // Critical: the whole strip flashes. Unmissable in peripheral vision.
    if (blinkOn) {
      for (int i = 0; i < 8; i++) {
        strip.set(i, RED, 25);
      }
    }
    strip.show();
    return;
  }

  int lit = strikes;
  if (lit > 8) {
    lit = 8;
  }

  for (int i = 0; i < lit; i++) {
    strip.set(i, RED, dim);
  }

  // One leading LED shows what is happening right now.
  if (lit < 8) {
    if (level >= 2) {
      strip.set(lit, RED, 25);
    } else if (level == 1) {
      strip.set(lit, 255, 150, 0, dim);   // amber - heads up
    } else {
      strip.set(lit, GREEN, dim);
    }
  }

  strip.show();
}

// ------------------------------------------------------------------ buzzes
void queueBuzz(int pulses) {
  pulsesLeft = pulses;
  nextPulseAt = millis();
}

void serviceBuzz() {
  if (pulsesLeft <= 0) {
    return;
  }
  unsigned long now = millis();
  if (now < nextPulseAt) {
    return;
  }
  // block = false, so this returns straight away and the motor runs on its own
  // STM32. Power defaults to the library's MAXIMUM.
  vibro.on(PULSE_MS, false);
  pulsesLeft--;
  nextPulseAt = now + PULSE_MS + PULSE_GAP_MS;
}

// -------------------------------------------------------------------- main
void setup() {
  matrix.begin();

  Modulino.begin();
  strip.begin();
  vibro.begin();

  clearFrame();
  matrix.draw(frame);
  updateStrip(0);

  Bridge.begin();
  Bridge.provide("alert", alert);
  Bridge.provide("reset", resetDrive);

  Monitor.begin();
  Monitor.println("alert sketch ready - levels 0-3, reset() clears the count");
}

void loop() {
  int level = currentLevel;
  if (level < 0) {
    level = 0;
  }
  if (level > 3) {
    level = 3;
  }

  unsigned long now = millis();
  if (now - lastBlinkAt >= 120) {
    lastBlinkAt = now;
    blinkOn = !blinkOn;
  }

  serviceBuzz();

  clearFrame();
  if (level >= 3) {
    if (blinkOn) {
      drawCross();
    }
  } else if (now < showCountUntil) {
    drawNumber(strikes);
  } else {
    drawBar(strikes, MAX_STRIKES);
  }
  matrix.draw(frame);

  updateStrip(level);
}

// Called from the Linux side via the Arduino Router Bridge.
void alert(int level) {
  // Count the TRANSITION, not the level. The laptop repeats the same level on
  // every sample, and one mistake must not tick the counter three times.
  if (level == 2 && lastLevel != 2) {
    strikes++;
    showCountUntil = millis() + COUNT_MS;
    queueBuzz(1);
  } else if (level == 3 && lastLevel != 3) {
    queueBuzz(3);
  }

  lastLevel = level;
  currentLevel = level;

  Monitor.print("alert level -> ");
  Monitor.print(level);
  Monitor.print("  strikes: ");
  Monitor.println(strikes);
}

// Call this at the start of a drive so the count does not carry over.
void resetDrive() {
  strikes = 0;
  lastLevel = 0;
  currentLevel = 0;
  pulsesLeft = 0;
  showCountUntil = 0;
  vibro.off();
  Monitor.println("counter reset");
}
