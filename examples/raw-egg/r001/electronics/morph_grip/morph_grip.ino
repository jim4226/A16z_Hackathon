// Morph force-limited gripper firmware, generated for raw-egg (see results.json)
// Arduino Nano (ATmega328P) + TowerPro MG996R (standard, metal gear) + fsr402
// Serial (115200): g = grip, o = open, t<N> = set target newtons, c<grams> = calibrate with a known mass
// Streams CSV at 50 Hz: t_ms,force_n,angle_deg,state
#include <Servo.h>

const int SERVO_PIN = 9;
const int FSR_PIN = A0;
const float VCC = 5.0;
const float ADC_MAX = 1023.0;
const float R_DIV = 3900.0;          // ohms, divider resistor to GND
float K_FSR = 10000.0;                // ohm*N: F = K_FSR / R_fsr (calibrate with c<grams>)
float TARGET_N = 2.34;            // from Morph: inside the window 1.17 - 16.0 N
const float LIMIT_N = 19.0;        // never exceed (object damage / sensor range)
const int OPEN_DEG = 14;      // from the generated rack-and-pinion: jaws fully open
const int CLOSED_DEG = 106;  // contact at 90 deg + overtravel; slots stop here (swap if your servo turns the other way)
const float STEP_DEG = 0.5;           // closing speed per 20 ms tick

Servo servo;
float angle = OPEN_DEG;
enum State { OPEN, CLOSING, HOLD } state = OPEN;
const char* NAMES[] = {"OPEN", "CLOSING", "HOLD"};

float readForce() {
  float v = analogRead(FSR_PIN) * VCC / ADC_MAX;
  if (v < 0.01) return 0.0;
  float r_fsr = R_DIV * (VCC - v) / v;
  return K_FSR / r_fsr;
}

void setup() {
  Serial.begin(115200);
  servo.attach(SERVO_PIN);
  servo.write(OPEN_DEG);
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == 'g') state = CLOSING;
    if (c == 'o') { state = OPEN; angle = OPEN_DEG; }
    if (c == 't') TARGET_N = min(Serial.parseFloat(), LIMIT_N);
    if (c == 'c') {                       // known mass resting on the sensor
      float grams = Serial.parseFloat();
      float v = analogRead(FSR_PIN) * VCC / ADC_MAX;
      if (v > 0.01) K_FSR = (grams / 1000.0 * 9.81) * (R_DIV * (VCC - v) / v);
      Serial.print("# K_FSR="); Serial.println(K_FSR);
    }
  }
  float f = readForce();
  int dir = CLOSED_DEG > OPEN_DEG ? 1 : -1;
  if (f > LIMIT_N) { angle -= dir * 3 * STEP_DEG; state = HOLD; }     // back off hard
  else if (state == CLOSING) {
    if (f >= TARGET_N) state = HOLD; else angle += dir * STEP_DEG;
  } else if (state == HOLD) {                                         // small bang-bang trim
    if (f < TARGET_N * 0.85) angle += dir * STEP_DEG * 0.5;
    if (f > TARGET_N * 1.15) angle -= dir * STEP_DEG * 0.5;
  }
  angle = constrain(angle, min(OPEN_DEG, CLOSED_DEG), max(OPEN_DEG, CLOSED_DEG));
  servo.write((int)angle);
  Serial.print(millis()); Serial.print(',');
  Serial.print(f, 3); Serial.print(',');
  Serial.print(angle, 1); Serial.print(',');
  Serial.println(NAMES[state]);
  delay(20);
}
