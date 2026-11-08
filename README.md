# Daily wired Wake-on-LAN backup

ESP32-WROOM (`esp32dev`) + Keyestudio KS0148 W5100 wake a Linux desktop daily
at **13:00 Europe/Helsinki**, including daylight-saving time. Wi-Fi supplies
NTP time; magic packets travel over the dedicated Ethernet cable.

The original wired POC was built and its correct magic packet was captured.
The earlier apparent wake result was a false positive: the desktop may also
power on because of BIOS settings without receiving WoL. Actual magic-packet
waking and the scheduling/backup extension still require controlled testing.


## Repeatable 30-second power-on test

For testing without rearming over serial, flash the dedicated build:

```sh
pio run -e every-boot-test -t upload --upload-port /dev/ttyUSB0
```

On **every power-on or reset**, it waits 30 seconds after initialization,
then sends WoL every ten seconds for ten minutes. Power-cycle it to repeat.
No serial command or successful Wi-Fi/NTP connection is required. Wi-Fi still
uses local secrets for clock synchronization, but the daily wake schedule is
disabled in this build so it cannot bypass the test countdown. Stored one-shot
requests are ignored without being consumed. `cancel` stops the current test;
the next reboot starts another countdown.

Keep Linux test mode enabled for an off-schedule boot. With dry-run enabled,
Linux logs the backup/shutdown decisions and stays powered on. Log in during
the grace period to cancel automation, or leave it logged out to test the
unattended flow. Restore daily operation by uploading environment `esp32dev`.

## Continuous broadcast build for BIOS testing

Build and upload the independent, minimal Ethernet-only test firmware:

```sh
pio run -e broadcast-test
pio run -e broadcast-test -t upload --upload-port /dev/ttyUSB0
pio device monitor -b 115200 --port /dev/ttyUSB0
```

It sends the same 102-byte magic packet at startup and every **ten seconds**
indefinitely while powered. Moving it to a USB power supply needs no serial
command: it starts automatically on each power-on. No Wi-Fi, time sync,
schedule, credentials or saved test request is used by this build.

Disable the Linux backup service during BIOS validation so it cannot interfere:
`sudo systemctl disable --now wol-backup.service`. First test shutdown with
ESP32 unpowered/Ethernet disconnected to establish whether the desktop wakes
without packets. Then test the powered broadcaster. A wake on cable insertion
or restored power alone is not proof of magic-packet waking. This continuous
build cannot separate link-up from packet-triggered wakes on its own.

Restore daily firmware with:

```sh
pio run -e esp32dev -t upload --upload-port /dev/ttyUSB0
```

`esp32dev` remains the default environment. The broadcaster does not consume
or clear any previously saved `test-on-boot` request; use `cancel` over serial
in the daily firmware before switching builds if a pending request remains.

## Wiring

| ESP32 | W5100 |
| --- | --- |
| GPIO18 → | CK / SCK |
| GPIO19 ← | MI / MISO |
| GPIO23 → | MO / MOSI |
| GPIO5 → | SS / CS |
| 5V/VIN → | +5 |
| GND → | G |

RESET and INT remain unconnected. ESP32 is separately USB-powered and must
remain powered while the desktop is off. Connect W5100 RJ45 directly to the
desktop's onboard NIC `enp0s31f6`.

