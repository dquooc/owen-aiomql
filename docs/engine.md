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
