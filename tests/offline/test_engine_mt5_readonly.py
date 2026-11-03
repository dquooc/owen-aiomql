"""A16 engine integration with an inert SDK, never native MetaTrader5."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))


class FakeSDK:
    def __init__(self):
        self.events = []
        self.account = SimpleNamespace(login=123, server='demo-server')
        self.terminal = SimpleNamespace(connected=True)
        self.result = ()
        self.initialized = True
        self.failure = None
        self.after_fetch = None
        self.mutations = 0

    def initialize(self, *, timeout):
        self.events.append(('initialize', timeout))
        if self.failure:
            raise self.failure
        return self.initialized

    def account_info(self):
        self.events.append('account')
        return self.account

    def terminal_info(self):
        self.events.append('terminal')
        return self.terminal

    def history_deals_get(self, start, end):
        self.events.append(('history', start, end))
        if self.after_fetch is not None:
            self.account = self.after_fetch
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    def shutdown(self):
        self.events.append('shutdown')

    def order_send(self, *args, **kwargs):
        self.mutations += 1
        raise AssertionError('native mutation touched')

    position_close = order_send
    Buy = order_send
    Sell = order_send


class EngineMT5ReadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('aiomql_engine.mt5_readonly'),
                             'A16 read-only session module is not implemented')
        from aiomql_engine.config import EngineConfig, EngineMode
        from aiomql_engine.mt5_readonly import MT5ReadOnlySession, ReadOnlySessionError
        self.Session, self.Error = MT5ReadOnlySession, ReadOnlySessionError
        self.config = EngineConfig(mode=EngineMode.READ_ONLY, mt5_enabled=True,
                                   mt5_expected_login=123, mt5_expected_server='demo-server')
        self.sdk = FakeSDK()
        self.loads = 0
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.end = self.start + timedelta(days=1)
        self.addCleanup(self.assert_no_mutations)

    def assert_no_mutations(self):
        self.assertEqual(self.sdk.mutations, 0)

    def loader(self):
        self.loads += 1
        return self.sdk

    def session(self):
        session = self.Session(self.config, sdk_loader=self.loader)
        self.addCleanup(session.shutdown)
        return session

    def history(self, session, **kwargs):
        return asyncio.run(session.history_deals_get(
            date_from=kwargs.get('start', self.start), date_to=kwargs.get('end', self.end)))

    def test_session_construction_is_lazy_and_open_shutdown_are_once(self):
        session = self.session()
        self.assertEqual((self.loads, self.sdk.events), (0, []))
        session.open()
        session.open()
        self.assertTrue(asyncio.run(session.terminal_info()).connected)
        self.assertEqual(asyncio.run(session.account_info()).login, 123)
        session.shutdown()
        session.shutdown()
        self.assertEqual(self.loads, 1)
        self.assertEqual(self.sdk.events.count(('initialize', 10000)), 1)
        self.assertEqual(self.sdk.events.count('shutdown'), 1)
        with self.assertRaises(self.Error):
            session.open()

    def test_history_has_exact_boundaries_and_before_after_account_guards(self):
        session = self.session()
        session.open()
        self.sdk.events.clear()
        self.sdk.result = ({'ticket': 1},)
        self.assertEqual(self.history(session), ({'ticket': 1},))
        self.assertEqual(self.sdk.events, ['account', 'terminal',
                                          ('history', self.start, self.end), 'account'])

    def test_history_utc_conversion_preserves_microseconds_and_equal_endpoints(self):
        session = self.session()
        session.open()
        local = datetime(2026, 1, 1, 7, 0, 0, 123456,
                         tzinfo=timezone(timedelta(hours=7)))
        self.history(session, start=local, end=local)
        utc = datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=timezone.utc)
        self.assertIn(('history', utc, utc), self.sdk.events)

    def test_invalid_or_excessive_history_ranges_fail_before_fetch(self):
        for start, end in ((self.start.replace(tzinfo=None), self.end),
                           (self.end, self.start), (self.start, self.start + timedelta(days=8))):
            with self.subTest(start=start):
                session = self.session()
                session.open()
                with self.assertRaises((ValueError, self.Error)):
                    self.history(session, start=start, end=end)
        self.assertFalse(any(isinstance(e, tuple) and e[0] == 'history' for e in self.sdk.events))

    def test_history_before_open_or_after_shutdown_never_fetches(self):
        session = self.session()
        for closed in (False, True):
            if closed:
                session.shutdown()
            with self.assertRaises(self.Error):
                self.history(session)
        self.assertEqual(self.sdk.events, [])

    def test_failed_initialization_shuts_down_and_cannot_retry(self):
        self.sdk.initialized = False
        session = self.session()
        with self.assertRaises(self.Error):
            session.open()
        with self.assertRaises(self.Error):
            session.open()
        session.shutdown()
        self.assertEqual(self.sdk.events, [('initialize', 10000), 'shutdown'])

    def test_sdk_exception_is_sanitized_and_cleanup_once(self):
        self.sdk.failure = RuntimeError('fake-password-secret')
        session = self.session()
        with self.assertRaises(self.Error) as caught:
            session.open()
        self.assertNotIn('fake-password-secret', str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(self.sdk.events.count('shutdown'), 1)

    def test_account_metadata_exception_is_sanitized_at_startup(self):
        class BrokenAccount:
            @property
            def login(self):
                raise RuntimeError('fake-password-secret')
        self.sdk.account = BrokenAccount()
        with self.assertRaises(self.Error) as caught:
            self.session().open()
        self.assertNotIn('fake-password-secret', str(caught.exception))
        self.assertEqual(self.sdk.events.count('shutdown'), 1)

    def test_shutdown_exception_is_sanitized_and_never_retried(self):
        def fail():
            self.sdk.events.append('shutdown')
            raise RuntimeError('fake-password-secret')
        self.sdk.shutdown = fail
        session = self.session()
        session.open()
        with self.assertRaises(self.Error) as caught:
            session.shutdown()
        self.assertNotIn('fake-password-secret', str(caught.exception))
        session.shutdown()
        self.assertEqual(self.sdk.events.count('shutdown'), 1)

    def test_initialize_cancellation_closes_once(self):
        self.sdk.failure = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            self.session().open()
        self.assertEqual(self.sdk.events.count('shutdown'), 1)

    def test_missing_sdk_does_not_initialize_and_error_is_sanitized(self):
        def fail():
            raise ImportError('fake-secret-path')
        session = self.Session(self.config, sdk_loader=fail)
        with self.assertRaises(self.Error) as caught:
            session.open()
        self.assertNotIn('fake-secret-path', str(caught.exception))
        self.assertEqual(self.sdk.events, [])

    def test_bad_account_at_startup_fails_closed_and_cleans_up(self):
        for account in (None, SimpleNamespace(login=999, server='demo-server'),
                        SimpleNamespace(login=123, server='other-server')):
            self.sdk.account = account
            session = self.session()
            with self.assertRaises(self.Error):
                session.open()
            self.assertEqual(self.sdk.events[-1], 'shutdown')
        self.assertFalse(any(isinstance(e, tuple) and e[0] == 'history' for e in self.sdk.events))

    def test_disconnected_or_missing_terminal_fails_startup(self):
        for terminal in (None, SimpleNamespace(connected=False)):
            self.sdk.terminal = terminal
            with self.assertRaises(self.Error):
                self.session().open()
            self.assertEqual(self.sdk.events[-1], 'shutdown')

    def test_account_switch_before_history_stops_before_fetch(self):
        session = self.session()
        session.open()
        self.sdk.account = SimpleNamespace(login=999, server='demo-server')
        with self.assertRaises(self.Error):
            self.history(session)
        self.assertFalse(any(isinstance(e, tuple) and e[0] == 'history' for e in self.sdk.events))
        self.assertEqual(self.sdk.events[-1], 'shutdown')

    def test_account_switch_after_history_discards_batch_and_closes(self):
        session = self.session()
        session.open()
        self.sdk.after_fetch = SimpleNamespace(login=999, server='demo-server')
        with self.assertRaises(self.Error):
            self.history(session)
        self.assertEqual(self.sdk.events[-2:], ['account', 'shutdown'])

    def test_disconnected_terminal_prevents_history(self):
        session = self.session()
        session.open()
        self.sdk.terminal = SimpleNamespace(connected=False)
        with self.assertRaises(self.Error):
            self.history(session)
        self.assertFalse(any(isinstance(e, tuple) and e[0] == 'history' for e in self.sdk.events))

    def test_history_none_and_sdk_error_are_not_empty_success(self):
        for result in (None, RuntimeError('fake-password-secret'), asyncio.CancelledError(), KeyboardInterrupt()):
            session = self.session()
            session.open()
            self.sdk.result = result
            expected = type(result) if isinstance(result, (asyncio.CancelledError, KeyboardInterrupt)) else self.Error
            with self.assertRaises(expected) as caught:
                self.history(session)
            self.assertNotIn('fake-password-secret', str(caught.exception))
            self.assertEqual(self.sdk.events[-1], 'shutdown')

    def test_cancelled_read_releases_session(self):
        session = self.session()
        session.open()
        async def cancel():
            task = asyncio.current_task()
            task.cancel()
            await session.account_info()
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(cancel())
        self.assertEqual(self.sdk.events[-1], 'shutdown')

    def test_no_arbitrary_sdk_or_broker_write_surface(self):
        session = self.session()
        session.open()
        for name in ('sdk', 'client', 'mt5', 'order_send', 'Buy', 'Sell', 'position_close', 'login'):
            with self.subTest(name=name), self.assertRaises(AttributeError):
                getattr(session, name)
        self.assertNotIn('demo-server', repr(session))

    def test_composition_runtime_owns_session_without_recovery_or_history(self):
        from aiomql_engine import compose_runtime
        runtime = compose_runtime(self.config, session_factory=lambda config: self.Session(config, sdk_loader=self.loader))
        self.addCleanup(runtime.shutdown)
        self.assertEqual(self.loads, 0)
        self.assertEqual(runtime.run(), 0)
        self.assertEqual(asyncio.run(runtime.read_only_session.account_info()).login, 123)
        runtime.shutdown()
        runtime.shutdown()
        self.assertEqual(self.sdk.events.count('shutdown'), 1)
        self.assertFalse(any(isinstance(e, tuple) and e[0] == 'history' for e in self.sdk.events))

    def test_run_engine_always_shuts_down_real_session(self):
        from aiomql_engine import run_engine
        self.assertEqual(run_engine(self.config, session_factory=lambda c: self.Session(c, sdk_loader=self.loader)), 0)
        self.assertEqual(self.sdk.events.count('shutdown'), 1)

    def test_runtime_failure_after_initialize_cleans_up(self):
        from aiomql_engine import run_engine
        class FailingSession(self.Session):
            def open(inner):
                super().open()
                raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            run_engine(self.config, session_factory=lambda c: FailingSession(c, sdk_loader=self.loader))
        self.assertEqual(self.sdk.events.count('shutdown'), 1)

    def test_missing_identity_and_recovery_stop_before_sdk_loading(self):
        from aiomql_engine import run_engine
        for config in (replace(self.config, mt5_expected_login=None),
                       replace(self.config, mt5_expected_server=None),
                       replace(self.config, recovery_enabled=True)):
            with self.assertRaises(ValueError):
                run_engine(config, session_factory=lambda c: self.Session(c, sdk_loader=self.loader))
        self.assertEqual(self.loads, 0)

    def test_offline_composition_never_calls_injected_factory(self):
        from aiomql_engine import run_engine
        from aiomql_engine.config import EngineConfig
        self.assertEqual(run_engine(EngineConfig(), session_factory=lambda c: self.fail('factory reached')), 0)

    def test_default_factory_loads_only_on_explicit_runtime_run(self):
        from aiomql_engine import compose_runtime
        with patch('aiomql_engine.mt5_readonly.import_module', side_effect=lambda name: self.loader()) as load:
            runtime = compose_runtime(self.config)
            self.addCleanup(runtime.shutdown)
            load.assert_not_called()
            self.assertEqual(runtime.run(), 0)
            load.assert_called_once_with('MetaTrader5')
            runtime.shutdown()
        self.assertEqual(self.sdk.events.count('shutdown'), 1)

    def test_safe_logs_do_not_include_identity_or_sdk_error_details(self):
        from aiomql_engine import run_engine
        from aiomql_engine.logging import configure_logging
        stream = io.StringIO()
        self.sdk.failure = RuntimeError('fake-password-secret')
        with patch('aiomql_engine.entrypoint.configure_logging',
                   side_effect=lambda level: configure_logging(level, stream=stream)):
            with self.assertRaises(self.Error):
                run_engine(self.config, session_factory=lambda c: self.Session(c, sdk_loader=self.loader))
        self.assertNotIn('fake-password-secret', stream.getvalue())
        self.assertNotIn('demo-server', stream.getvalue())
        self.assertIn('read_only=True', stream.getvalue())

    def test_sdk_mutation_attributes_are_never_even_bound(self):
        class ReadOnlyTrapSDK(FakeSDK):
            def __getattribute__(inner, name):
                if name in ('order_send', 'position_close', 'Buy', 'Sell'):
                    self.fail('SDK mutation attribute accessed')
                return super().__getattribute__(name)
        self.sdk = ReadOnlyTrapSDK()
        session = self.session()
        session.open()
        self.history(session)
        session.shutdown()

    def test_unsafe_flags_stop_before_adapter_factory(self):
        from aiomql_engine import run_engine
        for name, value in (('order_submission_enabled', True), ('live_trading_enabled', True),
                            ('dry_run', False), ('read_only', False)):
            config = replace(self.config)
            object.__setattr__(config, name, value)
            with self.assertRaises(ValueError):
                run_engine(config, session_factory=lambda c: self.fail('factory reached'))

    def test_config_environment_has_explicit_identity_without_credentials(self):
        from aiomql_engine.config import load_config
        config = load_config({'AIOMQL_ENGINE_MODE': 'read_only', 'AIOMQL_ENGINE_MT5_ENABLED': 'true',
                              'AIOMQL_ENGINE_MT5_EXPECTED_LOGIN': '123',
                              'AIOMQL_ENGINE_MT5_EXPECTED_SERVER': 'demo-server'})
        self.assertEqual(config, self.config)
        self.assertNotIn('demo-server', repr(config))
        for name, value in (('MT5_EXPECTED_LOGIN', 'fake-secret'), ('MT5_EXPECTED_SERVER', ' '),
                            ('MT5_TIMEOUT_MS', '0'), ('MT5_MAX_HISTORY_DAYS', '-1')):
            with self.assertRaises(ValueError) as caught:
                load_config({'AIOMQL_ENGINE_' + name: value})
            self.assertNotIn('fake-secret', str(caught.exception))

    def test_adapter_reuses_existing_source_protocol_with_no_assurance_upgrade(self):
        from test_deal_normalizer import ledger
        from uuid import uuid4
        session = self.session()
        session.open()
        source = ledger.MetaTraderDealHistorySource(session, broker_namespace='demo')
        account = ledger.AccountRef(mt5_account_id=uuid4(), broker_namespace='demo',
                                    server='demo-server', login=123,
                                    company_observed='Fake company', trade_mode=0)
        batch = asyncio.run(source.fetch(account, self.start, self.end))
        self.assertEqual(batch.deals, ())
        self.assertEqual(asyncio.run(source.current_account()).login, 123)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as folder:
            repository = ledger.SQLiteDealRepository(Path(folder) / 'a16.sqlite3')
            try:
                repository.register_account(account)
                execution = asyncio.run(ledger.DealIngestor(source, repository).ingest_range_with_evidence(
                    account_ref=account, from_time=self.start, to_time=self.end))
                self.assertIs(execution.source_evidence.assurance,
                              ledger.SourceHistoryAssurance.CALL_COMPLETED_UNVERIFIED)
            finally:
                repository.close()


class EngineMT5OfflineIsolationTests(unittest.TestCase):
    def test_fresh_process_offline_import_and_run_never_load_sdk_or_legacy_library(self):
        code = '''
import importlib.abc, sys
sys.path.insert(0, 'src')
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in ('MetaTrader5', 'aiomql'):
            raise AssertionError('forbidden import: ' + fullname)
sys.meta_path.insert(0, Deny())
import aiomql_engine
from aiomql_engine.config import EngineConfig
assert aiomql_engine.run_engine(EngineConfig()) == 0
assert 'MetaTrader5' not in sys.modules
'''
        result = subprocess.run([sys.executable, '-B', '-c', code],
                                cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
