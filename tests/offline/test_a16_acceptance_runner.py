"""Validate the native acceptance runner without native imports or a terminal."""

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

from test_engine_mt5_readonly import FakeSDK


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))


class A16AcceptanceRunnerTests(unittest.TestCase):
    def runner(self):
        path = ROOT / 'tools/windows/accept_a16_readonly.py'
        self.assertTrue(path.exists(), 'A16 native acceptance runner is missing')
        spec = importlib.util.spec_from_file_location('_a16_acceptance_runner', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_composition_one_fetch_negative_identity_and_cleanup_with_inert_sdk(self):
        from aiomql_engine.config import EngineConfig, EngineMode
        sdk = FakeSDK()
        sdk.ACCOUNT_TRADE_MODE_DEMO = 0
        sdk.account = SimpleNamespace(login=123, server='demo-server', trade_mode=0)
        config = EngineConfig(mode=EngineMode.READ_ONLY, mt5_enabled=True,
                              mt5_expected_login=123, mt5_expected_server='demo-server')
        report = self.runner().run_acceptance(
            config, sdk_loader=lambda: sdk,
            horizon=datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(report['runtime_result'], 0)
        self.assertEqual(report['retrieved_deals'], 0)
        self.assertEqual(report['history_calls'], 1)
        self.assertEqual(report['initialize_calls'], 2)
        self.assertEqual(report['shutdown_calls'], 2)
        self.assertTrue(report['wrong_identity_rejected_before_history'])
        self.assertEqual(report['invalid_ranges_rejected'], 3)
        self.assertEqual(report['native_order_send_calls'], 0)
        self.assertEqual(sdk.mutations, 0)

    def test_non_demo_account_stops_without_fetch_and_closes(self):
        from aiomql_engine.config import EngineConfig, EngineMode
        sdk = FakeSDK()
        sdk.ACCOUNT_TRADE_MODE_DEMO = 0
        sdk.account = SimpleNamespace(login=123, server='demo-server', trade_mode=2)
        config = EngineConfig(mode=EngineMode.READ_ONLY, mt5_enabled=True,
                              mt5_expected_login=123, mt5_expected_server='demo-server')
        with self.assertRaises(RuntimeError):
            self.runner().run_acceptance(config, sdk_loader=lambda: sdk)
        self.assertFalse(any(isinstance(event, tuple) and event[0] == 'history' for event in sdk.events))
        self.assertEqual(sdk.events.count('shutdown'), 1)
        self.assertEqual(sdk.mutations, 0)
