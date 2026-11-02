# Engine bootstrap

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
