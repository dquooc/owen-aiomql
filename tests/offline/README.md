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

## Invocation-bound source evidence (A4.4)

`DealIngestor.ingest_range_with_evidence()` accepts the same arguments as
`ingest_range()` and returns a frozen `IngestionExecution` with `report` and
`source_evidence`. The report-only API and RecoveryManager behavior remain
unchanged. Evidence uses the same ingestion ID, supplied AccountRef, and exact
UTC RecoveryRange (including microseconds) as that invocation's single fetch.

Evidence is absent on account-before, fetch, or account-after failure. After both
guards pass, even an empty batch has `CALL_COMPLETED_UNVERIFIED` evidence. No
current source path establishes `VERIFIED_COMPLETE`. Fingerprints come from the
actual normalized records passed to storage, in source order, including repeated
tickets. They reuse canonical payload hashes and normalizer versions.

Normalization failures leave gaps. A storage failure stops processing, retaining
fingerprints through the failing write and any earlier committed rows; later
records are unprocessed. `retrieved` still counts the entire fetched batch, so
fingerprint count can be smaller. Cancellation propagates without a result or a
retry and may leave earlier commits. Evidence does not approve reconciliation or
advance a checkpoint.

Run the focused tests without loading the SDK:

```sh
python -B -m unittest discover -s tests/offline -p 'test_ingestion_evidence_binding.py' -v
```

### A4.4 acceptance coverage

All test names below belong to `IngestionEvidenceTests` in
`test_ingestion_evidence_binding.py`, except the explicitly qualified existing
RecoveryManager tests. Adapter coverage uses an inert local client, not the SDK.

| Criterion | Acceptance requirement | Tests |
| --- | --- | --- |
| 1 | Evidence from the actual fetched batch; fetch exactly once | `test_actual_batch_once_canonical_fingerprints_order_and_duplicate_tickets`, `test_fingerprints_reuse_normalized_objects_passed_to_storage` |
| 2 | Report and evidence share ingestion ID | `test_actual_batch_once_canonical_fingerprints_order_and_duplicate_tickets`, `test_guarded_empty_fetch_is_unverified_with_generated_id` |
| 3 | Exact ingestion ID, AccountRef, RecoveryRange binding | `test_actual_batch_once_canonical_fingerprints_order_and_duplicate_tickets`, `test_repeated_invocations_do_not_share_evidence`, `test_execution_is_public_frozen_slotted_and_validates_bindings` |
| 4 | UTC and microseconds preserved | `test_utc_microseconds_and_equal_range` |
| 5 | Existing canonical normalization/hash/version semantics | `test_actual_batch_once_canonical_fingerprints_order_and_duplicate_tickets`, `test_fingerprints_reuse_normalized_objects_passed_to_storage` |
| 6 | Source ordering preserved | `test_actual_batch_once_canonical_fingerprints_order_and_duplicate_tickets` |
| 7 | Duplicate source tickets preserved | `test_actual_batch_once_canonical_fingerprints_order_and_duplicate_tickets` |
| 8 | Guarded empty fetch has retrieved=0, records=(), unverified assurance | `test_guarded_empty_fetch_is_unverified_with_generated_id` |
| 9 | Current source/adapter path never claims VERIFIED_COMPLETE | `test_source_adapter_with_inert_client_never_claims_verified_complete`, `test_guarded_empty_fetch_is_unverified_with_generated_id` |
| 10 | Normalization failures retain truthful retrieved count with incomplete fingerprints | `test_normalization_failures_keep_retrieved_and_valid_fingerprints` |
| 11 | Failed before/fetch/after guards produce no successful source evidence | `test_guard_and_fetch_failures_never_produce_evidence_or_retry` |
| 12 | Storage failures retain prior commits and evidence for the fetched batch | `test_storage_failure_keeps_commits_and_source_fingerprints_through_failure` |
| 13 | Cancellation propagates | `test_cancellation_propagates_at_each_stage_without_retry`, `test_storage_cancellation_preserves_prior_commit` |
| 14 | No hidden retry | `test_guard_and_fetch_failures_never_produce_evidence_or_retry`, `test_cancellation_propagates_at_each_stage_without_retry`, `test_storage_failure_keeps_commits_and_source_fingerprints_through_failure`, `test_fingerprints_reuse_normalized_objects_passed_to_storage` |
| 15 | RecoveryManager status mapping unchanged | `test_ledger_recovery_manager_ingestion.RecoveryManagerIngestionTests.test_success_reuses_prepare_and_preserves_exact_attempt_report_and_snapshot`, `.test_empty_success_remains_pending_with_no_checkpoint`, `.test_failed_report_preserves_errors_without_retry`, `.test_partial_report_maps_to_failed_without_altering_counters`; real-ingestor cases in `RecoveryManagerIngestionIntegrationTests` in the same module |
| 16 | Small backward-compatible API; existing report-only callers work | `test_legacy_api_still_returns_identical_report_and_one_fetch`, `test_legacy_api_forwards_arguments_and_returns_exact_execution_report`; existing `test_deal_ingestion.DealIngestionTests` |
| 17 | Narrow, frozen/slotted execution wrapper with runtime validation | `test_execution_is_public_frozen_slotted_and_validates_bindings` |

Run all tests referenced above, including the real-ingestor RecoveryManager cases:

```sh
python -B -m unittest discover -s tests/offline -p 'test_ingestion_evidence_binding.py' -v
python -B -m unittest discover -s tests/offline -p 'test_deal_ingestion.py' -v
python -B -m unittest discover -s tests/offline -p 'test_ledger_recovery_manager_ingestion.py' -v
```

The mapping covers cloud validation. Full Windows acceptance remains required;
the earlier full Linux offline run hung, and a worker-thread recovery test timed
out both with A4.4 and with the original ingestion implementation at `ea5818d`.

## RecoveryManager acceptance scope (A3)

This section records the original A3 scope. A4.7 adds observational reconciliation
without advancement; see [the current contracts](../../docs/reconciliation.md).

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

A4.7 now evaluates a consistent read snapshot after successful ingestion. This
remains observational: no snapshot ownership, reconcile-and-advance guarantee,
checkpoint advancement, or live MT5 safety is established.

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
