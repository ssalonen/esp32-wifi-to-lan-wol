#include "wake_schedule.h"
#include "boot_test.h"
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstdlib>

struct MemoryStore {
  uint32_t pending = 0;
  bool eraseWorks = true;
  uint32_t getUInt(const char*, uint32_t) { return pending; }
  bool remove(const char*) {
    if (!eraseWorks) return false;
    pending = 0;
    return true;
  }
};

int main() {
  MemoryStore flash;
  flash.pending = 30;
  const uint32_t delay = consumeBootTest(flash);
  assert(delay == 30);
  assert(flash.pending == 0);
  assert(consumeBootTest(flash) == 0);  // No replay on another power cycle.
  flash.pending = 30;
  flash.eraseWorks = false;
  assert(consumeBootTest(flash) == 0);  // Never arm if consuming failed.
  flash.eraseWorks = true;
  flash.pending = 3601;
  assert(consumeBootTest(flash) == 0);  // Ignore corrupt/out-of-range storage.
  assert(flash.pending == 0);
  flash.pending = 0;
  assert(consumeBootTest(flash) == 0);
  WakeSchedule schedule;
  tm local{};
  local.tm_hour = 13;
  assert(!WakeSchedule::dailyActive(local, false));
  assert(WakeSchedule::dailyActive(local, true));
  local.tm_min = 6;  // A fresh scheduler does not restart the window.
  assert(WakeSchedule::dailyActive(local, true));
  local.tm_min = 9;
  local.tm_sec = 59;
  assert(WakeSchedule::dailyActive(local, true));
  local.tm_min = 10;
  assert(!WakeSchedule::dailyActive(local, true));
  local.tm_hour = 12;
  local.tm_min = 59;
  assert(!WakeSchedule::dailyActive(local, true));
  assert(!schedule.testActive(100));
  schedule.armTest(100, 60000);
  assert(!schedule.testActive(60099));
  assert(schedule.testActive(60100));
  assert(schedule.testActive(660099));
  assert(!schedule.testActive(660100));
  // Timer remains correct over the ESP32 millis() rollover.
  schedule.armTest(UINT32_MAX - 999, 2000);
  assert(!schedule.testActive(999));
  assert(schedule.testActive(1000));
  schedule.cancelTest();
  assert(!schedule.testActive(2000));
  // Both sides of the Helsinki spring/autumn DST transitions use 13:00 local.
  setenv("TZ", kHelsinkiTimezone, 1);
  tzset();
  const int utcCases[][3] = {{3, 28, 11}, {3, 29, 10}, {10, 24, 10}, {10, 25, 11}};
  for (const auto& item : utcCases) {
    tm utc{};
    utc.tm_year = 126;
    utc.tm_mon = item[0] - 1;
    utc.tm_mday = item[1];
    utc.tm_hour = item[2];
    const time_t epoch = timegm(&utc);
    tm helsinki{};
    localtime_r(&epoch, &helsinki);
    assert(helsinki.tm_hour == 13);
    assert(WakeSchedule::dailyActive(helsinki, true));
  }
  puts("Firmware schedule tests passed");
}
