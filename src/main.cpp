#include <Arduino.h>
#include <SPI.h>
#include <Ethernet.h>
#include <EthernetUdp.h>
#include <cstring>
#include <WiFi.h>
#include <Preferences.h>
#include <esp_sntp.h>
#include <atomic>
#include <ctime>
#include "wake_schedule.h"
#include "boot_test.h"
#if __has_include("secrets.h")
#include "secrets.h"
#else
// Allows compiling without embedding real credentials. Daily wakes need NTP.
#define WOL_WIFI_SSID ""
#define WOL_WIFI_PASSWORD ""
#endif

#ifndef WOL_EVERY_BOOT_TEST
#define WOL_EVERY_BOOT_TEST 0
#endif

namespace {
constexpr int kSck = 18;
constexpr int kMiso = 19;
constexpr int kMosi = 23;
constexpr int kCs = 5;
constexpr uint16_t kPort = 9;
constexpr uint32_t kIntervalMs = 10000;
constexpr uint8_t kTargetMac[] = {0x38, 0xD5, 0x47, 0x14, 0xCC, 0x28};
// Locally administered, unicast MAC; distinct from the desktop's MAC.
uint8_t bridgeMac[] = {0x02, 0x00, 0x00, 0x00, 0x77, 0x02};
const IPAddress kLocalIp(192, 168, 77, 2);
const IPAddress kSubnet(255, 255, 255, 0);
const IPAddress kNoAddress(0, 0, 0, 0);
const IPAddress kBroadcast(255, 255, 255, 255);
uint8_t payload[6 + 16 * sizeof(kTargetMac)];
static_assert(sizeof(payload) == 102, "WoL payload must be 102 bytes");
EthernetUDP udp;
bool ethernetReady = false;
uint32_t lastAttemptMs = 0;
WakeSchedule schedule;
std::atomic<bool> clockSynced{false};
char command[48];
size_t commandLength = 0;
bool discardCommand = false;
bool wasActive = false;

void onTimeSync(struct timeval*) { clockSynced.store(true); }

bool saveBootTest(unsigned seconds) {
  Preferences preferences;
  if (!preferences.begin("wol-test", false)) return false;
  bool ok;
  if (seconds) {
    ok = preferences.putUInt("delay", seconds) == sizeof(uint32_t);
  } else {
    ok = !preferences.isKey("delay") || preferences.remove("delay");
  }
  preferences.end();
  return ok;
}

void restoreBootTest() {
  Preferences preferences;
  if (!preferences.begin("wol-test", false)) {
    Serial.println("FAIL: cannot read one-shot power-on test storage");
    return;
  }
  const uint32_t seconds = consumeBootTest(preferences);
  preferences.end();
  if (seconds) {
    schedule.armTest(millis(), seconds * 1000U);
    Serial.printf("One-shot power-on test: starts in %u seconds, lasts 600 seconds; stored request consumed\n",
                  static_cast<unsigned>(seconds));
  }
}

void readSerialCommands(uint32_t now) {
  // Bounded input handling leaves scheduling responsive, even with serial spam.
  for (unsigned count = 0; count < 64 && Serial.available(); ++count) {
    const char c = static_cast<char>(Serial.read());
    if (c == '\r') continue;
    if (c == '\n') {
      command[commandLength] = '\0';
      unsigned seconds = 0;
      char extra = 0;
      if (discardCommand) {
        Serial.println("Command too long; ignored");
      } else if (strcmp(command, "cancel") == 0) {
        schedule.cancelTest();
        Serial.println(saveBootTest(0) ? "Current/pending boot test cancelled; daily schedule remains enabled"
                                     : "Current test cancelled; FAIL clearing stored boot test");
      } else if (sscanf(command, "test-on-boot %u %c", &seconds, &extra) == 1 &&
                 seconds >= 1 && seconds <= 3600) {
        if (saveBootTest(seconds)) {
          schedule.cancelTest();
          Serial.printf("Saved one-shot test: next power-on starts a %u-second countdown; unplug now and move to USB supply\n", seconds);
        } else {
          Serial.println("FAIL: could not save one-shot test");
        }
      } else if (sscanf(command, "test %u %c", &seconds, &extra) == 1 &&
                 seconds >= 1 && seconds <= 3600) {
        schedule.armTest(now, seconds * 1000U);
        Serial.printf("Test armed: starts in %u seconds, lasts 600 seconds; use Linux test mode\n", seconds);
      } else if (commandLength) {
        Serial.println("Commands: test <seconds> | test-on-boot <seconds> | cancel (1..3600 seconds)");
      }
      commandLength = 0;
      discardCommand = false;
    } else if (commandLength < sizeof(command) - 1) {
      command[commandLength++] = c;
    } else {
      discardCommand = true;
    }
  }
}

void sendMagicPacket() {
  Serial.println("WoL send attempt: 102 bytes to 255.255.255.255:9, target 38:D5:47:14:CC:28");
  if (!ethernetReady) {
    Serial.println("FAIL: Ethernet/UDP initialization failed; packet not sent");
    return;
  }
  if (udp.beginPacket(kBroadcast, kPort) != 1) {
    Serial.println("FAIL: UDP beginPacket");
    return;
  }
  const size_t written = udp.write(payload, sizeof(payload));
  if (written != sizeof(payload)) {
    Serial.printf("FAIL: UDP buffered only %u/102 bytes; discarding packet\n",
                  static_cast<unsigned>(written));
    udp.stop();
    ethernetReady = udp.begin(kPort) == 1;
    return;
  }
  if (udp.endPacket() != 1) {
    Serial.println("FAIL: UDP endPacket");
    return;
  }
  Serial.println("SUCCESS: W5100 reports UDP sent (receipt/wake is not confirmed)");
}
}  // namespace

