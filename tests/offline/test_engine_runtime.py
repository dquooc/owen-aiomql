"""Explicit offline composition and synchronous resource-free lifecycle."""

import asyncio
from pathlib import Path
import sys
from unittest.mock import Mock, call, patch
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))


class EngineRuntimeWiringTests(unittest.TestCase):
    def test_composition_order_and_independent_nonlive_runtime_instances(self):
        from aiomql_engine import compose_runtime
        from aiomql_engine.config import EngineConfig
        from aiomql_engine.execution import build_runtime
        from aiomql_engine.safety import evaluate_startup_safety
        events, config = [], EngineConfig()
        def load():
            events.append('config')
            return config
        def logging(level):
            events.append('logging')
            return Mock()
        def safety(value):
            self.assertIs(value, config)
            events.append('safety')
            return evaluate_startup_safety(value)
        def build(decision):
            events.append('runtime')
            return build_runtime(decision)
        with patch('aiomql_engine.entrypoint.load_config', side_effect=load), \
                patch('aiomql_engine.entrypoint.configure_logging', side_effect=logging), \
                patch('aiomql_engine.entrypoint.evaluate_startup_safety', side_effect=safety), \
                patch('aiomql_engine.entrypoint.build_runtime', side_effect=build):
            first, second = compose_runtime(), compose_runtime()
        self.assertEqual(events, ['config', 'logging', 'safety', 'runtime'] * 2)
        self.assertIsNot(first, second)
        self.assertIsNot(first.policy, second.policy)
        self.assertIsNot(first.broker_writes, second.broker_writes)
        self.assertIsNone(first.broker_writes._writer)

    def test_default_runtime_lifecycle_does_not_create_files_or_activate_dependencies(self):
        from aiomql_engine import compose_runtime
        from aiomql_engine.config import EngineConfig
        with patch('aiomql_engine.entrypoint.configure_logging', return_value=Mock()), \
                patch('sqlite3.connect', side_effect=AssertionError('database')), \
                patch('socket.socket', side_effect=AssertionError('network')), \
                patch('asyncio.create_task', side_effect=AssertionError('task')):
            runtime = compose_runtime(EngineConfig())
            before = repr(runtime), hash(runtime)
            self.assertEqual(runtime.run(), 0)
            runtime.shutdown()
            runtime.shutdown()
            self.assertEqual(runtime.run(), 0)
            self.assertEqual((repr(runtime), hash(runtime)), before)

    def test_run_engine_preserves_runtime_status_and_always_shuts_down(self):
        from aiomql_engine import run_engine
        runtime = Mock()
        runtime.run.return_value = 9
        with patch('aiomql_engine.entrypoint.compose_runtime', return_value=runtime):
            self.assertEqual(run_engine(), 9)
        self.assertEqual(runtime.mock_calls, [call.run(), call.shutdown()])

    def test_internal_failure_cancellation_and_interrupt_propagate_after_cleanup(self):
        from aiomql_engine import run_engine
        for error in (RuntimeError('runtime'), asyncio.CancelledError(), KeyboardInterrupt()):
            runtime = Mock()
            runtime.run.side_effect = error
            with self.subTest(error=type(error).__name__), patch('aiomql_engine.entrypoint.compose_runtime', return_value=runtime):
                with self.assertRaises(type(error)):
                    run_engine()
            runtime.shutdown.assert_called_once_with()

    def test_startup_log_failure_cleans_up_constructed_runtime(self):
        from aiomql_engine import compose_runtime
        from aiomql_engine.config import EngineConfig
        logger, runtime = Mock(), Mock()
        logger.getChild.side_effect = RuntimeError('log setup')
        with patch('aiomql_engine.entrypoint.configure_logging', return_value=logger), \
                patch('aiomql_engine.entrypoint.build_runtime', return_value=runtime):
            with self.assertRaises(RuntimeError):
                compose_runtime(EngineConfig())
        runtime.shutdown.assert_called_once_with()

    def test_unsupported_recovery_and_unsafe_config_stop_before_runtime_construction(self):
        from aiomql_engine import compose_runtime
        from aiomql_engine.config import EngineConfig
        from aiomql_engine.safety import StartupSafetyError
        for config in (EngineConfig(recovery_enabled=True), EngineConfig(mt5_enabled=True)):
            with patch('aiomql_engine.entrypoint.configure_logging', return_value=Mock()), \
                    patch('aiomql_engine.entrypoint.build_runtime') as build:
                with self.assertRaises(StartupSafetyError):
                    compose_runtime(config)
            build.assert_not_called()

    def test_runtime_run_does_not_own_an_event_loop(self):
        from aiomql_engine import run_engine
        from aiomql_engine.config import EngineConfig
        async def existing_loop():
            with patch('asyncio.run', side_effect=AssertionError('nested run')):
                self.assertEqual(run_engine(EngineConfig()), 0)
        asyncio.run(existing_loop())

    def test_runtime_write_barrier_stays_enforced_after_run_and_shutdown(self):
        from aiomql_engine import compose_runtime
        from aiomql_engine.config import EngineConfig
        from aiomql_engine.execution import BrokerWriteDeniedError
        runtime = compose_runtime(EngineConfig())
        runtime.run()
        runtime.shutdown()
        async def deny():
            with self.assertRaises(BrokerWriteDeniedError):
                await runtime.broker_writes.submit({})
        asyncio.run(deny())
