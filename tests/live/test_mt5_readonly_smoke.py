"""Opt-in demo account/history smoke. Run with unittest, without pytest fixtures."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import unittest


ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
DEMO_LOGIN = 10013418577
DEMO_SERVER = "MetaQuotes-Demo"


class SmokeFailure(RuntimeError):
    """A diagnostic that contains no SDK exception text or credentials."""


def load_credentials(path=ENV_FILE, *, environ=None):
    environment = os.environ if environ is None else environ
    values = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            key, separator, value = line.strip().removeprefix("export ").partition("=")
            if separator and key.strip() in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER"):
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key.strip()] = value
    for key in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER"):
        if key in environment:
            values[key] = environment[key]
    password = values.get("MT5_PASSWORD", "")
    if not password.strip():
        raise SmokeFailure("MT5_PASSWORD is not configured locally.")
    if values.get("MT5_LOGIN") != str(DEMO_LOGIN) or values.get("MT5_SERVER") != DEMO_SERVER:
        raise SmokeFailure("Local login/server must match the configured MetaQuotes demo account.")
    return dict(login=DEMO_LOGIN, password=password, server=DEMO_SERVER)


def _failure(client, stage):
    code = client.last_error()[0]
    # The SDK description or exception text might contain sensitive input.
    safe_code = code if type(code) is int else "unavailable"
    return SmokeFailure(f"{stage} failed (MT5 error code: {safe_code}).")


def _verify_account(account, client):
    if account is None:
        raise _failure(client, "account_info")
    if account.login != DEMO_LOGIN or account.server != DEMO_SERVER:
        raise SmokeFailure("Connected login/server differs from the configured demo account.")
    if account.trade_mode != client.ACCOUNT_TRADE_MODE_DEMO:
        raise SmokeFailure("Connected account is not in demo trade mode.")


def run_readonly(client, *, login, password, server):
    if login != DEMO_LOGIN or server != DEMO_SERVER or not password.strip():
        raise SmokeFailure("Explicit configured demo credentials are required before connection.")
    stage = "initialize"
    try:
        if not client.initialize(login=login, password=password, server=server, timeout=10000):
            raise _failure(client, stage)
        stage = "authentication"
        if not client.login(login, password=password, server=server, timeout=10000):
            raise _failure(client, stage)
        stage = "account_info"
        account = client.account_info()
        _verify_account(account, client)
        stage = "terminal_info"
        terminal = client.terminal_info()
        if terminal is None or not terminal.connected:
            raise _failure(client, stage)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=7)
        stage = "history_deals_get"
        deals = client.history_deals_get(start, end)
        if deals is None:
            raise _failure(client, stage)
        stage = "account_info after fetch"
        _verify_account(client.account_info(), client)
        return {
            "initialize": "PASS", "authentication": "PASS",
            "account": {name: getattr(account, name) for name in
                        ("login", "server", "company", "currency", "balance", "equity", "trade_mode")},
            "terminal_connected": bool(terminal.connected),
            "history": {"requested_from": start.isoformat(), "requested_to": end.isoformat(),
                        "query": "PASS", "deal_count": len(deals),
                        "first_ticket": deals[0].ticket if deals else None,
                        "last_ticket": deals[-1].ticket if deals else None},
        }
    except SmokeFailure:
        raise
    except Exception as exc:
        raise SmokeFailure(f"{stage} raised {type(exc).__name__}; SDK details withheld.") from None
    finally:
        try:
            client.shutdown()
        except Exception:
            raise SmokeFailure("shutdown failed; SDK details withheld.") from None


class MT5ReadonlySmokeTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("AIOMQL_RUN_LIVE_MT5_TESTS") == "1",
                         "Set AIOMQL_RUN_LIVE_MT5_TESTS=1 to opt in to the read-only demo smoke")
    def test_configured_demo_connection_and_bounded_history(self):
        credentials = load_credentials()
        # Import only after opt-in and credential validation. No aiomql root
        # imports, fallback configuration, automatic reconnects, or live fixtures.
        import MetaTrader5 as mt5

        report = run_readonly(mt5, **credentials)
        print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    unittest.main()
