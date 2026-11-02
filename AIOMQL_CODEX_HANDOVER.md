# AIOMQL / MT5 ENGINE — CODEX PROJECT HANDOVER

> **Purpose:** Transfer the complete working context of the AIOMQL/MT5 Engine project into Codex so work can continue without reconstructing the roadmap from chat history.
>
> **Updated:** 2026-11-03
>
> **Local Windows repository:** `E:\BLC\Dev_AI_MT5`
>
> **GitHub repository:** `https://github.com/dquooc/owen-aiomql.git`
>
> **Primary branch:** `main`
>
> **IMPORTANT:** This document records accepted design decisions, test results reported during the project, and current roadmap state. Codex must still inspect the actual current checkout before modifying code. If the repository contradicts this handover, STOP and report the discrepancy instead of inventing replacement contracts.

---

# 1. PROJECT GOAL

Build a safe local Windows AIOMQL engine around the existing `aiomql` Python project and MetaTrader 5 Python API.

Current target architecture:

```text
Windows local
├── MetaTrader 5 terminal
└── AIOMQL_Engine.exe
     ├── Engine Entrypoint
     ├── Config
     ├── Logging
     ├── Startup Safety
     ├── Dry-run / Broker Write Barrier
     ├── Deal Ledger
     ├── Durable Checkpoint
     ├── Recovery
     ├── Reconciliation
     ├── Runtime Wiring
     └── SQLite initially
```

Runtime relationship:

```text
AIOMQL_Engine.exe
    ↓
MetaTrader5 Python API
    ↓
MetaTrader 5 terminal
    ↓
Broker
```

The current engine is intentionally conservative. Live trading/order submission is NOT part of the accepted release path yet.

---

# 2. ENVIRONMENT

## Windows local

Repository:

```text
E:\BLC\Dev_AI_MT5
```

Observed local environment:

```text
Python: 3.13.18
MetaTrader5 package: 5.0.5640
aiomql package: editable install from src/
```

MetaTrader5 import was verified inside:

```text
E:\BLC\Dev_AI_MT5\.venv
```

The package `aiomql` does not expose `aiomql.__version__`; use package metadata if version is needed.

## Cloud

Codex Cloud runs Linux.

Critical limitation:

```text
MetaTrader5 Python package is Windows-specific.
```

Therefore Cloud is suitable for:

- source implementation;
- offline tests;
- architecture work;
- static review;
- commits on a work branch.

Cloud must NOT be treated as final Windows acceptance for MT5/runtime/build tasks.

---

# 3. HARD SAFETY RULES

These rules override convenience.

## NEVER perform automatically

```text
order_send
market order
pending order
Buy
Sell
modify order
cancel order
close position
modify position
broker write
live trading activation
```

Do not send a “tiny test order”.

Do not send a demo order merely to verify trading.

## Credentials

- `.env` must remain ignored/untracked.
- Never print passwords, investor passwords, tokens, or full `.env`.
- Do not embed credentials in source, tests, build artifacts, prompts, or documentation.
- A local MetaQuotes demo account is configured on the user's Windows machine.
- Do not repeat its password.

## MT5 assurance

Current MT5 historical source assurance is intentionally:

```text
CALL_COMPLETED_UNVERIFIED
```

It means the guarded source call completed.

It does NOT prove:

- broker returned every historical deal;
- immutable broker-history snapshot;
- no A → B → A terminal account switch;
- no future broker correction.

Never upgrade this to `VERIFIED_COMPLETE` without a real stronger contract.

---

# 4. WORKING STYLE / CODEX RULES

Before every implementation task:

```text
inspect actual code
→ inspect existing tests/contracts
→ RED
→ minimal implementation
→ GREEN
→ relevant regression
→ source/diff audit
→ commit
```

If dedicated TDD/verification skills are unavailable, follow the workflow manually.

Do not claim unavailable skills.

Use separate commits for separate roadmap phases.

