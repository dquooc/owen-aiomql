"""Opt-in demo account/history smoke. Run with unittest, without pytest fixtures."""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4


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


class _ReadOnlySDK:
    """Expose only the accepted connection/read calls; never forward a writer."""

    _allowed = frozenset({"initialize", "login", "account_info", "terminal_info",
                          "history_deals_get", "last_error", "shutdown", "ACCOUNT_TRADE_MODE_DEMO"})

    def __init__(self, client):
        self._client = client
        self.broker_mutation_attempts = 0

    def deny_write(self, *args, **kwargs):
        self.broker_mutation_attempts += 1
        raise SmokeFailure("Broker mutation attempted; native writer was not invoked.")

    def __getattr__(self, name):
        if name not in self._allowed:
            return self.deny_write
        return getattr(self._client, name)


@contextmanager
def _demo_session(client, *, login, password, server):
    if login != DEMO_LOGIN or server != DEMO_SERVER or not password.strip():
        raise SmokeFailure("Explicit configured demo credentials are required before connection.")
    readonly = _ReadOnlySDK(client)
    stage = "initialize"
    # This process-local trap also protects accidental direct SDK access. Its
    # replacement raises before the saved native function can ever be called.
    with patch.object(client, "order_send", readonly.deny_write, create=True):
        try:
            if not readonly.initialize(timeout=10000):
                raise _failure(readonly, stage)
            stage = "current account before authentication"
            _verify_account(readonly.account_info(), readonly)
            stage = "authentication"
            if not readonly.login(login, password=password, server=server, timeout=10000):
                raise _failure(readonly, stage)
            stage = "account_info"
            account = readonly.account_info()
            _verify_account(account, readonly)
            stage = "terminal_info"
            terminal = readonly.terminal_info()
            if terminal is None or not terminal.connected:
                raise _failure(readonly, stage)
            stage = "read-only acceptance"
            yield readonly, account, terminal
            if readonly.broker_mutation_attempts:
                raise SmokeFailure("Broker mutation was attempted; native writer was not invoked.")
        except SmokeFailure:
            raise
        except Exception as exc:
            raise SmokeFailure(f"{stage} raised {type(exc).__name__}; SDK details withheld.") from None
        finally:
            try:
                readonly.shutdown()
            except Exception:
                raise SmokeFailure("shutdown failed; SDK details withheld.") from None


def run_readonly(client, *, login, password, server):
    with _demo_session(client, login=login, password=password, server=server) as (readonly, account, terminal):
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=7)
        deals = readonly.history_deals_get(start, end)
        if deals is None:
            raise _failure(readonly, "history_deals_get")
        _verify_account(readonly.account_info(), readonly)
        return {
            "initialize": "PASS", "authentication": "PASS",
            "account": {name: getattr(account, name) for name in
                        ("login", "server", "company", "currency", "balance", "equity", "trade_mode")},
            "terminal_connected": bool(terminal.connected),
            "history": {"requested_from": start.isoformat(), "requested_to": end.isoformat(),
                        "query": "PASS", "deal_count": len(deals),
                        "first_ticket": deals[0].ticket if deals else None,
                        "last_ticket": deals[-1].ticket if deals else None},
            "native_order_send_calls": 0,
            "broker_mutation_attempts": readonly.broker_mutation_attempts,
        }


