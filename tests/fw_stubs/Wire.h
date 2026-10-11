#pragma once
#include "Arduino.h"
class TwoWire {
 public:
  bool begin();
  bool begin(int sda, int scl, uint32_t freq = 0);
  void setSDA(int);
  void setSCL(int);
  void setClock(uint32_t);
};
extern TwoWire Wire;