Do not force-push or rewrite accepted history.

Stop when a task requires inventing a missing contract.

---

# 5. DEAL LEDGER — ACCEPTED FOUNDATION

`src/aiomql/ledger/` contains the recovery/reconciliation subsystem.

Core Deal Ledger behavior:

- `DealRepository` protocol.
- `SQLiteDealRepository`.
- SQLite tables include:
  - `ledger_accounts`
  - `deals`
  - `deal_observations`
  - `ledger_checkpoints`
- Stable UUID `mt5_account_id` maps:
  - `broker_namespace`
  - `server`
  - `login`
- Deal uniqueness:
  ```text
  (mt5_account_id, deal_ticket)
  ```

Ingestion semantics:

```text
new ticket
→ INSERTED

same ticket + same canonical hash
→ DUPLICATE

same ticket + different canonical hash
→ CONFLICT
→ preserve original
→ record observation
```

Account guards are performed around history fetch.

Partial fills are independent deals by deal ticket.

Per-record commit behavior is intentional: earlier valid records may remain committed if a later storage operation fails.

---

# 6. ROADMAP — CURRENT STATUS

Current normalized roadmap:

```text
A1  Durable Checkpoint                         ACCEPTED
A2  Recovery Planner                          ACCEPTED
A3  Recovery Manager                          ACCEPTED
A4  Reconciliation + Conditional Advancement  ACCEPTED

A5  Engine Entrypoint                         ACCEPTED
A6  Config                                    ACCEPTED
A7  Logging                                   ACCEPTED
A8  Startup Safety                            ACCEPTED
A9  Dry-run / Broker Write Barrier            ACCEPTED

A10 MT5 Read-only Smoke                       ACCEPTED (LIVE READ-ONLY ONLY)
A11 Runtime Wiring                            ACCEPTED
A12 Packaging Preparation                     ACCEPTED
A13 Build AIOMQL_Engine.exe                   COMPLETE ON WINDOWS
A14 Local Release Readiness                   OFFLINE/LOCAL CHECKS COMPLETED
A15 Final Local Acceptance                    OFFLINE RELEASE + BOUNDED
                                              MT5 LIVE READ-ONLY ACCEPTED
```

A10.1 and A10.2 have passed on Windows. The accepted release remains scoped to
offline/local execution and explicit bounded live read-only tests; it does not
activate an MT5 runtime service, automatic recovery, or broker writes.

---

# 7. A1 — DURABLE CHECKPOINT — ACCEPTED

Public checkpoint model:

```text
LedgerCheckpoint
- mt5_account_id: UUID
- completed_through: datetime
- updated_at: datetime
```

Properties:

- frozen/slotted;
- aware UTC;
- microsecond preservation.

Repository contract:

```text
get_checkpoint(mt5_account_id)
advance_checkpoint(account, completed_through)
```

Behavior:

- first checkpoint create;
- forward advance;
- same target idempotent;
- regression raises `CheckpointRegressionError`;
- SQLite `BEGIN IMMEDIATE`;
- multi-instance monotonicity;
- restart durability.

Known historical commits:

```text
19fbb7e feat: add durable ledger checkpoint persistence
e162803 feat: enforce monotonic ledger checkpoints
174af88 test: harden ledger checkpoint timezone handling
811782c test: verify ledger checkpoint restart durability
```

---

# 8. A2 — RECOVERY PLANNER — ACCEPTED

`RecoveryRange`:

```text
from_time
to_time
```

Frozen/slotted, UTC, microseconds, equal endpoints valid.

Planner:

```python
plan_recovery_range(
    *,
    checkpoint,
    current_time,
    overlap,
    bootstrap_start=None,
)
```

Checkpoint branch:

```text
checkpoint.completed_through - overlap
→ current_time
```

Bootstrap branch:

```text
bootstrap_start
→ current_time
```

Properties:

- pure;
- no I/O;
- no MT5;
- no clock ownership;
- no checkpoint mutation.

