"""Pure RecoveryRange validation using the isolated offline ledger package."""

from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timedelta, timezone
import unittest

from test_deal_normalizer import ledger
from test_ledger_checkpoint import OffsetlessTimezone, UnusableTimezone
from test_ledger_checkpoint_timezone import RepeatedHourTimezone


class RecoveryRangeTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(ledger, "RecoveryRange"), "RecoveryRange must be publicly exported")
        self.start = datetime(2026, 11, 1, 10, tzinfo=timezone.utc)
        self.end = datetime(2026, 11, 1, 10, 20, tzinfo=timezone.utc)

    def test_valid_utc_range_has_only_the_two_public_boundaries(self):
        result = ledger.RecoveryRange(self.start, self.end)
        self.assertEqual((result.from_time, result.to_time), (self.start, self.end))
        self.assertIs(result.from_time.tzinfo, timezone.utc)
        self.assertIs(result.to_time.tzinfo, timezone.utc)
        self.assertEqual(tuple(field.name for field in fields(result)), ("from_time", "to_time"))
        self.assertIn("RecoveryRange", ledger.__all__)

    def test_both_offset_boundaries_are_normalized_to_utc(self):
        zone = timezone(timedelta(hours=7))
        result = ledger.RecoveryRange(datetime(2026, 11, 1, 17, tzinfo=zone),
                                      datetime(2026, 11, 1, 17, 20, tzinfo=zone))
        self.assertEqual((result.from_time, result.to_time), (self.start, self.end))
        self.assertIs(result.from_time.tzinfo, timezone.utc)
        self.assertIs(result.to_time.tzinfo, timezone.utc)

    def test_mixed_equivalent_offsets_produce_zero_length_utc_range(self):
        for hour, offset in ((17, 7), (5, -5)):
            with self.subTest(offset=offset):
                result = ledger.RecoveryRange(
                    datetime(2026, 11, 1, hour, tzinfo=timezone(timedelta(hours=offset))), self.start)
                self.assertEqual((result.from_time, result.to_time), (self.start, self.start))
                self.assertIs(result.from_time.tzinfo, timezone.utc)
                self.assertIs(result.to_time.tzinfo, timezone.utc)

    def test_equal_endpoints_are_valid(self):
        result = ledger.RecoveryRange(self.start, self.start)
        self.assertEqual((result.from_time, result.to_time), (self.start, self.start))

    def test_reversed_range_is_rejected_without_swapping(self):
        with self.assertRaises(ValueError):
            ledger.RecoveryRange(self.end, self.start)

    def test_one_microsecond_reversal_is_rejected(self):
        with self.assertRaises(ValueError):
            ledger.RecoveryRange(datetime(2026, 11, 1, 10, 0, 0, 1, tzinfo=timezone.utc), self.start)

    def test_normalization_preserves_both_microsecond_values(self):
        zone = timezone(timedelta(hours=7))
        result = ledger.RecoveryRange(datetime(2026, 11, 1, 17, 0, 0, 123456, tzinfo=zone),
                                      datetime(2026, 11, 1, 17, 20, 0, 654321, tzinfo=zone))
        self.assertEqual(result.from_time, datetime(2026, 11, 1, 10, 0, 0, 123456, tzinfo=timezone.utc))
        self.assertEqual(result.to_time, datetime(2026, 11, 1, 10, 20, 0, 654321, tzinfo=timezone.utc))

    def test_naive_from_time_is_rejected(self):
        with self.assertRaises(ValueError):
            ledger.RecoveryRange(datetime(2026, 11, 1, 10), self.end)

    def test_naive_to_time_is_rejected(self):
        with self.assertRaises(ValueError):
            ledger.RecoveryRange(self.start, datetime(2026, 11, 1, 10, 20))

    def test_invalid_from_time_types_are_rejected_without_coercion(self):
        for value in (None, "2026-11-01T10:00:00+00:00", 1793527200, timedelta(), False):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ledger.RecoveryRange(value, self.end)

    def test_invalid_to_time_types_are_rejected_without_coercion(self):
        for value in (None, "2026-11-01T10:20:00+00:00", 1793528400, timedelta(), False):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ledger.RecoveryRange(self.start, value)

    def test_offsetless_timezone_is_rejected_on_either_boundary(self):
        invalid = datetime(2026, 11, 1, 10, tzinfo=OffsetlessTimezone())
        for start, end in ((invalid, self.end), (self.start, invalid)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                ledger.RecoveryRange(start, end)

    def test_unusable_timezone_error_propagates_on_either_boundary(self):
        invalid = datetime(2026, 11, 1, 10, tzinfo=UnusableTimezone())
        for start, end in ((invalid, self.end), (self.start, invalid)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                ledger.RecoveryRange(start, end)

    def test_fractional_hour_offset_normalizes_to_utc(self):
        zone = timezone(timedelta(hours=5, minutes=30))
        result = ledger.RecoveryRange(datetime(2026, 11, 1, 15, 30, tzinfo=zone),
                                      datetime(2026, 11, 1, 15, 50, tzinfo=zone))
        self.assertEqual((result.from_time, result.to_time), (self.start, self.end))
        self.assertIs(result.from_time.tzinfo, timezone.utc)
        self.assertIs(result.to_time.tzinfo, timezone.utc)

    def test_boundaries_cannot_be_reassigned(self):
        result = ledger.RecoveryRange(self.start, self.end)
        for name in ("from_time", "to_time"):
            with self.subTest(field=name), self.assertRaises(FrozenInstanceError):
                setattr(result, name, self.start)

    def test_construction_preserves_caller_datetimes(self):
        zone = timezone(timedelta(hours=7))
        start = datetime(2026, 11, 1, 17, 0, 0, 123456, tzinfo=zone)
        end = datetime(2026, 11, 1, 17, 20, 0, 654321, tzinfo=zone)
        ledger.RecoveryRange(start, end)
        self.assertEqual(start.isoformat(), "2026-11-01T17:00:00.123456+07:00")
        self.assertEqual(end.isoformat(), "2026-11-01T17:20:00.654321+07:00")
        self.assertIs(start.tzinfo, zone)
        self.assertIs(end.tzinfo, zone)

    def test_utc_conversion_overflow_is_not_clamped(self):
        underflow = datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1)))
        overflow = datetime(9999, 12, 31, 23, 59, 59, 999999,
                            tzinfo=timezone(timedelta(hours=-1)))
        for start, end in ((underflow, self.end), (self.start, overflow)):
            with self.subTest(start=start, end=end), self.assertRaises(OverflowError):
                ledger.RecoveryRange(start, end)

    def test_offset_ordering_uses_actual_instants(self):
        zone = timezone(timedelta(hours=7))
        result = ledger.RecoveryRange(datetime(2026, 11, 1, 17, tzinfo=zone), self.end)
        self.assertEqual((result.from_time, result.to_time), (self.start, self.end))
        with self.assertRaises(ValueError):
            ledger.RecoveryRange(self.start, datetime(2026, 11, 1, 16, 59, tzinfo=zone))

    def test_repeated_local_hour_is_ordered_after_utc_conversion(self):
        zone = RepeatedHourTimezone()
        first = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
        second = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)
        result = ledger.RecoveryRange(first, second)
        self.assertEqual(result.from_time, datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))
        self.assertEqual(result.to_time, datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc))
        with self.assertRaises(ValueError):
            ledger.RecoveryRange(second, first)


if __name__ == "__main__":
    unittest.main()
