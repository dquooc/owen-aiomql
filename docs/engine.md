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
