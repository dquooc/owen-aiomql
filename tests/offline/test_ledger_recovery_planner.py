"""Public recovery dispatch and validation precedence, entirely offline."""

from datetime import datetime, timedelta, timezone
import importlib
import unittest
from uuid import UUID

from test_deal_normalizer import PACKAGE, ledger
from test_ledger_checkpoint import OffsetlessTimezone, UnusableTimezone


class RecoveryPlannerTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(ledger, "plan_recovery_range"), "public recovery planner must exist")
        self.completed = datetime(2026, 11, 1, 10, tzinfo=timezone.utc)
        self.now = datetime(2026, 11, 1, 10, 20, tzinfo=timezone.utc)
        self.bootstrap = datetime(2026, 10, 31, 10, tzinfo=timezone.utc)
        self.checkpoint = ledger.LedgerCheckpoint(
            UUID("cc17f0e2-0784-4d99-81cc-ce44e990767c"), self.completed,
            datetime(2026, 11, 1, 10, 5, tzinfo=timezone.utc))

    def plan(self, **changes):
        values = dict(checkpoint=self.checkpoint, current_time=self.now,
                      overlap=timedelta(minutes=5), bootstrap_start=self.bootstrap)
        values.update(changes)
        return ledger.plan_recovery_range(**values)

    def assert_range(self, result, start, end):
        self.assertIsInstance(result, ledger.RecoveryRange)
        self.assertEqual((result.from_time, result.to_time), (start, end))
        self.assertIs(result.from_time.tzinfo, timezone.utc)
        self.assertIs(result.to_time.tzinfo, timezone.utc)

    def test_checkpoint_dispatch_returns_overlap_range(self):
        self.assert_range(self.plan(), datetime(2026, 11, 1, 9, 55, tzinfo=timezone.utc), self.now)

    def test_bootstrap_dispatch_uses_start_without_subtracting_overlap(self):
        self.assert_range(self.plan(checkpoint=None), self.bootstrap, self.now)

    def test_invalid_checkpoint_types_are_not_treated_as_missing(self):
        for checkpoint in ("bad", 123, object(), ledger.RecoveryRange(self.completed, self.now), False):
            with self.subTest(checkpoint=checkpoint), self.assertRaisesRegex(TypeError, "checkpoint"):
                self.plan(checkpoint=checkpoint)

    def test_checkpoint_ignores_unused_bootstrap_including_case_d(self):
        values = (None, "garbage", 123, datetime(2026, 11, 1, 10),
                  datetime(2030, 1, 1, tzinfo=timezone.utc),
                  datetime(2026, 11, 1, 10, tzinfo=OffsetlessTimezone()),
                  datetime(2026, 11, 1, 10, tzinfo=UnusableTimezone()))
        for bootstrap in values:
            with self.subTest(bootstrap=bootstrap):
                self.assert_range(self.plan(bootstrap_start=bootstrap),
                                  datetime(2026, 11, 1, 9, 55, tzinfo=timezone.utc), self.now)

    def test_bootstrap_none_is_rejected_with_useful_context(self):
        with self.assertRaises(ValueError) as raised:
            self.plan(checkpoint=None, bootstrap_start=None)
        self.assertIn("bootstrap_start", str(raised.exception))
        self.assertIn("checkpoint", str(raised.exception))

    def test_omitted_bootstrap_uses_missing_default_only_without_checkpoint(self):
        with self.assertRaisesRegex(ValueError, "bootstrap_start"):
            ledger.plan_recovery_range(checkpoint=None, current_time=self.now, overlap=timedelta())
        result = ledger.plan_recovery_range(checkpoint=self.checkpoint, current_time=self.now,
                                           overlap=timedelta())
        self.assert_range(result, self.completed, self.now)

    def test_invalid_bootstrap_is_rejected_when_selected(self):
        for bootstrap in ("garbage", 123, datetime(2026, 11, 1, 10),
                          datetime(2026, 11, 1, 10, tzinfo=OffsetlessTimezone()),
                          datetime(2026, 11, 1, 10, tzinfo=UnusableTimezone())):
            with self.subTest(bootstrap=bootstrap), self.assertRaises(ValueError):
                self.plan(checkpoint=None, bootstrap_start=bootstrap)

    def test_invalid_current_time_types_fail_in_both_branches(self):
        for checkpoint in (self.checkpoint, None):
            for now in (None, "2026-11-01T10:20:00+00:00", 123, False):
                with self.subTest(checkpoint=checkpoint, now=now), self.assertRaises(ValueError):
                    self.plan(checkpoint=checkpoint, current_time=now)

    def test_naive_current_time_fails_in_both_branches(self):
        for checkpoint in (self.checkpoint, None):
            with self.subTest(checkpoint=checkpoint), self.assertRaises(ValueError):
                self.plan(checkpoint=checkpoint, current_time=datetime(2026, 11, 1, 10, 20))

    def test_unusable_current_time_offsets_fail_in_both_branches(self):
        for checkpoint in (self.checkpoint, None):
            for zone in (OffsetlessTimezone(), UnusableTimezone()):
                with self.subTest(checkpoint=checkpoint, zone=type(zone).__name__), self.assertRaises(ValueError):
                    self.plan(checkpoint=checkpoint, current_time=datetime(2026, 11, 1, 10, 20, tzinfo=zone))

    def test_overlap_type_is_checked_in_both_branches(self):
        for checkpoint in (self.checkpoint, None):
            for overlap in (None, 5, "5 minutes", self.now, False):
                with self.subTest(checkpoint=checkpoint, overlap=overlap), self.assertRaisesRegex(TypeError, "overlap"):
                    self.plan(checkpoint=checkpoint, overlap=overlap)

    def test_negative_overlap_is_checked_in_both_branches(self):
        for checkpoint in (self.checkpoint, None):
            with self.subTest(checkpoint=checkpoint), self.assertRaisesRegex(ValueError, "overlap"):
                self.plan(checkpoint=checkpoint, overlap=timedelta(microseconds=-1))

    def test_zero_overlap_is_valid_in_both_branches(self):
        self.assert_range(self.plan(overlap=timedelta()), self.completed, self.now)
        self.assert_range(self.plan(checkpoint=None, overlap=timedelta()), self.bootstrap, self.now)

    def test_future_checkpoint_is_rejected_even_if_overlap_masks_it(self):
        checkpoint = ledger.LedgerCheckpoint(self.checkpoint.mt5_account_id,
            datetime(2026, 11, 1, 10, 30, tzinfo=timezone.utc), self.checkpoint.updated_at)
        with self.assertRaises(ValueError):
            self.plan(checkpoint=checkpoint, overlap=timedelta(minutes=30))

    def test_future_bootstrap_start_is_rejected(self):
        with self.assertRaises(ValueError):
            self.plan(checkpoint=None, bootstrap_start=datetime(2026, 11, 1, 10, 30, tzinfo=timezone.utc))

    def test_case_a_checkpoint_error_precedes_current_and_overlap_errors(self):
        with self.assertRaisesRegex(TypeError, "checkpoint"):
            self.plan(checkpoint="bad", current_time=datetime(2026, 11, 1, 10),
                      overlap=timedelta(seconds=-1))

    def test_case_b_current_error_precedes_overlap_type_error(self):
        with self.assertRaises(ValueError):
            self.plan(current_time=datetime(2026, 11, 1, 10), overlap="bad")

    def test_case_c_negative_overlap_precedes_missing_bootstrap(self):
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.plan(checkpoint=None, overlap=timedelta(seconds=-1), bootstrap_start=None)

    def test_current_error_precedes_bootstrap_validation(self):
        with self.assertRaises(ValueError) as raised:
            self.plan(checkpoint=None, current_time=datetime(2026, 11, 1, 10), bootstrap_start=None)
        self.assertIn("timezone-aware", str(raised.exception))

    def test_invalid_overlap_precedes_future_checkpoint_error(self):
        for overlap, error in (("bad", TypeError), (timedelta(seconds=-1), ValueError)):
            with self.subTest(overlap=overlap), self.assertRaisesRegex(error, "overlap"):
                self.plan(current_time=datetime(2026, 11, 1, 9, tzinfo=timezone.utc), overlap=overlap)

    def test_equivalent_offsets_produce_same_ranges_in_both_branches(self):
        zone = timezone(timedelta(hours=7))
        checkpoint = ledger.LedgerCheckpoint(self.checkpoint.mt5_account_id,
            datetime(2026, 11, 1, 17, tzinfo=zone), self.checkpoint.updated_at)
        now = datetime(2026, 11, 1, 17, 20, tzinfo=zone)
        self.assert_range(self.plan(checkpoint=checkpoint, current_time=now),
                          datetime(2026, 11, 1, 9, 55, tzinfo=timezone.utc), self.now)
        self.assert_range(self.plan(checkpoint=None, current_time=now,
                          bootstrap_start=datetime(2026, 10, 31, 17, tzinfo=zone)), self.bootstrap, self.now)

    def test_fractional_hour_offset_is_normalized_in_both_branches(self):
        zone = timezone(timedelta(hours=5, minutes=30))
        now = datetime(2026, 11, 1, 15, 50, tzinfo=zone)
        self.assert_range(self.plan(current_time=now),
                          datetime(2026, 11, 1, 9, 55, tzinfo=timezone.utc), self.now)
        self.assert_range(self.plan(checkpoint=None, current_time=now,
                          bootstrap_start=datetime(2026, 10, 31, 15, 30, tzinfo=zone)), self.bootstrap, self.now)

    def test_microseconds_are_preserved_in_both_branches(self):
        completed = datetime(2026, 11, 1, 10, 0, 0, 123456, tzinfo=timezone.utc)
        now = datetime(2026, 11, 1, 10, 20, 0, 654321, tzinfo=timezone.utc)
        checkpoint = ledger.LedgerCheckpoint(self.checkpoint.mt5_account_id, completed, self.checkpoint.updated_at)
        self.assert_range(self.plan(checkpoint=checkpoint, current_time=now, overlap=timedelta(microseconds=1)),
                          datetime(2026, 11, 1, 10, 0, 0, 123455, tzinfo=timezone.utc), now)
        self.assert_range(self.plan(checkpoint=None, bootstrap_start=completed, current_time=now), completed, now)

    def test_equal_endpoints_are_valid_in_both_branches(self):
        self.assert_range(self.plan(current_time=self.completed, overlap=timedelta()),
                          self.completed, self.completed)
        self.assert_range(self.plan(checkpoint=None, bootstrap_start=self.now), self.now, self.now)

    def test_repeated_calls_are_deterministic_in_both_branches(self):
        for checkpoint, start in ((self.checkpoint, datetime(2026, 11, 1, 9, 55, tzinfo=timezone.utc)),
                                  (None, self.bootstrap)):
            for _ in range(3):
                with self.subTest(checkpoint=checkpoint):
                    self.assert_range(self.plan(checkpoint=checkpoint), start, self.now)

    def test_planning_preserves_checkpoint_and_input_values(self):
        zone = timezone(timedelta(hours=7))
        now = datetime(2026, 11, 1, 17, 20, tzinfo=zone)
        bootstrap = datetime(2026, 10, 31, 17, tzinfo=zone)
        overlap = timedelta(minutes=5)
        self.plan(current_time=now, bootstrap_start=bootstrap, overlap=overlap)
        self.plan(checkpoint=None, current_time=now, bootstrap_start=bootstrap, overlap=overlap)
        self.assertEqual(self.checkpoint.mt5_account_id, UUID("cc17f0e2-0784-4d99-81cc-ce44e990767c"))
        self.assertEqual(self.checkpoint.completed_through, self.completed)
        self.assertEqual(self.checkpoint.updated_at, datetime(2026, 11, 1, 10, 5, tzinfo=timezone.utc))
        self.assertEqual(now.isoformat(), "2026-11-01T17:20:00+07:00")
        self.assertEqual(bootstrap.isoformat(), "2026-10-31T17:00:00+07:00")
        self.assertEqual(overlap, timedelta(seconds=300))

    def test_checkpoint_overlap_underflow_propagates(self):
        checkpoint = ledger.LedgerCheckpoint(self.checkpoint.mt5_account_id,
            datetime(1, 1, 1, tzinfo=timezone.utc), self.checkpoint.updated_at)
        with self.assertRaises(OverflowError):
            self.plan(checkpoint=checkpoint, overlap=timedelta(microseconds=1))

    def test_current_utc_conversion_overflow_precedes_overlap_in_both_branches(self):
        now = datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1)))
        for checkpoint in (self.checkpoint, None):
            with self.subTest(checkpoint=checkpoint), self.assertRaises(OverflowError):
                self.plan(checkpoint=checkpoint, current_time=now, overlap="bad")

    def test_selected_bootstrap_utc_conversion_overflow_propagates(self):
        bootstrap = datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1)))
        with self.assertRaises(OverflowError):
            self.plan(checkpoint=None, bootstrap_start=bootstrap)

    def test_public_export_works_without_exporting_private_helpers(self):
        recovery = importlib.import_module(f"{PACKAGE}.recovery")
        self.assertIs(ledger.plan_recovery_range, recovery.plan_recovery_range)
        self.assertIn("plan_recovery_range", ledger.__all__)
        for name in ("_plan_bootstrap_range", "_plan_checkpoint_range"):
            self.assertNotIn(name, ledger.__all__)
            self.assertFalse(hasattr(ledger, name))

    def test_required_keyword_arguments_have_no_defaults(self):
        values = dict(checkpoint=self.checkpoint, current_time=self.now, overlap=timedelta())
        for name in values:
            incomplete = {key: value for key, value in values.items() if key != name}
            with self.subTest(missing=name), self.assertRaisesRegex(TypeError, name):
                ledger.plan_recovery_range(**incomplete)
        with self.assertRaises(TypeError):
            ledger.plan_recovery_range(self.checkpoint, self.now, timedelta())


if __name__ == "__main__":
    unittest.main()
