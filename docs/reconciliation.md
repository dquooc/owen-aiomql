# Reconciliation evidence

## A4.5 ledger snapshot

`LedgerSnapshotReader.read_snapshot(account=..., requested_range=...)` returns
frozen/slotted `LedgerSnapshotEvidence`, containing immutable canonical
`LedgerRecordFingerprint` tuples and unresolved `LedgerConflictEvidence` pairs.
Account UUID/broker/server/login must match registration; observed metadata is
not an identity key. No registration, checkpoint read/write, or source call occurs.

SQLite uses one connection, `query_only`, and a deferred `BEGIN`. Account, deals,
and observations are read from the same snapshot, then the read transaction ends.
This provides read consistency, not ownership or a guarantee against later writes.
The account's deals and observations are scanned so timing corrections cannot be
lost by filtering only canonical rows. Persisted payloads are validated with the
existing normalizer; malformed evidence raises rather than becoming an empty view.

Ledger membership is the **closed numeric projection**
`from_epoch_microseconds <= executed_at_msc * 1000 <= to_epoch_microseconds`.
Integer timedelta arithmetic preserves microseconds without rounding or floating
timestamps. Equal endpoints select only a matching integer-millisecond record;
submillisecond equal endpoints select none. Results retain the exact RecoveryRange
and raw execution milliseconds. Canonical records sort by execution milliseconds
then numeric ticket; separate tickets/partial fills remain distinct.

This is a ledger view convention, **not** a claim about source inclusivity, broker
timezone, or completeness. Insertion/observation/checkpoint timestamps never select
range membership. An observation is relevant when either its original or variant
execution time belongs to the view; both fingerprints are exposed. There is no
conflict resolution state, so old observations remain disqualifying evidence.

`tests/offline/test_ledger_snapshot.py` covers account isolation/identity, canonical
fingerprints/order/partial fills/replay, exact UTC/microsecond bounds, empty/equal
endpoints, precision extremes, historical conflicts and moved execution times,
no database mutation, WAL concurrent-writer consistency, decode/storage errors,
restart/separate instances, and immutable runtime-validated evidence.

## A4.6 pure evaluator

`evaluate_reconciliation(attempt=..., ingestion_report=..., source_evidence=...,
ledger_evidence=...)` returns the existing `ReconciliationResult`, retaining the
exact attempt and report objects. It performs no repository/source I/O, clock
read, normalization, hashing, retry, or mutation.

Identity compares UUID, broker namespace, server and login, ignoring observed
company/trade-mode metadata. Invocation ID and every exact range boundary must
agree (canonical timezone-equivalent instants agree; differing microseconds do
not). The evaluator checks nonnegative integer counters, excluding bools, and
`retrieved = normalized + invalid`,
`normalized = inserted + duplicates + conflicts`. Empty success requires all
zero counters; nonempty success requires a positive retrieved count.

Deterministic precedence is:

1. Binding mismatch -> REJECTED/BINDING_MISMATCH.
2. FAILED/PARTIAL status -> REJECTED/INGESTION_INCOMPLETE.
3. Malformed/negative counters -> REJECTED/ACCOUNTING_MISMATCH.
4. Positive invalid count or errors -> REJECTED/INGESTION_INCOMPLETE.
5. Arithmetic/status/source-count inconsistency -> REJECTED/ACCOUNTING_MISMATCH.
6. Current or persisted conflict, or differing source variants of one ticket ->
   REJECTED/CONFLICT_PRESENT.
7. Impossible repeated-ticket insertion accounting -> REJECTED/ACCOUNTING_MISMATCH.
8. Missing fingerprints, unverified completeness, or timestamps outside the
   ledger projection -> INDETERMINATE/EVIDENCE_INSUFFICIENT.
9. Missing/extra/different canonical ledger fingerprints -> REJECTED/LEDGER_MISMATCH.
10. Otherwise APPROVED, with no reason and no advancement.

Identical repeated source entries represent one canonical account/ticket row and
require sufficient duplicate accounting; differing variants cannot be collapsed
into approval. Comparison ignores collection order, comparing all four fingerprint
fields. Extra canonical ledger rows are not ignored. Source timestamps outside
the ledger view are insufficient evidence, rather than invented endpoint rules.

Conservative completeness is mandatory: current CALL_COMPLETED_UNVERIFIED source
evidence remains INDETERMINATE even for matching nonempty or empty snapshots.
VERIFIED_COMPLETE is supported for a future independently stronger contract;
tests supplying it do not establish that the current source provides it.

`tests/offline/test_reconciliation_evaluator.py` covers bindings, counter domains
and arithmetic, processing failures, current/historical conflicts, each fingerprint
field, missing/extra rows, insert/duplicate mixes, repeated tickets, empty and
nonempty assurance, timestamp precision, precedence, deterministic unchanged inputs,
and guards against I/O/normalization/clock/source calls.

## A4.7 RecoveryManager integration

`RecoveryResult` appends `reconciliation_result: ReconciliationResult | None = None`
after its existing six fields. Legacy positional construction still works;
frozen/slots and runtime type validation remain. Preparation is unchanged.

`RecoveryManager` accepts an optional `snapshot_reader: LedgerSnapshotReader`.
By default it uses the same repository as `account_lookup`; that repository must
also implement the reader protocol for `recover()`. A separate reader may be
injected. Missing read capability is rejected before recovery side effects.
The manager knows no SQLite internals. `prepare()` needs no reader or ingestor.

The flow is prepare (identity, checkpoint, one horizon, planning), one
`ingest_range_with_evidence()` invocation, one snapshot for that exact account and
range, then the pure evaluator with the exact attempt/report/source/snapshot.
FAILED/PARTIAL ingestion returns FAILED with no reconciliation I/O and a None
reconciliation result. Successful ingestion includes the evaluator result:

| Reconciliation | Recovery outcome |
| --- | --- |
| REJECTED | FAILED |
| INDETERMINATE | PENDING_RECONCILIATION |
| APPROVED | PENDING_RECONCILIATION |

The last mapping is deliberately transitional: APPROVED is not COMPLETED, and
there is no COMPLETED enum member. Every manager return preserves the exact
`checkpoint_before` as `checkpoint_after`. No advancement or auto-registration
occurs, including on approval. Current guarded source evidence stays unverified;
old relevant conflicts can reject a duplicates-only current ingestion.

Snapshot/evaluator errors and cancellation propagate without fabricated results,
retry or compensation; earlier ingestion commits remain. Snapshot and evaluator
contracts are synchronous; cooperative task cancellation is delivered at awaits,
and CancelledError raised by either synchronous collaborator also propagates.
No background jobs or extra scheduling points are introduced.

Concurrent reconciliation is observational. Its snapshot can become stale after
the read transaction; this does not establish same-account reconcile-and-advance
safety, ownership, or a lease. Conditional checkpoint advancement is not included.

`tests/offline/test_recovery_reconciliation.py` exercises exact call/object flow,
nonempty/empty guarded reads, verified approval without advancement, current/old
conflicts, failed/partial short-circuit, persisted commits on storage failure and
cancellation, snapshot/evaluator exceptions, bootstrap/overlap, account isolation,
default/separate readers, invalid executions and backward-compatible result
construction. Existing A3 fixtures only adopt the evidence API and optional result
field; conflict outcomes change intentionally from pending to rejected/failed.
