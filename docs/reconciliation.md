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
