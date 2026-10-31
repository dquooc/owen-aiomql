"""Adversarial public-planner cases using only standard-library fixtures."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
import unittest
from uuid import UUID

from test_deal_normalizer import ledger
from test_ledger_checkpoint import OffsetlessTimezone
from test_ledger_checkpoint_timezone import RepeatedHourTimezone


class RaisingTimezone(tzinfo):
    """Count inspections and raise the exact supplied exception."""

    def __init__(self, failure):
        self.failure = failure
        self.calls = 0

    def utcoffset(self, dt):
        self.calls += 1
        raise self.failure


class InvalidOffsetTimezone(tzinfo):
    def __init__(self, offset):
        self.offset = offset

    def utcoffset(self, dt):
        return self.offset


class DistinctTimezone(tzinfo):
    """Fixed input offset with ordinary object identity/equality."""

    def utcoffset(self, dt):
        return timedelta(hours=7)


class UninspectableBootstrap:
    def __bool__(self):
        raise AssertionError("unused bootstrap truth value inspected")

    def __repr__(self):
        raise AssertionError("unused bootstrap representation inspected")

    def __str__(self):
        raise AssertionError("unused bootstrap string inspected")


class RecoveryAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.completed = datetime(2026, 11, 1, 10, tzinfo=timezone.utc)
        self.now = datetime(2026, 11, 1, 10, 20, tzinfo=timezone.utc)
        self.checkpoint = ledger.LedgerCheckpoint(
            UUID("cc17f0e2-0784-4d99-81cc-ce44e990767c"), self.completed,
            datetime(2026, 11, 1, 10, 5, tzinfo=timezone.utc))

    def plan(self, **changes):
        values = dict(checkpoint=self.checkpoint, current_time=self.now,
                      overlap=timedelta(minutes=5), bootstrap_start=self.completed)
        values.update(changes)
        return ledger.plan_recovery_range(**values)

    def assert_range(self, result, start, end):
        self.assertIsInstance(result, ledger.RecoveryRange)
        self.assertEqual((result.from_time, result.to_time), (start, end))
        self.assertIs(result.from_time.tzinfo, timezone.utc)
        self.assertIs(result.to_time.tzinfo, timezone.utc)

    def test_booleans_are_rejected_for_every_active_argument(self):
        for value in (True, False):
            for field, error in (("checkpoint", TypeError), ("current_time", ValueError),
                                 ("overlap", TypeError), ("bootstrap_start", ValueError)):
                args = {field: value}
                if field == "bootstrap_start":
                    args["checkpoint"] = None
                with self.subTest(field=field, value=value), self.assertRaises(error):
                    self.plan(**args)

    def test_timedelta_max_underflows_checkpoint_but_does_not_shift_bootstrap(self):
        with self.assertRaises(OverflowError):
            self.plan(overlap=timedelta.max)
        self.assert_range(self.plan(checkpoint=None, overlap=timedelta.max), self.completed, self.now)

    def test_timedelta_min_is_rejected_before_either_branch(self):
        for checkpoint in (self.checkpoint, None):
            with self.subTest(checkpoint=checkpoint), self.assertRaisesRegex(ValueError, "overlap"):
                self.plan(checkpoint=checkpoint, overlap=timedelta.min, bootstrap_start=None)

    def test_aware_datetime_limits_are_valid_normal_ranges(self):
        lower = datetime(1, 1, 1, tzinfo=timezone.utc)
        upper = datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
        for bound in (lower, upper):
            checkpoint = replace(self.checkpoint, completed_through=bound)
            with self.subTest(bound=bound):
                self.assert_range(self.plan(checkpoint=checkpoint, current_time=bound, overlap=timedelta()),
                                  bound, bound)
                self.assert_range(self.plan(checkpoint=None, bootstrap_start=bound, current_time=bound),
                                  bound, bound)
        self.assert_range(self.plan(checkpoint=None, bootstrap_start=lower, current_time=upper), lower, upper)

    def test_one_microsecond_overlap_can_reach_exact_datetime_min(self):
        completed = datetime(1, 1, 1, 0, 0, 0, 1, tzinfo=timezone.utc)
        now = datetime(1, 1, 1, 0, 0, 0, 2, tzinfo=timezone.utc)
        result = self.plan(checkpoint=replace(self.checkpoint, completed_through=completed),
                           current_time=now, overlap=timedelta(microseconds=1))
        self.assert_range(result, datetime(1, 1, 1, tzinfo=timezone.utc), now)

    def test_representable_large_overlap_at_datetime_max_keeps_precision(self):
        upper = datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
        result = self.plan(checkpoint=replace(self.checkpoint, completed_through=upper),
                           current_time=upper, overlap=timedelta(days=3652058))
        self.assert_range(result, datetime(1, 1, 1, 23, 59, 59, 999999, tzinfo=timezone.utc), upper)

    def test_near_24_hour_offsets_normalize_at_both_datetime_limits(self):
        offset = timedelta(hours=23, minutes=59, seconds=59, microseconds=999999)
        east, west = timezone(offset), timezone(-offset)
        cases = (
            (datetime(1, 1, 2, tzinfo=east), datetime(1, 1, 2, 0, 0, 0, 1, tzinfo=east),
             datetime(1, 1, 1, tzinfo=timezone.utc), datetime(1, 1, 1, 0, 0, 0, 2, tzinfo=timezone.utc)),
            (datetime(9999, 12, 30, 23, 59, 59, 999999, tzinfo=west), datetime(9999, 12, 31, tzinfo=west),
             datetime(9999, 12, 31, 23, 59, 59, 999997, tzinfo=timezone.utc),
             datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)),
        )
        for completed, now, start, end in cases:
            with self.subTest(year=now.year):
                checkpoint = replace(self.checkpoint, completed_through=completed)
                self.assert_range(self.plan(checkpoint=checkpoint, current_time=now,
                                  overlap=timedelta(microseconds=1)), start, end)

    def test_upper_utc_conversion_overflow_is_not_wrapped_or_clamped(self):
        zone = timezone(-timedelta(hours=23, minutes=59, seconds=59, microseconds=999999))
        invalid = datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=zone)
        for checkpoint in (self.checkpoint, None):
            with self.subTest(checkpoint=checkpoint), self.assertRaises(OverflowError):
                self.plan(checkpoint=checkpoint, current_time=invalid)
        with self.assertRaises(OverflowError):
            self.plan(checkpoint=None, bootstrap_start=invalid,
                      current_time=datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc))

    def test_public_dispatch_uses_absolute_order_during_repeated_hour(self):
        zone = RepeatedHourTimezone()
        first = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
        second = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)
        for checkpoint in (replace(self.checkpoint, completed_through=first), None):
            with self.subTest(checkpoint=checkpoint):
                self.assert_range(self.plan(checkpoint=checkpoint, bootstrap_start=first,
                                  current_time=second, overlap=timedelta()),
                                  datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc),
                                  datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc))
        for checkpoint in (replace(self.checkpoint, completed_through=second), None):
            with self.subTest(checkpoint=checkpoint), self.assertRaises(ValueError):
                self.plan(checkpoint=checkpoint, bootstrap_start=second,
                          current_time=first, overlap=timedelta(hours=2))
        self.assertEqual((first.fold, second.fold), (0, 1))
        self.assertIs(first.tzinfo, zone)
        self.assertIs(second.tzinfo, zone)

    def test_distinct_timezone_objects_yield_equal_deterministic_ranges(self):
        first_zone, second_zone = DistinctTimezone(), DistinctTimezone()
        self.assertIsNot(first_zone, second_zone)
        self.assertNotEqual(first_zone, second_zone)
        expected = ledger.RecoveryRange(datetime(2026, 11, 1, 9, 55, tzinfo=timezone.utc), self.now)
        for zone in (first_zone, second_zone):
            completed = datetime(2026, 11, 1, 17, tzinfo=zone)
            now = datetime(2026, 11, 1, 17, 20, tzinfo=zone)
            checkpoint = replace(self.checkpoint, completed_through=completed)
            for _ in range(3):
                self.assertEqual(self.plan(checkpoint=checkpoint, current_time=now), expected)
                self.assertEqual(self.plan(checkpoint=None, bootstrap_start=completed, current_time=now),
                                 ledger.RecoveryRange(self.completed, self.now))
            self.assertEqual((completed.hour, now.hour), (17, 17))
            self.assertIs(completed.tzinfo, zone)
            self.assertIs(now.tzinfo, zone)

    def test_selected_timezone_exceptions_propagate_by_identity(self):
        for field, checkpoint in (("current_time", self.checkpoint), ("current_time", None),
                                  ("bootstrap_start", None)):
            failure = RuntimeError("controlled timezone failure")
            zone = RaisingTimezone(failure)
            args = {field: datetime(2026, 11, 1, 10, tzinfo=zone), "checkpoint": checkpoint}
            with self.subTest(field=field, checkpoint=checkpoint):
                for _ in range(2):
                    with self.assertRaises(RuntimeError) as raised:
                        self.plan(**args)
                    self.assertIs(raised.exception, failure)

    def test_invalid_offset_values_preserve_model_exception_types(self):
        for offset, error in ((timedelta(hours=24), ValueError), (timedelta(hours=-24), ValueError),
                              (1, TypeError)):
            invalid = datetime(2026, 11, 1, 10, tzinfo=InvalidOffsetTimezone(offset))
            for field, checkpoint in (("current_time", self.checkpoint), ("current_time", None),
                                      ("bootstrap_start", None)):
                with self.subTest(offset=offset, field=field, checkpoint=checkpoint):
                    with self.assertRaises(error):
                        self.plan(**{field: invalid, "checkpoint": checkpoint})

    def test_dormant_bootstrap_is_not_inspected_even_when_hostile(self):
        zone = RaisingTimezone(AssertionError("dormant timezone inspected"))
        values = (object(), True, False, "bad", UninspectableBootstrap(), datetime(2026, 11, 1, 10),
                  datetime(2026, 11, 1, 10, tzinfo=OffsetlessTimezone()),
                  datetime(2026, 11, 1, 10, tzinfo=zone),
                  datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc),
                  datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1))))
        for index, bootstrap in enumerate(values):
            with self.subTest(index=index):
                self.assert_range(self.plan(bootstrap_start=bootstrap),
                                  datetime(2026, 11, 1, 9, 55, tzinfo=timezone.utc), self.now)
        self.assertEqual(zone.calls, 0)

    def test_invalid_checkpoint_preempts_all_hostile_other_arguments(self):
        current_zone = RaisingTimezone(AssertionError("current time inspected too early"))
        bootstrap_zone = RaisingTimezone(AssertionError("bootstrap inspected too early"))
        with self.assertRaisesRegex(TypeError, "checkpoint"):
            self.plan(checkpoint=object(), current_time=datetime(2026, 11, 1, 10, tzinfo=current_zone),
                      overlap=object(), bootstrap_start=datetime(2026, 11, 1, 10, tzinfo=bootstrap_zone))
        self.assertEqual((current_zone.calls, bootstrap_zone.calls), (0, 0))

    def test_hostile_current_preempts_negative_overlap_and_missing_bootstrap(self):
        failure = RuntimeError("current time failure must win")
        with self.assertRaises(RuntimeError) as raised:
            self.plan(checkpoint=None, current_time=datetime(2026, 11, 1, 10, tzinfo=RaisingTimezone(failure)),
                      overlap=timedelta(microseconds=-1), bootstrap_start=None)
        self.assertIs(raised.exception, failure)

    def test_bad_overlap_preempts_inspection_of_hostile_bootstrap(self):
        for checkpoint, overlap, error in ((None, object(), TypeError),
                                           (self.checkpoint, timedelta(microseconds=-1), ValueError)):
            zone = RaisingTimezone(AssertionError("bootstrap inspected before overlap validation"))
            with self.subTest(checkpoint=checkpoint), self.assertRaisesRegex(error, "overlap"):
                self.plan(checkpoint=checkpoint, overlap=overlap,
                          bootstrap_start=datetime(2026, 11, 1, 10, tzinfo=zone))
            self.assertEqual(zone.calls, 0)

    def test_account_uuid_does_not_change_range_calculation(self):
        other = replace(self.checkpoint, mt5_account_id=UUID("ac647462-f801-44f7-a60f-6b6c86c2b9e1"))
        expected = ledger.RecoveryRange(datetime(2026, 11, 1, 9, 55, tzinfo=timezone.utc), self.now)
        self.assertEqual(self.plan(), expected)
        self.assertEqual(self.plan(checkpoint=other), expected)

    def test_audit_time_does_not_change_range_calculation(self):
        for audit_time in (datetime(1, 1, 1, tzinfo=timezone.utc),
                           datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)):
            with self.subTest(audit_time=audit_time):
                self.assert_range(self.plan(checkpoint=replace(self.checkpoint, updated_at=audit_time)),
                                  datetime(2026, 11, 1, 9, 55, tzinfo=timezone.utc), self.now)

    def test_planner_preserves_checkpoint_field_objects(self):
        identity = self.checkpoint.mt5_account_id
        completed = self.checkpoint.completed_through
        audit_time = self.checkpoint.updated_at
        self.plan()
        self.assertIs(self.checkpoint.mt5_account_id, identity)
        self.assertIs(self.checkpoint.completed_through, completed)
        self.assertIs(self.checkpoint.updated_at, audit_time)


if __name__ == "__main__":
    unittest.main()
