# Engine bootstrap

The A5–A12 sections below record their original implementation/Cloud stages.
A8–A9 add the explicit startup gate and non-live runtime boundary, without
activating a connection. Later Windows artifact and A10 read-only acceptance
are recorded in the local release section below.

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
Read-only MT5 permission still does not connect. A11 did not validate A10 or
supply live read adapters or general/live runtime activation. The later explicit
Windows A10 smoke is recorded separately below.

## A12 Windows packaging preparation

The prepared launcher and OS-guarded build command are documented in
[tools/windows/README.md](../tools/windows/README.md). The target is
`AIOMQL_Engine.exe`, using a pinned, separate PyInstaller dependency and an onedir
console bundle of the current offline bootstrap only. No additional package data,
credential/config/database files, live SDK or runtime hooks are included.

Binary build was not performed in A12; it required the later Windows A13 work.
Cloud checks covered only structure, syntax, import safety, OS/Python guards and
mocked command/status contracts. A12 did not change project dependencies/lockfile
or establish A10 acceptance. Successful offline composition does not establish
MT5 connectivity or acceptance.

A11–A12 Cloud validation adds 8 runtime-lifecycle and 9 packaging-contract tests.
All 57 focused engine tests and 693/693 full offline tests pass with Python 3.13,
without skips/timeouts. Full concurrency verification uses local asyncio wakeup
sockets outside the sandbox with a 45-second bound. At that Cloud stage Windows
runtime/binary acceptance was outstanding; no A13 build or deployment was
performed by those checks.

## Windows local release record (2026-11-03)

