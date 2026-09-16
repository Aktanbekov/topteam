/*
 * AI Driving Test Coach - alert sketch (UNO Q microcontroller side)
 *
 * Drives three things from one alert level sent by the Linux side:
 *
 *   built-in 8x13 LED matrix   strike count, and a big X on a critical error
 *   Modulino Pixels (0x6C)     8 RGB LEDs: colour = severity, lit = strikes
 *   Modulino Vibro  (0x70)     a buzz you feel without looking
 *
 * Both Modulinos plug into the QWIIC connector and daisy-chain to each other.
 * No other wiring, no resistors, no transistor - the Vibro has its own MOSFET.
 *
 *   0 = driving fine      green,  no buzz
 *   1 = heads up          amber,  no buzz      (see "why level 1 is silent")
 *   2 = minor mistake     red,    one buzz,    strike count +1
 *   3 = critical mistake  red X,  three buzzes, ramping
 *
 * Every library call here matches the hardware test sketch that is known to
 * work on this board. The one thing that differs: nothing below blocks.
 * delay() would stall the bridge and make the board miss the next alert, so
 * the animations run off millis() and buzzes are queued.
 */

#include <string.h>  // memcmp / memcpy on the frame buffer

#include "Arduino_RouterBridge.h"
// Same order as hardware_test.ino, which is known to work on this board.
#include <Arduino_Modulino.h>
#include <Arduino_LED_Matrix.h>

// ---------------------------------------------------------------- hardware
// Arduino_LED_Matrix ships inside the Arduino Zephyr Core the UNO Q runs on -
// it is NOT in the library manager and does not need to be. Modulino is the
// only library to install.
ArduinoLEDMatrix matrix;
ModulinoPixels pixels;
ModulinoVibro vibro;

// Written out rather than read from the class: canvasWidth/canvasHeight are
// private members of Arduino_LED_Matrix, so they will not compile.
const int COLS = 13;
const int ROWS = 8;
const int PIXEL_COUNT = ROWS * COLS;  // 104

uint8_t frame[PIXEL_COUNT];
uint8_t lastFrame[PIXEL_COUNT];  // what is actually on the display now

// ~30 fps. Fast enough for the blink, slow enough that the matrix scan and the
// I2C bus both keep up.
const unsigned long DRAW_INTERVAL_MS = 33;
unsigned long lastDrawAt = 0;

// 1 is what the working hardware test writes, so that is what we write. If
// shapes ever look too dim, matrix.setGrayscaleBits() changes the scale and
// this becomes the top value for it.
const uint8_t PIXEL_ON = 1;

// How bright the strip sits. Low enough not to dazzle at night.
const uint8_t LED_DIM = 20;
const uint8_t LED_BRIGHT = 40;

// ------------------------------------------------------------------ state
// Set from the Linux side, read by loop(). The strike count lives here rather
// than on the laptop, so the existing alert(level) protocol did not change.
volatile int currentLevel = 0;
int strikes = 0;
int lastLevel = 0;

// How many minor mistakes fill the display. The brief allows 15 DMV minors,
// but 8 LEDs read better as the simplified "3 strikes" practice mode.
const int MAX_STRIKES = 3;

// ------------------------------------------------------------------ buzzes
// A buzz is a burst of pulses, played out by loop() so alert() can return at
// once and the bridge stays responsive.
const int MAX_PULSES = 3;
VibroPowerLevel pulsePower[MAX_PULSES];
unsigned long pulseLen[MAX_PULSES];
int pulseCount = 0;
int pulseIndex = 0;
unsigned long nextPulseAt = 0;

const unsigned long PULSE_GAP_MS = 160;

// ------------------------------------------------------------- animations
// After a new strike the matrix shows the number briefly, then returns to the
// bar. Reading digits at speed is a bad idea; a short confirmation right after
// the event is not.
unsigned long showCountUntil = 0;
const unsigned long COUNT_MS = 2000;

unsigned long lastBlinkAt = 0;
bool blinkOn = false;

// ------------------------------------------------------------------ digits
// 3x5 font, one byte per row, low 3 bits are the pixels.
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
  for (int i = 0; i < PIXEL_COUNT; i++) {
    frame[i] = 0;
  }
}

void setPixel(int row, int col) {
  if (row < 0 || row >= ROWS || col < 0 || col >= COLS) {
    return;
  }
  frame[row * COLS + col] = PIXEL_ON;
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
    drawDigit(value, 5);              // one digit, centred
  } else {
    drawDigit((value / 10) % 10, 3);  // two digits with a gap
    drawDigit(value % 10, 7);
  }
}

