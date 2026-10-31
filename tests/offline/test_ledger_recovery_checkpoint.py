"""Pure checkpoint-present recovery calculations without clock or storage I/O."""

from datetime import datetime, timedelta, timezone
import importlib
import unittest
from uuid import UUID

from test_deal_normalizer import PACKAGE, ledger
from test_ledger_checkpoint import OffsetlessTimezone, UnusableTimezone
from test_ledger_checkpoint_timezone import RepeatedHourTimezone


class RecoveryCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.recovery = importlib.import_module(f"{PACKAGE}.recovery")
        self.assertTrue(hasattr(self.recovery, "_plan_checkpoint_range"), "checkpoint range helper must exist")
        self.completed = datetime(2026, 11, 1, 10, tzinfo=timezone.utc)
        self.now = datetime(2026, 11, 1, 10, 20, tzinfo=timezone.utc)
        self.checkpoint = ledger.LedgerCheckpoint(
            UUID("cc17f0e2-0784-4d99-81cc-ce44e990767c"), self.completed,
            datetime(2026, 11, 1, 10, 5, tzinfo=timezone.utc))

    def plan(self, **changes):
        values = dict(checkpoint=self.checkpoint, current_time=self.now, overlap=timedelta(minutes=5))
        values.update(changes)
        return self.recovery._plan_checkpoint_range(**values)

    def checkpoint_at(self, completed):
        return ledger.LedgerCheckpoint(self.checkpoint.mt5_account_id, completed, self.checkpoint.updated_at)

    def test_five_minute_overlap_replays_before_inclusive_checkpoint(self):
        result = self.plan()
        self.assertIsInstance(result, ledger.RecoveryRange)
        self.assertEqual(result.from_time, datetime(2026, 11, 1, 9, 55, tzinfo=timezone.utc))
        self.assertEqual(result.to_time, self.now)

    def test_zero_overlap_starts_exactly_at_checkpoint(self):
        result = self.plan(overlap=timedelta())
        self.assertEqual((result.from_time, result.to_time), (self.completed, self.now))

    def test_equal_checkpoint_and_current_with_zero_overlap(self):
        result = self.plan(current_time=self.completed, overlap=timedelta())
        self.assertEqual((result.from_time, result.to_time), (self.completed, self.completed))

    def test_equal_checkpoint_and_current_with_positive_overlap(self):
        result = self.plan(current_time=self.completed)
        self.assertEqual(result.from_time, datetime(2026, 11, 1, 9, 55, tzinfo=timezone.utc))
        self.assertEqual(result.to_time, self.completed)

    def test_negative_overlap_is_rejected_with_context(self):
        for overlap in (timedelta(seconds=-1), timedelta(microseconds=-1)):
            with self.subTest(overlap=overlap), self.assertRaisesRegex(ValueError, "overlap"):
                self.plan(overlap=overlap)

    def test_invalid_overlap_types_are_rejected_without_coercion(self):
        for overlap in (None, 5, "5 minutes", self.now, False, 5.0):
            with self.subTest(overlap=overlap), self.assertRaises(TypeError):
                self.plan(overlap=overlap)

    def test_future_checkpoint_is_rejected(self):
        checkpoint = self.checkpoint_at(datetime(2026, 11, 1, 10, 30, tzinfo=timezone.utc))
        with self.assertRaises(ValueError):
            self.plan(checkpoint=checkpoint, overlap=timedelta())

    def test_overlap_cannot_mask_future_checkpoint(self):
        checkpoint = self.checkpoint_at(datetime(2026, 11, 1, 10, 30, tzinfo=timezone.utc))
        with self.assertRaises(ValueError):
            self.plan(checkpoint=checkpoint, overlap=timedelta(minutes=30))

    def test_one_microsecond_future_checkpoint_is_rejected(self):
        checkpoint = self.checkpoint_at(datetime(2026, 11, 1, 10, 0, 0, 1, tzinfo=timezone.utc))
        with self.assertRaises(ValueError):
            self.plan(checkpoint=checkpoint, current_time=self.completed)

    def test_one_microsecond_current_time_lead_is_valid(self):
        now = datetime(2026, 11, 1, 10, 0, 0, 1, tzinfo=timezone.utc)
        result = self.plan(current_time=now, overlap=timedelta())
        self.assertEqual((result.from_time, result.to_time), (self.completed, now))

    def test_equivalent_timezone_representations_are_not_future(self):
        checkpoint = self.checkpoint_at(
            datetime(2026, 11, 1, 17, tzinfo=timezone(timedelta(hours=7))))
        for now in (self.completed, datetime(2026, 11, 1, 5, tzinfo=timezone(timedelta(hours=-5)))):
            with self.subTest(now=now):
                result = self.plan(checkpoint=checkpoint, current_time=now, overlap=timedelta())
                self.assertEqual((result.from_time, result.to_time), (self.completed, self.completed))
                self.assertIs(result.from_time.tzinfo, timezone.utc)
                self.assertIs(result.to_time.tzinfo, timezone.utc)

    def test_fractional_hour_current_time_is_normalized(self):
        now = datetime(2026, 11, 1, 15, 50, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        result = self.plan(current_time=now)
        self.assertEqual(result.from_time, datetime(2026, 11, 1, 9, 55, tzinfo=timezone.utc))
        self.assertEqual(result.to_time, self.now)
        self.assertIs(result.to_time.tzinfo, timezone.utc)

    def test_overlap_subtraction_preserves_microsecond_precision(self):
        checkpoint = self.checkpoint_at(datetime(2026, 11, 1, 10, 0, 0, 123456, tzinfo=timezone.utc))
        now = datetime(2026, 11, 1, 10, 20, 0, 654321, tzinfo=timezone.utc)
        result = self.plan(checkpoint=checkpoint, current_time=now,
                           overlap=timedelta(minutes=5, microseconds=1))
        self.assertEqual(result.from_time, datetime(2026, 11, 1, 9, 55, 0, 123455, tzinfo=timezone.utc))
        self.assertEqual(result.to_time, now)

    def test_large_representable_overlap_has_no_arbitrary_limit(self):
        checkpoint = self.checkpoint_at(datetime(9999, 12, 31, tzinfo=timezone.utc))
        now = datetime(9999, 12, 31, 1, tzinfo=timezone.utc)
        result = self.plan(checkpoint=checkpoint, current_time=now, overlap=timedelta(days=3652058))
        self.assertEqual(result.from_time, datetime(1, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(result.to_time, now)

    def test_overlap_underflow_propagates_without_clamping(self):
        checkpoint = self.checkpoint_at(datetime(1, 1, 1, tzinfo=timezone.utc))
        with self.assertRaises(OverflowError):
            self.plan(checkpoint=checkpoint, current_time=datetime(1, 1, 2, tzinfo=timezone.utc),
                      overlap=timedelta(microseconds=1))

    def test_repeated_calls_are_deterministic(self):
        for _ in range(3):
            result = self.plan()
            self.assertEqual(result.from_time, datetime(2026, 11, 1, 9, 55, tzinfo=timezone.utc))
            self.assertEqual(result.to_time, self.now)

    def test_checkpoint_identity_boundary_and_audit_are_unchanged(self):
        self.plan()
        self.assertEqual(self.checkpoint.mt5_account_id, UUID("cc17f0e2-0784-4d99-81cc-ce44e990767c"))
        self.assertEqual(self.checkpoint.completed_through, datetime(2026, 11, 1, 10, tzinfo=timezone.utc))
        self.assertEqual(self.checkpoint.updated_at, datetime(2026, 11, 1, 10, 5, tzinfo=timezone.utc))

    def test_naive_current_time_is_rejected(self):
        with self.assertRaises(ValueError):
            self.plan(current_time=datetime(2026, 11, 1, 10, 20))

    def test_invalid_current_time_types_are_rejected(self):
        for now in (None, "2026-11-01T10:20:00+00:00", 123, timedelta(), False):
            with self.subTest(now=now), self.assertRaises(ValueError):
                self.plan(current_time=now)

    def test_unusable_current_time_offsets_are_rejected(self):
        for zone in (OffsetlessTimezone(), UnusableTimezone()):
            with self.subTest(zone=type(zone).__name__), self.assertRaises(ValueError):
                self.plan(current_time=datetime(2026, 11, 1, 10, 20, tzinfo=zone))

    def test_non_checkpoint_objects_are_rejected(self):
        for checkpoint in (None, "checkpoint", object(), self.completed):
            with self.subTest(checkpoint=checkpoint), self.assertRaises(TypeError):
                self.plan(checkpoint=checkpoint)

    def test_repeated_local_hour_future_check_uses_absolute_instants(self):
        zone = RepeatedHourTimezone()
        first = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
        second = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)
        result = self.plan(checkpoint=self.checkpoint_at(first), current_time=second, overlap=timedelta())
        self.assertEqual(result.from_time, datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))
        self.assertEqual(result.to_time, datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc))
        with self.assertRaises(ValueError):
            self.plan(checkpoint=self.checkpoint_at(second), current_time=first, overlap=timedelta(hours=2))


if __name__ == "__main__":
    unittest.main()
