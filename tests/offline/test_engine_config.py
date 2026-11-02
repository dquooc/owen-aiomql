"""Typed bootstrap settings without the trading Config singleton or credentials."""

from collections.abc import Mapping
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))


class EngineConfigTests(unittest.TestCase):
    def test_defaults_are_offline_and_safe_without_credentials(self):
        from aiomql_engine.config import EngineConfig, EngineMode, load_config
        config = load_config({})
        self.assertEqual(config, EngineConfig())
        self.assertIs(config.mode, EngineMode.OFFLINE)
        self.assertFalse(config.mt5_enabled or config.recovery_enabled or config.live_trading_enabled or config.order_submission_enabled)
        self.assertTrue(config.read_only and config.dry_run)

    def test_valid_explicit_environment_overrides(self):
        from aiomql_engine.config import EngineMode, LogLevel, load_config
        config = load_config({'AIOMQL_ENGINE_MODE': 'read_only', 'AIOMQL_ENGINE_DATABASE_PATH': 'data/ledger.sqlite3',
                              'AIOMQL_ENGINE_LOG_LEVEL': 'DEBUG', 'AIOMQL_ENGINE_MT5_ENABLED': 'true',
                              'AIOMQL_ENGINE_RECOVERY_ENABLED': 'true', 'AIOMQL_ENGINE_RECOVERY_OVERLAP_MS': '1234'})
        self.assertIs(config.mode, EngineMode.READ_ONLY)
        self.assertIs(config.log_level, LogLevel.DEBUG)
        self.assertEqual(config.database_path, Path('data/ledger.sqlite3'))
        self.assertEqual(config.recovery_overlap, timedelta(milliseconds=1234))
        self.assertTrue(config.mt5_enabled and config.recovery_enabled)

    def test_unset_and_empty_malformed_values_are_distinct_and_never_echoed(self):
        from aiomql_engine.config import load_config
        for key in ('MODE', 'LOG_LEVEL', 'MT5_ENABLED', 'RECOVERY_ENABLED', 'RECOVERY_OVERLAP_MS', 'DATABASE_PATH'):
            for value in ('', 'secret-test-value'):
                if key == 'DATABASE_PATH' and value:
                    value = '\x00' + value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError) as raised:
                    load_config({'AIOMQL_ENGINE_' + key: value})
                self.assertNotIn('secret-test-value', str(raised.exception))

    def test_malformed_bool_integer_duration_and_path_are_rejected(self):
        from aiomql_engine.config import load_config
        for key, values in (('MT5_ENABLED', ('yes', '1', 'TRUE', ' false ')),
                            ('RECOVERY_OVERLAP_MS', ('-1', '1.5', ' 1 ', '9' * 40)),
                            ('DATABASE_PATH', ('\x00', '.', '/', '   '))):
            for value in values:
                with self.subTest(key=key), self.assertRaises(ValueError):
                    load_config({'AIOMQL_ENGINE_' + key: value})

    def test_runtime_types_domains_and_unsupported_trading_flags(self):
        from aiomql_engine.config import EngineConfig
        config = EngineConfig()
        for field, value in (('mode', 'offline'), ('log_level', 'INFO'), ('database_path', 3),
                             ('mt5_enabled', 'false'), ('recovery_enabled', 1), ('read_only', 1),
                             ('dry_run', 'true'), ('recovery_overlap', -1),
                             ('recovery_overlap', timedelta(microseconds=-1))):
            with self.subTest(field=field), self.assertRaises((TypeError, ValueError)):
                replace(config, **{field: value})
        for field in ('live_trading_enabled', 'order_submission_enabled', 'dry_run', 'read_only'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                replace(config, **{field: field not in ('dry_run', 'read_only')})

    def test_known_names_only_no_environment_iteration_or_mutation(self):
        from aiomql_engine.config import load_config
        class KnownOnly(Mapping):
            def __getitem__(self, key):
                self.assert_known(key)
                raise KeyError(key)
            def assert_known(self, key):
                if not key.startswith('AIOMQL_ENGINE_'):
                    raise AssertionError('credential/unrelated access')
            def __iter__(self):
                raise AssertionError('environment dump')
            def __len__(self):
                raise AssertionError('environment dump')
        load_config(KnownOnly())
        env = {'UNRELATED': 'sentinel', 'MT5_PASSWORD': 'fake-only-secret', 'AIOMQL_ENGINE_UNKNOWN': 'ignored'}
        before = env.copy()
        self.assertNotIn('fake-only-secret', repr(load_config(env)))
        self.assertEqual(env, before)

    def test_frozen_slotted_independent_instances_and_no_sensitive_path_repr(self):
        from aiomql_engine.config import EngineConfig
        first, second = EngineConfig(), EngineConfig()
        self.assertIsNot(first, second)
        self.assertFalse(hasattr(first, '__dict__'))
        with self.assertRaises(FrozenInstanceError):
            first.mt5_enabled = True
        self.assertNotIn('fake-secret', repr(EngineConfig(database_path='fake-secret/ledger.sqlite3')))

    def test_entrypoint_loads_once_or_uses_explicit_config_without_runtime_work(self):
        from aiomql_engine import run_engine
        from aiomql_engine.config import EngineConfig
        with patch('aiomql_engine.entrypoint.load_config', return_value=EngineConfig()) as load:
            self.assertEqual(run_engine(), 0)
            load.assert_called_once_with()
            self.assertEqual(run_engine(EngineConfig()), 0)
            self.assertEqual(load.call_count, 1)
        with self.assertRaises(TypeError):
            run_engine(object())
