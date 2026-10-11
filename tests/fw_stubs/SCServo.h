#pragma once
#include "Arduino.h"
typedef char s8;
typedef unsigned char u8;
typedef unsigned short u16;
typedef short s16;
typedef unsigned long u32;
class SCSerial {   // Feetech SCServo library (SCSerial / SMS_STS) public API subset
 public:
  HardwareSerial *pSerial;
  int writeByte(u8 ID, u8 MemAddr, u8 bDat);
  int writeWord(u8 ID, u8 MemAddr, u16 wDat);
};
class SMS_STS : public SCSerial {
 public:
  int WritePosEx(u8 ID, s16 Position, u16 Speed, u8 ACC = 0);
  void SyncWritePosEx(u8 ID[], u8 IDN, s16 Position[], u16 Speed[], u8 ACC[]);
  int EnableTorque(u8 ID, u8 Enable);
  int ReadPos(int ID);
  int ReadLoad(int ID);
  int Ping(u8 ID);
};
