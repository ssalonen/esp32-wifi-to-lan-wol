import errno
import importlib.util
from collections import deque
import pathlib
import struct
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

MODULE = pathlib.Path(__file__).resolve().parents[1] / 'linux' / 'wol_backup.py'
spec = importlib.util.spec_from_file_location('wol_backup', MODULE)
wb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wb)


def frame(payload=None, *, source=bytes.fromhex('020000007702'), port=9, fragment=0):
    if payload is None:
        payload = b'\xff' * 6 + bytes.fromhex('38d54714cc28') * 16
    udp = struct.pack('!HHHH', 9, port, 8 + len(payload), 0) + payload
    ip = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20 + len(udp), 1, fragment,
                     128, 17, 0, bytes([192, 168, 77, 2]), b'\xff' * 4)
    return b'\xff' * 6 + source + b'\x08\x00' + ip + udp


class PacketTests(unittest.TestCase):
    def test_valid_and_corrupt_frames(self):
        self.assertTrue(wb.valid_magic_frame(frame()))
        for bad in (b'', frame()[:-1], frame(payload=b'\xff' * 102),
                    frame(payload=(b'\xff' * 6 + bytes.fromhex('38d54714cc28') * 15)),
                    frame(source=b'\x01' * 6), frame(port=7),
                    frame(fragment=0x2000), frame(fragment=1)):
            with self.subTest(frame=bad[:20]):
                self.assertFalse(wb.valid_magic_frame(bad))

    def test_wake_window_boot_limit_and_dst(self):
        zone = ZoneInfo('Europe/Helsinki')
        for month in (1, 7):
            at_one = datetime(2026, month, 7, 13, 0, tzinfo=zone)
            self.assertTrue(wb.eligible_trigger(100, at_one, False))
            self.assertFalse(wb.eligible_trigger(601, at_one, False))
            self.assertFalse(wb.eligible_trigger(100, at_one.replace(minute=10), False))
            self.assertFalse(wb.eligible_trigger(100, at_one.replace(hour=12), False))
            self.assertTrue(wb.eligible_trigger(100, at_one.replace(hour=12), True))
            self.assertFalse(wb.eligible_trigger(601, at_one.replace(hour=12), True))
        self.assertTrue(wb.daily_shutdown_blocked(datetime(2026, 7, 7, 12, 59, 50, tzinfo=zone)))
        self.assertTrue(wb.daily_shutdown_blocked(datetime(2026, 7, 7, 13, 10, 14, tzinfo=zone)))
        self.assertFalse(wb.daily_shutdown_blocked(datetime(2026, 7, 7, 13, 10, 16, tzinfo=zone)))


