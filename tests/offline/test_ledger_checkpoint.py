"""Checkpoint model contracts without MT5 or checkpoint persistence."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone, tzinfo
import unittest
from uuid import UUID

from test_deal_normalizer import ledger


class OffsetlessTimezone(tzinfo):
    def utcoffset(self, dt):
        return None


class UnusableTimezone(tzinfo):
    def utcoffset(self, dt):
        raise ValueError("offset unavailable")


class LedgerCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(ledger, "LedgerCheckpoint"), "LedgerCheckpoint must be publicly exported")
        self.account_id = UUID("cc17f0e2-0784-4d99-81cc-ce44e990767c")
        self.completed = datetime(2026, 10, 31, 10, tzinfo=timezone.utc)
        self.updated = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)

    def checkpoint(self, **changes):
        values = dict(mt5_account_id=self.account_id, completed_through=self.completed,
                      updated_at=self.updated)
        values.update(changes)
        return ledger.LedgerCheckpoint(**values)

    def test_valid_checkpoint_preserves_uuid_and_aware_utc_timestamps(self):
        checkpoint = self.checkpoint()
        self.assertEqual(checkpoint.mt5_account_id, self.account_id)
        self.assertEqual(checkpoint.completed_through, self.completed)
        self.assertEqual(checkpoint.updated_at, self.updated)
        self.assertIs(checkpoint.completed_through.tzinfo, timezone.utc)
        self.assertIs(checkpoint.updated_at.tzinfo, timezone.utc)

    def test_nil_uuid_is_rejected(self):
        with self.assertRaises(ValueError):
            self.checkpoint(mt5_account_id=UUID(int=0))

    def test_non_uuid_identity_is_rejected_without_coercion(self):
        for value in (str(self.account_id), 123, None, False):
            with self.subTest(value=value), self.assertRaises(TypeError):
                self.checkpoint(mt5_account_id=value)

    def test_naive_completed_through_is_rejected(self):
        with self.assertRaises(ValueError):
            self.checkpoint(completed_through=datetime(2026, 10, 31, 10))

    def test_naive_updated_at_is_rejected(self):
        with self.assertRaises(ValueError):
            self.checkpoint(updated_at=datetime(2026, 11, 1, 9))

    def test_non_datetime_timestamps_are_rejected(self):
        for name in ("completed_through", "updated_at"):
            for value in (None, "2026-10-31T10:00:00+00:00", 1793440800, False):
                with self.subTest(field=name, value=value), self.assertRaises(ValueError):
                    self.checkpoint(**{name: value})

    def test_offsetless_and_unusable_timezones_are_rejected(self):
        for name in ("completed_through", "updated_at"):
            for zone in (OffsetlessTimezone(), UnusableTimezone()):
                with self.subTest(field=name, zone=type(zone).__name__), self.assertRaises(ValueError):
                    self.checkpoint(**{name: datetime(2026, 10, 31, 10, tzinfo=zone)})

    def test_both_timestamps_are_normalized_to_utc(self):
        zone = timezone(timedelta(hours=7))
        checkpoint = self.checkpoint(
            completed_through=datetime(2026, 10, 31, 17, tzinfo=zone),
            updated_at=datetime(2026, 11, 1, 16, tzinfo=zone))
        self.assertEqual(checkpoint.completed_through, datetime(2026, 10, 31, 10, tzinfo=timezone.utc))
        self.assertEqual(checkpoint.updated_at, datetime(2026, 11, 1, 9, tzinfo=timezone.utc))
        self.assertIs(checkpoint.completed_through.tzinfo, timezone.utc)
        self.assertIs(checkpoint.updated_at.tzinfo, timezone.utc)

    def test_equivalent_instants_have_equal_canonical_values(self):
        other = self.checkpoint(
            completed_through=datetime(2026, 10, 31, 17, tzinfo=timezone(timedelta(hours=7))),
            updated_at=datetime(2026, 11, 1, 4, tzinfo=timezone(timedelta(hours=-5))))
        self.assertEqual(other, self.checkpoint())

    def test_utc_conversion_preserves_microseconds(self):
        zone = timezone(timedelta(hours=7))
        checkpoint = self.checkpoint(
            completed_through=datetime(2026, 10, 31, 17, 0, 0, 123456, tzinfo=zone),
            updated_at=datetime(2026, 11, 1, 16, 0, 0, 654321, tzinfo=zone))
        self.assertEqual(checkpoint.completed_through,
                         datetime(2026, 10, 31, 10, 0, 0, 123456, tzinfo=timezone.utc))
        self.assertEqual(checkpoint.updated_at,
                         datetime(2026, 11, 1, 9, 0, 0, 654321, tzinfo=timezone.utc))

    def test_checkpoint_fields_are_immutable(self):
        checkpoint = self.checkpoint()
        for name, value in (("mt5_account_id", UUID(int=1)),
                            ("completed_through", self.updated), ("updated_at", self.completed)):
            with self.subTest(field=name), self.assertRaises(FrozenInstanceError):
                setattr(checkpoint, name, value)

    def test_audit_time_may_precede_completed_boundary(self):
        checkpoint = self.checkpoint(updated_at=datetime(2026, 10, 30, 10, tzinfo=timezone.utc))
        self.assertLess(checkpoint.updated_at, checkpoint.completed_through)


if __name__ == "__main__":
    unittest.main()
