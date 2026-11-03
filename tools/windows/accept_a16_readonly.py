"""Explicit opt-in Windows A16 acceptance; one history query, no credentials/DB.

Run with the project interpreter and AIOMQL_RUN_A16_NATIVE_TESTS=1 in the child
process only. Engine identity/config comes from explicit AIOMQL_ENGINE_* names.
This runner never logs in, switches accounts or calls a broker mutation API.
"""

import asyncio
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from importlib import import_module
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from aiomql_engine import compose_runtime, run_engine
from aiomql_engine.config import EngineConfig, load_config
from aiomql_engine.mt5_readonly import MT5ReadOnlySession, ReadOnlySessionError
from aiomql_engine.safety import evaluate_startup_safety


def require(condition, stage):
    if not condition:
        raise RuntimeError('A16 acceptance failed: ' + stage)


def run_acceptance(config, *, sdk_loader=None, horizon=None):
    """Instrument only read/lifecycle calls around the actual default composition."""
    decision = evaluate_startup_safety(config)
    config.validate_mt5_identity()
    require(decision.read_only_mt5 and decision.dry_run and decision.read_only
            and not (decision.recovery_execution or decision.order_submission or decision.live_trading),
            'explicit read-only safety decision')
    end = horizon if horizon is not None else datetime.now(timezone.utc)
    start = end - timedelta(days=min(7, config.mt5_max_history_days))
    wrappers, loads = [], []
    denied_writes = []

    def deny_write(*args, **kwargs):
        denied_writes.append('denied')
        raise RuntimeError('Broker mutation trapped before native execution')

    class NativeReads:
        def __init__(self, sdk):
            self._sdk = sdk
            self.events = []
            self.owned = False

        def initialize(self, *, timeout):
            self.events.append('initialize')
            result = self._sdk.initialize(timeout=timeout)
            self.owned = result is True
            return result

        def account_info(self):
            account = self._sdk.account_info()
            require(account is not None and account.trade_mode == self._sdk.ACCOUNT_TRADE_MODE_DEMO,
                    'current terminal must be demo')
            require(account.login == config.mt5_expected_login and account.server == config.mt5_expected_server,
                    'current terminal must match the original expected demo identity')
            self.events.append('account:verified_demo')
            return account

        def terminal_info(self):
            result = self._sdk.terminal_info()
            require(result is not None and result.connected is True, 'connected terminal')
            self.events.append('terminal:connected')
            return result

        def history_deals_get(self, date_from, date_to):
            require((date_from, date_to) == (start, end), 'exact bounded UTC range')
            require(sum(w.events.count('history') for w in wrappers) == 0, 'one history query only')
            self.events.append('history')
            return self._sdk.history_deals_get(date_from, date_to)

        def shutdown(self):
            self.events.append('shutdown')
            result = self._sdk.shutdown()
            self.owned = False
            return result

    with ExitStack() as stack:
        trapped_sdks = set()

        def load(name):
            require(name == 'MetaTrader5', 'SDK import name')
            sdk = import_module(name) if sdk_loader is None else sdk_loader()
            loads.append('SDK loaded')
            if id(sdk) not in trapped_sdks:
                # Install a process-local tripwire. Never invoke it for a probe.
                stack.enter_context(patch.object(sdk, 'order_send', deny_write))
                trapped_sdks.add(id(sdk))
            wrapper = NativeReads(sdk)
            wrappers.append(wrapper)
            return wrapper

        stack.enter_context(patch('aiomql_engine.mt5_readonly.import_module', side_effect=load))
        require(run_engine(EngineConfig()) == 0 and not loads, 'offline engine must not load SDK')
        runtime = compose_runtime(config)
        require(not loads and isinstance(runtime.read_only_session, MT5ReadOnlySession),
                'composition must create a lazy A16 session')
        invalid_count = 0
        try:
            status = runtime.run()
            require(status == 0 and len(loads) == 1, 'native runtime initialization')
            session = runtime.read_only_session
            for invalid_start, invalid_end in ((start.replace(tzinfo=None), end),
                                                (start, None), (end, start)):
                try:
                    asyncio.run(session.history_deals_get(date_from=invalid_start, date_to=invalid_end))
                except ValueError:
                    invalid_count += 1
                else:
                    raise RuntimeError('Invalid history range was accepted')
            require(wrappers[0].events.count('history') == 0, 'invalid ranges must not reach SDK')
            deals = asyncio.run(session.history_deals_get(date_from=start, date_to=end))
            require(isinstance(deals, (tuple, list)), 'native history collection')
            retrieved = len(deals)
        finally:
            runtime.shutdown()
            runtime.shutdown()
        require(wrappers[0].events == ['initialize', 'account:verified_demo', 'terminal:connected',
                                      'account:verified_demo', 'terminal:connected', 'history',
                                      'account:verified_demo', 'shutdown'], 'pre/post guards and cleanup sequence')
        before = list(wrappers[0].events)
        try:
            asyncio.run(runtime.read_only_session.account_info())
        except ReadOnlySessionError:
            pass
        else:
            raise RuntimeError('Closed session remained readable')
        require(wrappers[0].events == before, 'closed session must not touch SDK')

        # Change only the expectation. No login/account switch or history retry.
        wrong = compose_runtime(replace(config, mt5_expected_login=config.mt5_expected_login + 1))
        try:
            try:
                wrong.run()
            except ReadOnlySessionError:
                pass
            else:
                raise RuntimeError('Wrong expected identity was accepted')
        finally:
            wrong.shutdown()
            wrong.shutdown()
        require(len(wrappers) == 2 and wrappers[1].events ==
                ['initialize', 'account:verified_demo', 'shutdown'], 'wrong identity must fail before history')
        require(not any(w.owned for w in wrappers) and not denied_writes, 'no owned connection or mutations')

    return {
        'sdk_lazy_load': True, 'initialize': 'PASS', 'terminal_connected': True,
        'account_pre_guard': 'PASS', 'account_post_guard': 'PASS', 'cleanup': 'PASS',
        'requested_from': start.isoformat(), 'requested_to': end.isoformat(),
        'configured_max_history_days': config.mt5_max_history_days,
        'retrieved_deals': retrieved, 'history_calls': 1, 'runtime_result': status,
        'explicit_read_only_config': True, 'safety_decision': 'PASS',
        'session_created_through_composition': True, 'wrong_identity_rejected_before_history': True,
        'invalid_ranges_rejected': invalid_count, 'closed_session_read_rejected': True,
        'initialize_calls': 2, 'shutdown_calls': 2, 'owned_connections_after_cleanup': 0,
        'mutation_attempts': len(denied_writes), 'native_order_send_calls': 0,
        'broker_mutation_calls': 0, 'order_mutations': 0, 'position_mutations': 0,
    }


def main():
    if sys.platform != 'win32' or os.environ.get('AIOMQL_RUN_A16_NATIVE_TESTS') != '1':
        print('A16 native acceptance requires Windows and explicit process-scoped opt-in.', file=sys.stderr)
        return 2
    try:
        require('MetaTrader5' not in sys.modules, 'fresh child process before SDK loading')
        report = run_acceptance(load_config())
    except Exception as error:
        # Never echo an SDK/config exception or arbitrary account/credential data.
        print('A16 native acceptance FAIL (' + type(error).__name__ + '); details withheld.', file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
