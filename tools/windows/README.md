# Windows engine packaging and local release record

A12 prepared packaging; the later A13 Windows build produced the local artifact
recorded below. A10 is now **ACCEPTED for explicit bounded live read-only tests**.
The packaged engine remains an offline bootstrap; the source-level A10 smoke
does not add a live SDK/session or automatic recovery to this binary.

## Local release record (2026-11-03)

The supplied release record matches read-only metadata and SHA-256 inspection of
the existing artifact. No rebuild or executable run was performed for this update.

| Field | Recorded value |
| --- | --- |
| Artifact | `dist\AIOMQL_Engine\AIOMQL_Engine.exe` |
| Size | `1767518 bytes` |
| LastWriteTime | `2026-11-03 01:01:04` (Asia/Saigon, UTC+07:00) |
| SHA256 | `AFA67FEF44AECDB1AB983D61CCA3C4E885FC741B046DD3D7E663F38426CFFD9C` |
| Build commit | **build commit chưa xác minh** |
| Distribution | Entire onedir bundle: `dist\AIOMQL_Engine\` |

Existing handover/build documentation, Git history/notes, and generated
PyInstaller spec/TOC/warning/xref records do not bind this binary to a source
commit. `b70967b` records packaging preparation, not verified binary provenance.
Neither the current checkout commit nor the A10 acceptance commit is assigned as
the binary's build commit. The SHA256 above covers the EXE, not the entire bundle.

Distribute the **whole `dist\AIOMQL_Engine\` directory**, including
`AIOMQL_Engine.exe`, `_internal\`, and all bundled dependencies with their relative
layout preserved. The EXE alone is not a complete onedir distribution.

Accepted Windows A10 evidence: **2/2 live PASS**, no skips, exit 0; **702/702
pre-live and 702/702 post-live offline PASS**, both exit 0. Source assurance stays
`CALL_COMPLETED_UNVERIFIED`; reconciliation is
`INDETERMINATE / EVIDENCE_INSUFFICIENT`, recovery stays
`PENDING_RECONCILIATION`, and the checkpoint is unchanged (`None -> None`, zero
advancement calls). Broker writes and order/position mutations are all **0**.
Trading and order submission remain **disabled**. These results come from the
completed source-level tests, not a new live run or a packaged MT5 runtime test.
See [the handover](../../AIOMQL_CODEX_HANDOVER.md#17-a10--mt5-read-only-smoke--accepted)
for the acceptance evidence and scope.

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
tests as part of artifact validation. The separately accepted Windows A10 smoke
does not establish future packaged live read-only runtime integration; A13 must
not infer MT5 acceptance merely because an offline executable starts.

Cloud/Linux checks cover launcher imports, script syntax/structure, mocked command
construction, the OS guard and offline regression only. They do not validate a
Windows binary, bundled SDK, terminal connection, signing or deployment.
