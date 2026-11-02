# Engine bootstrap

The A5–A7 sections below record their original stages. A8–A9 add the explicit
startup gate and non-live runtime boundary, without activating a connection.

The SDK-independent composition root is `aiomql_engine.main() -> int`, also
available through `python -m aiomql_engine` when `src` is on the Python import
path. No executable build, console-script registration or packaging choice is
made yet. The existing `aiomql` package eagerly imports SDK-dependent trading
modules; this independent root preserves that library's import/API behavior.

A5 provides a synchronous `run_engine() -> int` boundary with a successful empty
bootstrap. It owns no event loop, starts no tasks, and can be called from an
already running loop without nesting asyncio.run. main preserves runner status,
maps KeyboardInterrupt to 130, and lets internal exceptions/cancellation propagate.
Import does not bootstrap, load the SDK, read config, access credentials, connect,
create files, run recovery or advance checkpoints. Later phases wire bootstrap
configuration/logging; trading and runtime startup are outside A5.

## A6 bootstrap configuration

`EngineConfig` in `aiomql_engine.config` is frozen/slotted, with lexical Path
validation and strict enum/bool/nonnegative timedelta domains. It does not replace
the trading library's mutable account/state/store `aiomql.core.Config`, nor does
it instantiate it. No credentials are stored or requested. The path is omitted
from repr; validation messages do not echo supplied environment values.

`load_config(environ=None)` reads explicit `AIOMQL_ENGINE_` names only:

| Suffix | Default | Accepted environment value |
| --- | --- | --- |
| MODE | offline | offline, read_only |
| DATABASE_PATH | aiomql.sqlite3 | Nonblank file path, without NUL; no filesystem probing |
| LOG_LEVEL | INFO | DEBUG, INFO, WARNING, ERROR, CRITICAL |
| MT5_ENABLED | false | true, false |
| RECOVERY_ENABLED | false | true, false |
| RECOVERY_OVERLAP_MS | 0 | Nonnegative ASCII integer milliseconds |
| DRY_RUN / READ_ONLY | true | true (false is unsupported by current bootstrap) |
| LIVE_TRADING_ENABLED / ORDER_SUBMISSION_ENABLED | false | false (true is unsupported) |

Parsing is case-sensitive and distinguishes unset from malformed/empty. Unknown
environment names are ignored; there is no environment dump, mutation, .env
parser, config-file write, or credential lookup. Duration/path/enum/bool failures
raise without secret values. Static defaults are separate from parsing/validation.

`run_engine(config=None)` loads settings once, or uses an explicitly supplied
EngineConfig. MT5/recovery flags are requests for future wiring and perform no
work in A6: no file/database is created, no session/recovery/checkpoint starts.
This configuration domain restriction is not A8 startup safety implementation.

## A7 engine logging

The composition root now loads/validates config, calls
`configure_logging(LogLevel, stream=None)`, emits
`engine bootstrap initialized mode=<enum value>`, then returns 0. This does not
claim MT5 connection or readiness for trading. Unexpected configuration/logging
setup failures propagate; KeyboardInterrupt remains exit 130.

The standard-library console handler belongs only to the `aiomql_engine` logger
hierarchy, with `LEVEL logger.name message` format and no timestamp/clock read.
Child/module loggers inherit it. Root and trading-library loggers are untouched;
propagation from the engine to root is disabled. A short configuration lock makes
repeated setup reuse one owned handler, update stream/level and avoid duplication.
User-attached handlers are preserved and remain their owner's responsibility.
Import does not add handlers or configure any logger.

Startup logs only a fixed message and validated mode; they never log config repr,
database paths, arbitrary environment input or credential objects. There are no
credential fields in EngineConfig, no .env reads, no generic redaction framework,
file handler/rotation, remote logs or runtime recovery/trading wiring.

Run source bootstrap from the repository root without installing the trading SDK:

```sh
PYTHONPATH=src python -B -m aiomql_engine
```

PowerShell equivalent: set `$env:PYTHONPATH = 'src'` for the current shell, then
`python -B -m aiomql_engine`. No database/session/checkpoint work is performed.
Focused suites are `test_engine_entrypoint.py`, `test_engine_config.py`, and
`test_engine_logging.py`. Linux Cloud verification does not replace Windows
acceptance. A8 Startup Safety is not implemented by these bootstrap stages.

Cloud verification for A5–A7: 24 focused engine tests and 660/660 full offline
tests pass with Python 3.13 (24 added to the user-reported Windows baseline of
636 tests). The full offline run used local event-loop wakeup sockets outside the
sandbox, with a 45-second bound; no test was skipped. This is Cloud implementation
validation, and Windows acceptance remains required. No runtime work or packaging
is implied by a successful bootstrap exit.

## A8 startup safety

`evaluate_startup_safety(EngineConfig) -> StartupSafetyDecision` is pure: no
environment reads, normalization, filesystem, SDK, recovery, checkpoint or task
access. The frozen/slotted decision has explicit bool capabilities for offline
runtime, future MT5 connection/read-only access, recovery, order submission and
live trading, plus dry-run/read-only flags. It is not a live authorization token.

Default startup permits offline dry-run/read-only bootstrap and denies connection,
recovery and all broker writes. Offline plus MT5 enabled raises StartupSafetyError.
Recovery requests are rejected because recovery startup wiring does not exist.
Trading/order flags or disabled dry-run/read-only are rejected, even for malformed
config instances that bypassed A6 validation. Values/credentials are never echoed.
Config is neither mutated nor silently repaired.

