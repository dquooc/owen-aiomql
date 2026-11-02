"""Engine request boundary denies mutations without touching broker adapters."""

from collections.abc import Mapping
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import sys
from unittest.mock import AsyncMock, Mock, patch
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))


class EngineDryRunTests(unittest.IsolatedAsyncioTestCase):
    def decision(self):
        from aiomql_engine.config import EngineConfig
        from aiomql_engine.safety import evaluate_startup_safety
        return evaluate_startup_safety(EngineConfig())

    async def test_all_existing_request_mutations_deny_before_adapter_invocation(self):
        from aiomql_engine.execution import BrokerWriteBarrier, BrokerWriteDeniedError, BrokerWritePolicy
        writer = Mock()
        writer.submit = AsyncMock(return_value={'ticket': 123})
        barrier = BrokerWriteBarrier(BrokerWritePolicy(), _writer=writer)
        # Symbolic fake SDK actions mirror existing DEAL/PENDING/MODIFY/REMOVE/
        # SLTP/CLOSE_BY request paths. The barrier must not inspect or decode them.
        for request in ({'action': 'DEAL', 'type': 'BUY'}, {'action': 'DEAL', 'type': 'SELL'},
                        {'action': 'PENDING'}, {'action': 'MODIFY', 'order': 1},
                        {'action': 'REMOVE', 'order': 1}, {'action': 'SLTP', 'position': 1},
                        {'action': 'DEAL', 'position': 1}, {'action': 'CLOSE_BY', 'position': 1},
                        {'action': 'UNKNOWN'}, {}):
            before = request.copy()
            with self.subTest(request=request), self.assertRaises(BrokerWriteDeniedError) as raised:
                await barrier.submit(request)
            self.assertNotIn('ticket', str(raised.exception))
            self.assertEqual(request, before)
        writer.assert_not_called()
        writer.submit.assert_not_called()

    async def test_denial_does_not_inspect_secret_request_or_writer_objects(self):
        from aiomql_engine.execution import BrokerWriteBarrier, BrokerWriteDeniedError, BrokerWritePolicy
        class OpaqueRequest(Mapping):
            def __iter__(self): raise AssertionError('request access')
            def __len__(self): raise AssertionError('request access')
            def __getitem__(self, key): raise AssertionError('request access')
            def __repr__(self): raise AssertionError('secret repr')
        class OpaqueWriter:
            def __getattribute__(self, name): raise AssertionError('writer access')
        barrier = BrokerWriteBarrier(BrokerWritePolicy(), _writer=OpaqueWriter())
        with self.assertRaises(BrokerWriteDeniedError):
            await barrier.submit(OpaqueRequest())
        self.assertNotIn('OpaqueWriter', repr(barrier))

    async def test_default_runtime_exposes_dry_run_and_no_mutation_transport(self):
        from aiomql_engine.execution import BrokerWriteDeniedError, build_runtime
        runtime = build_runtime(self.decision())
        self.assertTrue(runtime.policy.dry_run and runtime.policy.read_only)
        self.assertTrue(runtime.policy.local_computation)
        self.assertFalse(runtime.policy.broker_writes)
        self.assertIsNone(runtime.broker_writes._writer)
        with self.assertRaises(BrokerWriteDeniedError):
            await runtime.broker_writes.submit({'action': 'DEAL'})

    async def test_policy_frozen_slotted_and_repeated_denials_have_no_effects(self):
        from aiomql_engine.execution import BrokerWriteDeniedError, build_runtime
        runtime = build_runtime(self.decision())
        before = repr(runtime), hash(runtime)
        for value in (runtime, runtime.policy, runtime.broker_writes):
            self.assertFalse(hasattr(value, '__dict__'))
            with self.assertRaises(FrozenInstanceError):
                setattr(value, next(iter(value.__dataclass_fields__)), None)
        for _ in range(3):
            with self.assertRaises(BrokerWriteDeniedError):
                await runtime.broker_writes.submit({})
        self.assertEqual((repr(runtime), hash(runtime)), before)

    async def test_future_or_malformed_policy_cannot_activate_live_submission(self):
        from aiomql_engine.execution import BrokerWriteBarrier, BrokerWriteDeniedError, BrokerWritePolicy, build_runtime
        for name, value in (('dry_run', False), ('read_only', False), ('dry_run', 'true')):
            with self.assertRaises((TypeError, ValueError)):
                replace(BrokerWritePolicy(), **{name: value})
        decision = self.decision()
        object.__setattr__(decision, 'order_submission', True)
        with self.assertRaises(ValueError):
            build_runtime(decision)
        policy = BrokerWritePolicy()
        object.__setattr__(policy, 'dry_run', False)
        with self.assertRaises(BrokerWriteDeniedError):
            await BrokerWriteBarrier(policy).submit({'action': 'DEAL'})

    async def test_read_only_connection_permission_still_has_no_broker_write_branch(self):
        from aiomql_engine.config import EngineConfig, EngineMode
        from aiomql_engine.safety import evaluate_startup_safety
        from aiomql_engine.execution import BrokerWriteDeniedError, build_runtime
        decision = evaluate_startup_safety(EngineConfig(mode=EngineMode.READ_ONLY, mt5_enabled=True))
        runtime = build_runtime(decision)
        with self.assertRaises(BrokerWriteDeniedError):
            await runtime.broker_writes.submit({'action': 'REMOVE'})

    async def test_entrypoint_builds_runtime_once_from_same_evaluated_decision(self):
        from aiomql_engine import run_engine
        from aiomql_engine.config import EngineConfig
        from aiomql_engine.execution import build_runtime
        decision = self.decision()
        logger = Mock()
        with patch('aiomql_engine.entrypoint.configure_logging', return_value=logger), \
                patch('aiomql_engine.entrypoint.evaluate_startup_safety', return_value=decision) as gate, \
                patch('aiomql_engine.entrypoint.build_runtime', wraps=build_runtime) as build:
            self.assertEqual(run_engine(EngineConfig()), 0)
        gate.assert_called_once()
        build.assert_called_once_with(decision)

    async def test_invalid_runtime_bindings_fail_without_silent_capability_changes(self):
        from aiomql_engine.execution import BrokerWriteBarrier, BrokerWritePolicy, EngineRuntime, build_runtime
        with self.assertRaises(TypeError):
            build_runtime(object())
        with self.assertRaises(TypeError):
            BrokerWriteBarrier(object())
        with self.assertRaises(TypeError):
            EngineRuntime(BrokerWritePolicy(), object())