---

# 9. A3 — RECOVERY MANAGER — ACCEPTED

Key types:

```text
RecoveryAttempt
RecoveryResult
RecoveryOutcome
RecoveryConfig
```

Original manager semantics:

```text
SUCCESS       → PENDING_RECONCILIATION
SUCCESS_EMPTY → PENDING_RECONCILIATION
FAILED        → FAILED
PARTIAL       → FAILED
```

Manager responsibilities:

```text
verify account identity
→ read checkpoint
→ capture current_time exactly once
→ plan recovery range
→ ingest
→ return explicit result
```

A3 specifically forbade checkpoint advancement before reconciliation.

Cancellation propagates.

No hidden retry.

No auto-registration.

---

# 10. A4 — RECONCILIATION — ACCEPTED

A4 was completed in multiple phases.

## A4.2 — Result models

`ReconciliationStatus`:

```text
APPROVED
REJECTED
INDETERMINATE
```

`ReconciliationReason`:

```text
BINDING_MISMATCH
INGESTION_INCOMPLETE
ACCOUNTING_MISMATCH
CONFLICT_PRESENT
LEDGER_MISMATCH
EVIDENCE_INSUFFICIENT
```

`STALE_CHECKPOINT` is intentionally NOT a reconciliation reason.

`ReconciliationResult`:

```text
attempt
ingestion_report
status
reason
```

Invariant:

```text
APPROVED → reason is None
REJECTED / INDETERMINATE → reason required
```

Known commit:

```text
58ebb23 feat: add reconciliation result models
```

## A4.3 — Source evidence

Commit:

```text
ea5818d update code
```

Contracts:

```text
SourceHistoryAssurance
SourceRecordFingerprint
SourceBatchEvidence
```

Assurance enum includes:

```text
CALL_COMPLETED_UNVERIFIED
VERIFIED_COMPLETE
```

Fingerprint fields:

```text
deal_ticket
payload_hash
executed_at_msc
normalizer_version
```

Source batch evidence:

```text
ingestion_id
account
requested_range
assurance
retrieved
records
```

Important:

- canonical hash is reused;
- no second hashing algorithm;
- duplicate source tickets preserved;
- evidence may contain fewer fingerprints than `retrieved` if normalization fails;
- successful empty fetch is representable.

## A4.4 — Actual-batch evidence binding

Commit:

```text
a0a8bd6 feat: bind reconciliation evidence to ingestion batch
```

Added:

```text
ingest_range_with_evidence()
IngestionExecution(report, source_evidence)
```

Critical invariant:

```text
source fetch EXACTLY ONCE
→ same fetched batch
→ ingestion report + source evidence
```

No second source fetch is allowed for reconciliation.

## A4.5 — Ledger snapshot evidence

Commit:

```text
05e25ef feat: add reconciliation ledger snapshot evidence
```

Added concepts:

```text
LedgerRecordFingerprint
LedgerConflictEvidence
LedgerSnapshotEvidence
LedgerSnapshotReader
SQLite read_snapshot()
```

Deals + conflict observations are read in a consistent SQLite read transaction.

Range membership uses stored source execution time, not insertion/observation time.

## A4.6 — Pure evaluator

Commit:

```text
ff891ae feat: add pure reconciliation evaluator
```

Function:

```text
evaluate_reconciliation(...)
```

No I/O.

No source fetch.

No repository read.

No clock.

No checkpoint mutation.

Current conservative policy:

```text
CALL_COMPLETED_UNVERIFIED
→ INDETERMINATE / EVIDENCE_INSUFFICIENT
```

even if source batch and ledger match.

This is intentional.

## A4.7 — Manager integration

Commit:

```text
4a2b058 feat: integrate reconciliation into recovery manager
```

Flow:

```text
prepare
→ ingest exact batch + evidence
→ ledger snapshot
→ pure reconciliation
→ RecoveryResult
```

