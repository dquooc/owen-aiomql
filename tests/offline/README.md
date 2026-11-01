# Execution core offline tests

Run only this directory with the standard-library runner:

```powershell
python -B -m unittest discover -s tests/offline -p 'test_*.py' -v
```

`-s tests/offline` limits discovery to the fake-SDK tests. `-B` prevents
bytecode files from being written. These tests do not import pytest or its
fixtures. Do not use unrestricted pytest discovery for this suite:
`tests/live/conftest.py` has autouse fixtures that log in to MT5 and clean up
positions with `order_send`.

## RecoveryManager acceptance scope (A3)

The offline suite covers durable per-account checkpoints, pure range planning,
immutable recovery configuration/results, read-only account preflight, and
`RecoveryManager.prepare()` / `recover()` with an injected single horizon clock.
It verifies exact UTC boundaries, failure/cancellation propagation, retained partial
commits, caller-controlled replay, and no checkpoint creation or advancement.
Successful or empty ingestion returns `PENDING_RECONCILIATION`; failed or partial
ingestion returns `FAILED`. These outcomes do not establish completed recovery.

Reconciliation, recovery checkpoint advancement, same-account reconcile/advance
coordination, automatic startup recovery, and live MT5 recovery validation remain
unimplemented. PostgreSQL, the Local Engine entrypoint and packaging/`.exe`, and
the EA bridge are also outside the implemented scope.

## RecoveryManager concurrency boundary (A3.9)

Same-account concurrency is **CONDITIONALLY SAFE** for the current ingestion-only,
no-checkpoint-advancement stage. Attempts keep their checkpoint/range/report/horizon
in local variables. Overlapping replay relies on account-scoped ledger uniqueness
and idempotent observations; there is no whole-flow serialization guarantee.
Dependencies must stay bound to the intended ledger/account while calls run.

The concurrency suite uses entry/release events to hold two recoveries in flight,
including shared manager/ingestor calls with distinct horizons, failure/cancellation
isolation, and deduplicated conflict observations. A separate thread-barrier case
runs two real managers/repository instances against one temporary SQLite file;
event-loop tests alone would serialize the synchronous persistence phase.
Production manager code creates no tasks, workers, retries, or locks.

Different accounts require account-scoped sources/ingestors and isolated sessions.
A shared ingestor is tested only with a fixed-account fake source that supports
independent concurrent fetches. This does not establish live MT5 terminal safety:
before/after login/server guards cannot detect an A -> B -> A session switch
during a fetch, and broker namespace still depends on correct application binding.

Before future reconciliation and checkpoint advancement are introduced, same-account
attempts need an explicit serialization/coordination design. Overlapping attempts
can otherwise reconcile different horizons against stale checkpoint snapshots;
checkpoint monotonicity alone does not validate that evidence. Possible future
mechanisms include caller serialization, per-account single-flight/leases, attempt
versions, or reconciliation approval tied to the exact range. None is implemented
by A3.9, and no Reconciliation or checkpoint advancement is added here.
