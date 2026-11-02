"""Pure startup capability decisions; no SDK or runtime activation."""

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import sys
from unittest.mock import Mock, patch
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))


class EngineSafetyTests(unittest.TestCase):
    def test_defaults_are_explicitly_offline_dry_run_and_without_runtime_permissions(self):
        from aiomql_engine.config import EngineConfig
        from aiomql_engine.safety import evaluate_startup_safety
        decision = evaluate_startup_safety(EngineConfig())
        self.assertTrue(decision.offline_runtime and decision.dry_run and decision.read_only)
        self.assertFalse(any((decision.mt5_connection, decision.read_only_mt5, decision.recovery_execution,
                              decision.order_submission, decision.live_trading)))

    def test_offline_connection_and_unwired_recovery_requests_fail_closed(self):
        from aiomql_engine.config import EngineConfig
        from aiomql_engine.safety import StartupSafetyError, evaluate_startup_safety
        for config in (EngineConfig(mt5_enabled=True), EngineConfig(recovery_enabled=True)):
            with self.assertRaises(StartupSafetyError):
                evaluate_startup_safety(config)

    def test_future_read_only_dry_run_connection_permission_does_not_connect(self):
        from aiomql_engine.config import EngineConfig, EngineMode
        from aiomql_engine.safety import evaluate_startup_safety
        decision = evaluate_startup_safety(EngineConfig(mode=EngineMode.READ_ONLY, mt5_enabled=True))
        self.assertFalse(decision.offline_runtime)
        self.assertTrue(decision.mt5_connection and decision.read_only_mt5 and decision.dry_run)
        self.assertFalse(decision.order_submission or decision.live_trading or decision.recovery_execution)

    def test_unsafe_contradictory_or_malformed_flags_are_never_approved(self):
        from aiomql_engine.config import EngineConfig
        from aiomql_engine.safety import StartupSafetyError, evaluate_startup_safety
        # Defense in depth for malformed/deserialized instances; normal A6
        # construction already rejects several of these unsupported settings.
        for changes in ({'live_trading_enabled': True}, {'order_submission_enabled': True},
                         {'dry_run': False}, {'read_only': False}, {'mt5_enabled': 'secret-test'},
                         {'live_trading_enabled': True, 'order_submission_enabled': True}, {'mode': 'secret-test'}):
            config = EngineConfig()
            for key, value in changes.items():
                object.__setattr__(config, key, value)
            with self.subTest(changes=changes), self.assertRaises(StartupSafetyError) as raised:
                evaluate_startup_safety(config)
            self.assertNotIn('secret-test', str(raised.exception))

    def test_evaluator_is_deterministic_pure_and_does_not_read_environment(self):
        from aiomql_engine.config import EngineConfig
        from aiomql_engine.safety import evaluate_startup_safety
        config = EngineConfig()
        before = repr(config), hash(config)
        with patch('os.getenv', side_effect=AssertionError('environment')), \
                patch('os._Environ.__getitem__', side_effect=AssertionError('environment')), \
                patch('builtins.open', side_effect=AssertionError('file')):
            first = evaluate_startup_safety(config)
            self.assertEqual(first, evaluate_startup_safety(config))
        self.assertEqual((repr(config), hash(config)), before)

    def test_decision_is_frozen_slotted_and_cannot_grant_unsafe_capabilities(self):
        from aiomql_engine.config import EngineConfig
        from aiomql_engine.safety import evaluate_startup_safety
        decision = evaluate_startup_safety(EngineConfig())
        self.assertFalse(hasattr(decision, '__dict__'))
        with self.assertRaises(FrozenInstanceError):
            decision.order_submission = True
        for changes in ({'order_submission': True}, {'live_trading': True}, {'recovery_execution': True},
                         {'mt5_connection': True}, {'dry_run': False}, {'offline_runtime': 1}):
            with self.subTest(changes=changes), self.assertRaises((TypeError, ValueError)):
                replace(decision, **changes)

    def test_entrypoint_evaluates_once_after_logging_setup_and_emits_safe_summary(self):
        from aiomql_engine import run_engine
        from aiomql_engine.config import EngineConfig
        from aiomql_engine.safety import evaluate_startup_safety
        events, logger = [], Mock()
        def configure(level):
            events.append('logging')
            return logger
        def evaluate(config):
            events.append('safety')
            return evaluate_startup_safety(config)
        with patch('aiomql_engine.entrypoint.configure_logging', side_effect=configure), \
                patch('aiomql_engine.entrypoint.evaluate_startup_safety', side_effect=evaluate) as gate:
            self.assertEqual(run_engine(EngineConfig(database_path='fake-secret/file.sqlite3')), 0)
        gate.assert_called_once()
        self.assertEqual(events, ['logging', 'safety'])
        self.assertNotIn('fake-secret', str(logger.mock_calls))
        self.assertIn('dry_run', str(logger.mock_calls))

    def test_rejected_startup_propagates_without_a_success_message(self):
        from aiomql_engine import run_engine
        from aiomql_engine.config import EngineConfig
        from aiomql_engine.safety import StartupSafetyError
        logger = Mock()
        with patch('aiomql_engine.entrypoint.configure_logging', return_value=logger):
            with self.assertRaises(StartupSafetyError):
                run_engine(EngineConfig(mt5_enabled=True))
        logger.getChild.assert_not_called()