SPI initialization explicitly uses SCK=18, MISO=19, MOSI=23, CS=5.
The standard Arduino [Ethernet 2.0.2](https://github.com/arduino-libraries/Ethernet/tree/2.0.2)
library supports W5100 with ESP32 Arduino 2.0.7; both Ethernet and Wi-Fi are
compiled together. Espressif32 platform 6.1.0 is pinned. No alternative Ethernet
library is needed.

## Firmware setup

Create your local `include/secrets.h` from the example, then edit it with
your 2.4 GHz Wi-Fi credentials. Do not overwrite an already configured copy:

```sh
cp include/secrets.example.h include/secrets.h
chmod 600 include/secrets.h
```

`include/secrets.h` is git-ignored. Credentials are compiled into the firmware;
they are not printed by the application. Without this file the firmware still
builds, but automatic daily waking remains disabled until it can sync NTP.

```sh
pio run
pio device list
pio run -t upload --upload-port /dev/ttyUSB0
pio device monitor -b 115200 --port /dev/ttyUSB0
```

The general upload and monitor commands are `pio run -t upload` and
`pio device monitor -b 115200`. Specify the actual USB port if autodetection
picks `/dev/ttyS0`. If `pio` is not on PATH, use
`~/.platformio/penv/bin/pio` in its place.

The bridge uses MAC `02:00:00:00:77:02`, IPv4 `192.168.77.2/24`, no gateway/DNS,
and broadcasts to `255.255.255.255:9`. The desktop needs no IPv4 address on
this isolated link. Payload is exactly 102 bytes: six `ff` bytes followed by
`38:d5:47:14:cc:28` repeated 16 times.

There is **no unconditional startup packet**. After the first NTP sync, the
bridge sends every ten seconds during **13:00 inclusive to 13:10 exclusive**.
Reboot at 13:06 uses the remaining four minutes; reboot at 13:15 waits until
tomorrow. Once synchronized, an unavailable Wi-Fi network does not stop the
running clock; after power loss a new synchronization is required. The firmware
uses the current EU DST rules for Helsinki. Status is printed every 30 seconds.

USB serial commands, terminated by Enter:

```text
test 30
test-on-boot 30
cancel
```

`test 30` arms a **ten-minute** packet burst starting 30 seconds later (allowed
delay: 1–3600 seconds). It works without NTP, uses elapsed time, and leaves the
daily schedule unchanged. This immediate test is lost when power is removed.

For moving the board from the computer to a USB power supply, use
**`test-on-boot 30`** instead. Wait for the “Saved one-shot test” confirmation,
then unplug it directly while the monitor remains open. Connect it to the USB
power supply: the next boot starts the **30-second countdown**, followed by the
ten-minute burst. The pending request uses ESP32 NVS flash and is consumed
before arming, so further power cycles do not replay it. A reset also counts as
a boot; avoid resetting/reopening the monitor between arming and moving power.
Power loss after consumption cancels that test; arm it again if needed.
`cancel` stops an active test and clears a pending next-boot request; it does
not disable the daily schedule. These commands do not change
Linux configuration or overrides a login guard. There are no Wi-Fi control APIs,
OTA updates, or NAS credentials in the ESP32.

## Linux behavior

The root systemd service captures raw Ethernet frames on `enp0s31f6`, so no
IPv4 configuration is required. It validates the bridge MAC/IP, broadcast
address, UDP port 9, and complete target payload. This is identification on an
isolated link, not cryptographic authentication.

The waking packet arrives before Linux can capture it; a subsequent packet is
the trigger. Eligibility requires a matching packet within the **first ten
minutes of system uptime**, and during 13:00–13:10 Helsinki time. Test mode
removes only the time-of-day restriction and shortens the login grace.

1. Wait **five minutes after detection** for a human login (test mode: 30 seconds).
2. Any human login before backup starts cancels both backup and shutdown.
3. Otherwise, run `/usr/local/bin/nas-backup-pull.sh` as root without interactive
   `sudo`. Its stdout/stderr and exit status appear in the journal. The script
   must run in the foreground until the backup is actually finished.
4. Any login during/after backup permanently cancels shutdown for that boot;
   an already running backup is allowed to finish.
5. After backup exits, **including nonzero exit or failure to launch**, request
   normal poweroff if the guards permit it. There is no backup retry.

The service watches logind `SessionNew` events and checks existing sessions;
a brief login followed by logout still cancels automation. Graphical, terminal,
SSH and root sessions count; only sessions explicitly classified as `greeter`
are excluded. A lock screen does not make an existing user session disappear.
Unknown session state or failed monitoring cancels automation.

Shutdown waits at least **ten minutes + 15 seconds from the first captured
packet**, at least 15 seconds since the last matching packet, and outside the
daily transmit window plus a margin. This conservatively overestimates the
remaining burst. A fast backup can therefore finish well before shutdown.
Even in test mode, the complete wake/holdoff test takes roughly 10–11 minutes,
not 24 hours. Login checks continue throughout this wait and run again before
poweroff. Shutdown inhibitors are respected. A login exactly concurrent with
the final poweroff request remains a small race; this does not lock out logins.

At most one automation attempt is permitted per boot. `/run/wol-backup/claimed`
persists across service restarts and clears on reboot. A daemon failure does
not cause a restart or a second backup. Starting it after the first ten minutes
of uptime will not retroactively trigger a backup.

A manual boot during the window can qualify, as agreed; logging in prevents
shutdown. Linux also needs a reasonably correct clock for the production time
window. Keep WoL enabled on the NIC (`Wake-on: g`) and in firmware/BIOS; the
service does not alter those settings.

## Install Linux files

Requirements: systemd/logind, Python **3.9+** with timezone data for
`Europe/Helsinki`, `loginctl`, `systemctl` supporting `--check-inhibitors=yes`,
`dbus-monitor`, and coreutils `stdbuf`. No Python packages are required.
SSH/console/desktop login must create logind sessions (normal PAM/systemd setup).
Untracked custom login mechanisms cannot be guarded.

From this project directory on the desktop:

```sh
sudo install -d -m 755 /usr/local/lib/wol-backup
sudo install -m 644 linux/wol_backup.py /usr/local/lib/wol-backup/wol_backup.py
sudo install -m 644 linux/wol-backup.service /etc/systemd/system/wol-backup.service
sudo install -m 644 linux/wol-backup.default /etc/default/wol-backup
sudo systemd-analyze verify /etc/systemd/system/wol-backup.service
sudo systemctl daemon-reload
sudo systemctl enable wol-backup.service
```

These commands enable it for the **next boot**; they do not start it on the
current interactive session. The service runs as root and becomes ready before
ordinary login services. Startup also waits for the named Ethernet device; a
30-second startup job timeout prevents a missing NIC from delaying logins
indefinitely. Verify your actual login units respect this ordering
if you have a custom setup. No dependency on `network-online.target` is needed:
the service brings `enp0s31f6` administratively up before starting capture.
It needs Ethernet carrier for capture, but no wired IPv4 address. Transient
`ENETDOWN` errors wait rather than aborting the boot attempt, and a down link
never counts as packet silence for shutdown. Keep the GNOME Ethernet switch
on with IPv4/IPv6 disabled to avoid DHCP attempts. Wi-Fi remains the route to
the NAS; the root backup script must handle any NAS/network failure as usual.

The shipped `/etc/default/wol-backup` contains:

```sh
WOL_BACKUP_TEST_MODE=0
WOL_BACKUP_DRY_RUN=1
```

**Dry-run replaces the real backup with `/bin/true` and only logs poweroff.**
After validation, edit it with `sudoedit /etc/default/wol-backup` and set
`WOL_BACKUP_DRY_RUN=0` to enable both the real root backup and real shutdown.
Use `WOL_BACKUP_TEST_MODE=1` for off-schedule tests and the 30-second grace;
return it to `0` for the normal daily schedule. Configuration is read when the
service starts, so make changes before the test boot.

Inspect results after logging in:

```sh
sudo journalctl -b -u wol-backup.service --no-pager
sudo journalctl -b -1 -u wol-backup.service --no-pager
```

The second command needs persistent journaling to inspect the previous boot.
To stop and disable automation:

```sh
sudo systemctl disable --now wol-backup.service
```

## Hardware and end-to-end testing

First keep the desktop powered on and capture a test packet:

```sh
sudo tcpdump -ni enp0s31f6 -c 1 -s 200 -vvv -XX 'udp dst port 9'
```

Enter `test 30` over USB serial. Expect UDP payload length 102, beginning with
six `ff` bytes followed by repeated `38:d5:47:14:cc:28`. The hex dump includes
Ethernet/IP/UDP headers before the magic payload. A successful firmware send
only confirms W5100 transmission; capture confirms delivery. W5100 physical
link status is unavailable through the library API.

For an unattended boot test without waiting until tomorrow:

1. Install the Linux files, enable the service, set test mode `1` and leave
   dry-run `1`. Upload the new firmware with your local Wi-Fi settings.
2. Send `test-on-boot 30` over serial and wait for the save confirmation.
   Unplug the ESP32 directly from the computer. Shut down the desktop, then
   plug the ESP32 into the separate USB power supply. It starts sending after
   30 seconds. Keep it powered throughout the ten-minute burst. No serial
   connection or Wi-Fi/NTP synchronization is required for this test.
3. Let the desktop wake without logging in. Wait roughly 11 minutes. It should
   remain on in dry-run. Then log in and inspect the journal for detection,
   30-second grace, dry-run backup and “would request systemctl poweroff”.
4. Repeat with dry-run `0` to exercise the actual root backup and shutdown.
   Configure before shutting down; this explicitly runs your backup script.
5. Test login during the grace: neither backup nor poweroff should occur.
   Test login during backup/holdoff: backup may finish, poweroff must not occur,
   including if that session logs out again. Remain logged out during the
   unattended test, including via SSH; checking logs over SSH cancels it.
6. Backup failure is covered by automated policy tests. To test on hardware,
   use the backup script's own supported failure/test mechanism if available;
   this project does not overwrite the existing backup script.
7. Restore test mode `0` and dry-run `0` for daily operation. Check NTP clock
   logs before relying on the schedule.

## Development checks

```sh
sh tests/run.sh
pio run
systemd-analyze verify linux/wol-backup.service
```

Host C++ tests cover schedule boundaries, reboot timing and timer rollover;
Python tests cover exact packets, eligibility, grace, login cancellation,
backup failure, transmission holdoff, session events and once-per-boot claims.
They do not invoke the actual backup or poweroff.

The updated firmware was built successfully with PlatformIO Core 6.1.19,
Espressif32 6.1.0, Arduino ESP32 2.0.7 and Ethernet 2.0.2. Development uses a
writable temporary PlatformIO core pointing at installed packages:

```sh
PLATFORMIO_CORE_DIR=/tmp/wol-pio-core PATH=/home/salski/.platformio/penv/bin:$PATH pio run
```

No live backup service has been installed/enabled, firmware uploaded, real
backup run, or shutdown performed by the development checks. The new logind,
boot ordering, Wi-Fi/NTP, and end-to-end behavior still need the hardware tests
above.

Development verification: host C++ assertions and **18 Python tests pass**,
including blocked login classification and packet arrival during final shutdown
checks. The Ethernet-startup fix adds tests for a down interface and deferred
packet capture; the current Python suite contains 21 tests. The updated systemd
unit passed `systemd-analyze verify`. Hardware end-to-end testing is still required.

After updating the Linux listener or unit, reinstall both files and reload
systemd before the next test boot:

```sh
sudo install -m 644 linux/wol_backup.py /usr/local/lib/wol-backup/wol_backup.py
sudo install -m 644 linux/wol-backup.service /etc/systemd/system/wol-backup.service
sudo systemctl daemon-reload
```

Do not reset the once-per-boot claim to retry after a failed boot; reboot for a
clean test. The ESP32 every-boot-test firmware needs no changes for this fix.
