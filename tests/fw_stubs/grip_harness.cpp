// Appended to a generated Feetech morph_arm.ino by tests/test_robot_grip_force.py: the Arduino and SCServo calls the
// sketch makes, answered and recorded, then O, G and 30 s of 10 ms control ticks with a force sensor that reads
// argv[1] newtons per unit the jaw closes past 0 (0: no sensor fitted, or no reading). Prints one RESULT line.
#include <cstdio>
static unsigned long _ms = 0;
unsigned long millis() { return _ms; }
void delay(unsigned long d) { _ms += d; }
void pinMode(int, int) {}
void digitalWrite(int, int) {}
int digitalRead(int) { return HIGH; }
float SQUEEZE_N_PER_UNIT = 0.0f;   // force sensor model: 0 = nothing fitted (or wired); else N per unit closed past 0
int analogRead(int pin) {
  if (pin == PIN_VSERVO_SENSE) return (int)(VSERVO_NOMINAL / VSERVO_DIVIDER / ADC_VREF * ADC_MAX);
  float f = -gC * SQUEEZE_N_PER_UNIT;
  if (f <= 0.0f) return 0;
  float r = FSR_K / f, v = ADC_VREF * FSR_R_DIV / (FSR_R_DIV + r);
  return (int)(v / ADC_VREF * ADC_MAX);
}
void analogReadResolution(int) {}
size_t Print::print(const char *s) { fputs(s, stdout); return 0; }
size_t Print::print(char c) { fputc(c, stdout); return 0; }
size_t Print::print(int v) { printf("%d", v); return 0; }
size_t Print::print(unsigned int v) { printf("%u", v); return 0; }
size_t Print::print(long v) { printf("%ld", v); return 0; }
size_t Print::print(unsigned long v) { printf("%lu", v); return 0; }
size_t Print::print(float v, int d) { printf("%.*f", d, v); return 0; }
size_t Print::print(double v, int d) { printf("%.*f", d, v); return 0; }
size_t Print::println(const char *s) { printf("%s\n", s); return 0; }
size_t Print::println(int v) { printf("%d\n", v); return 0; }
size_t Print::println(unsigned long v) { printf("%lu\n", v); return 0; }
size_t Print::println(float v, int d) { printf("%.*f\n", d, v); return 0; }
size_t Print::println(double v, int d) { printf("%.*f\n", d, v); return 0; }
size_t Print::println() { printf("\n"); return 0; }
void HardwareSerial::begin(unsigned long, uint32_t, int8_t, int8_t) {}
bool HardwareSerial::setPins(int8_t, int8_t, int8_t, int8_t) { return true; }
void HardwareSerial::setTX(int) {}
void HardwareSerial::setRX(int) {}
int HardwareSerial::available() { return 0; }
int HardwareSerial::read() { return -1; }
size_t HardwareSerial::write(uint8_t c) { fputc(c, stdout); return 1; }
void HardwareSerial::flush() {}
HardwareSerial::operator bool() const { return true; }
HardwareSerial Serial, Serial1, Serial2;
static int torque_writes = 0;
static unsigned last_torque = 0;
int SCSerial::writeByte(u8, u8, u8) { return 0; }
int SCSerial::writeWord(u8 id, u8 addr, u16 v) { if (id == GRIP_ID && addr == 48) { torque_writes++; last_torque = v; } return 0; }
int SMS_STS::WritePosEx(u8, s16, u16, u8) { return 0; }
void SMS_STS::SyncWritePosEx(u8 *, u8, s16 *, u16 *, u8 *) {}
int SMS_STS::EnableTorque(u8, u8) { return 0; }
int SMS_STS::ReadPos(int) { return -1; }
int SMS_STS::ReadLoad(int) { return 0; }
int SMS_STS::Ping(u8) { return 0; }

int main(int argc, char **argv) {
  SQUEEZE_N_PER_UNIT = argc > 1 ? (float)atof(argv[1]) : 0.0f;
  setup();
  char o[] = "O"; command(o);
  for (int i = 0; i < 500; i++) { _ms += 10; tick(0.01f); }
  char g[] = "G"; command(g);
  float lowest = gC;
  for (int i = 0; i < 3000; i++) { _ms += 10; tick(0.01f); if (gC < lowest) lowest = gC; }
  printf("\nRESULT lowest=%.4f final=%.4f closed=%.4f min=%.4f state=%s force=%.3f torque_writes=%d torque=%u\n",
         lowest, gC, GRIP_CLOSED, GRIP_MIN, GRIP_NAME[gstate], force, torque_writes, last_torque);
  return 0;
}
