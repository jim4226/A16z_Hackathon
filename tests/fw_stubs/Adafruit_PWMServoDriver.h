#pragma once
#include "Arduino.h"
#include "Wire.h"
class Adafruit_PWMServoDriver {   // signatures as in Adafruit PWM Servo Driver Library 3.x
 public:
  Adafruit_PWMServoDriver(const uint8_t addr = 0x40, TwoWire &i2c = Wire);
  bool begin(uint8_t prescale = 0);
  void setOscillatorFrequency(uint32_t freq);
  void setPWMFreq(float freq);
  uint8_t setPWM(uint8_t num, uint16_t on, uint16_t off);
  void writeMicroseconds(uint8_t num, uint16_t Microseconds);
};
