# Read-only demo smoke

`test_mt5_readonly_smoke.py` is a standalone unittest entrypoint. It skips unless
`AIOMQL_RUN_LIVE_MT5_TESTS=1`, and validates local credentials before importing the
SDK. Configure `MT5_LOGIN`, `MT5_PASSWORD`, and `MT5_SERVER` in the ignored project
`.env`; process environment values take precedence. Use the investor password if
available. Keep the password local.

The smoke is restricted to the requested demo login/server and verifies demo trade
mode. It reads selected account fields, terminal connection state, and seven days
of deal history, including an account check after the fetch. Empty history passes
if the query succeeds. It always shuts down the SDK connection. Diagnostics omit
passwords, raw account dumps, and SDK error descriptions.

From the repository root, after configuring the password and opening MT5, use the
existing project environment:

```powershell
$env:AIOMQL_RUN_LIVE_MT5_TESTS="1"
.\.venv\Scripts\python.exe -B tests/live/test_mt5_readonly_smoke.py -v
Remove-Item Env:AIOMQL_RUN_LIVE_MT5_TESTS
```

Run this entrypoint with unittest, not pytest. The other live tests use autouse
pytest fixtures that connect to MT5 and close positions during cleanup. The raw
SDK entrypoint avoids those fixtures and the normal application's fallback
configuration/reconnect behavior. It does not invoke the trading framework.

Before an enabled live run, the offline regression must pass:

```powershell
python -B -m unittest discover -s tests/offline -p 'test_*.py' -v
```
