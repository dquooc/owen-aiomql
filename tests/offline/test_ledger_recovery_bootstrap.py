"""Explicit bootstrap boundaries through the isolated offline ledger package."""

from datetime import datetime, timedelta, timezone
import importlib
import importlib.util
import unittest

from test_deal_normalizer import PACKAGE, ledger
from test_ledger_checkpoint import OffsetlessTimezone, UnusableTimezone


class RecoveryBootstrapTests(unittest.TestCase):
    def setUp(self):
        module_name = f"{PACKAGE}.recovery"
        self.assertIsNotNone(importlib.util.find_spec(module_name), "bootstrap recovery module must exist")
        self.recovery = importlib.import_module(module_name)
        self.start = datetime(2026, 11, 1, 10, tzinfo=timezone.utc)
        self.now = datetime(2026, 11, 1, 10, 20, tzinfo=timezone.utc)

    def test_normal_bootstrap_uses_exact_explicit_boundaries(self):
        result = self.recovery._plan_bootstrap_range(bootstrap_start=self.start, current_time=self.now)
        self.assertIsInstance(result, ledger.RecoveryRange)
        self.assertEqual((result.from_time, result.to_time), (self.start, self.now))

    def test_both_offset_inputs_are_normalized_to_utc(self):
        zone = timezone(timedelta(hours=7))
        result = self.recovery._plan_bootstrap_range(
            bootstrap_start=datetime(2026, 11, 1, 17, tzinfo=zone),
            current_time=datetime(2026, 11, 1, 17, 20, tzinfo=zone))
        self.assertEqual((result.from_time, result.to_time), (self.start, self.now))
        self.assertIs(result.from_time.tzinfo, timezone.utc)
        self.assertIs(result.to_time.tzinfo, timezone.utc)

    def test_mixed_equivalent_offsets_allow_zero_length_bootstrap(self):
        result = self.recovery._plan_bootstrap_range(
            bootstrap_start=datetime(2026, 11, 1, 17, tzinfo=timezone(timedelta(hours=7))),
            current_time=self.start)
        self.assertEqual((result.from_time, result.to_time), (self.start, self.start))
        self.assertIs(result.from_time.tzinfo, timezone.utc)
        self.assertIs(result.to_time.tzinfo, timezone.utc)

    def test_equal_boundaries_are_valid(self):
        result = self.recovery._plan_bootstrap_range(bootstrap_start=self.start, current_time=self.start)
        self.assertEqual((result.from_time, result.to_time), (self.start, self.start))

    def test_future_bootstrap_start_is_rejected(self):
        with self.assertRaises(ValueError):
            self.recovery._plan_bootstrap_range(bootstrap_start=self.now, current_time=self.start)

    def test_naive_bootstrap_start_is_rejected(self):
        with self.assertRaises(ValueError):
            self.recovery._plan_bootstrap_range(
                bootstrap_start=datetime(2026, 11, 1, 10), current_time=self.now)

    def test_naive_current_time_is_rejected(self):
        with self.assertRaises(ValueError):
            self.recovery._plan_bootstrap_range(
                bootstrap_start=self.start, current_time=datetime(2026, 11, 1, 10, 20))

    def test_invalid_bootstrap_start_including_none_is_rejected(self):
        for value in (None, "2026-11-01T10:00:00+00:00", 1793527200, timedelta(), False):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.recovery._plan_bootstrap_range(bootstrap_start=value, current_time=self.now)

    def test_invalid_current_time_including_none_is_rejected(self):
        for value in (None, "2026-11-01T10:20:00+00:00", 1793528400, timedelta(), False):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.recovery._plan_bootstrap_range(bootstrap_start=self.start, current_time=value)

    def test_unusable_offsets_are_rejected_on_both_inputs(self):
        for zone in (OffsetlessTimezone(), UnusableTimezone()):
            invalid = datetime(2026, 11, 1, 10, tzinfo=zone)
            for start, now in ((invalid, self.now), (self.start, invalid)):
                with self.subTest(zone=type(zone).__name__, start=start, now=now), self.assertRaises(ValueError):
                    self.recovery._plan_bootstrap_range(bootstrap_start=start, current_time=now)

    def test_microseconds_are_preserved_exactly(self):
        result = self.recovery._plan_bootstrap_range(
            bootstrap_start=datetime(2026, 11, 1, 10, 0, 0, 123456, tzinfo=timezone.utc),
            current_time=datetime(2026, 11, 1, 10, 0, 1, 654321, tzinfo=timezone.utc))
        self.assertEqual(result.from_time, datetime(2026, 11, 1, 10, 0, 0, 123456, tzinfo=timezone.utc))
        self.assertEqual(result.to_time, datetime(2026, 11, 1, 10, 0, 1, 654321, tzinfo=timezone.utc))

    def test_fractional_hour_offset_is_normalized(self):
        zone = timezone(timedelta(hours=5, minutes=30))
        result = self.recovery._plan_bootstrap_range(
            bootstrap_start=datetime(2026, 11, 1, 15, 30, tzinfo=zone),
            current_time=datetime(2026, 11, 1, 15, 50, tzinfo=zone))
        self.assertEqual((result.from_time, result.to_time), (self.start, self.now))
        self.assertIs(result.from_time.tzinfo, timezone.utc)
        self.assertIs(result.to_time.tzinfo, timezone.utc)

    def test_repeated_calls_are_deterministic(self):
        expected = ledger.RecoveryRange(self.start, self.now)
        for _ in range(3):
            result = self.recovery._plan_bootstrap_range(bootstrap_start=self.start, current_time=self.now)
            self.assertEqual(result, expected)

    def test_omitted_bootstrap_or_current_time_has_no_default(self):
        with self.assertRaises(TypeError):
            self.recovery._plan_bootstrap_range(current_time=self.now)
        with self.assertRaises(TypeError):
            self.recovery._plan_bootstrap_range(bootstrap_start=self.start)


if __name__ == "__main__":
    unittest.main()
