/*
 * AI Driving Test Coach - alert sketch (UNO Q microcontroller side)
 *
 * Smoke test only: no external wiring needed, uses the built-in LED.
 * The Linux side calls alert(level); this sketch blinks a pattern per level.
 *
 *   0 = driving fine      -> LED off
 *   1 = heads up          -> slow blink
 *   2 = minor mistake     -> fast blink
 *   3 = critical mistake  -> rapid strobe
 *
 * Once the signal path works, swap LED_BUILTIN for the real green/yellow/red
 * LEDs and the vibration motor.
 */

#include "Arduino_RouterBridge.h"

// The UNO Q built-in LED is active-low: LOW turns it ON.
const int LED_ON  = LOW;
const int LED_OFF = HIGH;

// Blink half-period in ms for each level. 0 means "stay off".
const unsigned long LEVEL_PERIOD_MS[4] = {0, 500, 150, 60};

volatile int currentLevel = 0;

unsigned long lastToggleMs = 0;
bool ledIsOn = false;

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LED_OFF);

  Bridge.begin();
  Bridge.provide("alert", alert);

  Monitor.begin();
  Monitor.println("alert sketch ready - waiting for levels 0-3");
}

void loop() {
  int level = currentLevel;

  // Level 0: LED stays off, nothing to animate.
  if (level <= 0) {
    if (ledIsOn) {
      digitalWrite(LED_BUILTIN, LED_OFF);
      ledIsOn = false;
    }
    return;
  }

  // Clamp so an unexpected level can't index past the table.
  if (level > 3) {
    level = 3;
  }

  // Non-blocking blink so the bridge keeps responding while we animate.
  unsigned long now = millis();
  if (now - lastToggleMs >= LEVEL_PERIOD_MS[level]) {
    lastToggleMs = now;
    ledIsOn = !ledIsOn;
    digitalWrite(LED_BUILTIN, ledIsOn ? LED_ON : LED_OFF);
  }
}

// Called from the Linux side via the Arduino Router Bridge.
void alert(int level) {
  currentLevel = level;

  Monitor.print("alert level -> ");
  Monitor.println(level);
}