`RecoveryResult` evolved to include:

```text
reconciliation_result: ReconciliationResult | None = None
```

At A4.7 checkpoint still did not advance.

## A4.8 — Conditional checkpoint advancement

Commit:

```text
62b9478 feat: add conditional recovery checkpoint advancement
```

Added:

```text
advance_checkpoint_if_expected()
ConditionalCheckpointRepository
CheckpointExpectationError
LedgerSnapshotChangedError
AccountRecoveryCoordinator
```

Required atomic behavior:

```text
BEGIN IMMEDIATE
→ compare durable expected checkpoint
→ revalidate relevant ledger snapshot
→ write checkpoint
→ commit
```

`expected=None` means checkpoint must not yet exist.

Checkpoint expectation compares durable recovery boundary semantics, not `updated_at` audit metadata.

Checkpoint target:

```text
attempt.planned_range.to_time
```

EXACTLY.

Never latest deal, ledger max, observation time, or a new clock.

Same-account in-process recovery is coordinated.

Different accounts remain independent.

## A4.9 — Hardening

Commit:

```text
fedd93d fix: harden recovery checkpoint commit safety
```

Covered:

- stage failures;
- cancellation;
- same-account concurrency;
- cross-manager CAS race;
- crash boundaries;
- restart durability;
- snapshot→advance race;
- commit uncertainty handling.

No blind checkpoint retry.

## A4.10 — Final reconciliation docs/audit

Commit:

```text
aafb40d docs: finalize reconciliation acceptance
```

Cloud final A4 suite reported:

```text
636/636 PASS
```

Windows acceptance later passed the full local suite.

A4 is considered ACCEPTED.

---

# 11. WINDOWS ACCEPTANCE AFTER A4

Windows local full regression reported:

```text
Ran 601 tests in 8.482s
OK
```

This accepted A4.4–A4.10 on the actual Windows environment.

---

# 12. A5 — ENGINE ENTRYPOINT — ACCEPTED

Commit:

```text
0e8efe1 feat: add engine entrypoint
```

Created package:

```text
aiomql_engine
```

API includes:

```text
main() -> int
run_engine(...) -> int
python -m aiomql_engine
```

Properties:

- no import side effects;
- no MT5 initialization on import;
- no DB/recovery startup on import;
- no logging setup on import;
- no nested event-loop ownership.

---

# 13. A6 — ENGINE CONFIG — ACCEPTED

Commit:

```text
4713bd9 feat: add engine configuration
```

`EngineConfig` is frozen/slotted.

Defaults are conservative:

```text
offline
dry_run = true
read_only = true
MT5 disabled
recovery disabled
order_submission disabled
live_trading disabled
```

Environment loader only reads explicit:

```text
AIOMQL_ENGINE_*
```

variables.

No credential fields are stored in this engine config.

No `.env` loading is required for offline bootstrap.

---

# 14. A7 — LOGGING — ACCEPTED

Commit:

```text
3def983 feat: add engine logging
```

API:

```text
configure_logging(...)
```

Behavior:

- dedicated `aiomql_engine` handler;
- deterministic format:
  ```text
  LEVEL logger.name message
  ```
- repeated setup does not duplicate handlers;
- no package-import logging configuration;
- startup logs only safe capability information;
- no config/env/credential dump.

Cloud reported:

```text
660/660 PASS
```

for A5–A7 final offline validation.

---

# 15. A8 — STARTUP SAFETY — ACCEPTED

Commit:

```text
4233645 feat: add engine startup safety gate
```

Added:

```text
StartupSafetyDecision
StartupSafetyError
evaluate_startup_safety()
```

Pure/deterministic evaluator.

Default state:

```text
offline
dry-run
read-only
MT5 connection forbidden
recovery forbidden
order submission forbidden
live trading forbidden
```

Contradictory/unsafe configuration fails closed.

---

