#!/usr/bin/env python3
"""Boot-scoped wired WoL backup runner. Requires root, logind and dbus-monitor."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import errno
import logging
import os
from pathlib import Path
import re
import select
import socket
import struct
import subprocess
import threading
import time
from zoneinfo import ZoneInfo

LOG = logging.getLogger('wol-backup')
ZONE = ZoneInfo('Europe/Helsinki')
BACKUP = '/usr/local/bin/nas-backup-pull.sh'
BRIDGE_MAC = bytes.fromhex('020000007702')
TARGET_MAC = bytes.fromhex('38d54714cc28')
MAGIC = b'\xff' * 6 + TARGET_MAC * 16
BOOT_WINDOW = 600
TRANSMIT_WINDOW = 600
QUIET_MARGIN = 15


def valid_magic_frame(frame):
    """Accept only the untagged IPv4 UDP broadcast emitted by our bridge."""
    if len(frame) < 42 or frame[:6] != b'\xff' * 6:
        return False
    if frame[6:12] != BRIDGE_MAC or frame[12:14] != b'\x08\x00':
        return False
    ip = frame[14:]
    ihl = (ip[0] & 15) * 4
    if ip[0] >> 4 != 4 or ihl < 20 or len(ip) < ihl + 8 or ip[9] != 17:
        return False
    total = struct.unpack_from('!H', ip, 2)[0]
    fragment = struct.unpack_from('!H', ip, 6)[0]
    if fragment & 0x3fff or total != ihl + 8 + 102 or len(ip) < total:
        return False
    if ip[12:16] != bytes([192, 168, 77, 2]) or ip[16:20] != b'\xff' * 4:
        return False
    source, dest, length = struct.unpack_from('!HHH', ip, ihl)
    return (source == 9 and dest == 9 and length == 110
            and ip[ihl + 8:total] == MAGIC)


def eligible_trigger(uptime, now, test_mode):
    return 0 <= uptime < BOOT_WINDOW and (test_mode or (now.hour == 13 and now.minute < 10))


def daily_shutdown_blocked(now):
    seconds = now.hour * 3600 + now.minute * 60 + now.second
    return 13 * 3600 - QUIET_MARGIN <= seconds < 13 * 3600 + TRANSMIT_WINDOW + QUIET_MARGIN


class Policy:
    """Pure policy: all timers use elapsed boot time, never the adjustable clock."""
    def __init__(self, grace):
        self.grace = grace
        self.state = 'listening'
        self.first_packet = None
        self.last_packet = None
        self.cancelled = False

    def packet(self, now, eligible):
        if self.state == 'listening' and eligible:
            self.first_packet = now
            self.state = 'grace'
        if self.first_packet is not None:
            self.last_packet = now

    def tick(self, now, human_seen=False, healthy=True, backup_status=None, daily_blocked=False, sessions_settled=True):
        if self.state == 'done':
            return None
        self.cancelled |= human_seen or not healthy
        if self.cancelled and self.state != 'backup':
            self.state = 'done'
            return 'cancel'
        if not sessions_settled:
            return None
        if self.state == 'listening' and now >= BOOT_WINDOW:
            self.state = 'done'
            return 'done'
        if self.state == 'grace' and now - self.first_packet >= self.grace:
            self.state = 'backup'
            return 'start_backup'
        if self.state == 'backup' and backup_status is not None:
            self.state = 'holdoff'
            if self.cancelled:
                self.state = 'done'
                return 'cancel'
        if self.state == 'holdoff' and self._quiet(now, daily_blocked):
            self.state = 'shutdown_pending'
            return 'poweroff'
        return None

    def _quiet(self, now, daily_blocked):
        return (not daily_blocked
                and now - self.first_packet >= TRANSMIT_WINDOW + QUIET_MARGIN
                and now - self.last_packet >= QUIET_MARGIN)

    def confirm_shutdown(self, now, daily_blocked, packets_drained=True):
        if self.state != 'shutdown_pending':
            return False
        if packets_drained and self._quiet(now, daily_blocked):
            self.state = 'done'
            return True
        self.state = 'holdoff'
        return False


def is_human_session(properties):
    # Greeters and systemd user managers are background infrastructure, not
    # authenticated human sessions. Actual SSH/TTY/graphical/root logins have
    # separate user/user-early sessions; unknown classes still cancel safely.
    return properties.get('Class') not in ('greeter', 'manager', 'manager-early')


def run_checked(args):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE,
                          text=True, timeout=5, env={**os.environ, 'LC_ALL': 'C'}).stdout


def session_properties(session):
    output = run_checked(['loginctl', 'show-session', session, '--no-pager',
                          '--property=Class', '--property=User'])
    properties = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
    if not properties.get('Class') or not properties.get('User'):
        raise RuntimeError('Incomplete logind session information')
    return properties


def current_human_sessions():
    output = run_checked(['loginctl', 'list-sessions', '--no-legend', '--no-pager'])
    return [line.split()[0] for line in output.splitlines()
            if line.strip() and is_human_session(session_properties(line.split()[0]))]


class SessionEventParser:
    def __init__(self):
        self.await_session = False

    def feed(self, line):
        if line.startswith('signal '):
            self.await_session = ('interface=org.freedesktop.login1.Manager;' in line
                                  and 'member=SessionNew' in line)
            if 'member=NameAcquired' in line:
                return 'ready'
        elif self.await_session:
            match = re.fullmatch(r'\s*string "([A-Za-z0-9_-]+)"\s*', line)
            if match:
                self.await_session = False
                return match.group(1)
        return None


class LoginGuard:
    """Latch new human logins, even when the session disappears between polls."""
    def __init__(self):
        self.seen = threading.Event()
        self.failed = threading.Event()
        self.ready = threading.Event()
        self.settled = threading.Event()
        self.settled.set()
        self.stopping = threading.Event()
        self.process = None
        self.thread = None

    def start(self):
        self.process = subprocess.Popen(
            ['stdbuf', '-oL', 'dbus-monitor', '--system', '--monitor',
             "type='signal',interface='org.freedesktop.login1.Manager',member='SessionNew'"],
            stdout=subprocess.PIPE, text=True, bufsize=1,
            env={**os.environ, 'LC_ALL': 'C'})
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()
        if not self.ready.wait(8) or not self.healthy:
            raise RuntimeError('logind event monitor did not become ready')
        self.scan()

    @property
    def healthy(self):
        return not self.failed.is_set() and self.process is not None and self.process.poll() is None

    def _watch(self):
        parser = SessionEventParser()
        try:
            for line in self.process.stdout:
                event = parser.feed(line)
                if event == 'ready':
                    self.ready.set()
                elif event:
                    # Suppress side effects immediately, before the potentially slow lookup.
                    self.settled.clear()
                    try:
                        if is_human_session(session_properties(event)):
                            LOG.info('Human login observed: session %s; automatic shutdown cancelled', event)
                            self.seen.set()
                    except Exception:
                        self.failed.set()
                        raise
                    finally:
                        self.settled.set()
        except Exception:
            LOG.exception('Cannot verify new session; cancelling automation')
            self.failed.set()
        finally:
            if not self.stopping.is_set():
                self.failed.set()

    def scan(self):
        try:
            sessions = current_human_sessions()
            if sessions:
                LOG.info('Human sessions present: %s', ', '.join(sessions))
                self.seen.set()
        except Exception:
            LOG.exception('Cannot verify current sessions; cancelling automation')
            self.failed.set()

    def allow_side_effect(self):
        # Wait for any already-observed SessionNew to be classified. A fast
        # login/logout can be absent from scan() while this lookup is pending.
        if not self.settled.wait(6):
            self.failed.set()
            return False
        self.scan()
        if not self.settled.wait(6):
            self.failed.set()
            return False
        return self.healthy and not self.seen.is_set() and self.settled.is_set()

    def close(self):
        self.stopping.set()
        if self.process is not None:
            self.process.terminate()
            self.process.wait(timeout=5)
        if self.thread is not None:
            self.thread.join(timeout=6)
        if self.process is not None and self.process.stdout is not None:
            self.process.stdout.close()


@contextmanager
def claim_boot(directory):
    """Atomic claim persists across service restarts; /run clears on reboot."""
    directory.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(directory / 'claimed', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError('This boot already had an automation attempt; not repeating') from exc
    os.close(fd)
    yield


def notify_ready():
    address = os.environ.get('NOTIFY_SOCKET')
    if address:
        if address.startswith('@'):
            address = '\0' + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(b'READY=1', address)


def start_backup(dry_run):
    command = ['/bin/true'] if dry_run else [BACKUP]
    LOG.info('%sStarting root backup command: %s', 'DRY RUN: would run ' if dry_run else '', BACKUP)
    # Inherit stdout/stderr for the journal, and avoid interactive prompts.
    return subprocess.Popen(command, stdin=subprocess.DEVNULL)


def request_poweroff(dry_run):
    if dry_run:
        LOG.info('DRY RUN: would request systemctl poweroff')
    else:
        LOG.info('Requesting systemctl poweroff (without forcing or ignoring inhibitors)')
        subprocess.run(['systemctl', '--check-inhibitors=yes', 'poweroff'],
                       stdin=subprocess.DEVNULL, check=True, timeout=30)


def uptime():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def interface_is_up(interface):
    """Layer-2 readiness only; no IPv4 address or internet connection needed."""
    device = Path('/sys/class/net') / interface
    try:
        if not int((device / 'flags').read_text().strip(), 16) & 1:
            return False
        return (device / 'carrier').read_text().strip() == '1'
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENODEV, errno.ENETDOWN, errno.EINVAL):
            return False
        raise


def drain_packets(capture, policy, test_mode, interface=None):
    """True means a ready capture queue was drained; down is never silence."""
    if interface is not None and not interface_is_up(interface):
        return False
    for _ in range(256):
        try:
            data = capture.recv(2048)
        except BlockingIOError:
            return interface is None or interface_is_up(interface)
        except OSError as exc:
            if exc.errno == errno.ENETDOWN:
                return False
            raise
        if valid_magic_frame(data):
            now = uptime()
            before = policy.state
            policy.packet(now, eligible_trigger(now, datetime.now(ZONE), test_mode))
            if before == 'listening' and policy.state == 'grace':
                LOG.info('Matching WoL packet detected; waiting %ss for human login', policy.grace)
    # Under sustained traffic, return to the loop instead of assuming silence.
    return False


def run(args):
    if os.geteuid() != 0:
        raise RuntimeError('Run as root (packet capture and the backup require it)')
    guard = LoginGuard()
    with claim_boot(Path(args.state_dir)), socket.socket(
            socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0800)) as capture:
        capture.bind((args.interface, 0))
        capture.setblocking(False)
        # Readiness precedes ordinary logins through Type=notify + Before= ordering.
        try:
            guard.start()
            notify_ready()
            LOG.info('Listening on %s; mode=%s, dry-run=%s, grace=%ss, boot age=%.1fs',
                     args.interface, 'test' if args.test_mode else 'daily', args.dry_run,
                     30 if args.test_mode else 300, uptime())
            policy = Policy(30 if args.test_mode else 300)
            backup = None
            backup_status = None
            next_scan = 0
            capture_waiting = False
            while True:
                now = uptime()
                if now >= next_scan:
                    guard.scan()
                    next_scan = now + 1
                # Keep capture draining while backup runs; packet silence is a shutdown guard.
                readable, _, _ = select.select([capture], [], [], 0.2)
                if readable:
                    drained = drain_packets(capture, policy, args.test_mode, args.interface)
                    if not drained:
                        if not capture_waiting:
                            LOG.warning('Ethernet capture not ready or backlogged; waiting without treating it as packet silence')
                        capture_waiting = True
                        # ENETDOWN may leave select() immediately readable. Avoid a busy loop.
                        time.sleep(0.2)
                    elif capture_waiting:
                        LOG.info('Ethernet capture ready; continuing packet monitoring')
                        capture_waiting = False
                if backup is not None:
                    status = backup.poll()
                    if status is not None and backup_status is None:
                        backup_status = status
                        LOG.info('Backup exited with status %s; failure still permits guarded shutdown', status)
                action = policy.tick(uptime(), guard.seen.is_set(), guard.healthy,
                                     backup_status, daily_shutdown_blocked(datetime.now(ZONE)),
                                     guard.settled.is_set())
                if action == 'start_backup':
                    # Scan once more at the side-effect boundary.
                    if not guard.allow_side_effect():
                        LOG.info('Login/guard failure before backup; cancelling')
                        return
                    try:
                        backup = start_backup(args.dry_run)
                    except OSError:
                        LOG.exception('Backup launch failed; treating as exit status 127')
                        backup_status = 127
                elif action == 'poweroff':
                    if not guard.allow_side_effect():
                        LOG.info('Login/guard failure before shutdown; cancelling shutdown')
                        return
                    drained = drain_packets(capture, policy, args.test_mode, args.interface)
                    if not policy.confirm_shutdown(uptime(), daily_shutdown_blocked(datetime.now(ZONE)), drained):
                        LOG.info('Transmission/window guard changed during login check; resuming holdoff')
                        continue
                    if guard.seen.is_set() or not guard.healthy or not guard.settled.is_set():
                        LOG.info('Login/guard changed at final shutdown check; cancelling shutdown')
                        return
                    request_poweroff(args.dry_run)
                    return
                elif action in ('cancel', 'done'):
                    LOG.info('Automation finished: %s; no shutdown requested', action)
                    return
        finally:
            guard.close()


def env_flag(name, default):
    value = os.environ.get(name, default)
    if value not in ('0', '1'):
        raise ValueError(name + ' must be 0 or 1')
    return value == '1'


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interface', default='enp0s31f6')
    parser.add_argument('--state-dir', default='/run/wol-backup')
    parser.add_argument('--test-mode', action=argparse.BooleanOptionalAction,
                        default=env_flag('WOL_BACKUP_TEST_MODE', '0'))
    parser.add_argument('--dry-run', action=argparse.BooleanOptionalAction,
                        default=env_flag('WOL_BACKUP_DRY_RUN', '1'))
    args = parser.parse_args()
    try:
        run(args)
    except Exception:
        LOG.exception('Automation stopped; no further shutdown will be requested')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
