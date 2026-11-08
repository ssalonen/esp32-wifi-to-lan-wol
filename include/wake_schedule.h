#pragma once
#include <cstdint>
#include <ctime>

// POSIX TZ equivalent to Europe/Helsinki under current EU DST rules.
constexpr char kHelsinkiTimezone[] = "EET-2EEST,M3.5.0/3,M10.5.0/4";

class WakeSchedule {
 public:
  static bool dailyActive(const tm& local, bool clockValid) {
    return clockValid && local.tm_hour == 13 && local.tm_min < 10;
  }
  void armTest(uint32_t now, uint32_t delayMs) {
    armed_ = true;
    armedAt_ = now;
    delayMs_ = delayMs;
  }
  bool testActive(uint32_t now) {
    if (!armed_) return false;
    const uint32_t elapsed = now - armedAt_;
    if (elapsed >= delayMs_ + 600000U) {
      armed_ = false;
      return false;
    }
    return elapsed >= delayMs_;
  }
  void cancelTest() { armed_ = false; }

 private:
  bool armed_ = false;
  uint32_t armedAt_ = 0;
  uint32_t delayMs_ = 0;
};