# 16. A9 — DRY-RUN / BROKER WRITE BARRIER — ACCEPTED

Commit:

```text
55f23e6 feat: enforce engine dry-run broker write barrier
```

Added:

```text
BrokerWritePolicy
BrokerWriteBarrier
EngineRuntime
build_runtime()
BrokerWriteDeniedError
```

Critical invariant:

```text
dry-run
→ broker mutation denied BEFORE adapter invocation
```

Denied operations include order/position mutation paths exposed by the engine seam.

No fake success.

No fake order ticket.

Underlying writer call count on denial:

```text
0
```

Cloud final A8–A9 suite:

```text
676/676 PASS
```

---

# 17. A10 — MT5 READ-ONLY SMOKE — ACCEPTED

A10 was intentionally split because Cloud cannot run MetaTrader5. Windows local
acceptance is now complete for live read-only access only. The evidence below
records the completed run; it is not an instruction to reconnect or rerun live.

## A10.1 — LIVE CONNECTION / BOUNDED HISTORY — PASS

This was manually run on Windows local.

Environment:

```text
Python 3.13.18
MetaTrader5 5.0.5640
```

Verified:

```text
mt5.initialize() = True
last_error = Success
account_info() = PASS
terminal_info() = PASS
terminal connected = True
```

A bounded 7-day history query was executed:

```text
history_deals_get(start, end)
```

Result:

```text
history_result_is_none = False
deal_count = 1
last_error = Success
```

Existing opt-in live test was run:

```powershell
$env:AIOMQL_RUN_LIVE_MT5_TESTS="1"
python -B -m unittest discover -s tests/live -p "test_mt5_readonly_smoke.py" -v
```

Result:

```text
Ran 1 test
OK
```

The live test reported:

```text
initialize PASS
authentication PASS
account_info PASS
terminal connected true
bounded history PASS
deal_count 1
```

Then the opt-in variable was removed.

Native `order_send` was NOT executed.

Broker writes:

```text
0
```

## A10.2 — RecoveryManager LIVE READ-ONLY SMOKE — ACCEPTED

Accepted on 2026-11-03 (Asia/Saigon), using Windows native and the checkout:

```text
E:\BLC\Dev_AI_MT5
Interpreter: E:\BLC\Dev_AI_MT5\.venv\Scripts\python.exe
Branch: main
Base HEAD: b70967bd39952bdb303c503275b92f19bc15ce56
```

The existing opt-in unittest entrypoint was extended and executed. The combined
run revalidated A10.1 and exercised A10.2: **2/2 live tests PASS**, no skips,
exit code **0**. No unrestricted pytest or other live suite was run. Opt-in was
scoped to the child test process. No account identity or credential values are
recorded in this acceptance summary.

Verified end-to-end path:

```text
live MT5 read-only
→ existing MetaTraderDealHistorySource
→ existing DealIngestor
→ TEMP SQLite
→ RecoveryManager
→ ledger snapshot
→ reconciliation
```

Observed A10.2 evidence:

```text
UTC window: 2026-10-26T19:22:07.199759+00:00
         -> 2026-11-02T19:22:07.199759+00:00
source fetch count = 1 (no evidence refetch)
account-before guard = PASS
account-after guard = PASS
retrieved deals = 1
inserted deals = 1
ingestion status = SUCCESS
ledger snapshot reads = 1
recovery horizon clock calls = 1
```

The aggregate live run made two bounded history reads: one for A10.1 and one for
the A10.2 recovery invocation. The actual-batch evidence retained its existing
conservative assurance:

```text
SourceHistoryAssurance.CALL_COMPLETED_UNVERIFIED

→ ReconciliationStatus.INDETERMINATE
→ ReconciliationReason.EVIDENCE_INSUFFICIENT
→ RecoveryOutcome.PENDING_RECONCILIATION
→ checkpoint unchanged
```

Observed safety and cleanup:

