"""Stable recovery policy and clocks without recovery orchestration."""

from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest
from uuid import UUID

from test_deal_normalizer import ledger
from test_ledger_checkpoint import OffsetlessTimezone, UnusableTimezone


class RecoveryConfigTests(unittest.TestCase):
    def setUp(self):
        for name in ("RecoveryConfig", "Clock", "utc_now"):
            self.assertTrue(hasattr(ledger, name), f"{name} must be publicly exported")
        self.start = datetime(2026, 11, 1, 10, 0, 0, 123456, tzinfo=timezone.utc)

    def test_zero_overlap_and_missing_bootstrap_are_allowed(self):
        config = ledger.RecoveryConfig(overlap=timedelta(0), bootstrap_start=None)
        self.assertEqual(config.overlap, timedelta(0))
        self.assertIsNone(config.bootstrap_start)
        self.assertIsNone(ledger.RecoveryConfig(overlap=timedelta(0)).bootstrap_start)
        self.assertEqual(tuple(field.name for field in fields(config)), ("overlap", "bootstrap_start"))

    def test_positive_overlap_is_preserved_exactly(self):
        overlap = timedelta(days=2, seconds=301, microseconds=123456)
        self.assertIs(ledger.RecoveryConfig(overlap=overlap).overlap, overlap)

    def test_negative_overlap_is_rejected(self):
        for value in (timedelta(microseconds=-1), timedelta.min):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ledger.RecoveryConfig(overlap=value)

    def test_wrong_overlap_types_are_rejected_without_conversion(self):
        for value in (None, 0, 5.0, False, True, "5 minutes", self.start):
            with self.subTest(value=value), self.assertRaises(TypeError):
                ledger.RecoveryConfig(overlap=value)

    def test_maximum_overlap_is_not_capped(self):
        self.assertIs(ledger.RecoveryConfig(overlap=timedelta.max).overlap, timedelta.max)

    def test_utc_bootstrap_is_preserved(self):
        config = ledger.RecoveryConfig(overlap=timedelta(0), bootstrap_start=self.start)
        self.assertEqual(config.bootstrap_start, self.start)
        self.assertIs(config.bootstrap_start.tzinfo, timezone.utc)

    def test_equivalent_offsets_preserve_utc_instant_and_microseconds(self):
        for hour, minute, offset in ((17, 0, timedelta(hours=7)),
                                     (15, 30, timedelta(hours=5, minutes=30)),
                                     (5, 0, timedelta(hours=-5))):
            with self.subTest(offset=offset):
                bootstrap = datetime(2026, 11, 1, hour, minute, 0, 123456, tzinfo=timezone(offset))
                config = ledger.RecoveryConfig(overlap=timedelta(0), bootstrap_start=bootstrap)
                self.assertEqual(config.bootstrap_start, self.start)
                self.assertIs(config.bootstrap_start.tzinfo, timezone.utc)
                self.assertEqual(bootstrap.microsecond, 123456)
                self.assertEqual(bootstrap.utcoffset(), offset)

    def test_naive_bootstrap_is_rejected(self):
        with self.assertRaises(ValueError):
            ledger.RecoveryConfig(overlap=timedelta(0), bootstrap_start=self.start.replace(tzinfo=None))

    def test_invalid_bootstrap_types_are_rejected(self):
        for value in (False, True, 0, "2026-11-01T10:00:00+00:00", timedelta(0), object()):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ledger.RecoveryConfig(overlap=timedelta(0), bootstrap_start=value)

    def test_unusable_timezone_is_rejected(self):
        for zone in (OffsetlessTimezone(), UnusableTimezone()):
            with self.subTest(zone=type(zone).__name__), self.assertRaises(ValueError):
                ledger.RecoveryConfig(overlap=timedelta(0),
                                      bootstrap_start=datetime(2026, 11, 1, 10, tzinfo=zone))

    def test_utc_conversion_overflow_propagates(self):
        for bootstrap in (datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1))),
                          datetime(9999, 12, 31, 23, 59, 59, 999999,
                                   tzinfo=timezone(timedelta(hours=-1)))):
            with self.subTest(bootstrap=bootstrap), self.assertRaises(OverflowError):
                ledger.RecoveryConfig(overlap=timedelta(0), bootstrap_start=bootstrap)

    def test_configuration_fields_are_frozen(self):
        config = ledger.RecoveryConfig(overlap=timedelta(0), bootstrap_start=self.start)
        for name, value in (("overlap", timedelta(minutes=1)), ("bootstrap_start", None)):
            with self.subTest(field=name), self.assertRaises(FrozenInstanceError):
                setattr(config, name, value)
        self.assertFalse(hasattr(config, "__dict__"))

    def test_equivalent_configurations_compare_equal(self):
        first = ledger.RecoveryConfig(overlap=timedelta(minutes=5), bootstrap_start=self.start)
        second = ledger.RecoveryConfig(overlap=timedelta(seconds=300),
                                       bootstrap_start=datetime(2026, 11, 1, 17, 0, 0, 123456,
                                                                tzinfo=timezone(timedelta(hours=7))))
        self.assertEqual(first, second)
        self.assertNotEqual(first, ledger.RecoveryConfig(overlap=timedelta(0), bootstrap_start=self.start))

    def test_production_clock_returns_current_aware_utc(self):
        before = datetime.now(timezone.utc)
        captured = ledger.utc_now()
        after = datetime.now(timezone.utc)
        self.assertIsInstance(captured, datetime)
        self.assertIs(captured.tzinfo, timezone.utc)
        self.assertLessEqual(before, captured)
        self.assertLessEqual(captured, after)

    def test_clock_contract_accepts_deterministic_callable(self):
        clock: ledger.Clock = lambda: self.start
        self.assertIs(clock(), self.start)

    def test_config_passes_directly_to_both_planner_branches(self):
        config = ledger.RecoveryConfig(overlap=timedelta(minutes=5, microseconds=1),
                                       bootstrap_start=self.start)
        now = datetime(2026, 11, 1, 10, 20, 0, 654321, tzinfo=timezone.utc)
        checkpoint = ledger.LedgerCheckpoint(UUID(int=1), self.start, self.start)
        for supplied, expected_start in (
                (checkpoint, datetime(2026, 11, 1, 9, 55, 0, 123455, tzinfo=timezone.utc)),
                (None, self.start)):
            with self.subTest(checkpoint=supplied):
                planned = ledger.plan_recovery_range(checkpoint=supplied, current_time=now,
                                                      overlap=config.overlap, bootstrap_start=config.bootstrap_start)
                self.assertEqual((planned.from_time, planned.to_time), (expected_start, now))

    def test_missing_bootstrap_decision_remains_with_planner(self):
        config = ledger.RecoveryConfig(overlap=timedelta(0))
        checkpoint = ledger.LedgerCheckpoint(UUID(int=1), self.start, self.start)
        planned = ledger.plan_recovery_range(checkpoint=checkpoint, current_time=self.start,
                                              overlap=config.overlap, bootstrap_start=config.bootstrap_start)
        self.assertEqual((planned.from_time, planned.to_time), (self.start, self.start))
        with self.assertRaises(ValueError):
            ledger.plan_recovery_range(checkpoint=None, current_time=self.start,
                                       overlap=config.overlap, bootstrap_start=config.bootstrap_start)

    def test_public_import_executes_real_ledger_without_loading_mt5(self):
        package_dir = Path(__file__).resolve().parents[2] / "src" / "aiomql"
        script = textwrap.dedent("""
            import sys
            from types import ModuleType

            package = ModuleType("aiomql")
            package.__path__ = [sys.argv[1]]
            sys.modules["aiomql"] = package
            from aiomql.ledger import RecoveryConfig, Clock, utc_now
            from aiomql.ledger import __all__

            assert {"RecoveryConfig", "Clock", "utc_now"} <= set(__all__)
            assert RecoveryConfig.__name__ == "RecoveryConfig"
            assert callable(utc_now)
            assert "MetaTrader5" not in sys.modules
        """)
        completed = subprocess.run([sys.executable, "-B", "-c", script, str(package_dir)],
                                   capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