class PolicyTests(unittest.TestCase):
    def policy(self, grace=300):
        policy = wb.Policy(grace)
        policy.packet(100, True)
        return policy

    def test_grace_then_backup_then_shutdown_even_on_failure(self):
        for status in (0, 1, 127):
            p = self.policy()
            self.assertIsNone(p.tick(399))
            self.assertEqual(p.tick(400), 'start_backup')
            self.assertIsNone(p.tick(401, backup_status=status))
            self.assertIsNone(p.tick(714))
            self.assertEqual(p.tick(715), 'poweroff')
            self.assertIsNone(p.tick(716))

    def test_login_during_grace_cancels_everything(self):
        p = self.policy()
        self.assertEqual(p.tick(101, human_seen=True), 'cancel')
        self.assertIsNone(p.tick(800))

    def test_short_login_during_backup_latches_shutdown_cancellation(self):
        p = self.policy()
        self.assertEqual(p.tick(400), 'start_backup')
        self.assertIsNone(p.tick(401, human_seen=True))
        self.assertEqual(p.tick(402, backup_status=0), 'cancel')
        self.assertIsNone(p.tick(800))

    def test_login_after_backup_or_monitor_failure_cancels(self):
        for kwargs in ({'human_seen': True}, {'healthy': False}):
            p = self.policy()
            p.tick(400)
            p.tick(401, backup_status=1)
            self.assertEqual(p.tick(715, **kwargs), 'cancel')

    def test_packet_silence_and_daily_window_delay_shutdown(self):
        p = self.policy()
        p.tick(400)
        p.tick(401, backup_status=0)
        p.packet(710, False)
        self.assertIsNone(p.tick(715))
        self.assertIsNone(p.tick(725, daily_blocked=True))
        self.assertEqual(p.tick(726), 'poweroff')

    def test_pending_classification_defers_backup_and_shutdown(self):
        p = self.policy()
        self.assertIsNone(p.tick(400, sessions_settled=False))
        self.assertEqual(p.tick(401), 'start_backup')
        self.assertIsNone(p.tick(402, backup_status=0))
        self.assertIsNone(p.tick(715, sessions_settled=False))
        self.assertEqual(p.tick(716), 'poweroff')

    def test_unhealthy_monitor_stops_grace_but_allows_running_backup_to_finish(self):
        p = self.policy()
        self.assertEqual(p.tick(101, healthy=False), 'cancel')
        p = self.policy()
        p.tick(400)
        self.assertIsNone(p.tick(401, healthy=False))
        self.assertEqual(p.tick(402, backup_status=1), 'cancel')

    def test_shutdown_rechecks_packets_and_clock_after_slow_login_check(self):
        for changed in ('packet', 'clock', 'backlog'):
            with self.subTest(changed=changed):
                p = self.policy()
                p.tick(400)
                p.tick(401, backup_status=0)
                self.assertEqual(p.tick(715), 'poweroff')
                if changed == 'packet':
                    p.packet(718, False)
                self.assertFalse(p.confirm_shutdown(720, daily_blocked=changed == 'clock',
                                                    packets_drained=changed != 'backlog'))
                if changed != 'backlog':
                    self.assertIsNone(p.tick(721, daily_blocked=changed == 'clock'))
                self.assertEqual(p.tick(740), 'poweroff')
                self.assertTrue(p.confirm_shutdown(740, daily_blocked=False))
                self.assertIsNone(p.tick(741))

    def test_test_mode_grace_and_no_packet_timeout(self):
        p = self.policy(30)
        self.assertIsNone(p.tick(129))
        self.assertEqual(p.tick(130), 'start_backup')
        p = wb.Policy(300)
        p.packet(10, False)
        self.assertIsNone(p.tick(599))
        self.assertEqual(p.tick(600), 'done')


class SessionTests(unittest.TestCase):
    def test_classification_includes_ssh_tty_and_unknown_excludes_greeter(self):
        self.assertFalse(wb.is_human_session({'Class': 'greeter', 'User': '120'}))
        for props in ({'Class': 'user', 'User': '1000'},
                      {'Class': 'user', 'User': '0'},
                      {'Class': 'user-early', 'User': '1000'}, {},
                      {'Class': 'background', 'User': '1000'}):
            self.assertTrue(wb.is_human_session(props))

    def test_gdm_user_manager_is_not_a_human_login(self):
        # logind creates a separate manager-early session for the GDM greeter.
        self.assertFalse(wb.is_human_session({'Class': 'manager-early', 'User': '60578'}))
        # User managers may also linger after the corresponding login ends.
        self.assertFalse(wb.is_human_session({'Class': 'manager', 'User': '1000'}))
        self.assertTrue(wb.is_human_session({'Class': 'user', 'User': '1000'}))
        self.assertTrue(wb.is_human_session({'Class': 'user-early', 'User': '0'}))
        self.assertTrue(wb.is_human_session({'Class': 'unknown-manager', 'User': '1000'}))

    def test_session_event_parser_catches_login_and_logout(self):
        parser = wb.SessionEventParser()
        lines = [
            'signal time=1 sender=org.freedesktop.DBus -> destination=:1.2; member=NameAcquired',
            '   string ":1.2"',
            'signal time=2 sender=:1.1 -> destination=(null destination); interface=org.freedesktop.login1.Manager; member=SessionNew',
            '   string "42"',
            '   object path "/org/freedesktop/login1/session/_342"',
        ]
        events = [parser.feed(line) for line in lines]
        self.assertEqual([e for e in events if e], ['ready', '42'])

    def test_pending_login_classification_blocks_side_effect(self):
        entered = threading.Event()
        release = threading.Event()
        finish = threading.Event()
        guard = wb.LoginGuard()
        results = []

        def lines():
            yield 'signal time=2 sender=:1.1; interface=org.freedesktop.login1.Manager; member=SessionNew'
            yield '   string "42"'
            finish.wait(3)

        def classify(session):
            self.assertEqual(session, '42')
            entered.set()
            release.wait(3)
            return {'Class': 'user', 'User': '1000'}

        guard.process = SimpleNamespace(stdout=lines(), poll=lambda: None)
        with patch.object(wb, 'session_properties', classify), patch.object(wb, 'current_human_sessions', return_value=[]):
            watcher = threading.Thread(target=guard._watch)
            watcher.start()
            self.assertTrue(entered.wait(1))
            try:
                self.assertFalse(guard.settled.is_set())
                checker = threading.Thread(target=lambda: results.append(guard.allow_side_effect()))
                checker.start()
                checker.join(0.05)
                self.assertEqual(results, [])
                release.set()
                checker.join(1)
                self.assertEqual(results, [False])
                self.assertTrue(guard.seen.is_set())
            finally:
                release.set()
                guard.stopping.set()
                finish.set()
                watcher.join(1)

    def test_once_per_boot_claim_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            with wb.claim_boot(pathlib.Path(directory)):
                with self.assertRaises(RuntimeError):
                    with wb.claim_boot(pathlib.Path(directory)):
                        pass
            with self.assertRaises(RuntimeError):
                with wb.claim_boot(pathlib.Path(directory)):
                    pass