```text
checkpoint_before = None
checkpoint_after = None
checkpoint advancement calls = 0
native order_send calls = 0
broker writes = 0
order mutations = 0
position mutations = 0
temporary SQLite only; explicit test AccountRef registration
temporary SQLite cleanup = PASS
connection shutdown = PASS
persistent database modified = NO
```

The account reference used actual verified demo account data and was registered
only in the temporary ledger. Existing SQLite files were hash-checked and did
not change during the live run. A process-local native-writer trap and the
read-only facade blocked mutation before native execution. Conditional and
legacy checkpoint advancement were instrumented to fail before mutation.

The separate engine probe used the existing composition/startup gate with:

```text
mode = read_only
mt5_enabled = true
dry_run = true
read_only = true
recovery_enabled = false
order_submission = false
live_trading = false
runtime exit = 0
denied mutation probes = 12
underlying LOCAL FAKE writer calls = 0
broker results = [] (no fake success or ticket)
automatic startup recovery = rejected by the existing gate
```

No native MT5 writer was supplied to the fake-writer probe. The explicit
RecoveryManager test did not enable startup recovery or change runtime code.

Offline verification with the same project interpreter:

```text
preceding baseline: 693/693 PASS, exit 0
focused read-only smoke contracts: 19/19 PASS, exit 0
pre-live full offline: 702/702 PASS, 11.825s, exit 0
post-live full offline: 702/702 PASS, 10.609s, exit 0
```

The nine added offline tests cover empty/nonempty recovery, guard/fetch failures,
sanitized SDK failures, account-switch prevention, checkpoint-write rejection,
native-write trapping, read-only facade denial, and the local engine probe.
HEAD and source digest were unchanged across the pre/live/post verification.
The SQLite files generated by the live test were cleaned up. The 75 additional
temporary SQLite fixtures created by the post-live offline run were also removed
after exact path/hash/ignored/untracked checks; existing database files were kept.

Reviewed acceptance files:

```text
tests/live/test_mt5_readonly_smoke.py
tests/offline/test_mt5_readonly_smoke_contract.py
```

`git diff --check` passed (exit 0); Git emitted LF-to-CRLF notices for the test
files. Credentials were not recorded, and `.env` was verified ignored/untracked
during the original acceptance task. The later documentation/commit review did
not read `.env` or rerun live tests.

Do NOT weaken reconciliation to force APPROVED or construct `VERIFIED_COMPLETE`
for the current real MT5 source. A10 acceptance does not establish broker-history
completeness, automatic recovery readiness, or permission to trade.

---

# 18. A11 — RUNTIME WIRING — ACCEPTED

Commit:

```text
aac9eec feat: wire engine runtime composition
```

Composition root:

```text
compose_runtime(config=None) -> EngineRuntime
```

Flow:

```text
config
→ logging
→ startup safety
→ runtime construction
```

`run_engine()` preserves runtime status and calls shutdown in `finally`.

No active MT5 service.

No background task.

No recovery auto-run.

No hidden singleton/service locator.

---

# 19. A12 — PACKAGING PREPARATION — ACCEPTED

Commit:

```text
b70967b build: prepare Windows engine packaging
```

Files:

```text
tools/windows/build_engine.py
tools/windows/engine_entry.py
tools/windows/requirements-build.txt
tools/windows/README.md
```

Packaging strategy:

```text
PyInstaller 6.16.0
console bundle
onedir
```

Entrypoint:

```text
engine_entry.py
→ aiomql_engine:main
```

Prepared build commands:

```powershell
python -m pip install -r tools/windows/requirements-build.txt
python tools/windows/build_engine.py
```

Expected output:

```text
dist\AIOMQL_Engine\AIOMQL_Engine.exe
```

Cloud final A11–A12 suite reported:

```text
693/693 PASS
```

---

# 20. A13 — WINDOWS BUILD — COMPLETE

A13 was performed manually on Windows.

PyInstaller completed successfully.

Observed output included:

