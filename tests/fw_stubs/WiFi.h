#pragma once
#include "Arduino.h"
class WiFiClient : public Stream {
 public:
  int available() override;
  int read() override;
  size_t write(uint8_t) override;
  uint8_t connected();
  operator bool();
};
class WiFiServer {
 public:
  WiFiServer(uint16_t port);
  void begin();
  WiFiClient available();
};
class WiFiClass {
 public:
  bool softAP(const char *ssid, const char *pass = nullptr);
  int begin(const char *ssid, const char *pass);
};
extern WiFiClass WiFi;
