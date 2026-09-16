/*
 * UNO Q hardware test - the known-good baseline.
 *
 * Verified working on the board 2026-09-15. Exercises all three outputs with
 * no bridge, no network and no laptop involved, so when the real alert sketch
 * misbehaves this tells you whether the hardware or the software is at fault.
 *
 * Deploy this first after any wiring change. If this passes and
 * alert_sketch.ino does not, the problem is in our code, not the modules.
 *
 * Keep the API calls here as the reference: alert_sketch.ino is written to
 * match them exactly. Note it uses blocking delay(), which is fine here and
 * NOT fine in the alert sketch, where it would stall the router bridge.
 */

#include <Arduino_Modulino.h>
#include <Arduino_LED_Matrix.h>

ArduinoLEDMatrix matrix;
ModulinoVibro vibro;
ModulinoPixels pixels;

void setup() {
  Serial.begin(115200);
  delay(1500);

  Serial.println();
  Serial.println("=================================");
  Serial.println(" UNO Q HARDWARE TEST");
  Serial.println("=================================");

  Serial.println("Starting UNO Q LED matrix...");
  matrix.begin();

  Serial.println("Starting Modulino bus...");
  Modulino.begin();

  Serial.print("Looking for Vibro... ");
  Serial.println(vibro.begin() ? "FOUND!" : "NOT FOUND!");

  Serial.print("Looking for Pixels... ");
  Serial.println(pixels.begin() ? "FOUND!" : "NOT FOUND!");

  delay(1000);
}

void loop() {
  Serial.println();
  Serial.println("------- TEST START -------");

  // ---------------------------------------------- 1. onboard LED matrix
  Serial.println("1. Testing onboard LED matrix");

  uint8_t frame[104];  // 8 rows x 13 columns

  for (int i = 0; i < 104; i++) {
    frame[i] = 1;
  }
  matrix.draw(frame);
  delay(1000);

  for (int i = 0; i < 104; i++) {
    frame[i] = 0;
  }
  matrix.draw(frame);
  delay(500);

  for (int pixel = 0; pixel < 104; pixel++) {
    for (int i = 0; i < 104; i++) {
      frame[i] = 0;
    }
    frame[pixel] = 1;
    matrix.draw(frame);
    delay(25);
  }

  for (int i = 0; i < 104; i++) {
    frame[i] = 0;
  }
  matrix.draw(frame);
  delay(500);

  // ------------------------------------------------- 2. vibration motor
  Serial.println("2. Testing vibration motor");

  Serial.println("Gentle");
  vibro.on(500, GENTLE);
  delay(700);

  Serial.println("Medium");
  vibro.on(500, MEDIUM);
  delay(700);

  Serial.println("Maximum");
  vibro.on(800, MAXIMUM);
  delay(1000);

  vibro.off();

  // --------------------------------------------------- 3. the RGB strip
  Serial.println("3. Testing 8 RGB LEDs");

  pixels.clear();
  pixels.show();
  delay(500);

  Serial.println("Red chase");
  for (int i = 0; i < 8; i++) {
    pixels.clear();
    pixels.set(i, 255, 0, 0, 30);
    pixels.show();
    delay(200);
  }

  Serial.println("Green chase");
  for (int i = 0; i < 8; i++) {
    pixels.clear();
    pixels.set(i, 0, 255, 0, 30);
    pixels.show();
    delay(200);
  }

  Serial.println("Blue chase");
  for (int i = 0; i < 8; i++) {
    pixels.clear();
    pixels.set(i, 0, 0, 255, 30);
    pixels.show();
    delay(200);
  }

  // ------------------------------------------------ 4. all eight colours
  Serial.println("4. Testing individual RGB LEDs");

  pixels.clear();
  pixels.set(0, 255, 0, 0, 30);      // red
  pixels.set(1, 0, 255, 0, 30);      // green
  pixels.set(2, 0, 0, 255, 30);      // blue
  pixels.set(3, 255, 255, 0, 30);    // yellow
  pixels.set(4, 255, 0, 255, 30);    // purple
  pixels.set(5, 0, 255, 255, 30);    // cyan
  pixels.set(6, 255, 120, 0, 30);    // orange
  pixels.set(7, 255, 255, 255, 30);  // white
  pixels.show();
  delay(2500);

  // -------------------------------------------------- 5. alert sequence
  Serial.println("5. Testing ALERT sequence");

  for (int x = 0; x < 3; x++) {
    for (int i = 0; i < 8; i++) {
      pixels.set(i, 255, 0, 0, 40);
    }
    pixels.show();
    vibro.on(250, MAXIMUM);
    delay(300);

    pixels.clear();
    pixels.show();
    delay(300);
  }

  pixels.clear();
  pixels.show();
  vibro.off();

  Serial.println("------- TEST COMPLETE -------");
  Serial.println("Restarting in 3 seconds...");
  delay(3000);
}