```text
Build complete! The results are available in:
E:\BLC\Dev_AI_MT5\dist
```

Executable exists:

```text
E:\BLC\Dev_AI_MT5\dist\AIOMQL_Engine\AIOMQL_Engine.exe
```

`Test-Path` returned:

```text
True
```

This was a real Windows build, not a Cloud claim.

Build output is generated artifact state and is not necessarily committed to Git.

## Recorded artifact / provenance (2026-11-03)

The supplied release record was checked against the existing file using read-only
metadata/hash inspection; all values match. LastWriteTime is Windows local time
in Asia/Saigon (UTC+07:00).

| Field | Recorded value |
| --- | --- |
| Artifact | `dist\AIOMQL_Engine\AIOMQL_Engine.exe` |
| Size | `1767518 bytes` |
| LastWriteTime | `2026-11-03 01:01:04` |
| SHA256 | `AFA67FEF44AECDB1AB983D61CCA3C4E885FC741B046DD3D7E663F38426CFFD9C` |
| Build commit | **build commit chưa xác minh** |
| Distribution | Entire `dist\AIOMQL_Engine\` onedir bundle |

The existing handover/release docs, Git history/notes, and generated PyInstaller
spec/TOC/warning/xref records contain no verified binary-to-source-commit binding.
`b70967b` is packaging preparation, not proof of the commit used for this binary.
Do not assign the current checkout or A10 acceptance commit as its build commit.
The checksum above identifies only the EXE, not every file in the onedir bundle.

Distribute **all of `dist\AIOMQL_Engine\`**, preserving the EXE, `_internal\`,
dependencies and relative layout. Do not distribute the EXE alone. See
[the Windows release record](tools/windows/README.md#local-release-record-2026-11-03).
The artifact remains the offline bootstrap with trading/order submission
disabled; source-level A10 acceptance does not establish packaged MT5 runtime
integration. This record update did not rebuild or execute the artifact, run
live tests, or change runtime code.

---

# 21. A14 / A15 — LOCAL OFFLINE RELEASE STATE

The local Windows checkout reported:

```text
Ran 693 tests in 10.821s
OK
```

and:

```text
git status --short
→ clean

