"""Windows-only A13 build command, prepared but not executed in A12."""

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def build_command() -> list[str]:
    """Describe a console onedir bootstrap bundle; no data or integration hooks."""
    return [
        sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm', '--onedir', '--console',
        '--name', 'AIOMQL_Engine', '--paths', str(ROOT / 'src'),
        '--distpath', str(ROOT / 'dist'), '--workpath', str(ROOT / 'build' / 'engine'),
        '--specpath', str(ROOT / 'build' / 'engine'),
        '--exclude-module', 'MetaTrader5', str(ROOT / 'tools' / 'windows' / 'engine_entry.py'),
    ]


def main() -> int:
    """Reject unsupported hosts before invoking a packager; preserve build status."""
    if sys.platform != 'win32':
        print('Windows build required in A13; no build performed.', file=sys.stderr)
        return 2
    if sys.version_info[:2] < (3, 13):
        print('Python 3.13 or later is required; no build performed.', file=sys.stderr)
        return 2
    return subprocess.run(build_command(), cwd=ROOT, check=False).returncode


if __name__ == '__main__':
    raise SystemExit(main())
