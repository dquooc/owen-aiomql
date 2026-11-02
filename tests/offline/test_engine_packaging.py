"""A12 structure-only contracts; never run a packager or Windows build."""

import contextlib
import importlib.util
import io
from pathlib import Path
import runpy
import sys
import tomllib
from unittest.mock import Mock, patch
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
BUILD = ROOT / 'tools/windows/build_engine.py'
ENTRY = ROOT / 'tools/windows/engine_entry.py'


class EnginePackagingTests(unittest.TestCase):
    def builder(self):
        spec = importlib.util.spec_from_file_location('engine_build_contract', BUILD)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_build_command_targets_stable_launcher_name_and_project_local_outputs(self):
        build = self.builder()
        command = build.build_command()
        self.assertEqual(command[:3], [sys.executable, '-m', 'PyInstaller'])
        for option, expected in (('--name', 'AIOMQL_Engine'), ('--paths', ROOT / 'src'),
                                  ('--distpath', ROOT / 'dist'), ('--workpath', ROOT / 'build/engine'),
                                  ('--specpath', ROOT / 'build/engine')):
            self.assertEqual(command[command.index(option) + 1], str(expected))
        self.assertEqual(command[-1], str(ENTRY))
        self.assertIn('--onedir', command)
        self.assertIn('--console', command)

    def test_unsupported_os_rejects_without_process_or_file_work(self):
        build = self.builder()
        with patch.object(build.sys, 'platform', 'linux'), patch.object(build.subprocess, 'run') as run, \
                patch('pathlib.Path.mkdir', side_effect=AssertionError('filesystem')), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(build.main(), 2)
        run.assert_not_called()

    def test_windows_command_contract_only_mocked_process_and_exit_status(self):
        build = self.builder()
        with patch.object(build.sys, 'platform', 'win32'), \
                patch.object(build.subprocess, 'run', return_value=Mock(returncode=7)) as run:
            self.assertEqual(build.main(), 7)
        run.assert_called_once_with(build.build_command(), cwd=ROOT, check=False)

    def test_unsupported_python_rejects_before_packager_invocation(self):
        build = self.builder()
        with patch.object(build.sys, 'platform', 'win32'), patch.object(build.sys, 'version_info', (3, 12)), \
                patch.object(build.subprocess, 'run') as run, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(build.main(), 2)
        run.assert_not_called()

    def test_launcher_import_is_inert_and_main_delegates_to_public_callable(self):
        import aiomql_engine
        with patch.object(aiomql_engine, 'main', return_value=9) as main:
            runpy.run_path(str(ENTRY), run_name='packaging_import_contract')
            main.assert_not_called()
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(str(ENTRY), run_name='__main__')
        self.assertEqual(raised.exception.code, 9)
        main.assert_called_once_with()

    def test_no_data_collection_or_credentials_and_no_embedded_machine_paths(self):
        import ast
        build = self.builder()
        command = build.build_command()
        self.assertFalse(any(flag in command for flag in ('--add-data', '--collect-all', '--hidden-import')))
        self.assertIn('MetaTrader5', command)  # Explicitly excluded from offline bootstrap.
        self.assertEqual(command[command.index('--exclude-module') + 1], 'MetaTrader5')
        for path in (BUILD, ENTRY):
            text = path.read_text()
            for forbidden in ('order_send', 'history_deals_get', '.env', 'password', 'MetaQuotes', 'tests/live', '.sqlite3'):
                self.assertNotIn(forbidden, text)
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertFalse(node.value.startswith(('/', 'C:\\', 'D:\\')))
        self.assertFalse(any('.env' in arg or '.sqlite3' in arg for arg in command))

    def test_packaging_dependency_is_separate_from_runtime_and_dev(self):
        project = tomllib.loads((ROOT / 'pyproject.toml').read_text())
        self.assertEqual(project['project']['requires-python'], '>=3.13')
        self.assertEqual(project['project']['dependencies'], ['metatrader5>=5.0.5640', 'mplfinance>=0.12.10b0', 'pandas>=3.0.1'])
        self.assertNotIn('pyinstaller', str(project).lower())
        requirements = (ROOT / 'tools/windows/requirements-build.txt').read_text().strip()
        self.assertEqual(requirements, 'pyinstaller==6.16.0')

    def test_packaging_documentation_preserves_unaccepted_a10_and_no_binary_claim(self):
        text = (ROOT / 'tools/windows/README.md').read_text()
        for expected in ('Python 3.13', 'AIOMQL_Engine.exe', 'A10', 'NOT accepted',
                         'Binary build NOT performed in A12.', 'Windows build required in A13.',
                         'requirements-build.txt', 'build_engine.py', 'dist/AIOMQL_Engine', 'package data'):
            self.assertIn(expected, text)

    def test_importing_build_script_does_not_run_packager_or_change_database(self):
        with patch('subprocess.run', side_effect=AssertionError('process')), \
                patch('sqlite3.connect', side_effect=AssertionError('database')):
            build = self.builder()
        self.assertTrue(callable(build.main))
