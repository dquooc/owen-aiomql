"""Engine-owned console logging with safe startup fields and no global reset."""

import io
import logging
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))


class EngineLoggingTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger('aiomql_engine')
        previous = self.logger.handlers[:], self.logger.level, self.logger.propagate
        self.logger.handlers = []
        def cleanup():
            for handler in self.logger.handlers:
                handler.close()
            self.logger.handlers, self.logger.level, self.logger.propagate = previous
        self.addCleanup(cleanup)

    def test_requested_level_and_deterministic_console_format(self):
        from aiomql_engine.config import LogLevel
        from aiomql_engine.logging import configure_logging
        stream = io.StringIO()
        logger = configure_logging(LogLevel.WARNING, stream=stream)
        logger.info('filtered')
        logger.warning('message %s', 'value')
        self.assertEqual(stream.getvalue(), 'WARNING aiomql_engine message value\n')

    def test_repeated_initialization_reuses_owned_handler_and_updates_stream_and_level(self):
        from aiomql_engine.config import LogLevel
        from aiomql_engine.logging import configure_logging
        first, second = io.StringIO(), io.StringIO()
        logger = configure_logging(LogLevel.INFO, stream=first)
        handler = logger.handlers[0]
        configure_logging(LogLevel.DEBUG, stream=second)
        self.assertEqual(logger.handlers, [handler])
        logger.debug('once')
        self.assertEqual(first.getvalue(), '')
        self.assertEqual(second.getvalue(), 'DEBUG aiomql_engine once\n')

    def test_module_child_logger_inherits_engine_configuration(self):
        from aiomql_engine.config import LogLevel
        from aiomql_engine.logging import configure_logging
        stream = io.StringIO()
        configure_logging(LogLevel.INFO, stream=stream)
        logging.getLogger('aiomql_engine.recovery').info('event')
        self.assertEqual(stream.getvalue(), 'INFO aiomql_engine.recovery event\n')

    def test_root_and_unrelated_library_logging_are_preserved(self):
        from aiomql_engine.config import LogLevel
        from aiomql_engine.logging import configure_logging
        root, library = logging.getLogger(), logging.getLogger('aiomql')
        before = root.handlers[:], root.level, library.handlers[:], library.level, library.propagate
        configure_logging(LogLevel.DEBUG, stream=io.StringIO())
        self.assertEqual((root.handlers, root.level, library.handlers, library.level, library.propagate), before)
        self.assertFalse(self.logger.propagate)

    def test_user_attached_engine_handler_is_not_removed_or_reconfigured(self):
        from aiomql_engine.config import LogLevel
        from aiomql_engine.logging import configure_logging
        handler = logging.NullHandler(level=logging.ERROR)
        self.logger.addHandler(handler)
        configure_logging(LogLevel.INFO, stream=io.StringIO())
        configure_logging(LogLevel.INFO, stream=io.StringIO())
        self.assertIn(handler, self.logger.handlers)
        self.assertEqual(handler.level, logging.ERROR)
        self.assertEqual(len(self.logger.handlers), 2)

    def test_invalid_level_fails_before_changing_logger(self):
        from aiomql_engine.logging import configure_logging
        before = self.logger.handlers[:], self.logger.level, self.logger.propagate
        for value in ('INFO', 20, True, None):
            with self.assertRaises(TypeError):
                configure_logging(value)
        self.assertEqual((self.logger.handlers, self.logger.level, self.logger.propagate), before)

    def test_entrypoint_loads_then_configures_and_emits_only_safe_startup_mode(self):
        from aiomql_engine import run_engine
        from aiomql_engine.config import EngineConfig
        from aiomql_engine.logging import configure_logging
        stream, events = io.StringIO(), []
        config = EngineConfig(database_path='fake-secret/ledger.sqlite3')
        def load():
            events.append('load')
            return config
        def configure(level):
            events.append('logging')
            return configure_logging(level, stream=stream)
        with patch('aiomql_engine.entrypoint.load_config', side_effect=load), \
                patch('aiomql_engine.entrypoint.configure_logging', side_effect=configure):
            self.assertEqual(run_engine(), 0)
            self.assertEqual(run_engine(config), 0)
        self.assertEqual(events, ['load', 'logging', 'logging'])
        expected = ('INFO aiomql_engine.entrypoint engine bootstrap initialized mode=offline\n'
                    'INFO aiomql_engine.entrypoint startup capabilities offline=True dry_run=True read_only=True '
                    'mt5_connection=False read_only_mt5=False recovery=False orders=False live=False\n')
        self.assertEqual(stream.getvalue(), expected * 2)
        self.assertNotIn('fake-secret', stream.getvalue())
        self.assertNotIn('connected', stream.getvalue())

    def test_logging_configuration_failure_is_not_hidden_as_success(self):
        from aiomql_engine import run_engine
        from aiomql_engine.config import EngineConfig
        with patch('aiomql_engine.entrypoint.configure_logging', side_effect=RuntimeError('logging setup')):
            with self.assertRaises(RuntimeError):
                run_engine(EngineConfig())
