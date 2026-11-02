"""Actual independent composition root, without loading the trading package."""

import asyncio
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SRC))


class EngineEntrypointTests(unittest.TestCase):
    def test_actual_import_and_execution_have_no_runtime_side_effects(self):
        script = '''
import asyncio, importlib.abc, logging, sqlite3, sys, threading
sys.path.insert(0, sys.argv[1])
class BlockTrading(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname == 'aiomql' or fullname.startswith('aiomql.') or fullname == 'MetaTrader5':
            raise AssertionError('trading import forbidden')
sys.meta_path.insert(0, BlockTrading())
def audit(event, args):
    if event in ('socket.connect', 'sqlite3.connect', 'os.mkdir', 'subprocess.Popen'):
        raise AssertionError(event)
    if event == 'open' and (str(args[0]).endswith('.env') or isinstance(args[1], str) and any(c in args[1] for c in 'wax+')):
        raise AssertionError('runtime file access')
sys.addaudithook(audit)
handlers = tuple(logging.getLogger().handlers)
threads = tuple(threading.enumerate())
import aiomql_engine
import aiomql_engine.__main__
assert tuple(logging.getLogger().handlers) == handlers
with __import__('unittest.mock', fromlist=['patch']).patch.object(asyncio, 'create_task', side_effect=AssertionError('task')):
    assert aiomql_engine.main() == 0
assert tuple(threading.enumerate()) == threads
assert 'aiomql' not in sys.modules and 'MetaTrader5' not in sys.modules
'''
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run([sys.executable, "-B", "-c", script, str(SRC)], cwd=cwd,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(Path(cwd).iterdir()), [])

    def test_main_and_runner_return_success(self):
        from aiomql_engine import main, run_engine
        self.assertEqual(run_engine(), 0)
        self.assertEqual(main(), 0)

    def test_main_preserves_runner_status(self):
        from aiomql_engine import main
        with patch('aiomql_engine.entrypoint.run_engine', return_value=7):
            self.assertEqual(main(), 7)

    def test_internal_failure_propagates(self):
        from aiomql_engine import main
        with patch('aiomql_engine.entrypoint.run_engine', side_effect=RuntimeError('bootstrap')):
            with self.assertRaises(RuntimeError):
                main()

    def test_keyboard_interrupt_is_exit_130(self):
        from aiomql_engine import main
        with patch('aiomql_engine.entrypoint.run_engine', side_effect=KeyboardInterrupt):
            self.assertEqual(main(), 130)

    def test_cancellation_is_not_swallowed(self):
        from aiomql_engine import main
        with patch('aiomql_engine.entrypoint.run_engine', side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                main()

    def test_callable_does_not_own_or_nest_an_event_loop(self):
        from aiomql_engine import main
        async def running():
            with patch.object(asyncio, 'run', side_effect=AssertionError('nested loop')):
                self.assertEqual(main(), 0)
        asyncio.run(running())

    def test_module_execution_uses_explicit_exit_status(self):
        script = "import sys,runpy; sys.path.insert(0,sys.argv[1]); runpy.run_module('aiomql_engine',run_name='__main__')"
        result = subprocess.run([sys.executable, '-B', '-c', script, str(SRC)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