A READ_ONLY mode config with MT5 enabled can represent future connection and
read-only access permissions while still forbidding broker writes. Evaluating or
logging these permissions does not connect; A10 is explicitly out of scope here.
Startup loads config, configures logging, evaluates safety exactly once, logs only
safe mode/capability fields and returns. A rejected gate propagates without a
success message. Import remains inert. There is no startup recovery or checkpoint
advancement. These capabilities do not modify the independent trading library.

## A9 dry-run execution boundary

`build_runtime(StartupSafetyDecision) -> EngineRuntime` revalidates the decision
and produces immutable `BrokerWritePolicy` and `BrokerWriteBarrier` objects.
The composition root builds this handle exactly once after the gate. It starts
no runtime loop, read adapter, recovery, database or checkpoint work. Public
main/run_engine still return exit status, not a trading/session handle.

The engine's single mutation seam is async `BrokerWriteBarrier.submit(request)`.
Existing library inspection shows market/new orders and position closes use DEAL,
pending orders use PENDING, changes use MODIFY/SLTP, cancellations use REMOVE, and
close-by uses CLOSE_BY. Those request shapes all reach the same engine barrier;
it denies every request, including unknown shapes, before inspecting request data
or accessing an adapter. Denial raises BrokerWriteDeniedError and returns no fake
success, ticket or broker result. No native mutation function is invoked.

The process default runtime binds no writer at all. An optional opaque writer
can be supplied directly to the barrier for composition tests/future integration;
its repr is hidden and A9 contains no call to it. Even a malformed future policy
cannot activate a live execution branch: live activation is not implemented.
Repeated denial does not mutate inputs, runtime objects or adapter state.

Policy permits local computation/planning/logging; it does not execute arbitrary
callbacks or label them safe. Local database work retains its existing subsystem
contracts. Read adapter passthrough is intentionally not supplied before A10.
Recovery/checkpoint execution is never implied by dry-run or runtime construction.

The barrier is an engine application boundary, not a Python security sandbox.
Future engine wiring must route every broker mutation through this seam and must
not expose an unguarded legacy trader/SDK. Direct use of the existing trading
library outside the engine is unchanged, as are its tests and live test behavior.

Focused suites `test_engine_safety.py` and `test_engine_dry_run.py` cover pure
capabilities, contradictory/malformed requests, future read-only permissions,
exact startup ordering, zero adapter calls, immutable policy, repeated denial,
secret-safe diagnostics and no runtime activation. Windows acceptance is required.
A10 MT5 Read-only Smoke is not implemented or executed in Cloud.

A8–A9 Cloud verification passes 40 focused engine tests and all 676 offline tests,
without skips or timeout (16 added to the A7 suite). Full offline concurrency
verification used local event-loop wakeup sockets outside the sandbox with a
45-second bound. Legacy `src/aiomql` and live tests are unchanged. This remains
Cloud implementation validation; Windows acceptance is required before A10.

## A11 explicit offline runtime composition

`compose_runtime(config=None) -> EngineRuntime` is the single composition root:
load/validate config, configure engine logging, evaluate startup safety, build the
existing runtime/policy/barrier, emit safe capability fields, return the handle.
Construction is explicit and isolated per call; no service locator, runtime
singleton, source, repository, database or RecoveryManager is created.

`run_engine(config=None)` owns `runtime.run()` and a finally-protected
`runtime.shutdown()`. Status is preserved; exceptions/cancellation propagate;
main still maps KeyboardInterrupt to 130. If startup logging raises after the
runtime was constructed, composition shuts it down before propagating the error.
Explicit callers of compose_runtime own the same run/shutdown pair themselves.

The current runtime has no active services/resources: run completes synchronously
with 0, and shutdown is a repeatable no-op. Repeated runs are resource-free and
do not change the write barrier. No event loop, background worker or daemon is
invented. The console logger belongs to the composition root, not runtime cleanup.
Existing policy/barrier fields and construction remain backward compatible.

Recovery-disabled startup does not build recovery dependencies. Recovery-enabled
startup remains unsupported and fails at safety before dependency construction.
Read-only MT5 permission still does not connect. A10 has NOT passed and this work
does not supply live read adapters or general/live runtime activation.

## A12 Windows packaging preparation

The prepared launcher and OS-guarded build command are documented in
[tools/windows/README.md](../tools/windows/README.md). The target is
`AIOMQL_Engine.exe`, using a pinned, separate PyInstaller dependency and an onedir
console bundle of the current offline bootstrap only. No additional package data,
credential/config/database files, live SDK or runtime hooks are included.

Binary build NOT performed in A12. Windows build required in A13. Cloud checks
only structure, syntax, import safety, OS/Python guards and mocked command/status
contracts. Project dependencies/lockfile are unchanged. A10 remains NOT accepted;
successful offline composition does not establish MT5 connectivity or acceptance.

A11–A12 Cloud validation adds 8 runtime-lifecycle and 9 packaging-contract tests.
All 57 focused engine tests and 693/693 full offline tests pass with Python 3.13,
without skips/timeouts. Full concurrency verification uses local asyncio wakeup
sockets outside the sandbox with a 45-second bound. Windows runtime/binary
acceptance remains outstanding; no A13 build or deployment was performed.
