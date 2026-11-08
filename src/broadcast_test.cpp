#include <Arduino.h>
#include <SPI.h>
#include <Ethernet.h>
#include <EthernetUdp.h>
#include <cstring>

namespace {
constexpr uint32_t kIntervalMs = 10000;
constexpr uint16_t kPort = 9;
const uint8_t kTargetMac[] = {0x38, 0xD5, 0x47, 0x14, 0xCC, 0x28};
uint8_t bridgeMac[] = {0x02, 0x00, 0x00, 0x00, 0x77, 0x02};
const IPAddress kLocalIp(192, 168, 77, 2);
const IPAddress kSubnet(255, 255, 255, 0);
const IPAddress kNone(0, 0, 0, 0);
const IPAddress kBroadcast(255, 255, 255, 255);
uint8_t payload[6 + 16 * sizeof(kTargetMac)];
static_assert(sizeof(payload) == 102, "WoL payload must be 102 bytes");
EthernetUDP udp;
bool ready = false;
uint32_t lastAttempt = 0;

void sendPacket() {
  Serial.println("WoL attempt: 102 bytes -> 255.255.255.255:9, target 38:D5:47:14:CC:28");
  if (!ready) {
    Serial.println("FAIL: Ethernet/UDP initialization failed");
    return;
  }
  if (udp.beginPacket(kBroadcast, kPort) != 1) {
    Serial.println("FAIL: UDP beginPacket");
    return;
  }
  if (udp.write(payload, sizeof(payload)) != sizeof(payload)) {
    Serial.println("FAIL: incomplete payload; discarding packet");
    udp.stop();
    ready = udp.begin(kPort) == 1;
    return;
  }
  Serial.println(udp.endPacket() == 1
                     ? "SUCCESS: W5100 reports packet sent (wake not confirmed)"
                     : "FAIL: UDP endPacket");
}
}

void setup() {
  Serial.begin(115200);
  Serial.println("\nBIOS TEST: startup WoL, then every 10 seconds while powered");
  Serial.println("SPI: SCK=18 MISO=19 MOSI=23 CS=5; RESET/INT unconnected");
  Serial.println("No Wi-Fi, NTP, schedule, or stored test requests in this build");
  memset(payload, 0xFF, 6);
  for (size_t i = 0; i < 16; ++i) {
    memcpy(payload + 6 + i * sizeof(kTargetMac), kTargetMac, sizeof(kTargetMac));
  }
  SPI.begin(18, 19, 23, 5);
  Ethernet.init(5);
  Ethernet.begin(bridgeMac, kLocalIp, kNone, kNone, kSubnet);
  if (Ethernet.hardwareStatus() != EthernetW5100) {
    Serial.println("FAIL: W5100 not detected; check power and SPI wiring");
  } else if (Ethernet.localIP() != kLocalIp || Ethernet.subnetMask() != kSubnet) {
    Serial.println("FAIL: static Ethernet configuration readback mismatch");
  } else {
    Serial.println("Ethernet OK: W5100, MAC 02:00:00:00:77:02, IPv4 192.168.77.2/24");
    ready = udp.begin(kPort) == 1;
    Serial.println(ready ? "UDP initialization OK" : "FAIL: UDP initialization");
  }
  lastAttempt = millis();
  sendPacket();
}

void loop() {
  const uint32_t now = millis();
  if (static_cast<uint32_t>(now - lastAttempt) >= kIntervalMs) {
    lastAttempt = now;
    sendPacket();
  }
  delay(1);
}