class SideEffectTests(unittest.TestCase):
    def test_dry_run_executes_harmless_command_and_only_logs_poweroff(self):
        process = wb.start_backup(True)
        self.assertEqual(process.wait(timeout=2), 0)
        with patch.object(wb.subprocess, 'run', side_effect=AssertionError('dry-run attempted poweroff')):
            with self.assertLogs(wb.LOG, level='INFO') as logs:
                wb.request_poweroff(True)
        self.assertTrue(any('would request systemctl poweroff' in line for line in logs.output))

    def test_shutdown_packet_drain_records_a_new_packet_before_final_decision(self):
        packets = deque([frame()])
        class Capture:
            def recv(self, size):
                if not packets:
                    raise BlockingIOError()
                return packets.popleft()
        p = wb.Policy(300)
        p.packet(100, True)
        p.tick(400)
        p.tick(401, backup_status=0)
        self.assertEqual(p.tick(715), 'poweroff')
        with patch.object(wb, 'uptime', return_value=720):
            self.assertTrue(wb.drain_packets(Capture(), p, True))
        self.assertFalse(p.confirm_shutdown(720, False))
        self.assertEqual(p.tick(735), 'poweroff')

    def test_network_down_does_not_abort_or_count_as_packet_silence(self):
        class Capture:
            def recv(self, size):
                raise OSError(errno.ENETDOWN, 'Network is down')
        p = wb.Policy(300)
        p.packet(100, True)
        p.tick(400)
        p.tick(401, backup_status=0)
        self.assertEqual(p.tick(715), 'poweroff')
        drained = wb.drain_packets(Capture(), p, True)
        self.assertFalse(drained)
        self.assertFalse(p.confirm_shutdown(715, False, drained))

    def test_capture_waits_for_interface_up_then_receives_magic(self):
        packets = deque([frame()])
        class Capture:
            def recv(self, size):
                if not packets:
                    raise BlockingIOError()
                return packets.popleft()
        p = wb.Policy(30)
        with patch.object(wb, 'interface_is_up', return_value=False):
            self.assertFalse(wb.drain_packets(Capture(), p, True, 'enp0s31f6'))
        self.assertEqual(len(packets), 1)
        self.assertEqual(p.state, 'listening')
        with patch.object(wb, 'interface_is_up', return_value=True), patch.object(wb, 'uptime', return_value=20):
            self.assertTrue(wb.drain_packets(Capture(), p, True, 'enp0s31f6'))
        self.assertEqual(p.state, 'grace')
        self.assertEqual(p.tick(50), 'start_backup')

    def test_unexpected_capture_error_remains_fatal(self):
        class Capture:
            def recv(self, size):
                raise OSError(errno.EIO, 'unexpected I/O failure')
        with self.assertRaises(OSError):
            wb.drain_packets(Capture(), wb.Policy(30), True)

    def test_missing_or_dead_monitor_cancels_side_effect(self):
        guard = wb.LoginGuard()
        with patch.object(wb, 'current_human_sessions', return_value=[]):
            self.assertFalse(guard.allow_side_effect())
        guard.process = SimpleNamespace(poll=lambda: 1)
        self.assertFalse(guard.healthy)


if __name__ == '__main__':
    unittest.main()
