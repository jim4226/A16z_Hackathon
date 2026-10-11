// Minimal Arduino API stub so generated sketches can be checked with `g++ -fsyntax-only` (tests only).
#pragma once
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <math.h>
#define HIGH 1
#define LOW 0
#define INPUT 0
#define OUTPUT 1
#define INPUT_PULLUP 2
#define SERIAL_8N1 0x800001c
#define LED_BUILTIN 13
#define A0 14
#define A1 15
#define A2 16
#define A3 17
#define A4 18
#define A5 19
#define SDA 20
#define SCL 21
template <class T, class L, class H> T constrain(T x, L lo, H hi) { return x < lo ? (T)lo : (x > hi ? (T)hi : x); }
unsigned long millis();
void delay(unsigned long);
void pinMode(int, int);
void digitalWrite(int, int);
int digitalRead(int);
int analogRead(int);
void analogReadResolution(int);
class Print {
 public:
  virtual size_t write(uint8_t) = 0;
  size_t print(const char *);
  size_t print(char);
  size_t print(int);
  size_t print(unsigned int);
  size_t print(long);
  size_t print(unsigned long);
  size_t print(float, int = 2);
  size_t print(double, int = 2);
  size_t println(const char *);
  size_t println(int);
  size_t println(unsigned long);
  size_t println(float, int = 2);
  size_t println(double, int = 2);
  size_t println();
};
class Stream : public Print {
 public:
  virtual int available() = 0;
  virtual int read() = 0;
};
class HardwareSerial : public Stream {
 public:
  void begin(unsigned long, uint32_t = SERIAL_8N1, int8_t rx = -1, int8_t tx = -1);
  bool setPins(int8_t rx, int8_t tx, int8_t cts = -1, int8_t rts = -1);
  void setTX(int);
  void setRX(int);
  int available() override;
  int read() override;
  size_t write(uint8_t) override;
  void flush();
  operator bool() const;
};
extern HardwareSerial Serial;
extern HardwareSerial Serial1;
extern HardwareSerial Serial2;
