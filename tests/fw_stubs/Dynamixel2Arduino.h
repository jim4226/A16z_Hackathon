#pragma once
#include "Arduino.h"
enum ParamUnit { UNIT_RAW = 0, UNIT_PERCENT, UNIT_RPM, UNIT_DEGREE, UNIT_MILLI_AMPERE };
enum OperatingMode { OP_CURRENT = 0, OP_VELOCITY = 1, OP_POSITION = 3, OP_EXTENDED_POSITION = 4, OP_CURRENT_BASED_POSITION = 5, OP_PWM = 16 };
namespace ControlTableItem {
enum ControlTableItemIndex { PROFILE_ACCELERATION = 108, PROFILE_VELOCITY = 112, GOAL_POSITION = 116, PRESENT_POSITION = 132 };
}
class Dynamixel2Arduino {   // ROBOTIS Dynamixel2Arduino public API subset
 public:
  Dynamixel2Arduino(HardwareSerial &port, int dir_pin = -1);
  void begin(unsigned long baud = 57600);
  bool setPortProtocolVersion(float version);
  bool ping(uint8_t id = 0xFE);
  bool torqueOn(uint8_t id);
  bool torqueOff(uint8_t id);
  bool setOperatingMode(uint8_t id, uint8_t mode);
  bool setGoalPosition(uint8_t id, float value, uint8_t unit = UNIT_RAW);
  bool setGoalPWM(uint8_t id, float value, uint8_t unit = UNIT_RAW);
  float getPresentPosition(uint8_t id, uint8_t unit = UNIT_RAW);
  bool writeControlTableItem(uint8_t item_idx, uint8_t id, int32_t data, uint32_t timeout = 100);
};
