# Reconciliation evidence

The A4.5–A4.7 sections describe their accepted historical stages. A4.8 below
supersedes the transitional no-advancement mapping for approved evidence.

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

## A4.8 conditional advancement and ownership

`ConditionalCheckpointRepository.advance_checkpoint_if_expected(account, target,
expected=..., expected_snapshot=...)` returns only after commit. SQLite compares
the registered account and current checkpoint under `BEGIN IMMEDIATE`: None
requires absence; an existing expectation compares UUID and completed_through.
updated_at is audit metadata, not a boundary version. Staleness raises
`CheckpointExpectationError`; it is not a reconciliation reason. Equal targets
preserve the existing checkpoint/audit time, and backward targets still raise
`CheckpointRegressionError`. The legacy A1 monotonic API is unchanged.

The manager always supplies its approved snapshot. The same write transaction
revalidates canonical records and unresolved conflicts using the same connection,
before modifying the checkpoint. A changed relevant snapshot raises
`LedgerSnapshotChangedError`. No writer can interleave between that validation
and commit. Standalone CAS without a snapshot is a low-level boundary primitive,
not authorization to declare reconciliation complete.

Only APPROVED plus a committed guarded CAS yields COMPLETED. Its target is
exactly `attempt.planned_range.to_time`, and checkpoint_after is the storage
return. All failed/pending paths preserve checkpoint_before. The injected planning
clock remains once per attempt; the storage wall clock generates updated_at audit
metadata only and is never used as a completion boundary. Storage/commit errors
propagate without blind retry or a successful result.

`AccountRecoveryCoordinator` owns the full verify/prepare/ingest/snapshot/evaluate/
advance/result lifecycle by persisted UUID. To avoid duplicate identity reads or
pre-lock checkpoint capture, ownership is acquired using the caller's non-nil
UUID, then registration is verified as the first preparation step under ownership.
An invalid mapping cannot reach checkpoint/clock/source work.

The process default is shared across manager instances, threads and event loops.
A short threading mutex protects registry creation/handoff/removal only; it never
spans application I/O or awaits. FIFO waiters use futures on their own loops and
threadsafe callbacks, without polling/background tasks. Cancelled queued/granted
waiters release only their own ownership; entries disappear when idle. Same-task
reentrancy raises. Separate accounts progress independently. A coordinator can be
injected for an explicit separate coordination domain; durable CAS still protects
independent processes/domains. This is not a distributed lease, and abandoning a
live owner/event loop without cancellation is outside the cooperative contract.

Current production source assurance remains CALL_COMPLETED_UNVERIFIED and thus
pending. Verified evidence used by advancement tests is an offline stronger-source
double, not a new MT5 guarantee. Windows final acceptance is required.

## A4.9 failure, cancellation and crash boundaries

The manager validates the advancement return's type, account and exact planned
end before producing COMPLETED. A broken adapter returning None, another account,
or another boundary cannot create a completed result. SQLite's guarded transaction
checks the expected checkpoint and the relevant snapshot under the same writer
reservation; another writer cannot enter after revalidation and before commit.

Stale-checkpoint and stale-ledger errors propagate without retry or fabricated
reconciliation reasons. A new relevant row/conflict/time correction after approval
invalidates advancement. Valid changes for another account or outside the range
do not invalidate this range's evidence. Historical conflicts remain unresolved;
source A -> B -> A switches and future broker corrections are still not proven
absent by two session guards.

Transaction-body failure/cancellation rolls back uncommitted advancement. Earlier
deal commits remain and a fresh recovery replays them. After advancement commits,
failure/cancellation during result construction does not undo durable progress:
restart reads the NEW boundary and plans normally from it, without reconstructing
the lost result or using an already-completed shortcut.

An SQLite commit exception is not treated as success. The conditional adapter
closes the write connection to release/roll back any still-active transaction,
then reads the checkpoint through a fresh connection exactly once. It raises
`CheckpointCommitError`, preserving the original SQLite error as its cause:

- `observation_error is None`: checkpoint_after is the observed durable checkpoint,
  or None for confirmed absence;
- otherwise the durable observation is unavailable, and None must NOT be read as
  confirmed absence.