git diff --check
→ PASS
```

The EXE exists locally.

At that historical point, the project was treated as:

```text
AIOMQL LOCAL OFFLINE RELEASE ACCEPTED
MT5 READ-ONLY LIVE ACCEPTANCE PENDING
```

The A10 Windows evidence in section 17 now closes the bounded live read-only
acceptance item. Current release scope is:

```text
AIOMQL LOCAL OFFLINE RELEASE ACCEPTED
MT5 BOUNDED LIVE READ-ONLY ACCEPTANCE ACCEPTED
```

A10 acceptance evidence is **2/2 live PASS**, **702/702 pre-live offline PASS**
and **702/702 post-live offline PASS**, all exit 0. Source assurance remains
`CALL_COMPLETED_UNVERIFIED`; recovery is `PENDING_RECONCILIATION`, checkpoint
unchanged, and broker writes are **0**. Trading/order submission remain disabled.
The existing binary's release record and unverified build commit are in section
20; distribute its entire onedir bundle rather than only the EXE.

Do NOT claim:

```text
full MT5 runtime accepted
production trading ready
live trading ready
```

The accepted A10 read-only smoke does not establish any of those broader claims.
Later runtime/trading phases require an explicit new task and acceptance scope.

---

# 22. IMPORTANT GIT HISTORY

Known recent accepted lineage:

```text
b70967b build: prepare Windows engine packaging
aac9eec feat: wire engine runtime composition
55f23e6 feat: enforce engine dry-run broker write barrier
4233645 feat: add engine startup safety gate
3def983 feat: add engine logging
4713bd9 feat: add engine configuration
0e8efe1 feat: add engine entrypoint
aafb40d docs: finalize reconciliation acceptance
fedd93d fix: harden recovery checkpoint commit safety
62b9478 feat: add conditional recovery checkpoint advancement
4a2b058 feat: integrate reconciliation into recovery manager
ff891ae feat: add pure reconciliation evaluator
05e25ef feat: add reconciliation ledger snapshot evidence
a0a8bd6 feat: bind reconciliation evidence to ingestion batch
ea5818d update code
58ebb23 feat: add reconciliation result models
7e2120e test: add MT5 demo read-only smoke coverage
aef6278 docs: finalize recovery manager acceptance
```

At one verified Windows point:

```text
main HEAD = b70967bd39952bdb303c503275b92f19bc15ce56
working tree clean
origin/main up to date
```

Codex must verify current HEAD before work.

---

# 23. CLOUD VS WINDOWS WORKFLOW

## Use Codex Cloud for

- architecture;
- offline implementation;
- pure logic;
- SQLite tests;
- static review;
- long autonomous A→B→C workflows;
- commits on `work`.

## Use Windows local for

- MetaTrader5 package;
- MT5 terminal connection;
- `tests/live`;
- A10 live read-only;
- executable build;
- packaged EXE smoke;
- any future terminal-bound validation.

Never let Cloud claim live MT5 acceptance.

---

# 24. CURRENT ACCEPTANCE / WORKTREE STATE

## A10 closed — no new phase started

The reviewed A10 result is **ACCEPTED in the bounded live read-only scope**.
Both live tests passed and the pre/post offline suites each passed 702/702.
See section 17 for actual assurance, reconciliation, checkpoint, and zero-mutation
evidence. No runtime source changes were needed.

The acceptance commit is limited to the two reviewed A10 test files and this
handover. It must not include the existing `D uv.lock` or untracked `uvlock/`.
Read-only metadata inspection found that `uvlock/uv.lock` matches `HEAD:uv.lock`
byte-for-byte (289150 bytes; SHA-256
`60f749f3ac396a3e9add876225a88c408740b11ba2d1c352afb2d72aa69c5518`).
The root lockfile is missing. This is consistent with a local relocation rather
than a dependency-content change; metadata/Git do not identify the actor or
operation that caused it. Do not restore, move, delete, or stage these lockfile
changes without a separate user request.

Do not rerun live tests or begin a new roadmap phase merely because acceptance
has been recorded. No push is authorized by this acceptance task.

---

# 25. CODEX HANDOFF INSTRUCTION

When starting a new Codex conversation, use:

```text
Read AIOMQL_CODEX_HANDOVER.md first.

Treat it as the project handover and accepted roadmap context.

Then inspect the current repository before modifying anything.

If the checkout contradicts the handover, STOP and report the discrepancy.

Do not replace accepted contracts without evidence.

A10 is ACCEPTED for explicit bounded live read-only tests only.
Pre/post offline: 702/702 PASS; live: 2/2 PASS.
No new phase is authorized. Wait for the user's next scoped request.

The accepted Windows local checkout is:
E:\BLC\Dev_AI_MT5

Never call native order_send or perform broker writes.
```

---

# 26. POST-A10 GUIDANCE

A10.2 has passed. The original A1–A15 roadmap is effectively closed for the
current offline/read-only engine release, within the acceptance boundaries above.

Do NOT automatically proceed into live trading.

A new roadmap should be explicitly designed for any future phase involving:

- MT5 runtime connection wiring;
- persistent runtime recovery scheduling;
- live order execution;
- trading permission activation;
- EA bridge;
- licensing/business platform;
- monitoring/telemetry.

Any phase that permits broker writes requires a new explicit safety review and demo-only acceptance plan.

---

# 27. FINAL SOURCE-OF-TRUTH RULE

This handover is a coordination document.

For implementation truth, priority is:

```text
current source
→ current tests
→ current Git history
→ this handover
→ old chat summaries
```

If anything disagrees:

```text
STOP
→ report discrepancy
→ do not invent
```