def load_ledger():
    """Use the offline suite's isolated-package pattern, without aiomql root."""
    package = "_aiomql_ledger_readonly_smoke"
    if package not in sys.modules:
        folder = Path(__file__).resolve().parents[2] / "src" / "aiomql" / "ledger"
        spec = importlib.util.spec_from_file_location(
            package, folder / "__init__.py", submodule_search_locations=[str(folder)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[package] = module
        spec.loader.exec_module(module)
    return sys.modules[package]


def check_engine_readonly_safety():
    """Exercise actual composition and denial, using only a local fake writer."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from aiomql_engine import compose_runtime
    from aiomql_engine.config import EngineConfig, EngineMode
    from aiomql_engine.execution import BrokerWriteBarrier, BrokerWriteDeniedError
    from aiomql_engine.safety import StartupSafetyError, evaluate_startup_safety

    class LocalWriter:
        calls = 0

        async def submit(self, request):
            self.calls += 1
            raise AssertionError("Local writer must remain unreachable")

    config = EngineConfig(mode=EngineMode.READ_ONLY, mt5_enabled=True, dry_run=True,
                          read_only=True, recovery_enabled=False, order_submission_enabled=False,
                          live_trading_enabled=False)
    decision = evaluate_startup_safety(config)
    runtime = compose_runtime(config)
    writer = LocalWriter()
    results, denied = [], []

    async def probe():
        barriers = (runtime.broker_writes, BrokerWriteBarrier(runtime.policy, writer))
        for barrier in barriers:
            for action in ("DEAL", "PENDING", "MODIFY", "SLTP", "REMOVE", "CLOSE_BY"):
                try:
                    results.append(await barrier.submit({"action": action}))
                except BrokerWriteDeniedError:
                    denied.append(action)

    try:
        exit_code = runtime.run()
        asyncio.run(probe())
    finally:
        runtime.shutdown()
    try:
        compose_runtime(replace(config, recovery_enabled=True))
    except StartupSafetyError:
        recovery_rejected = True
    else:
        recovery_rejected = False
    if (exit_code != 0 or writer.calls or results or len(denied) != 12 or not recovery_rejected
            or not decision.mt5_connection or not decision.read_only_mt5
            or decision.recovery_execution or decision.order_submission or decision.live_trading):
        raise SmokeFailure("Engine read-only safety acceptance failed.")
    return {"mode": config.mode.value, "mt5_enabled": config.mt5_enabled,
            "dry_run": runtime.policy.dry_run, "read_only": runtime.policy.read_only,
            "recovery_enabled": decision.recovery_execution, "order_submission": decision.order_submission,
            "live_trading": decision.live_trading, "runtime_exit": exit_code,
            "denied_probes": len(denied), "underlying_fake_writer_calls": writer.calls,
            "broker_results": results, "automatic_recovery_rejected": recovery_rejected}


def run_recovery_readonly(client, *, login, password, server):
    ledger = load_ledger()
    with _demo_session(client, login=login, password=password, server=server) as (readonly, observed, terminal):
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=7)
        guards, executions, snapshots = [], [], []
        counts = {"fetch": 0, "clock": 0, "advance": 0}

        class HistoryReads:
            async def account_info(self):
                info = readonly.account_info()
                _verify_account(info, readonly)
                guards.append("before:PASS" if counts["fetch"] == 0 else "after:PASS")
                return info

            async def history_deals_get(self, *, date_from, date_to):
                counts["fetch"] += 1
                if counts["fetch"] != 1 or (date_from, date_to) != (start, end):
                    raise SmokeFailure("Recovery must perform exactly one fetch of the explicit UTC window.")
                return readonly.history_deals_get(date_from, date_to)

        def horizon():
            counts["clock"] += 1
            return end

        def deny_advancement(*args, **kwargs):
            counts["advance"] += 1
            raise SmokeFailure("Unexpected checkpoint advancement; blocked before mutation.")

        with tempfile.TemporaryDirectory(prefix="aiomql-a10-2-") as folder:
            temp_folder = Path(folder)
            repository = ledger.SQLiteDealRepository(temp_folder / "recovery.sqlite3")
            try:
                # The namespace is an explicit demo-test binding to the verified
                # server, not an inference that company metadata is an identity.
                # The UUID is registered and used only inside this new temp DB.
                account = ledger.AccountRef(mt5_account_id=uuid4(), broker_namespace=DEMO_SERVER,
                                            server=observed.server, login=observed.login,
                                            company_observed=observed.company, trade_mode=observed.trade_mode)
                repository.register_account(account)
                registered = repository.resolve_account(broker_namespace=DEMO_SERVER,
                                                         server=observed.server, login=observed.login)
                if registered != account:
                    raise SmokeFailure("Temporary account registration did not retain observed identity.")
                repository.advance_checkpoint_if_expected = deny_advancement
                repository.advance_checkpoint = deny_advancement
                before = repository.get_checkpoint(account.mt5_account_id)
                source = ledger.MetaTraderDealHistorySource(HistoryReads(), broker_namespace=DEMO_SERVER)
                ingestor = ledger.DealIngestor(source, repository)
                ingest = ingestor.ingest_range_with_evidence

                async def capture_execution(**kwargs):
                    execution = await ingest(**kwargs)
                    executions.append(execution)
                    return execution

                ingestor.ingest_range_with_evidence = capture_execution
                read_snapshot = repository.read_snapshot

                def capture_snapshot(**kwargs):
                    snapshot = read_snapshot(**kwargs)
                    snapshots.append(snapshot)
                    return snapshot

                repository.read_snapshot = capture_snapshot
                manager = ledger.RecoveryManager(
                    account_lookup=repository, checkpoints=repository, snapshot_reader=repository,
                    config=ledger.RecoveryConfig(overlap=timedelta(0), bootstrap_start=start),
                    clock=horizon, ingestor=ingestor)
                result = asyncio.run(manager.recover(account=registered))
                after = repository.get_checkpoint(account.mt5_account_id)
                execution = executions[0]
                evidence = execution.source_evidence
                reconciliation = result.reconciliation_result
                ingestion = result.ingestion_report
                report = {
                    "requested_from": start.isoformat(), "requested_to": end.isoformat(),
                    "source_fetch_count": counts["fetch"], "account_guards": guards,
                    "retrieved": ingestion.retrieved, "inserted": ingestion.inserted,
                    "ingestion_status": ingestion.status.value,
                    "ingestion_error_stages": [error.stage.value for error in ingestion.errors],
                    "source_assurance": evidence.assurance.value if evidence else None,
                    "reconciliation_status": reconciliation.status.value if reconciliation else None,
                    "reconciliation_reason": reconciliation.reason.value if reconciliation and reconciliation.reason else None,
                    "recovery_outcome": result.outcome.value,
                    "checkpoint_before": None if before is None else before.completed_through.isoformat(),
                    "checkpoint_after": None if after is None else after.completed_through.isoformat(),
                    "checkpoint_advancement_calls": counts["advance"],
                    "snapshot_reads": len(snapshots), "clock_calls": counts["clock"],
                    "persisted_deals": len(snapshots[0].records) if snapshots else 0,
                    "temporary_sqlite": repository.path.parent == temp_folder.resolve(),
                    "persistent_database_modified": False,
                    "native_order_send_calls": 0,
                    "broker_mutation_attempts": readonly.broker_mutation_attempts,
                }
                clean = (ingestion.status in (ledger.IngestionStatus.SUCCESS, ledger.IngestionStatus.SUCCESS_EMPTY)
                         and evidence is not None
                         and evidence.assurance is ledger.SourceHistoryAssurance.CALL_COMPLETED_UNVERIFIED
                         and reconciliation is not None
                         and reconciliation.status is ledger.ReconciliationStatus.INDETERMINATE
                         and reconciliation.reason is ledger.ReconciliationReason.EVIDENCE_INSUFFICIENT
                         and result.outcome is ledger.RecoveryOutcome.PENDING_RECONCILIATION
                         and before == after == result.checkpoint_before == result.checkpoint_after
                         and counts == {"fetch": 1, "clock": 1, "advance": 0}
                         and guards == ["before:PASS", "after:PASS"] and len(snapshots) == 1
                         and len(executions) == 1 and not readonly.broker_mutation_attempts)
                if not clean:
                    raise SmokeFailure("Recovery acceptance failed: " + json.dumps(report, ensure_ascii=True))
            finally:
                repository.close()
        report["temporary_sqlite_cleaned"] = not temp_folder.exists()
        if not report["temporary_sqlite_cleaned"]:
            raise SmokeFailure("Temporary SQLite cleanup failed.")
    report["shutdown"] = True
    return report


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

    @unittest.skipUnless(os.environ.get("AIOMQL_RUN_LIVE_MT5_TESTS") == "1",
                         "Set AIOMQL_RUN_LIVE_MT5_TESTS=1 to opt in to the read-only demo smoke")
    def test_recovery_manager_readonly_with_temporary_sqlite(self):
        engine = check_engine_readonly_safety()
        credentials = load_credentials()
        import MetaTrader5 as mt5

        report = run_recovery_readonly(mt5, **credentials)
        print(json.dumps({"a10_2_recovery": report, "engine_safety": engine}, ensure_ascii=True))


if __name__ == "__main__":
    unittest.main()
