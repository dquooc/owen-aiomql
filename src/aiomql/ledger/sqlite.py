"""Local SQLite implementation of the account-scoped Deal Ledger."""

from contextlib import contextmanager, closing
from dataclasses import fields, replace
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from typing import Iterator
from uuid import UUID, uuid5

from .models import AccountRef, LedgerCheckpoint, NormalizedDeal, RecoveryRange
from .ledger_snapshot import LedgerRecordFingerprint, LedgerConflictEvidence, LedgerSnapshotEvidence, execution_in_range
from .normalize import NORMALIZER_VERSION, normalize_deal
from .repository import AccountIdentityError, CheckpointRegressionError, DealObservation, IngestOutcome, IngestResult
from .repository import CheckpointExpectationError, LedgerSnapshotChangedError


_OBSERVATION_NAMESPACE = UUID("a3aa2b42-8d9d-41a5-8763-a593f60851d5")

_DEAL_COLUMNS = (
    "id", "mt5_account_id", "deal_ticket", "order_ticket", "position_id", "symbol",
    "deal_type", "deal_entry", "magic", "volume", "price", "commission", "swap",
    "profit", "broker_fee", "broker_time_seconds", "executed_at_msc", "reason",
    "sl", "tp", "comment", "external_id", "raw_payload", "payload_hash",
    "normalizer_version", "first_seen_at", "ingestion_id",
)