// A calm bar: how much of the strike budget is used. No reading required,
// which is the whole point while the car is moving.
//
// Three rows tall for the used portion and two for the empty track. A single
// row of 13 pixels is hard to see at a glance in daylight, and at zero strikes
// that is the entire display - easy to mistake for a dead matrix.
void drawBar(int used, int total) {
  int filled = (total <= 0) ? 0 : (used * COLS) / total;
  for (int c = 0; c < COLS; c++) {
    setPixel(3, c);  // the track, always present so the display is never blank
    setPixel(4, c);
    if (c < filled) {
      setPixel(2, c);  // thicken what is used
      setPixel(5, c);
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
// Colour carries severity, the number lit carries the strike count. One
// glance, two facts, no counting.
void updatePixels(int level) {
  pixels.clear();

  if (level >= 3) {
    // Critical: the whole strip flashes. Unmissable in peripheral vision.
    if (blinkOn) {
      for (int i = 0; i < 8; i++) {
        pixels.set(i, 255, 0, 0, LED_BRIGHT);
      }
    }
    pixels.show();
    return;
  }

  int lit = strikes;
  if (lit > 8) {
    lit = 8;
  }
  for (int i = 0; i < lit; i++) {
    pixels.set(i, 255, 0, 0, LED_DIM);
  }

  // One leading LED shows what is happening right now.
  if (lit < 8) {
    if (level >= 2) {
      pixels.set(lit, 255, 0, 0, LED_BRIGHT);
    } else if (level == 1) {
      pixels.set(lit, 255, 120, 0, LED_DIM);  // amber - heads up
    } else {
      pixels.set(lit, 0, 255, 0, LED_DIM);    // green - driving fine
    }
  }

  pixels.show();
}

// ------------------------------------------------------------------ buzzes
void queueBuzz(const VibroPowerLevel *powers, const unsigned long *lengths, int n) {
  if (n > MAX_PULSES) {
    n = MAX_PULSES;
  }
  for (int i = 0; i < n; i++) {
    pulsePower[i] = powers[i];
    pulseLen[i] = lengths[i];
  }
  pulseCount = n;
  pulseIndex = 0;
  nextPulseAt = millis();
}

void serviceBuzz() {
  if (pulseIndex >= pulseCount) {
    return;
  }
  unsigned long now = millis();
  if (now < nextPulseAt) {
    return;
  }
  // on(len_ms, VibroPowerLevel) is the non-blocking overload - it posts the
  // length to the Vibro's own STM32 and returns immediately.
  vibro.on(pulseLen[pulseIndex], pulsePower[pulseIndex]);
  nextPulseAt = now + pulseLen[pulseIndex] + PULSE_GAP_MS;
  pulseIndex++;
}

// One firm pulse for a minor mistake.
void buzzMinor() {
  const VibroPowerLevel powers[] = {MEDIUM};
  const unsigned long lengths[] = {160};
  queueBuzz(powers, lengths, 1);
}

// Three pulses that build. Ramping rather than three identical hits means the
// driver is alerted, not startled - startling a learner is its own hazard.
void buzzCritical() {
  const VibroPowerLevel powers[] = {GENTLE, INTENSE, MAXIMUM};
  const unsigned long lengths[] = {120, 160, 260};
  queueBuzz(powers, lengths, 3);
}

// -------------------------------------------------------------------- main
void setup() {
  matrix.begin();

  // Boot self-test: every LED on for a moment, exactly as the hardware test
  // does it. If you see this flash, the matrix is wired and working and any
  // later blankness is our drawing code, not the display. delay() is fine here
  // - the bridge is not up yet.
  for (int i = 0; i < PIXEL_COUNT; i++) {
    frame[i] = PIXEL_ON;
  }
  matrix.draw(frame);
  delay(600);

  clearFrame();
  memcpy(lastFrame, frame, sizeof(frame));
  matrix.draw(frame);

  Modulino.begin();

  Monitor.begin();

  // Say which modules answered. If one is missing this is the line that tells
  // you, instead of a silent board you have to guess about.
  Monitor.print("Vibro: ");
  Monitor.println(vibro.begin() ? "found" : "NOT FOUND - check the Qwiic cable");
  Monitor.print("Pixels: ");
  Monitor.println(pixels.begin() ? "found" : "NOT FOUND - check the Qwiic cable");

  updatePixels(0);

  Bridge.begin();
  Bridge.provide("alert", alert);
  Bridge.provide("reset", resetDrive);
  Bridge.provide("mcu_ping", mcu_ping);

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

  // Rebuild the picture, but only PUSH it when it changed and at a sane rate.
  //
  // The first version of this called matrix.draw() and pixels.show() straight
  // from loop(), which on this board is thousands of times a second. The strip
  // survived it - a solid colour still looks solid - but the matrix showed
  // nothing at all: the scan never settles on a frame if you keep replacing it.
  // The working hardware test always had a delay() between draws, which is what
  // hid the problem.
  if (now - lastDrawAt >= DRAW_INTERVAL_MS) {
    lastDrawAt = now;

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

    if (memcmp(frame, lastFrame, sizeof(frame)) != 0) {
      memcpy(lastFrame, frame, sizeof(frame));
      matrix.draw(frame);
    }

    updatePixels(level);
  }
}

// Called from the Linux side via the Arduino Router Bridge.
void alert(int level) {
  // Count the TRANSITION, not the level. The laptop repeats the same level on
  // every sample, and one mistake must not tick the counter three times.
  if (level == 2 && lastLevel != 2) {
    strikes++;
    showCountUntil = millis() + COUNT_MS;
    buzzMinor();
  } else if (level == 3 && lastLevel != 3) {
    buzzCritical();
  }

  lastLevel = level;
  currentLevel = level;

  Monitor.print("alert level -> ");
  Monitor.print(level);
  Monitor.print("  strikes: ");
  Monitor.println(strikes);
}

// Health check. Unlike alert() and resetDrive() this RETURNS something, so the
// Linux side can use a bridge REQUEST and actually get an answer back. That
// makes it the one call that proves the sketch is running - a notify only
// proves the router accepted the bytes, not that anybody acted on them.
const char* mcu_ping() {
  return "pong";
}

// Call this at the start of a drive so the count does not carry over.
void resetDrive() {
  strikes = 0;
  lastLevel = 0;
  currentLevel = 0;
  pulseCount = 0;
  pulseIndex = 0;
  showCountUntil = 0;
  vibro.off();
  Monitor.println("counter reset");
}
