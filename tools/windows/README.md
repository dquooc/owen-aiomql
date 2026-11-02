# Windows engine packaging preparation (A12)

Binary build NOT performed in A12.
Windows build required in A13.
A10 MT5 Read-only Smoke is NOT accepted. This preparation does not resolve it.

## Strategy and prerequisites

Use 64-bit Windows with CPython/Python 3.13, matching the offline development
baseline. PyInstaller 6.16.0 is pinned as an isolated build dependency: a mainstream
freezer with Python 3.13 support, no compiler requirement for this pure-Python
bootstrap, and a simple console/onedir layout for inspecting the first artifact.
The pin is preparation, not evidence of a successful Windows installation/build.
No Nuitka/alternative toolchain or runtime hook is added.

The distribution's runtime dependencies in pyproject.toml and its lockfile remain
unchanged; test/dev and packaging dependencies are separate. The current engine
bootstrap uses only the standard library and aiomql_engine; its bundle does not
need pandas/Jupyter or the trading package. The MetaTrader5 Python package is
Windows-specific and is explicitly excluded from this offline bootstrap artifact.
Later live/read-only integration will require a compatible Windows SDK, an MT5
desktop terminal and separate packaging/acceptance work. No terminal, account,
credential or live access is needed to build this offline target.

The stable target is engine_entry.py -> aiomql_engine:main. Project version is
4.1.2 in pyproject.toml; no aiomql import/version lookup is used. No executable
version resource is fabricated at this stage. Additional package data: **none**.
No data collection, config file, database, credential file or development tree is
bundled. The launcher does not load .env or a production DB.

## Prepared A13 commands (do not run as part of A12)

From repository root in a dedicated Windows build environment:

```powershell
python --version
python -m pip install -r tools/windows/requirements-build.txt
python tools/windows/build_engine.py
```

The script invokes the current interpreter's PyInstaller and rejects non-Windows
hosts or Python older than 3.13 before process/output creation. Use the documented
3.13 baseline for the first accepted build. It uses paths derived from the checkout,
with no embedded developer-machine directory. Generated spec/work files go to
build/engine; output goes to dist/AIOMQL_Engine/AIOMQL_Engine.exe. These build/dist
directories are already ignored. The command refreshes that generated output;
do not place user-owned data there. No executable or installer is committed.

## Required A13 validation

Run full offline tests before the actual Windows build. Inspect the onedir bundle
for unintended data/dependencies, then run the executable from an empty temporary
working directory with safe default engine settings. Verify exit status, safe
capability logs, no SDK/session, no DB/checkpoint creation and no broker mutation.
Exercise malformed/contradictory settings and repeated startup. Do not run live
tests as part of artifact validation. A10 remains a separate Windows prerequisite
for any future live read-only integration; A13 must not claim it passed merely
because an offline executable starts.

Cloud/Linux checks cover launcher imports, script syntax/structure, mocked command
construction, the OS guard and offline regression only. They do not validate a
Windows binary, bundled SDK, terminal connection, signing or deployment.