The observation does not establish which writer committed. The adapter never
retries a write or fabricates COMPLETED from a lost commit response. Cancellation
is still propagated as CancelledError. Snapshot/evaluator/storage exceptions also
release ownership; cancellation during ownership handoff cannot strand the next
waiter. Tests use deterministic events, not polling sleeps.

`test_recovery_hardening.py` covers the stage failure/cancellation matrix, ownership
handoff races, independent-manager CAS, snapshot mutation races, safe irrelevant
mutations, restart/replay and lost results. `test_checkpoint_crash.py` covers
uncommitted rollback, lost commit responses before/after actual commit, failed
durable rereads, writer exclusion and ownership across thread event loops.

Cloud's filesystem/network sandbox can prevent asyncio's cross-thread wakeup
socket, even in a stdlib-only program. These offline concurrency tests pass when
run outside that sandbox with local sockets enabled. This does not require MT5,
external services, credentials or a change to production code.

## A4.10 final contract audit

The accepted starting commit is `4a2b058`, with user-reported Windows acceptance
of 601/601 offline tests for A4.4–A4.7. New Cloud verification passes 636/636 tests
(35 added tests), including the previously sandbox-blocked thread tests. This is
Linux implementation evidence, not Windows final acceptance.

| Boundary | Current contract and verification |
| --- | --- |
| Planner | Pure, explicit horizon, exact UTC/microseconds; existing planner suites |
| Ingestion | One guarded fetch; report/evidence share invocation/account/range; A4.4 suite |
| Source evidence | Passive immutable models; actual normalized batch fingerprints |
| Snapshot | Read-only consistent account/range transaction; moved-time conflicts observable |
| Evaluator | Pure; binding/accounting/conflicts/fingerprints/assurance checked |
| Ownership | Whole lifecycle by account UUID; shared process default, cancellation/handoff cleanup |
| Advancement | APPROVED only; expected checkpoint and unchanged relevant evidence checked in one writer transaction |
| Target | Exactly attempt.planned_range.to_time; never a deal/observation/current-clock boundary |
| Result | COMPLETED only after committed guarded advancement and validated storage return |
| Failures | No blind retry, fabricated success, compensation or swallowed cancellation |
| Restart | Read durable checkpoint; replay after precommit failure, new planning after committed progress |

Final outcome mapping:

| State | Outcome | Advancement |
| --- | --- | --- |
| FAILED/PARTIAL ingestion | FAILED | None |
| REJECTED reconciliation | FAILED | None |
| INDETERMINATE reconciliation | PENDING_RECONCILIATION | None |
| APPROVED + successful guarded commit | COMPLETED | Exact planned range end |
| Stale checkpoint/snapshot or storage/evaluator error | Exception | No success claimed |

The production adapter/ingestor still supplies CALL_COMPLETED_UNVERIFIED, which
does not prove every broker historical deal was returned, an immutable source
snapshot, absence of A -> B -> A terminal switches, or absence of future broker
corrections. Matching real history can therefore remain PENDING_RECONCILIATION.
No test-supplied VERIFIED_COMPLETE assurance was added to production collection.

Public ledger exports are explicit, unique and contain no SQLite transaction
helpers/test fixtures. New public evidence uses frozen/slotted typed tuples, with
no new Any-based DTO. Source/ledger fingerprint types intentionally distinguish
provenance while sharing scalar validation semantics. Deferred model references
avoid import cycles and can be resolved with the public ledger type namespace;
the package imports with an inert parent without loading MetaTrader5. Audit only
added missing scalar/helper annotations and removed an obsolete test-only barrier.

Static inspection confirms one injected horizon call and one source fetch per
attempt; the new wall-clock read is checkpoint updated_at audit metadata only.
The evaluator/planner do not read clocks. Existing ingestion catches Exception
to report stage errors, while CancelledError escapes; new BaseException handlers
only release ownership/roll back and re-raise. The coordinator's queue loop is
handoff, not an operation retry. There are no new direct MT5/trading calls,
background tasks, automatic registration or hidden source/checkpoint retries.

Remaining boundaries: no distributed ownership lease, no protection against future
broker corrections after completion, and no claim of live MT5 multi-session safety.
The SQLite implementation scans account history to validate moved-time variants;
large-history performance is not benchmarked. Full Windows offline acceptance is
required before declaring final A4 acceptance. No later roadmap work is included.