The existing Windows artifact is
`dist\AIOMQL_Engine\AIOMQL_Engine.exe` (1767518 bytes; LastWriteTime
`2026-11-03 01:01:04`, Asia/Saigon). Its SHA256 and full release record are in
[the Windows packaging/release document](../tools/windows/README.md#local-release-record-2026-11-03).
**build commit chưa xác minh**: existing records do not bind the binary to a
source commit. A packaging-preparation, acceptance, or current checkout commit
must not be substituted for build provenance.

Distribute the entire onedir directory `dist\AIOMQL_Engine\`, including its
`_internal\` dependencies; distributing only the EXE is incomplete. The artifact
remains an offline bootstrap with trading/order submission disabled. Its presence
and checksum do not establish a packaged MT5 connection or automatic recovery.

A10 is **ACCEPTED in the bounded live read-only scope**: **2/2 live PASS**,
**702/702 pre-live and 702/702 post-live offline PASS**, all exit 0. Actual-batch
assurance remains `CALL_COMPLETED_UNVERIFIED`; reconciliation remains
`INDETERMINATE / EVIDENCE_INSUFFICIENT`, recovery remains
`PENDING_RECONCILIATION`, and the checkpoint is unchanged with zero advancement.
Broker writes and order/position mutations are **0**. This is the previously
completed explicit source-level smoke, not new runtime wiring or a binary live
test. No rebuild, live run, or runtime change accompanies this documentation
update. See [the handover](../AIOMQL_CODEX_HANDOVER.md#17-a10--mt5-read-only-smoke--accepted)
for the accepted evidence.

## A16 engine-owned read-only session

A16 extends the earlier bootstrap stages above with a real session seam. Fake-SDK
implementation validation and the separate Windows native read-only acceptance
below have passed. The A10 acceptance and existing binary record remain historical
evidence for their original scope; they were not substituted for A16 validation.

`aiomql_engine.mt5_readonly.MT5ReadOnlySession` implements the existing
`aiomql.ledger.source.MetaTraderHistoryClient` structurally: async
`account_info()` and keyword-only `history_deals_get(date_from=..., date_to=...)`.
It also exposes async `terminal_info()` and synchronous `open()` / `shutdown()`.
Account/terminal results are minimal immutable engine snapshots. History retains
raw SDK records for the existing source/normalizer/ingestor; A16 adds no evidence
model, account registration, ledger wiring, or reconciliation policy.

`compose_runtime(config, session_factory=...)` evaluates the existing safety gate,
requires an explicit expected account binding, then constructs the injected
`ReadOnlySession`. Construction does not load or initialize the SDK. Only
`runtime.run()` opens it. `run_engine(config, session_factory=...)` owns the
run/finally-shutdown pair and does not fetch history. Explicit composition callers
own the same lifecycle and may issue bounded reads between run and shutdown.
Offline composition never invokes the session factory. Recovery remains rejected.

The default loader imports `MetaTrader5` only inside explicit session open. The
session binds only initialize/account/terminal/history/shutdown callables; it
exposes no SDK facade, login/account-switch operation, arbitrary attribute
forwarding or broker writer. It uses the terminal's existing authentication;
configure/sign in to the intended terminal separately. No credentials, `.env`
loader or credential fallback is introduced.

Additional EngineConfig / explicit `AIOMQL_ENGINE_` environment settings:

| Suffix | Default | Contract |
| --- | --- | --- |
| MT5_EXPECTED_LOGIN | unset | Positive integer; required before MT5 session construction |
| MT5_EXPECTED_SERVER | unset | Exact nonblank server identity; required before construction |
| MT5_TIMEOUT_MS | 10000 | Positive initialization timeout, at most 2147483647 ms |
| MT5_MAX_HISTORY_DAYS | 7 | Positive maximum duration per history request, at most 999999999 days |

Connectivity still requires `MODE=read_only`, `MT5_ENABLED=true`,
`DRY_RUN=true`, `READ_ONLY=true`, with trading/order submission/recovery disabled.
Identity settings are excluded from config repr and startup logs. Startup logs
describe requested capabilities, not proof that connection has succeeded.

Open validates account login/server and terminal.connected. Every bounded history
read validates aware ordered UTC endpoints (preserving microseconds), enforces the
configured duration limit, checks account and terminal before the SDK call, then
checks account again before returning the batch. A mismatch discards the batch and
closes the session. Empty history is valid; None is a failure. Source collection
and record validation remain in MetaTraderDealHistorySource and DealIngestor.
Real source assurance stays `CALL_COMPLETED_UNVERIFIED`.

Open is idempotent while active; shutdown is idempotent and permanently closes the
session. Each initialization attempt has at most one native shutdown attempt,
including failed initialization, startup verification failure and read errors.
SDK exceptions are replaced by fixed diagnostics; cancellation/KeyboardInterrupt
propagate after cleanup. A shutdown failure closes the local handle and is not
retried; if cleanup fails during another error, the original error is preserved.

Native SDK calls are synchronous, including those behind async read signatures.
A session lock serializes native operations; there is no worker, service loop,
scheduler, automatic retry or recovery. Cancellation is checked before reads;
an in-flight native call cannot be interrupted, and the configured initialization
timeout is not a history timeout. Callers must use finally-protected shutdown.
Session locks do not coordinate separate sessions/processes or prevent external
terminal account switching (including A -> B -> A). This remains an application
read-only boundary, not a Python security sandbox or history-completeness proof.

Offline verification uses `tests/offline/test_engine_mt5_readonly.py` with fake SDK
mutation traps and existing engine/source contracts. The A10 local safety probe
now injects an inert read session so it continues to test capability/denial without
native SDK access. The obsolete packaging test requiring the old phrase
`NOT accepted` was removed; executable build/launcher contracts remain covered.

A16 does not change the build script's SDK exclusion or implement persistent
recovery, production SQLite composition, monitoring, packaging, or trading.

### A16 Windows native acceptance — 2026-11-03

**ACCEPTED for explicit bounded read-only sessions through engine composition.**
Executed in `E:\BLC\Dev_AI_MT5` with `.venv\Scripts\python.exe`, Python 3.13.18
and MetaTrader5 distribution 5.0.5640. Base HEAD remained `581b48b`; A16 source was
uncommitted. No commit, push, rebuild, account login/switch or credential loading
was performed. Opt-in and engine settings were scoped to a fresh child process.

`tools/windows/accept_a16_readonly.py` exercised the actual default
`compose_runtime -> MT5ReadOnlySession -> runtime.run -> shutdown` path. A narrow
instrumentation facade forwarded only native initialize, account/terminal reads,
history and shutdown. The SDK importer was traced, with a process-local native
writer tripwire installed but never invoked. Offline/default startup did not load
the SDK; read-only composition remained lazy until runtime.run. The currently
authenticated terminal was verified as demo without printing account identity.

Observed positive native attempt:

```text
runtime result = 0
initialize PASS; terminal connected PASS
pre-fetch expected-account guard PASS
UTC request: 2026-10-27T03:47:23.814628+00:00
          -> 2026-11-03T03:47:23.814628+00:00
configured limit = 7 days; actual window = 7 days
native history calls = 1; result not None; retrieved deals = 1
post-fetch expected-account guard PASS
shutdown PASS; repeated cleanup idempotent
closed session read rejected without another native call
```

Three invalid/unbounded/reversed history requests were rejected before any native
history call. A second native attempt changed only the expected identity; the
same actual demo account was observed and rejected by the A16 adapter before
history. No account switch was attempted. Across both attempts, native initialize
calls were 2, shutdown calls were 2, and no connection remained owned by the
runner. Disconnected/missing terminal cases were checked safely with fake SDKs.

```text
native order_send calls = 0
broker mutation calls = 0
order mutations = 0
position mutations = 0
writer tripwire attempts = 0
```

Source A16 files and all 865 pre-existing SQLite files were checksum-identical
immediately before/after the native run. No ledger/recovery path was activated.
Existing source assurance stays CALL_COMPLETED_UNVERIFIED; this acceptance makes
no claim of complete history, checkpoint completion, packaged runtime readiness,
or trading capability.

Pre-review confirmed the removed packaging test checked prose strings only,
including obsolete `NOT accepted` wording. Runtime, write-barrier, SDK exclusion,
launcher and build contracts remain covered; no behavioral replacement was needed.

Verification: **31/31 focused A16 PASS**, **2/2 runner fake-contract PASS**, native
acceptance runner **exit 0**, and post-native full offline **734/734 PASS**, no
skips, 12.360s, exit 0. The two added runner tests cover successful one-fetch
composition/negative identity/cleanup and a non-demo rejection before history.
`git diff --check` passed. Native testing did not execute the old A10 live suite
or unrestricted pytest discovery.