void setup() {
  Serial.begin(115200);
  Serial.println("\nDaily wired W5100 Wake-on-LAN bridge");
  Serial.println("SPI: SCK=18 MISO=19 MOSI=23 CS=5; RESET/INT unconnected");
  memset(payload, 0xFF, 6);
  for (size_t i = 0; i < 16; ++i) {
    memcpy(payload + 6 + i * sizeof(kTargetMac), kTargetMac, sizeof(kTargetMac));
  }

  SPI.begin(kSck, kMiso, kMosi, kCs);
  Ethernet.init(kCs);
  Serial.println("Initializing Ethernet: MAC=02:00:00:00:77:02, IPv4=192.168.77.2/24");
  // Static configuration: no DHCP, DNS, or gateway is required.
  // This overload returns void; inspect hardware and register readback instead.
  Ethernet.begin(bridgeMac, kLocalIp, kNoAddress, kNoAddress, kSubnet);
  if (Ethernet.hardwareStatus() != EthernetW5100) {
    Serial.println("FAIL: expected W5100 not detected; check power and SPI wiring");
  } else if (Ethernet.localIP() != kLocalIp || Ethernet.subnetMask() != kSubnet) {
    Serial.println("FAIL: Ethernet static IPv4 configuration readback mismatch");
  } else {
    Serial.println("Ethernet initialization OK: W5100 detected, static IPv4 read back");
    Serial.println("W5100 link status is unavailable through this API; verify cable/link LEDs and tcpdump");
    ethernetReady = udp.begin(kPort) == 1;
    Serial.println(ethernetReady ? "UDP initialization OK (local port 9)"
                                 : "FAIL: UDP socket initialization");
  }
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  sntp_set_time_sync_notification_cb(onTimeSync);
  configTzTime(kHelsinkiTimezone, "pool.ntp.org", "time.cloudflare.com");
  if (strlen(WOL_WIFI_SSID)) {
    WiFi.begin(WOL_WIFI_SSID, WOL_WIFI_PASSWORD);
    Serial.println("Wi-Fi connecting for NTP; daily wakes wait for first synchronization");
  } else {
    Serial.println("Wi-Fi credentials absent: copy include/secrets.example.h to include/secrets.h");
  }
#if WOL_EVERY_BOOT_TEST
  schedule.armTest(millis(), 30000U);
  Serial.println("EVERY-BOOT TEST: 30-second countdown, then ten minutes of WoL every 10 seconds");
  Serial.println("Repeats on EVERY power-on/reset; no NTP or serial arming required; daily wake disabled");
#else
  Serial.println("Daily window: 13:00-13:10 Europe/Helsinki, every 10 seconds");
  Serial.println("No unconditional startup packet. USB commands: test 30 | test-on-boot 30 | cancel");
  restoreBootTest();
#endif
}

void loop() {
  const uint32_t now = millis();
  readSerialCommands(now);
  const time_t wallTime = time(nullptr);
  tm local{};
  localtime_r(&wallTime, &local);
  const bool validTime = clockSynced.load() && wallTime >= 1704067200;
  const bool active = (!WOL_EVERY_BOOT_TEST && WakeSchedule::dailyActive(local, validTime)) || schedule.testActive(now);
  if (active && (!wasActive || static_cast<uint32_t>(now - lastAttemptMs) >= kIntervalMs)) {
    lastAttemptMs = now;
    sendMagicPacket();
  }
  if (wasActive && !active) Serial.println("Wake window ended; transmissions stopped");
  wasActive = active;

  static uint32_t lastStatusMs = 0;
  if (static_cast<uint32_t>(now - lastStatusMs) >= 30000U) {
    lastStatusMs = now;
    if (validTime) {
      char stamp[40];
      strftime(stamp, sizeof(stamp), "%Y-%m-%d %H:%M:%S %Z", &local);
      Serial.printf("Clock: %s; Wi-Fi: %s; window: %s; next daily start: %s 13:00\n",
                    stamp, WiFi.status() == WL_CONNECTED ? "connected" : "disconnected",
                    active ? "active" : "idle", local.tm_hour < 13 ? "today" : "tomorrow");
    } else {
      Serial.println("Waiting for NTP synchronization; automatic daily wake disabled");
    }
  }
  delay(5);
}
