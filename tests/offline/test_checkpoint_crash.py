"""Uncommitted/uncertain commit boundaries and cross-loop ownership."""

import asyncio
from contextlib import closing
from datetime import timedelta
import sqlite3
from threading import Event, Thread
from unittest.mock import patch
import unittest

import test_checkpoint_conditional as fixtures
from test_deal_normalizer import ledger


class CheckpointCrashTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.ConditionalCheckpointTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.repo, self.account, self.start, self.target = fixture.repo, fixture.account, fixture.start, fixture.target

    def test_uncommitted_checkpoint_update_is_rolled_back_on_cancellation(self):
        before = self.repo.advance_checkpoint(self.account, self.start)
        original = self.repo._connect

        class Connection:
            def __init__(inner):
                inner.conn = original()

            def execute(inner, sql, *args):
                result = inner.conn.execute(sql, *args)
                if sql.startswith("UPDATE ledger_checkpoints"):
                    raise asyncio.CancelledError("after UPDATE before commit")
                return result

            def __getattr__(inner, name):
                return getattr(inner.conn, name)

        with patch.object(self.repo, "_connect", side_effect=Connection):
            with self.assertRaises(asyncio.CancelledError):
                self.repo.advance_checkpoint_if_expected(self.account, self.target, expected=before)
        other = ledger.SQLiteDealRepository(self.repo.path)
        self.addCleanup(other.close)
        self.assertEqual(other.get_checkpoint(self.account.mt5_account_id), before)

    def test_commit_error_observes_durable_state_without_retry_before_or_after_actual_commit(self):
        original = self.repo._connect
        for did_commit in (False, True):
            calls = []

            class Connection:
                def __init__(inner):
                    inner.conn = original()

                def commit(inner):
                    calls.append("commit")
                    if did_commit:
                        inner.conn.commit()
                    raise sqlite3.OperationalError("commit return lost")

                def __getattr__(inner, name):
                    return getattr(inner.conn, name)

            with self.subTest(did_commit=did_commit), patch.object(self.repo, "_connect", side_effect=Connection):
                with self.assertRaises(ledger.CheckpointCommitError) as raised:
                    self.repo.advance_checkpoint_if_expected(self.account, self.target, expected=None)
            error = raised.exception
            self.assertIsNone(error.observation_error)
            durable = self.repo.get_checkpoint(self.account.mt5_account_id)
            self.assertEqual(error.checkpoint_after, durable)
            if did_commit:
                self.assertEqual(durable.completed_through, self.target)
            else:
                self.assertIsNone(durable)
            self.assertEqual(calls, ["commit"])

    def test_commit_error_with_failed_durable_read_does_not_claim_absence(self):
        original = self.repo._connect

        class Connection:
            def __init__(inner):
                inner.conn = original()

            def commit(inner):
                raise sqlite3.OperationalError("commit")

            def __getattr__(inner, name):
                return getattr(inner.conn, name)

        failure = sqlite3.OperationalError("durable state unavailable")
        with patch.object(self.repo, "_connect", side_effect=Connection), \
                patch.object(self.repo, "get_checkpoint", side_effect=failure) as read:
            with self.assertRaises(ledger.CheckpointCommitError) as raised:
                self.repo.advance_checkpoint_if_expected(self.account, self.target, expected=None)
        self.assertIs(raised.exception.observation_error, failure)
        read.assert_called_once_with(self.account.mt5_account_id)

    def test_writer_cannot_change_relevant_evidence_after_guard_before_checkpoint_commit(self):
        requested = ledger.RecoveryRange(self.start, self.target)
        expected = self.repo.read_snapshot(account=self.account, requested_range=requested)
        original = self.repo._snapshot_from_connection
        contender = sqlite3.connect(self.repo.path, isolation_level=None, timeout=0)
        self.addCleanup(contender.close)

        def guarded(conn, account, requested_range):
            result = original(conn, account, requested_range)
            with self.assertRaises(sqlite3.OperationalError):
                contender.execute("BEGIN IMMEDIATE")
            return result

        with patch.object(self.repo, "_snapshot_from_connection", side_effect=guarded):
            result = self.repo.advance_checkpoint_if_expected(self.account, self.target, expected=None,
                                                              expected_snapshot=expected)
        self.assertEqual(self.repo.get_checkpoint(self.account.mt5_account_id), result)
        contender.execute("BEGIN IMMEDIATE")
        contender.rollback()

    def test_ownership_handoff_across_two_thread_event_loops_and_cleanup(self):
        coordinator = ledger.AccountRecoveryCoordinator()
        held, queued, second_entered, release = Event(), Event(), Event(), Event()
        loops, tasks, failures = [], [], []

        def run(index):
            loop = asyncio.new_event_loop()
            loops.append(loop)

            async def work():
                if index:
                    queued.set()
                async with coordinator.hold(self.account.mt5_account_id):
                    if index:
                        second_entered.set()
                    else:
                        held.set()
                        ready = loop.create_future()
                        # The parent schedules readiness on this loop; no polling.
                        tasks.append(ready)
                        release.set()
                        await ready

            task = loop.create_task(work())
            try:
                loop.run_until_complete(task)
            except BaseException as exc:
                failures.append(exc)
            finally:
                loop.close()

        first = Thread(target=run, args=(0,), daemon=True)
        second = Thread(target=run, args=(1,), daemon=True)
        first.start()
        self.assertTrue(held.wait(3))
        self.assertTrue(release.wait(3))
        second.start()
        self.assertTrue(queued.wait(3))
        self.assertFalse(second_entered.is_set())
        loops[0].call_soon_threadsafe(tasks[0].set_result, None)
        first.join(5)
        second.join(5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(second_entered.is_set())
        self.assertEqual(coordinator._entries, {})


if __name__ == "__main__":
    unittest.main()