class SQLiteDealRepository:
    """One connection per operation; SQLite constraints arbitrate concurrent writers."""

    def __init__(self, db_path: str | Path):
        if str(db_path) == ":memory:":
            raise ValueError("Deal Ledger requires a durable SQLite file")
        self.path = Path(db_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._closed = False
        self._initialize_schema()

    def close(self) -> None:
        """Prevent further operations; connections are closed after each call."""
        self._closed = True

    def _connect(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("Deal repository is closed")
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize_schema(self) -> None:
        # executescript() commits any open transaction before running; keep
        # schema creation in the same explicit transaction as other writes.
        statements = (
            """
                CREATE TABLE IF NOT EXISTS ledger_accounts (
                    mt5_account_id TEXT PRIMARY KEY,
                    broker_namespace TEXT NOT NULL,
                    server TEXT NOT NULL,
                    login TEXT NOT NULL,
                    company_observed TEXT NOT NULL,
                    trade_mode INTEGER NOT NULL,
                    UNIQUE (broker_namespace, server, login)
                )
            """,
            """
                CREATE TABLE IF NOT EXISTS ledger_checkpoints (
                    mt5_account_id TEXT NOT NULL PRIMARY KEY
                        REFERENCES ledger_accounts(mt5_account_id),
                    completed_through TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """,
            """
                CREATE TABLE IF NOT EXISTS deals (
                    id TEXT PRIMARY KEY,
                    mt5_account_id TEXT NOT NULL REFERENCES ledger_accounts(mt5_account_id),
                    deal_ticket TEXT NOT NULL,
                    order_ticket TEXT NOT NULL,
                    position_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    deal_type INTEGER NOT NULL,
                    deal_entry INTEGER NOT NULL,
                    magic TEXT NOT NULL,
                    volume TEXT NOT NULL,
                    price TEXT NOT NULL,
                    commission TEXT NOT NULL,
                    swap TEXT NOT NULL,
                    profit TEXT NOT NULL,
                    broker_fee TEXT NOT NULL,
                    broker_time_seconds INTEGER NOT NULL,
                    executed_at_msc INTEGER NOT NULL,
                    reason INTEGER,
                    sl TEXT,
                    tp TEXT,
                    comment TEXT,
                    external_id TEXT,
                    raw_payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    normalizer_version INTEGER NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    ingestion_id TEXT NOT NULL,
                    UNIQUE (mt5_account_id, deal_ticket)
                )
            """,
            "CREATE INDEX IF NOT EXISTS idx_deals_order ON deals (mt5_account_id, order_ticket)",
            "CREATE INDEX IF NOT EXISTS idx_deals_position ON deals (mt5_account_id, position_id)",
            """
                CREATE TABLE IF NOT EXISTS deal_observations (
                    id TEXT PRIMARY KEY,
                    deal_id TEXT NOT NULL REFERENCES deals(id),
                    payload_hash TEXT NOT NULL,
                    raw_payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    ingestion_id TEXT NOT NULL,
                    normalizer_version INTEGER NOT NULL,
                    UNIQUE (deal_id, payload_hash)
                )
            """,
        )
        with self._write() as conn:
            for statement in statements:
                conn.execute(statement)

    @staticmethod
    def _account_from_row(row: sqlite3.Row) -> AccountRef:
        return AccountRef(mt5_account_id=UUID(row["mt5_account_id"]),
                          broker_namespace=row["broker_namespace"], server=row["server"],
                          login=int(row["login"]), company_observed=row["company_observed"],
                          trade_mode=row["trade_mode"])

    @staticmethod
    def _identity(account: AccountRef) -> tuple[str, str, str]:
        return account.broker_namespace, account.server, str(account.login)

    def register_account(self, account: AccountRef) -> AccountRef:
        if not isinstance(account, AccountRef):
            raise TypeError("account must be an AccountRef")
        account_id = str(account.mt5_account_id)
        identity = self._identity(account)
        with self._write() as conn:
            by_id = conn.execute("SELECT * FROM ledger_accounts WHERE mt5_account_id = ?", (account_id,)).fetchone()
            by_identity = conn.execute(
                "SELECT * FROM ledger_accounts WHERE broker_namespace = ? AND server = ? AND login = ?",
                identity).fetchone()
            if by_id is not None and self._identity(self._account_from_row(by_id)) != identity:
                raise AccountIdentityError("account UUID is already mapped to a different broker/server/login")
            if by_identity is not None and by_identity["mt5_account_id"] != account_id:
                raise AccountIdentityError("broker/server/login is already mapped to a different account UUID")
            if by_id is None:
                conn.execute(
                    "INSERT INTO ledger_accounts VALUES (?, ?, ?, ?, ?, ?)",
                    (account_id, *identity, account.company_observed, account.trade_mode))
            else:
                # These are observed metadata, not immutable identity fields.
                conn.execute("UPDATE ledger_accounts SET company_observed = ?, trade_mode = ? "
                             "WHERE mt5_account_id = ?",
                             (account.company_observed, account.trade_mode, account_id))
        return account

    def resolve_account(self, *, broker_namespace: str, server: str, login: int) -> AccountRef | None:
        if not isinstance(broker_namespace, str) or not isinstance(server, str):
            raise TypeError("broker_namespace and server must be strings")
        if isinstance(login, bool) or not isinstance(login, int) or login <= 0:
            raise ValueError("login must be a positive integer")
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM ledger_accounts WHERE broker_namespace = ? AND server = ? AND login = ?",
                (broker_namespace, server, str(login))).fetchone()
        return self._account_from_row(row) if row is not None else None

    @staticmethod
    def _checkpoint_from_row(row: sqlite3.Row) -> LedgerCheckpoint:
        return LedgerCheckpoint(
            mt5_account_id=UUID(row["mt5_account_id"]),
            completed_through=datetime.fromisoformat(row["completed_through"]),
            updated_at=datetime.fromisoformat(row["updated_at"]))

    def get_checkpoint(self, mt5_account_id: UUID) -> LedgerCheckpoint | None:
        """Read a registered account's checkpoint without creating or repairing it.

        Unknown accounts raise AccountIdentityError. Invalid persisted timestamps
        propagate decoding/model errors rather than being treated as missing.
        """
        account_id = self._account_id(mt5_account_id)
        if mt5_account_id.int == 0:
            raise ValueError("mt5_account_id cannot be the nil UUID")
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT a.mt5_account_id, c.mt5_account_id AS checkpoint_account_id, "
                "c.completed_through, c.updated_at FROM ledger_accounts AS a "
                "LEFT JOIN ledger_checkpoints AS c ON c.mt5_account_id = a.mt5_account_id "
                "WHERE a.mt5_account_id = ?", (account_id,)).fetchone()
        if row is None:
            raise AccountIdentityError("account is not registered in this ledger")
        if row["checkpoint_account_id"] is None:
            return None
        return self._checkpoint_from_row(row)

    def advance_checkpoint(self, account: AccountRef, completed_through: datetime) -> LedgerCheckpoint:
        """Create or advance a checkpoint; equal boundaries leave storage unchanged.

        Account registration/identity is checked without registration side effects.
        Compare canonical UTC instants inside the write transaction. Earlier
        boundaries raise CheckpointRegressionError without changing storage.
        """
        if not isinstance(account, AccountRef):
            raise TypeError("account must be an AccountRef")
        checkpoint = LedgerCheckpoint(account.mt5_account_id, completed_through, datetime.now(timezone.utc))
        account_id = str(checkpoint.mt5_account_id)
        with self._write() as conn:
            registered = conn.execute(
                "SELECT * FROM ledger_accounts WHERE mt5_account_id = ?", (account_id,)).fetchone()
            if registered is None:
                raise AccountIdentityError("account is not registered in this ledger")
            if self._identity(self._account_from_row(registered)) != self._identity(account):
                raise AccountIdentityError("supplied account context differs from persisted identity")
            existing = conn.execute(
                "SELECT * FROM ledger_checkpoints WHERE mt5_account_id = ?", (account_id,)).fetchone()
            if existing is not None:
                current = self._checkpoint_from_row(existing)
                if checkpoint.completed_through == current.completed_through:
                    return current
                if checkpoint.completed_through < current.completed_through:
                    raise CheckpointRegressionError(
                        f"checkpoint regression for account {account_id}: "
                        f"current={current.completed_through.isoformat(timespec='microseconds')}, "
                        f"requested={checkpoint.completed_through.isoformat(timespec='microseconds')}")
                checkpoint = replace(checkpoint, updated_at=datetime.now(timezone.utc))
                conn.execute(
                    "UPDATE ledger_checkpoints SET completed_through = ?, updated_at = ? WHERE mt5_account_id = ?",
                    (checkpoint.completed_through.isoformat(timespec="microseconds"),
                     checkpoint.updated_at.isoformat(timespec="microseconds"), account_id))
            else:
                conn.execute(
                    "INSERT INTO ledger_checkpoints (mt5_account_id, completed_through, updated_at) VALUES (?, ?, ?)",
                    (account_id, checkpoint.completed_through.isoformat(timespec="microseconds"),
                     checkpoint.updated_at.isoformat(timespec="microseconds")))
        return checkpoint

    def advance_checkpoint_if_expected(self, account: AccountRef, completed_through: datetime, *,
                                       expected: LedgerCheckpoint | None,
                                       expected_snapshot: LedgerSnapshotEvidence | None = None) -> LedgerCheckpoint:
        """CAS the recovery boundary and revalidate evidence under BEGIN IMMEDIATE.

        The snapshot read uses this SAME connection/transaction, so another writer
        cannot insert a contradictory row between validation and durable commit.
        updated_at is generated only as audit metadata; the target is caller-supplied.
        Storage/commit errors propagate without reporting success or blind retry.
        """
        if not isinstance(account, AccountRef):
            raise TypeError("account must be an AccountRef")
        if expected is not None and not isinstance(expected, LedgerCheckpoint):
            raise TypeError("expected must be a LedgerCheckpoint or None")
        if expected_snapshot is not None and not isinstance(expected_snapshot, LedgerSnapshotEvidence):
            raise TypeError("expected_snapshot must be LedgerSnapshotEvidence or None")
        boundary = RecoveryRange(completed_through, completed_through).to_time
        if expected is not None and expected.mt5_account_id != account.mt5_account_id:
            raise CheckpointExpectationError("expected checkpoint belongs to another account")
        account_id = str(account.mt5_account_id)
        with self._write() as conn:
            registered = conn.execute("SELECT * FROM ledger_accounts WHERE mt5_account_id = ?", (account_id,)).fetchone()
            if registered is None or self._identity(self._account_from_row(registered)) != self._identity(account):
                raise AccountIdentityError("conditional advancement account is unknown or mismatched")
            row = conn.execute("SELECT * FROM ledger_checkpoints WHERE mt5_account_id = ?", (account_id,)).fetchone()
            current = self._checkpoint_from_row(row) if row is not None else None
            if ((current is None) != (expected is None)
                    or (current is not None and expected is not None
                        and current.completed_through != expected.completed_through)):
                raise CheckpointExpectationError("durable checkpoint boundary changed since preparation")
            if current is not None and boundary < current.completed_through:
                raise CheckpointRegressionError("conditional checkpoint target precedes current boundary")
            if expected_snapshot is not None:
                if (expected_snapshot.account.mt5_account_id != account.mt5_account_id
                        or self._identity(expected_snapshot.account) != self._identity(account)
                        or expected_snapshot.requested_range.to_time != boundary):
                    raise LedgerSnapshotChangedError("snapshot is not bound to this account/advancement target")
                snapshot = self._snapshot_from_connection(conn, account, expected_snapshot.requested_range)
                if (snapshot.records != expected_snapshot.records or snapshot.conflicts != expected_snapshot.conflicts):
                    raise LedgerSnapshotChangedError("relevant ledger snapshot changed before advancement")
            if current is not None and boundary == current.completed_through:
                return current
            checkpoint = LedgerCheckpoint(account.mt5_account_id, boundary, datetime.now(timezone.utc))
            if current is None:
                conn.execute("INSERT INTO ledger_checkpoints VALUES (?, ?, ?)",
                             (account_id, boundary.isoformat(timespec="microseconds"),
                              checkpoint.updated_at.isoformat(timespec="microseconds")))
            else:
                conn.execute("UPDATE ledger_checkpoints SET completed_through = ?, updated_at = ? WHERE mt5_account_id = ?",
                             (boundary.isoformat(timespec="microseconds"),
                              checkpoint.updated_at.isoformat(timespec="microseconds"), account_id))
        return checkpoint

    @staticmethod
    def _deal_values(deal: NormalizedDeal) -> tuple:
        return (
            str(deal.id), str(deal.mt5_account_id), str(deal.deal_ticket),
            str(deal.order_ticket), str(deal.position_id), deal.symbol, deal.deal_type,
            deal.deal_entry, str(deal.magic), str(deal.volume), str(deal.price),
            str(deal.commission), str(deal.swap), str(deal.profit), str(deal.broker_fee),
            deal.broker_time_seconds, deal.executed_at_msc, deal.reason,
            str(deal.sl) if deal.sl is not None else None,
            str(deal.tp) if deal.tp is not None else None, deal.comment, deal.external_id,
            deal.raw_payload_json, deal.payload_hash, deal.normalizer_version,
            deal.first_seen_at.astimezone(timezone.utc).isoformat(timespec="microseconds"),
            deal.ingestion_id,
        )

    @staticmethod
    def _validate_deal(account: AccountRef, deal: NormalizedDeal) -> None:
        """Reject inconsistent models before any write or replay classification.

        Version 1's JSON snapshot contains the SDK primitives needed to run the
        authoritative normalizer again. Ingestion metadata is validated as input,
        not compared with the first observation already stored in the database.
        """
        if type(deal.normalizer_version) is not int or deal.normalizer_version != NORMALIZER_VERSION:
            raise ValueError("unsupported deal normalizer_version")
        if not isinstance(deal.raw_payload_json, str):
            raise ValueError("deal raw_payload_json must be a canonical JSON object")
        try:
            raw = json.loads(deal.raw_payload_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("deal raw_payload_json must be valid canonical JSON") from exc
        expected = normalize_deal(account, raw, deal.first_seen_at, deal.ingestion_id)
        for field in fields(NormalizedDeal):
            if field.name in ("first_seen_at", "ingestion_id"):
                # normalize_deal validates these; aware offsets and new run IDs
                # are legitimate, independent of broker payload and Deal identity.
                continue
            actual, derived = getattr(deal, field.name), getattr(expected, field.name)
            # Equality alone accepts False == 0 and float/int == Decimal values.
            if type(actual) is not type(derived):
                raise ValueError(f"deal {field.name} has an invalid normalized type")
            if isinstance(actual, Decimal) and not actual.is_finite():
                raise ValueError(f"deal {field.name} must be finite")
            if actual != derived:
                raise ValueError(f"deal {field.name} is inconsistent with its account/raw payload")

    def ingest(self, account: AccountRef, deal: NormalizedDeal) -> IngestResult:
        if not isinstance(account, AccountRef) or not isinstance(deal, NormalizedDeal):
            raise TypeError("ingest requires AccountRef and NormalizedDeal")
        if deal.mt5_account_id != account.mt5_account_id:
            raise AccountIdentityError("deal account ID differs from supplied account context")
        self._validate_deal(account, deal)
        account_id = str(account.mt5_account_id)
        with self._write() as conn:
            registered = conn.execute(
                "SELECT * FROM ledger_accounts WHERE mt5_account_id = ?", (account_id,)).fetchone()
            if registered is None:
                raise AccountIdentityError("account is not registered in this ledger")
            if self._identity(self._account_from_row(registered)) != self._identity(account):
                raise AccountIdentityError("supplied account context differs from persisted identity")

            placeholders = ", ".join("?" for _ in _DEAL_COLUMNS)
            columns = ", ".join(_DEAL_COLUMNS)
            inserted = conn.execute(
                f"INSERT INTO deals ({columns}) VALUES ({placeholders}) "
                "ON CONFLICT(mt5_account_id, deal_ticket) DO NOTHING",
                self._deal_values(deal)).rowcount
            if inserted:
                return IngestResult(IngestOutcome.INSERTED, deal.id)

            original = conn.execute(
                "SELECT id, payload_hash FROM deals WHERE mt5_account_id = ? AND deal_ticket = ?",
                (account_id, str(deal.deal_ticket))).fetchone()
            if original is None:
                raise RuntimeError("deal conflict did not resolve to an existing row")
            original_id = UUID(original["id"])
            if original["payload_hash"] == deal.payload_hash:
                return IngestResult(IngestOutcome.DUPLICATE, original_id)

            observation_id = uuid5(_OBSERVATION_NAMESPACE, f"{original_id}:{deal.payload_hash}")
            added = conn.execute(
                "INSERT INTO deal_observations "
                "(id, deal_id, payload_hash, raw_payload, observed_at, ingestion_id, normalizer_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(deal_id, payload_hash) DO NOTHING",
                (str(observation_id), str(original_id), deal.payload_hash, deal.raw_payload_json,
                 deal.first_seen_at.astimezone(timezone.utc).isoformat(timespec="microseconds"),
                 deal.ingestion_id, deal.normalizer_version)).rowcount
            return IngestResult(IngestOutcome.CONFLICT, original_id, observation_added=bool(added))

    @staticmethod
    def _deal_from_row(row: sqlite3.Row) -> NormalizedDeal:
        return NormalizedDeal(
            id=UUID(row["id"]), mt5_account_id=UUID(row["mt5_account_id"]),
            deal_ticket=int(row["deal_ticket"]), order_ticket=int(row["order_ticket"]),
            position_id=int(row["position_id"]), symbol=row["symbol"],
            deal_type=row["deal_type"], deal_entry=row["deal_entry"], magic=int(row["magic"]),
            volume=Decimal(row["volume"]), price=Decimal(row["price"]),
            commission=Decimal(row["commission"]), swap=Decimal(row["swap"]),
            profit=Decimal(row["profit"]), broker_fee=Decimal(row["broker_fee"]),
            broker_time_seconds=row["broker_time_seconds"], executed_at_msc=row["executed_at_msc"],
            reason=row["reason"], sl=Decimal(row["sl"]) if row["sl"] is not None else None,
            tp=Decimal(row["tp"]) if row["tp"] is not None else None,
            comment=row["comment"], external_id=row["external_id"],
            raw_payload_json=row["raw_payload"], payload_hash=row["payload_hash"],
            normalizer_version=row["normalizer_version"],
            first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
            ingestion_id=row["ingestion_id"],
        )

    @staticmethod
    def _account_id(mt5_account_id: UUID) -> str:
        if not isinstance(mt5_account_id, UUID):
            raise TypeError("mt5_account_id must be a UUID")
        return str(mt5_account_id)

    @staticmethod
    def _ticket(ticket: int, *, positive: bool = False) -> str:
        if isinstance(ticket, bool) or not isinstance(ticket, int) or ticket < (1 if positive else 0):
            raise ValueError("ticket must be a non-negative integer" if not positive else
                             "deal_ticket must be a positive integer")
        return str(ticket)

    def get(self, mt5_account_id: UUID, deal_ticket: int) -> NormalizedDeal | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM deals WHERE mt5_account_id = ? AND deal_ticket = ?",
                (self._account_id(mt5_account_id), self._ticket(deal_ticket, positive=True))).fetchone()
        return self._deal_from_row(row) if row is not None else None

    def _list(self, account_id: UUID, ticket: int, column: str) -> tuple[NormalizedDeal, ...]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM deals WHERE mt5_account_id = ? AND {column} = ? "
                "ORDER BY executed_at_msc, deal_ticket",
                (self._account_id(account_id), self._ticket(ticket))).fetchall()
        return tuple(self._deal_from_row(row) for row in rows)

    def list_by_order(self, mt5_account_id: UUID, order_ticket: int) -> tuple[NormalizedDeal, ...]:
        return self._list(mt5_account_id, order_ticket, "order_ticket")

    def list_by_position(self, mt5_account_id: UUID, position_id: int) -> tuple[NormalizedDeal, ...]:
        return self._list(mt5_account_id, position_id, "position_id")

    def list_observations(self, mt5_account_id: UUID, deal_ticket: int) -> tuple[DealObservation, ...]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT o.* FROM deal_observations AS o JOIN deals AS d ON d.id = o.deal_id "
                "WHERE d.mt5_account_id = ? AND d.deal_ticket = ? "
                "ORDER BY o.observed_at, o.id",
                (self._account_id(mt5_account_id), self._ticket(deal_ticket, positive=True))).fetchall()
        return tuple(DealObservation(
            id=UUID(row["id"]), deal_id=UUID(row["deal_id"]), payload_hash=row["payload_hash"],
            raw_payload_json=row["raw_payload"], observed_at=datetime.fromisoformat(row["observed_at"]),
            ingestion_id=row["ingestion_id"], normalizer_version=row["normalizer_version"])
            for row in rows)

    def read_snapshot(self, *, account: AccountRef, requested_range: RecoveryRange) -> LedgerSnapshotEvidence:
        """Read account, canonical deals and variants under one deferred snapshot.

        Scan this account's variants as their execution time can differ from the
        original. Reuse the authoritative normalizer to validate stored payloads;
        decoding/corruption errors propagate, including for out-of-view rows.
        The read transaction ends before returning; no future-state lease exists.
        """
        if not isinstance(account, AccountRef) or not isinstance(requested_range, RecoveryRange):
            raise TypeError("read_snapshot requires AccountRef and RecoveryRange")
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("BEGIN")
            try:
                result = self._snapshot_from_connection(conn, account, requested_range)
                conn.commit()
                return result
            finally:
                if conn.in_transaction:
                    conn.rollback()

    def _snapshot_from_connection(self, conn: sqlite3.Connection, account: AccountRef,
                                  requested_range: RecoveryRange) -> LedgerSnapshotEvidence:
        registered = conn.execute("SELECT * FROM ledger_accounts WHERE mt5_account_id = ?",
                                  (str(account.mt5_account_id),)).fetchone()
        if registered is None or self._identity(self._account_from_row(registered)) != self._identity(account):
            raise AccountIdentityError("snapshot account is unknown or differs from persisted identity")
        rows = conn.execute("SELECT * FROM deals WHERE mt5_account_id = ?",
                            (str(account.mt5_account_id),)).fetchall()
        originals = {}
        for row in rows:
            deal = self._deal_from_row(row)
            self._validate_deal(account, deal)
            originals[str(deal.id)] = LedgerRecordFingerprint(
                deal.deal_ticket, deal.payload_hash, deal.executed_at_msc, deal.normalizer_version)
        observations = conn.execute(
            "SELECT o.* FROM deal_observations AS o JOIN deals AS d ON d.id = o.deal_id "
            "WHERE d.mt5_account_id = ? ORDER BY o.deal_id, o.payload_hash",
            (str(account.mt5_account_id),)).fetchall()
        conflicts = []
        for row in observations:
            variant = normalize_deal(account, json.loads(row["raw_payload"]),
                                     datetime.fromisoformat(row["observed_at"]), row["ingestion_id"])
            if (type(row["normalizer_version"]) is not int
                    or row["normalizer_version"] != variant.normalizer_version
                    or row["payload_hash"] != variant.payload_hash
                    or row["deal_id"] != str(variant.id)):
                raise ValueError("stored conflict is inconsistent with its canonical payload")
            alternative = LedgerRecordFingerprint(variant.deal_ticket, row["payload_hash"],
                                                   variant.executed_at_msc, row["normalizer_version"])
            original = originals[row["deal_id"]]
            conflict = LedgerConflictEvidence(original, alternative)
            if (execution_in_range(original.executed_at_msc, requested_range)
                    or execution_in_range(alternative.executed_at_msc, requested_range)):
                conflicts.append(conflict)
        records = tuple(sorted((r for r in originals.values()
                                if execution_in_range(r.executed_at_msc, requested_range)),
                               key=lambda r: (r.executed_at_msc, r.deal_ticket)))
        return LedgerSnapshotEvidence(account, requested_range, records, tuple(conflicts))
