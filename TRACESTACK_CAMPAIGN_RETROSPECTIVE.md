# TraceStack Campaign Retrospective — Phase 1: C66–C76 Evidence Reconstruction

> **Artifact type:** Analysis / evidence only. Not a plan. No recommendations,
> priorities, severity scores, or Concern 77 proposals appear in this document.
>
> **Method:** Read-only inspection of (a) production source in
> `apps/orchestrator/**`, (b) the target Git repository
> `workspace/tracestack-clean` and its worktree refs, (c) filesystem run
> artifacts under `data/runs/RUN-20260930-000001`, (d) the reconstruction
> manifest `data/recovery/tracestack_reconstruction_manifest.json`, (e) commit
> topology of the accepted orchestrator revision, and (f) restore-verified
> backup metadata `data/backups/*.json`.
>
> **Evidence precedence used:** executable tests / mutation evidence > production
> implementation > durable DB/runtime evidence (via restore-verified backup
> metadata, since the live PostgreSQL was not reachable from this inspection
> environment) > Git topology > campaign artifacts > documentation > commit
> messages > inference. Inference is explicitly labelled.
>
> **Critical provenance caveat carried throughout:** the reconstruction manifest
> states verbatim
> `RECONSTRUCTED STATE — NOT ORIGINAL DATABASE HISTORY. Original PostgreSQL
> campaign history was lost; rows inserted from ...`. Where a durable fact is
> sourced from that reconstruction rather than an unbroken original record, it is
> labelled **reconstructed**. No original row was rewritten by this review.

---

## 0. Canonical anchors used (verified before citation)

| Anchor | Value | Verification in this review |
|---|---|---|
| Orchestrator accepted revision | `b2e137f811dd5a16305e2dd26f3ee813a7776476` | `refs/heads/main` = HEAD; clean tracked tree (only untracked `RECOVERY_PROMPT.md`) |
| TraceStack final integration | `4588bba10b237ffb4d2640a4488461e5462c25cf` | `workspace/tracestack-clean` `refs/heads/agent/integration` == this SHA; single parent `e8b9eae` (fast-forward) |
| TS-110 recovery TaskRun | `c31db56e-4962-4c3b-942b-20ae768b732a` | `RUN-20260930-000001/outcome.json` `run_id` field |
| TS-110 final state (canonical prior observation) | COMPLETE / SUCCEEDED, gen 2, attempt 1, 2 model runs, 16 run events | Partially corroborated; gen 2 and 16 events not independently re-derived — see §6 and §7 |
| Final restore-verified backup | `orchestrator-20260930T215132Z-4a79e0da.dump` / SHA-256 `38101df1…73cfd31` | `sha256sum` matches; metadata `verified_at=20260930T215139Z`, `alembic_revision=c1f4a7d29b60`, `source_counts_compared=true` |
| Pre-C76 stranded-state backup | `orchestrator-20260930T213706Z-e190ea70.dump` / SHA-256 `e747df32…b16897c4` | `sha256sum` matches; `verified_at=20260930T213713Z`, `alembic_revision=c1f4a7d29b60` |
| C73 human source commit | `cbff2c4bd919b860c73e3cb061bccff11789c37a` | `refs/heads/master` of `tracestack-clean`; ancestor of `4588bba` |
| C73 integration resolution commit | `e8b9eae300363a8a6020220c7f31a53729b4ad3b` | merge commit, parents `fc6abc5`(1st) + `cbff2c4`(2nd); body names it the `integration_resolution_commit` for escalation `fc9bf9b0-9683-41fa-aaa8-f8f53567b92a` |

Mechanism commits for each concern were confirmed as **ancestors of HEAD** before
citation (Part 1).

---

## PART 1 — Per-concern reconstruction

### Concern 66 — A slow provider call outlived its database transaction
*Heading:* `concerns.md:2757`. Mechanism commit(s): transaction boundary +
`require_in_flight` revalidation path (documented in entry; C67 later formalized
ownership).

- **A. Trigger.** Project `d98cb1e7…`, TS-109 run `RUN-20260928-000005`
  (`9760dfeb-3112-4b4f-b67f-5ae4daa7b2b1`): attempt 2's provider 600 s timeout
  exceeded PostgreSQL's 300 s `idle_in_transaction_session_timeout`; PG killed the
  idle-in-transaction session, `ModelTimeout` raised, rollback then raised
  `psycopg.errors.IdleInTransactionSessionTimeout` masking the real error. Run
  stayed `RUNNING`, task `VERIFYING`, integration `fc6abc5`, TS-110 never started.
- **B. Failure boundary.** **SUPERVISOR** (transaction lifetime policy).
  Secondary **INFRASTRUCTURE** (PG idle-in-transaction timeout interacting with a
  deliberately longer provider timeout). Provider timeout itself = **MODEL/ENVIRONMENT**.
- **C. Missing invariant.** *No database transaction may be held across an external
  model inference; the request and its result/failure are persisted in separate
  transactions with a post-call revalidation.*
- **D. Mechanism introduced.** Pre-provider durable checkpoint + `commit`, provider
  awaited with **no transaction open**, new post-call transaction that executes
  `TaskRunRepository.require_in_flight` (`SELECT … FOR UPDATE`) before recording;
  `session_scope` uses SQLAlchemy `Session.invalidate()` when rollback reports an
  invalid DBAPI connection, preserving the original model exception.
- **E. Enforcement point.** Transaction boundary (pre/post-call split) +
  `require_in_flight` locked revalidation at the post-call write; `session_scope`
  rollback classification.
- **F. Strongest tests.** `tests/integration/test_fix_loop_resume.py` (no open
  txn across provider; persisted `ModelTimeout`→`MODEL_TIMEOUT→RETRY`; invalidated
  pre-call session gets a valid post-call checkout; `BUILD_FAILED→SEND_TO_CODER→slow
  attempt 2→ModelTimeout` resumable). `tests/unit/test_db_session_cleanup.py`
  (invalidated rollback cannot mask model failure). Post-concern-66 preservation
  read recorded the real `RUN-20260928-000005` signature as **reconstructed**.
- **G. Mutation evidence (documented).** (1) remove coder pre-provider checkpoint →
  RED (`session.in_transaction() is True` inside provider); (2) drop post-call
  `require_in_flight` → RED (committed SUCCEEDED model row after abandonment);
  (3) omit `Session.invalidate()` → RED (dead session not discarded).
- **H. Later campaign exercise.** **YES** — directly exercised: the same
  `RUN-20260928-000005` was recovered by C67 (gen 0→1) and then reached a real
  attempt-3 provider timeout whose `ModelTimeout` row was durably persisted
  (`duration_ms≈600000`), i.e. the fix held under the original failure.
- **I. Observed result.** The provider failure was recorded durably without masking
  and without stranding the transaction; the run remained in a well-formed,
  resumable state. Facts recorded, not scored.
- **J. Remaining boundary.** It guarantees *durable, fenced recording*, not that a
  stranded in-flight run has an **operator action** to continue it — that gap is
  C67. Timeout alignment is defense-in-depth only, never the mechanism.

### Concern 67 — A stranded in-flight run had no supported way to be continued
*Heading:* `concerns.md:2900`. Mechanism commit: `ca17e9ae032695121c152d3297145277263ff396`
(+ prior `c4101e0`) — ownership columns/migration `a1f47b0c93d2`, `acquire_execution`,
`recover_incomplete`.

- **A. Trigger.** C66 proposed `POST /tasks/{id}/resume` as the recovery action;
  executed against the live run it returned `409 EntityConflict: Task TS-109 is not
  paused`. No operation *continued* an existing in-flight `TaskRun` whose owner could
  no longer proceed; `run_next`'s implicit sweep had no mutual exclusion (two
  requests → two dispatchers).
- **B. Failure boundary.** **SUPERVISOR** (missing operator-safe continuation + no
  execution-identity primitive).
- **C. Missing invariant.** *A run may have at most one effective executor at a time,
  and an operator must be able to continue a stranded in-flight run without creating
  a duplicate executor or a new run.*
- **D. Mechanism introduced.** `task_runs` gained `execution_generation` (monotonic
  fencing token), `execution_owner` (held/not-held marker, never a liveness claim),
  `execution_started_at` (audit). `acquire_execution` = one guarded `UPDATE`
  incrementing generation + stamping owner; `release_execution` clears only for the
  current holder; `require_in_flight(expected_generation=…)` folded the generation
  into the same locked predicate, raising `RunOwnershipLostError`. Read-only,
  fail-closed `assess_recoverability` with named checks; `POST /runs/{id}/recover` +
  `GET /runs/{id}/recoverability` (run-scoped durable UUID); `external_run_id` exposed.
- **E. Enforcement point.** Execution ownership — the single guarded `UPDATE`
  acquisition + the generation predicate on the same `FOR UPDATE` lock at every
  durable write (`durable_checkpoint`, `prepare_workspace`, `deliver`).
- **F. Strongest tests.** `tests/integration/test_concern67.py` (65 tests) incl. the
  real stranding→recovery→`COMPLETED` as one run; subprocess/interpreter isolation;
  HTTP contract. `test_migrations.py` pre-existing rows → gen 0/no owner. **PostgreSQL
  concurrency** (Barrier): two simultaneous `recover_run` → exactly one authorization
  at gen 1, one `EntityConflict`, two `task_runs` rows.
- **G. Mutation evidence (documented).** A) `acquire_execution` check-then-act → RED
  `[1,1]` (two executors); B) drop `expected_generation` → RED
  `RunOwnershipLostError` not raised; C) recovery opens a replacement run → RED
  `3 == 2`; D) reconstruction returns `run.attempt_number` → RED (next=1); E)
  terminal/abandoned guards always pass → RED.
- **H. Later campaign exercise.** **YES** — C68's live discovery is literally the
  *first real use of C67's recovery* on `RUN-20260928-000005` (gen 0→1). C71/C76 build
  on the same ownership primitive.
- **I. Observed result.** Recovery acted on the existing run, appended exactly one
  `RUN_RECOVERY_AUTHORIZED`, kept `run_number`/identity, and fenced the old owner.
- **J. Remaining boundary.** Recovery still required a coder attempt to be available.
  A run with a *spent* budget but pending deterministic settlement was reported
  `recoverable:false` — that permanently-stranded shape is C68. It also did not name
  the *post-approval delivery* stranding — that is C76.

### Concern 68 — Exhausting the attempt budget made a run permanently unrecoverable
*Heading:* `concerns.md:3160`. Mechanism commit: `125fc017030c0d4a7fac0155edc5c97f311c3bbd`.

- **A. Trigger.** Via C67's real recovery of `RUN-20260928-000005`: reconstructed
  next attempt 4 of `max_attempts=3`; `GET …/recoverability` refused on
  `attempt_accounting_reconstructable`. Run stranded: `RUNNING`/`CODING`,
  `attempt_number=3`, gen 1, owner `NULL`, `candidate_commit=NULL`, and abandon would
  write the wrong (lossy, no-escalation) ending.
- **B. Failure boundary.** **SUPERVISOR** (one check conflated two questions). The
  attempt-3 provider timeout itself = **ENVIRONMENT/MODEL**.
- **C. Missing invariant.** *Exhausting the coder-attempt budget must not make an
  in-flight run unrecoverable while deterministic terminal settlement remains pending;
  recovery may reacquire ownership solely to settle and must not manufacture an attempt.*
- **D. Mechanism introduced.** Split `attempt_accounting_reconstructable` (can the
  record be trusted) from `workflow_action_available` (is anything left), surfaced as
  `recovery_mode ∈ {continue, settlement_only, refuse}`. No settlement code added to
  the recovery service — it reuses the fix loop's own empty-range →
  `settle(ESCALATED, RETRY_EXHAUSTED)` path.
- **E. Enforcement point.** Recovery assessment + the same `acquire_execution` guard;
  enforcement of "no new attempt" is the fix loop's own budget arithmetic, reused.
- **F. Strongest tests.** `tests/integration/test_concern68.py` (25 tests) rebuilt the
  post-C67 signature; negative evidence via **witnesses** (`NeverCalledCoder`/
  `NeverCalledReviewer`, sentinel verification script) proving zero coder/reviewer/
  verification/candidate work and unmoved `agent/integration`; PostgreSQL simultaneous
  settlement → one owner, one `RUN_RECOVERY_AUTHORIZED(settlement_only)`.
- **G. Mutation evidence (documented).** A) `workflow_action_available=False` for the
  settlement branch → RED (10 tests); B) widen fix-loop ceiling → RED (coder asked
  during settlement); C) non-empty range → RED; D) settlement advances `attempt_number`
  → RED `4==3`; E) skip `acquire_execution` → RED `[1,1]`.
- **H. Later campaign exercise.** **PARTIAL.** C68's own discovery was a live exercise
  of settlement_only. Its *mechanism shape* (recover-to-settle-without-model) is the
  direct structural ancestor of C76's `delivery_only`, which was **directly exercised**
  on TS-110 (Part 6).
- **I. Observed result.** (For the settlement path it names) — the run closes with the
  escalation the fix loop already owes; no model call is manufactured.
- **J. Remaining boundary.** `settlement_only` covers *no approved candidate exists*.
  It does **not** cover the case where a *committed, approved candidate exists but
  delivery crashed* (task `APPROVED`, `candidate_commit=NULL`) — that is C76. It does
  not integrate anything.

### Concern 69 — Truthful `RETRY_TASK` escalation option text
*Not in `concerns.md`; evidence is the commit + tests.* Mechanism commit:
`062e2529ea6cd41699dfa692e2789fd79949c6de` ("Concern 69: truthful RETRY_TASK escalation
option text").

- **A. Trigger.** The live TS-109 escalation exposed that the `RETRY_TASK` option text
  claimed the retry would "reword or split" the task, which it does not do (a retry
  re-runs the same specification).
- **B. Failure boundary.** **SUPERVISOR** (operator-facing message accuracy) with
  **OPERATOR/PROCEDURE** impact (a human choosing on a false description).
- **C. Missing invariant.** *An escalation option must describe only effects the system
  actually performs.*
- **D. Mechanism introduced.** Corrected both `RETRY_TASK` option producers —
  `run_escalation_options()` (`domain/escalation.py:246`) and
  `escalation_options()` (`domain/review.py:723`) — to state "same specification retry".
- **E. Enforcement point.** Escalation/review option rendering (pure text producers).
- **F. Strongest tests.** `tests/unit/test_run_escalation.py` —
  `test_retry_task_option_does_not_claim_to_reword_or_split`,
  `test_retry_task_option_communicates_same_specification_retry`,
  `test_review_retry_task_option_does_not_claim_to_reword_or_split`.
- **G. Mutation evidence.** None reported for C69; the concern's own entry (commit body)
  documents the added failing-then-passing tests but no controlled mutation table.
  (Stated absence, not invented coverage.)
- **H. Later campaign exercise.** **NO EVIDENCE** — text-level fix; no campaign event
  re-triggers an option claim beyond the test.
- **I. Observed result.** Only test evidence found; no runtime re-exercise recorded.
- **J. Remaining boundary.** Guarantees the *wording* is truthful; it cannot guarantee
  the human's *decision* is correct, and it is not enforced by any durable state
  transition.

### Concern 70 — A small change to a large file had no safe way to be made
*Heading:* `concerns.md:3370`. Mechanism commit: `8175bee` (+ `2404c60` docs correction;
C72 follow-on `99fce0a`).

- **A. Trigger.** `RUN-20260928-000007` (`eac296d6-f041-4fa3-abfd-44d1a15c4186`), TS-109.
  Target file 11,813 B / 39 existing tests. Attempt 1 did the *right* thing but was
  340 B over the whole-file allowance (15,106 > 14,766). Told to "fit", attempt 2
  fit by **deleting the 39 existing tests** (98 additions, **315 deletions**, 413-line
  diff > 150) — a scope-guard catch, but the model was made *worse* by the only lever
  it had (a size limit on whole-file edits).
- **B. Failure boundary.** **MODEL** (deleted on purpose to satisfy a limit) +
  **SUPERVISOR/HARNESS** (a representation whose cheapest legal response is to remove
  the work: "a guard a model can satisfy by removing the work is not a guard on the
  work").
- **C. Missing invariant.** *The cost charged for a change must not be the size of the
  file the change lands in; a model must be able to express a small change without
  re-emitting (and therefore risking) unrelated content.*
- **D. Mechanism introduced.** A fourth edit operation `replace` (`operation:"replace"`,
  empty `content`, `oldText`/`newText`). `oldText` must occur **exactly once** (zero /
  multiple matches refuse — no fuzzy fallback). Flat emission ceiling
  `MAX_TARGETED_EDIT_PAYLOAD_BYTES=8000` charged on `oldText+newText`; result bounded
  separately by `context_max_file_bytes`; whole change-set applied atomically if any
  targeted edit present; `max_files_changed` counts distinct paths. `EDIT_SCHEMA_VERSION`
  → `code-edits/3`, `CODER_PROMPT_VERSION` → `coder-prompt/2`.
- **E. Enforcement point.** Edit parser (`domain/edits.py` / `services/code_edits.py`) +
  scope guard on the resulting Git diff (`domain/scope.py`); exact-unique-match rule.
- **F. Strongest tests.** 57 tests spanning representation, application, scope guards
  driven through `run_coding_attempt`, whole-file preservation, and prompt disclosure.
  Also documents two tests that were *vacuously green* before the mutation run
  (tautological emission-ceiling test; fixture writing baseline after
  `prepare_workspace`) and their repair — strong self-correction evidence.
- **G. Mutation evidence (documented).** A) `_resolve_targeted` first-match (drop
  multiple-match refusal) → RED (6 tests); B) `result_max_bytes` check stubbed False →
  RED; C) raise `MAX_TARGETED_EDIT_PAYLOAD_BYTES` 8000→100000 → **survived until the
  tautological test was rewritten** (then RED ×4); D) `atomic=…` forced False → RED (9);
  E) `proposed_paths` count entries vs distinct paths → RED.
- **H. Later campaign exercise.** **YES** — TS-110's successful attempt used
  `coder-prompt/3+code-edits/3` (`RUN-20260930-000001/outcome.json`) and landed a
  36-line, 2-file change cleanly in scope; the representation problem C70 targets is
  exactly the shape a healthy later run took. (Note: TS-110's payload is a *whole-file*-
  shaped low-complexity change; it demonstrates the schema path, not necessarily a
  `replace` op — labelled PARTIAL-strong / direct for the schema, inference for the op.)
- **I. Observed result.** Scope decisions computed on the real diff; the deleting-to-fit
  failure mode is structurally closed by charging only the changed region.
- **J. Remaining boundary.** Bounds size/representation; does **not** decide semantic
  correctness (reviewer still does), and the exact-unique-match rule trades refusal for
  safety — a near-match the model meant is refused, not applied.

### Concern 71 — Request cancellation must not strand durable workflow state
*Heading:* `concerns.md:3634`. Mechanism commit:
`da4c5c3885e64a24308b6100ee333e44c803ec60` ("name the cancellation, fence the
settlement it performs").

- **A. Trigger.** HTTP client disconnect during a long workflow;
  `asyncio.CancelledError` is a `BaseException`, bypasses `except Exception` in
  `WorkflowRunner._execute`, so the run stayed `RUNNING`/task `CODING` with no terminal
  settlement. **Explicit, important correction recorded:** the initially-cited
  `RUN-20260928-000008` (`RUN-20260929` family) is **not** evidence for C71 — its row
  is a `ModelTimeout` (an ordinary `Exception`), cancellation was *inferred not
  observed*. That run is a **C67 recoverability case**, and C71 does not
  retroactively settle/reclassify it.
- **B. Failure boundary.** **SUPERVISOR** (exception taxonomy: `BaseException`
  handling). The RUN-000008 mis-attribution was itself a **TEST/HARNESS-observability /
  operator-inference** error, corrected in-document.
- **C. Missing invariant.** *Loss or cancellation of the initiating HTTP request must
  not leave a durable `TaskRun` indefinitely `RUNNING`; HTTP request lifetime must not
  determine whether durable state is settled.*
- **D. Mechanism introduced.** `WorkflowRunner._execute` catches `CancelledError`
  **by name** → synchronous `_settle_cancelled_run`: rollback, one transaction,
  run→`FAILED`/`WORKFLOW_CANCELLED` *if still in flight and still at the generation this
  dispatch took*, task→`FAILED` only if the state machine permits, one `RUN_CANCELLED`
  event, finalize active-runtime, re-raise. Handler kept narrow so `SystemExit`/
  `KeyboardInterrupt`/`GeneratorExit` are not written as cancellations.
- **E. Enforcement point.** State-machine transition + the generation fence on
  `finish(expected_generation=…)` (`repositories/task_runs.py`) during settlement.
- **F. Strongest tests.** `tests/integration/test_concern_71_cancellation.py`
  (13 tests, event/cursor-hook gated, no sleeps) incl. cancellation-during-provider,
  second-cancellation-cannot-interrupt-settlement, ownership-released-exactly-once,
  GeneratorExit/Interrupt/Exit **not** recorded, provider-failure ≠ cancellation
  (both directions), superseded-dispatch-cannot-settle, cancellation-never-overwrites-
  abandonment.
- **G. Mutation evidence (documented).** A) delete handler → RED×6; B) skip settlement
  when attempt recorded → RED×3; C) skip ownership release → RED×4; D) consume budget in
  settlement → RED×3; **E) drop rollback before settlement → 13 passed — NOT CAUGHT**
  (reported honestly: no suspension point holds a txn, thanks to C66; kept as defensive
  symmetry); F) drop generation fence → RED×1 (proves fence load-bearing).
- **H. Later campaign exercise.** **PARTIAL / test-only for the cancellation path.** No
  campaign event is recorded where a real client disconnect delivered `CancelledError`
  to a live run (RUN-000008 was reclassified away from it). The generation-fence
  interaction with recovery **is** exercised (C67/C68/C76 reuse the same fence).
- **I. Observed result.** For the reproduced-in-test case: run settles
  `WORKFLOW_CANCELLED`, ownership released, budget preserved, non-cancellation
  BaseExceptions not recorded. No live cancellation settlement was observed.
- **J. Remaining boundary.** Settles a *cancelled* run; does not settle a *crashed
  process* (nothing catches in-process) — crash-to-stranded durable state is the C76
  territory, and RUN-000008-style ownerless in-flight runs still need C67 recovery.

### Concern 72 — Declare targeted-edit limits before generation
*Heading:* `concerns.md:3879`. Mechanism commit: `99fce0a78a60076e001719e902c2c6790e2290f7`
("70 72 concerns").

- **A. Trigger.** `RUN-000008` recovery attempt 2 (`coder-prompt/2+code-edits/3`) spent
  399,372 ms / 6,013 in / 6,073 out tokens, returned two targeted `replace` edits whose
  second was `oldText 11,813 B + newText 3,734 B = 15,547 B` — correctly rejected
  atomically by the 8,000 B ceiling, but the coder learned the hard limit **only in
  post-hoc rejection feedback**, after consuming the attempt.
- **B. Failure boundary.** **SUPERVISOR/HARNESS** (prompt under-disclosed a constraint
  the model could otherwise honour). Model spent budget but obeyed the rules it knew.
- **C. Missing invariant.** *Every representation constraint a model can act on while
  composing must be disclosed before generation; only application/scope constraints
  (which depend on staged content or the diff) stay downstream.*
- **D. Mechanism introduced.** `render_coding_instructions` now reads the authoritative
  `domain.edits.MAX_TARGETED_EDIT_PAYLOAD_BYTES` and discloses the exact UTF-8
  `len(oldText)+len(newText)` accounting, requires the smallest sufficiently unique
  `oldText`, forbids placeholder/ellipsis claims, retains whole-file
  `update`/`create` as the fallback. `CODER_PROMPT_VERSION`→`coder-prompt/3`;
  `code-edits/3` unchanged. **No limit was raised or weakened.**
- **E. Enforcement point.** Prompt rendering, single-sourced from the same constant the
  parser reads (no second copy).
- **F. Strongest tests.** `tests/unit/test_concern72.py` (added first; six failures on
  baseline prompt) — exact-value disclosure, per-edit scope, UTF-8 accounting, minimal
  unique fragments, placeholder prohibition, whole-file alternatives, shared-source.
- **G. Mutation evidence (contract, documented).** Changing the authoritative constant to
  `37` moves both the rendered prompt and the parser boundary together; removing the
  disclosure / minimal-fragment guidance / placeholder prohibition each independently
  reddens its focused test; changing validator accounting away from documented UTF-8
  combined sizes reddens the shared contract test.
- **H. Later campaign exercise.** **YES (indirect)** — TS-110 ran under
  `coder-prompt/3+code-edits/3` and produced a small in-budget change with zero
  rejected edits (`completion-report.json` `rejected_edits: []`), i.e. the disclosure
  path was in force for the successful campaign task.
- **I. Observed result.** Prompt/parser share one constant; no over-limit edit consumed a
  TS-110 attempt.
- **J. Remaining boundary.** Discloses the *emission* ceiling only. Result ceiling
  (`context_max_file_bytes`), distinct-path `max_files_changed`, and diff
  `max_diff_lines` remain deliberate downstream guards — disclosure does not preclude a
  later scope refusal.

### Concern 73 — Human (`COMPLETED_BY_HAND`) commit must be integrated into the baseline
Original mechanism commit: `7a4e9f0fb6e03b66277febc810f4ac18ba7667f9`
(+ conflict-extension `560da40e80affc8db6af19ad3f775f75d087cb11`). Docs:
`CONCERN_73_IMPLEMENTATION.md`.

- **A. Trigger.** When an operator resolves an escalation as `COMPLETED_BY_HAND` and
  supplies a human commit, that commit previously was not integrated into
  `agent/integration` before the task was marked COMPLETE, so dependent tasks (TS-110)
  could start from a baseline missing the human work. The real TS-109 case produced a
  **merge conflict** between the human commit and the agent line — motivating the
  operator-resolved conflict contract in the follow-up commit.
- **B. Failure boundary.** **SUPERVISOR** (provenance/integration ordering) +
  **OPERATOR/PROCEDURE** (conflict resolution carried by a person).
- **C. Missing invariant.** *A task cannot reach COMPLETE via human completion unless
  the human work is durably present in the integration baseline with recorded
  provenance; and a human/agent integration conflict must be resolved as an auditable
  durable event that preserves the human commit unchanged, not silently overwritten.*
- **D. Mechanism introduced.** `human_commit` column (migration `e8a3c7f21d49`) on
  `human_escalations`; `integrate_human_commit()` in `services/integration.py`
  (validate → idempotency check → worktree → merge → cumulative verify → advance ref →
  `provenance:"human"` → fail-closed); `apply_escalation_answer()` integrates **before**
  marking COMPLETE (rollback on failure); reconciliation endpoint
  `POST /escalations/{id}/reconcile-human-commit` for pre-C73 history. Conflict
  extension: `services/human_resolution.py` + migration `c1f4a7d29b60`
  `integration_resolution_commit`, recording the operator-resolved merge as a distinct
  durable commit.
- **E. Enforcement point.** Integration service (merge + cumulative verification + last
  ref move) and the escalation-resolution transaction boundary; provenance recorded in
  the event payload.
- **F. Strongest tests.** `tests/integration/test_concern73.py` (19 tests: valid
  integration, dependent baseline containment, human-vs-model provenance, nonexistent
  commit fail-closed, merge-conflict fail-closed, no fake automated candidate recorded,
  etc.); `tests/integration/test_concern73_conflict_resolution.py` (conflict contract);
  `tests/unit/test_human_resolution_invariant.py`.
- **G. Mutation evidence.** Mutation tables are documented for the surrounding
  integration/ownership concerns (67/68/70/71/76); the C73 entry does not present a
  dedicated mutation table in the reviewed files. Stated as not-found rather than
  inferred.
- **H. Later campaign exercise.** **YES — directly exercised, with durable artifacts.**
  `e8b9eae` is the real `integration_resolution_commit`: a two-parent merge
  (1st `fc6abc5` baseline, 2nd `cbff2c4` human commit preserved unchanged), body names
  escalation `fc9bf9b0-9683-41fa-aaa8-f8f53567b92a`, resolution scope
  `navigation-stack.ts` + its test. It became TS-110's `starting_commit`.
- **I. Observed result.** The human work reached the baseline as a second parent of a
  merge commit, preserving `cbff2c4` byte-for-byte; TS-110 then built on that
  reconciled baseline.
- **J. Remaining boundary.** Handles integration of an *accepted* human commit and its
  conflict. It does not recover a *supervisor process that crashed between approval and
  delivery of an agent candidate* (that's C76), nor does it guarantee the human's
  resolution is semantically correct — that is the operator's judgement.

### Concern 74 — Fail-closed test-database isolation
Mechanism commit: `e10e589adee59b270fc04a7d9c256b6af72eea7c` ("Close Concern 74
acceptance gaps"). Evidence: `tests/db_safety.py`, `tests/test_concern74_db_safety.py`,
`tests/conftest.py`.

- **A. Trigger.** Stated verbatim in `db_safety.py`: *"The historical orchestrator
  database was lost because the test harness could destructively operate on the runtime
  database."* The loss is corroborated by the reconstruction manifest's
  `RECONSTRUCTED STATE — NOT ORIGINAL DATABASE HISTORY` statement.
- **B. Failure boundary.** **TEST/HARNESS** (destructive teardown reaching a shared
  runtime DB) with **DATA LOSS** consequence; **OPERATOR/PROCEDURE** (no explicit
  test-target requirement).
- **C. Missing invariant.** *No destructive database operation may proceed unless the
  target is positively established to be an isolated test database; if that cannot be
  proven, zero destructive SQL runs.*
- **D. Mechanism introduced.** `assert_test_database_safe()` — fail-closed validator:
  requires non-empty `TEST_DATABASE_URL`; rejects the runtime DB names
  (`orchestrator`/`_dev`/`_prod`/`_production`); rejects equality with runtime
  `DATABASE_URL` (host/port/db identity, independent of credentials); requires a safe
  prefix (`test_`, `orchestrator_test`, `race_`, `lock_`, `recover_`, `settle_`,
  `verify_`, `c6x_`); requires lowercase-identifier shape; honours an explicit
  `ORCHESTRATOR_TEST_DATABASE` marker. `drop_all_tables_for_test` revalidates the actual
  teardown target.
- **E. Enforcement point.** Every destructive DB operation (drop_all, DROP DATABASE,
  create) via `db_safety` + the `conftest.py` `engine` fixture.
- **F. Strongest tests.** `tests/test_concern74_db_safety.py` (59 lines added); the
  validator's own rejection paths; C70/C71/C72 entries each restate that the PostgreSQL
  runs deliberately used isolated DBs (`test_orchestrator`, `test_c71_validation`,
  `test_c72_validation`) and **never** the live `orchestrator` DB precisely because the
  teardown does `drop_all`.
- **G. Mutation evidence.** Not presented as a dedicated mutation table in the reviewed
  files; the mechanism is a positive-assertion guard, and its "fails closed → zero SQL"
  property is directly assertable by rejection tests. Stated as not-found.
- **H. Later campaign exercise.** **YES (as precondition).** The C75 backup-verification
  path drops a disposable `verify_*` database *"through the Concern 74 safety guard"*
  (README:265), and the final backup metadata shows exactly such a disposable restore
  (`verified_database: verify_a552f9a9394c`) separate from `source_database:
  orchestrator`.
- **I. Observed result.** Restore-verification isolated runtime from disposable DB; no
  destructive op against the runtime DB was recorded in the artifacts reviewed.
- **J. Remaining boundary.** Protects against *destructive test operations*; it is not a
  substitute for backups (C75) and does not by itself reconstruct lost history
  (that's the reconstruction importer).

### Concern 75 — Backup / restore tooling with verification
Mechanism commit: `10380bc98bfbed5e0cbe6a36f012ad8833db95be` ("concern 75 backup tool
fallback"). Recovery importer commit: `416bb016ffa1dee30ed8fbfec249f151c25021e9`
("Campaign recovery phase 2: evidence-backed reconstruction importer"). Docs: README
`## Database backups`.

- **A. Trigger.** The DB-loss incident (C74) exposed that there was no dependable,
  *verified* recovery point, and the campaign had to reconstruct state from filesystem
  artifacts. The tool also needed to work whether `pg_dump`/`pg_restore` were on the host
  **or** only available via the Compose `postgres` service.
- **B. Failure boundary.** **INFRASTRUCTURE** (client binary availability / host-vs-
  compose execution) + **SUPERVISOR/OPERATOR-PROCEDURE** (no backup/verify discipline).
- **C. Missing invariant.** *A recovery point is not trusted until it has been restored
  into an isolated disposable database and its row/schema identity compared to source;
  retention must never evict the only known-good verified backup.*
- **D. Mechanism introduced.** `services/backups.py`: host client binaries first,
  deliberate `docker compose … postgres` fallback (no arbitrary-container scanning),
  writes `.dump` + adjacent `.json` metadata (creation time, source DB identity,
  source_revision, dirty flag, alembic revision, PG version, filename, size, SHA-256,
  row counts, execution mode, restore-check results). `verified_at` stays null until
  `verify-backup` restores the dump into a disposable `verify_*` DB and drops it **via
  the C74 guard**. Retention keeps the latest seven **verified** backups. Reconstruction
  importer (`reconstruction_importer.py` + `tracestack_reconstruction_manifest.json` +
  `scripts/reconstruct_campaign.py`) rebuilds durable rows from filesystem evidence.
- **E. Enforcement point.** Backup/restore service + isolated verify DB + C74 safety
  guard on the drop; metadata SHA-256 as the integrity token.
- **F. Strongest tests.** `tests/unit/test_database_backups.py` (235 lines);
  `tests/unit/test_reconstruction_importer.py` (623 lines);
  `tests/integration/test_reconstruction_c73_contract.py` (proves reconstructed C73
  rows carry the right provenance).
- **G. Mutation evidence.** Not presented as a mutation table in the reviewed files;
  the reconstruction importer is instead pinned by the *canonical-state contract* tests
  (reconstructed rows must satisfy alembic head + counts). Stated as not-found.
- **H. Later campaign exercise.** **YES — directly exercised, durable artifacts present.**
  Both cited backups exist with matching SHA-256; the final `4a79e0da` dump's metadata
  shows `verified_at=20260930T215139Z`, `verified_database=verify_a552f9a9394c`,
  `source_database=orchestrator`, `source_counts_compared=true`, alembic
  `c1f4a7d29b60`. The reconstruction manifest records the lost-history recovery itself.
- **I. Observed result.** A verified recovery point existed both *before* the C76 POST
  (`e190ea70`) and after settlement (`4a79e0da`); restore ran in an isolated DB.
- **J. Remaining boundary.** Verification proves the *dump* restores to a matching DB;
  it does not prove the reconstructed rows equal the *original* pre-loss history
  (manifest: omitted `model_runs`, `run_events`, `reviews`, etc. are reconstruction
  artifacts, not the original rows). It does not make the *live* orchestrator survive a
  crash mid-transaction — that is C66/C67/C76.

### Concern 76 — An approved candidate whose delivery crashed is owed delivery
Mechanism commit (HEAD): `b2e137f811dd5a16305e2dd26f3ee813a7776476` ("Concern 76: recover
delivery across worktree namespaces"). Evidence: `services/run_recovery.py`
(`RecoveryMode.DELIVERY_ONLY`, `_assess_stranded_delivery_candidate`),
`services/integration.py` (`_usable_or_recreated_integration_worktree`),
`services/git_errors.py` (`WorktreeUnusable`), `tests/integration/test_concern76.py`.

- **A. Trigger.** A third stranding shape C67/C68 could not name: the reviewer
  **approved**, the candidate was **committed**, and the process **died inside the
  delivery transaction**. Result: task `APPROVED`, run still `RUNNING` with no owner,
  `candidate_commit = NULL`, yet a committed clean worktree held the approved result.
  Re-entering the fix loop would either re-ask the coder/reviewer to reproduce existing
  work or refuse and strand the approval forever. The **root cause of the crash** is an
  **environment/namespace** defect: a linked worktree whose absolute `.git` metadata was
  written under a **container path a host run cannot resolve**
  (`run_recovery.py:130`; `test_concern76.py` docstring "host/container worktree path
  portability").
- **B. Failure boundary.** **MIXED — SUPERVISOR** (no delivery-only recovery mode) +
  **ENVIRONMENT/INFRASTRUCTURE** (host↔container worktree path namespace mismatch).
- **C. Missing invariant.** *Once an approved, committed, in-scope, verification-passing
  candidate exists in supervisor-readable Git state, a crash between approval and
  delivery must be recoverable to integration without any further model call, any new
  attempt, or any new TaskRun; and a namespace-broken worktree may be repaired only when
  it is disposable supervisor state, never when it holds reviewed work.*
- **D. Mechanism introduced.** `assess_recoverability` recognises a fourth mode
  `DELIVERY_ONLY` and **derives rather than trusts** the candidate from supervisor-owned
  Git state via `_assess_stranded_delivery_candidate`: HEAD is real, clean, descends
  from the run's `starting_commit`, falls inside declared scope, is backed by an
  approving review + passing candidate gates, and is not contradicted by any recorded
  `candidate_commit` — each check fails closed. The run re-enters the **normal**
  delivery node through the graph (`_after_prepare` routes `APPROVED`→`deliver`);
  `_commit_or_reconcile` records the derived candidate idempotently;
  `integrate_candidate` will not move the ref twice if a crash already advanced it.
  Namespace repair (`_usable_or_recreated_integration_worktree`) rebuilds **only** the
  disposable, supervisor-owned `_integration` worktree; an unusable **task** worktree is
  a refusal (it may be the only copy of approved/escalated work).
- **E. Enforcement point.** Recovery assessment (`delivery_only` derivation, fail-closed)
  + execution-generation fence (reused from C67) + delivery node idempotency +
  `contains_commit` integration idempotency + namespace-bounded worktree repair.
- **F. Strongest tests.** `tests/integration/test_concern76.py` (767 lines):
  `test_foreign_namespace_integration_worktree_is_unusable_without_repair`,
  `test_usable_or_recreated_repairs_stale_integration_metadata`,
  `test_recreation_is_idempotent_when_worktree_is_healthy`,
  `test_usable_integration_worktree_with_unresolvable_baseline_raises_without_destroy`,
  `test_integration_repair_never_touches_an_unrelated_worktree`,
  `test_stranded_approved_run_is_assessed_as_delivery_only`,
  `test_delivery_only_recovery_completes_without_model_calls` (end-to-end through the
  graph), the six recovery-refusal cases (no candidate work / wrong ancestry / scope
  mismatch / review not approved / supplied candidate disagrees / worktree unusable /
  dispatch holds run), `test_integrate_candidate_is_idempotent_when_already_in_baseline`,
  `test_generation_fencing_blocks_a_superseded_executor`.
- **G. Mutation evidence.** A dedicated mutation table is not present in the reviewed
  C76 entry; instead the mechanism carries a *battery of explicit refusal tests*
  (each derivation predicate has a negative test) and idempotency tests that pin the
  failure modes. Reported as test-evidence, not invented mutation coverage.
- **H. Later campaign exercise.** **YES — directly exercised (this is the campaign's
  terminal event).** TS-110 `RUN-20260930-000001` / TaskRun
  `c31db56e-4962-4c3b-942b-20ae768b732a` (Part 6). Durable corroboration: Git
  `agent/integration = 4588bba` with single parent `e8b9eae` (= `starting_commit`), and
  the restore-verified final backup's **whole-DB** `model_runs = 2` and `task_runs = 8`,
  i.e. no extra model call and no extra TaskRun for TS-110.
- **I. Observed result.** An approved, undelivered candidate was integrated exactly once
  by fast-forward; no coder/reviewer call is present in the durable counts; the disposable
  `_integration` worktree is the only worktree touched. (Per-run `generation 1→2` and the
  exact per-TS-110 `run_events` figure are canonical-state assertions not independently
  separable from this environment — see §7.)
- **J. Remaining boundary.** Covers *post-approval, pre-delivery* crash with a committed
  candidate in the run worktree. It explicitly refuses when: the task worktree metadata
  is namespace-broken (it is **not** rebuilt), the candidate fails ancestry/scope/
  review/gate derivation, a dispatch currently holds the run, or the recorded
  `candidate_commit` disagrees with the worktree HEAD. It does not address the general
  host↔container namespace problem for *task* worktrees, nor concurrent-operator recovery
  beyond the single guarded `UPDATE`.

---

## PART 2 — Evidence matrix

| Concern | Trigger | Failure class | Missing invariant | Mechanism introduced | Enforcement point | Strongest test evidence | Mutation evidence | Later campaign exercise | Observed result | Explicit remaining boundary | Evidence references |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **C66** | Provider 600s > PG 300s idle-txn killed the txn; rollback masked `ModelTimeout` | SUPERVISOR + INFRA | No DB txn held across model inference | Pre/post-call commit split + `require_in_flight` revalidation + `Session.invalidate()` | txn boundary + FOR UPDATE | `test_fix_loop_resume.py`, `test_db_session_cleanup.py` | 3 mutations, all RED | YES (RUN-000005 real attempt-3 timeout persisted) | Failure recorded durably, run well-formed | No operator action to *continue* an in-flight run | concerns.md:2757 |
| **C67** | `/resume` 409 "not paused"; no safe way to continue a stranded in-flight run | SUPERVISOR | ≤1 effective executor; operator can continue without duplicate | `execution_generation`/`_owner` + `acquire_execution` guarded UPDATE + recover endpoint + assess | execution ownership / generation fence | `test_concern67.py` (65) + PG Barrier races | 5 mutations, all RED | YES (first live recovery gen0→1) | one run, one auth event, old owner fenced | Spent budget + pending settle still refused | ca17e9a, concerns.md:2900 |
| **C68** | Attempt budget spent → `recoverable:false`, run permanently stranded | SUPERVISOR | Spent budget ≠ unrecoverable when settle pending | `recovery_mode` split; reuse fix-loop's `settle(ESCALATED)` | recovery assessment + same guard | `test_concern68.py` (25) + witnesses + PG | 5 mutations, all RED | PARTIAL (settlement_only live-discovered; shape reused by C76) | closes with owed escalation, no model call | Does not cover a *committed approved candidate* (C76) | 125fc01, concerns.md:3160 |
| **C69** | `RETRY_TASK` option text claimed it reworded/splits the task | SUPERVISOR + OPERATOR | Options describe only real effects | corrected two option producers | escalation/review rendering | `test_run_escalation.py` (3) | none reported | NO EVIDENCE | wording only | cannot guarantee human decision correct | 062e252 |
| **C70** | Whole-file ceiling forced model to delete 39 tests (315 del) to "fit" | MODEL + HARNESS | cost ≠ size of host file | `replace` op, exact-unique match, flat 8k emission + result bound, atomic set | edit parser + diff scope | 57 tests + 2 vacuous-green tests found & fixed | 5 mutations (1 survived until test rewritten) | YES (TS-110 on code-edits/3) | deleting-to-fit structurally closed | does not decide semantic correctness | 8175bee, concerns.md:3370 |
| **C71** | `CancelledError` bypasses `except Exception`; run stranded. RUN-000008 **mis-attributed, corrected** | SUPERVISOR (+observability) | HTTP request loss must not strand durable run | named `CancelledError` handler → sync settle + generation fence + narrow BaseException | state-machine txn + finish fence | `test_concern_71_cancellation.py` (13, no sleeps) | 6 mutations; **E NOT CAUGHT** (reported) | PARTIAL (test-only; live case reclassified to C67) | test-settled WORKFLOW_CANCELLED; live cancel not observed | crash-to-stranded ≠ cancellation | da4c5c3, concerns.md:3634 |
| **C72** | Model hit 8k ceiling only in post-hoc rejection feedback (RUN-000008, 15.5kB edit) | SUPERVISOR/HARNESS | act-on-able constraints disclosed pre-generation | prompt reads authoritative `MAX_TARGETED_EDIT_PAYLOAD_BYTES`; UTF-8 accounting; placeholder ban | prompt render, single constant | `test_concern72.py` (added-first, 6 baseline fails) | contract mutations (constant→37, removals) | YES (indirect — TS-110 prompt/3, 0 rejected) | no over-limit TS-110 attempt | result/scope ceilings stay downstream | 99fce0a, concerns.md:3879 |
| **C73** | `COMPLETED_BY_HAND` human commit not in baseline; downstream would miss it; real conflict occurred | SUPERVISOR + OPERATOR | no COMPLETE unless human work durably in baseline; conflict = auditable merge preserving human commit | `human_commit` col + `integrate_human_commit` + pre-COMPLETE + reconcile; conflict ext `human_resolution` + `integration_resolution_commit` | integration svc (merge→verify→advance) | `test_concern73.py` (19) + conflict + invariant tests | not presented | YES (e8b9eae merge, human cbff2c4 2nd parent) | human work in baseline, unchanged, TS-110 built on it | ≠ C76 approval/delivery crash; not semantic judgement of human fix | 7a4e9f0, 560da40, C73 impl doc |
| **C74** | Historical DB **lost** — harness could destructively touch runtime DB | TEST/HARNESS (data loss) | destructive op requires proven isolated test DB, else zero SQL | `assert_test_database_safe` fail-closed + safe prefixes + marker | conftest engine + every destructive op | `test_concern74_db_safety.py` + rejection paths | not presented (positive-assert guard) | YES (precondition; C75 verify drops `verify_*` under the guard) | runtime isolated from disposable DB | not a backup; not a reconstruction | e10e589, db_safety.py, README:265 |
| **C75** | After DB loss, no trusted verified recovery point; backup tool must work host or compose | INFRASTRUCTURE + OPERATOR-PROC | a recovery point is trusted only after isolated restore + compare; never evict last good | `backups.py` (host→compose fallback) + metadata SHA-256 + `verify-backup` into disposable `verify_*` + retention 7 + reconstruction importer | backup/restore svc + C74 guard | `test_database_backups.py`, `test_reconstruction_importer.py`, C73 contract | not presented | YES (both backups verified; `4a79e0da` isolated verify DB) | verified recovery point before & after C76 | reconstructed ≠ original history; no live-crash survival | 10380bc, 416bb01, README `## Database backups` |
| **C76** | Reviewer approved + candidate committed + process died in delivery txn; container-path worktree unresolvable on host | MIXED SUPERVISOR + ENVIRONMENT | approved committed candidate owed delivery, no model/attempt/TaskRun; repair only disposable integration wt | `RecoveryMode.DELIVERY_ONLY` + derived candidate (fail-closed) + normal `deliver` re-entry + idempotent integrate + namespace worktree repair (integration only) | recovery assessment + gen fence + `contains_commit` + namespace repair | `test_concern76.py` (767; refusal battery + end-to-end) | not presented (refusal/idempotency battery) | **YES — terminal TS-110 event** | 4588bba FF-landed once; DB `model_runs=2`, no extra run | refuses on broken **task** wt / derivation fail / owner held / disagreement | b2e137f, run_recovery.py:130 |

---

## PART 3 — Campaign failure taxonomy (independent of concern numbering)

Each entry: ORIGIN / DETECTION / CONTAINMENT / RECOVERY / DATA LOSS. Detection,
containment and recovery are kept distinct.

1. **Whole-file edit makes a small change unaffordable → model deletes unrelated
   work.** (RUN-20260928-000007) ORIGIN: MIXED (MODEL action, HARNESS representation).
   DETECTION: scope guard on the diff (413 lines / 315 deletions) + the arithmetic
   ceiling (15,106 > 14,766). CONTAINMENT: scope guard refused the attempt, so the
   destructive edit was never integrated. RECOVERY: not needed for integration
   (refused); the design gap was closed by C70's `replace`. DATA LOSS: **NONE** (target
   repo untouched by the refused attempt; the *attempt* was lost, not durable state).

2. **Provider timeout outlives the DB transaction.** (RUN-20260928-000005) ORIGIN:
   SUPERVISOR (txn lifetime) triggered by ENVIRONMENT (provider > idle-txn timeout).
   DETECTION: `ModelTimeout` + `IdleInTransactionSessionTimeout`. CONTAINMENT: run
   stayed well-formed RUNNING; no false integration. RECOVERY: C66 durable recording,
   then C67 recovery, then C68 settlement path. DATA LOSS: **PARTIAL** — the attempt-2
   `model_runs` **row** was lost (artifact dir survived).

3. **Targeted-edit protocol violation (over-limit `replace`).** (RUN-20260928-000008
   recovery attempt 2, 15,547 B) ORIGIN: SUPERVISOR/HARNESS (the actionable 8 KB
   constraint was not disclosed before generation). DETECTION: parser rejected
   atomically (15,547 > 8,000). CONTAINMENT: atomic refusal, no partial edit. RECOVERY:
   C72 moved the limit into the prompt; the attempt budget was consumed before the
   model was told the constraint. DATA LOSS: **NONE**.

4. **Scope violation vs ceiling (whole-file attempt).** (RUN-20260928-000007/000004)
   ORIGIN: SUPERVISOR/HARNESS (feedback taught deletion-to-fit). DETECTION: scope guard.
   CONTAINMENT: SCOPE_VIOLATION terminal reason recorded (task_runs). RECOVERY: C70.
   DATA LOSS: **NONE**.

5. **Semantic/verification failure (`BUILD_FAILED`).** (RUN-000005 attempt 1) ORIGIN:
   MODEL. DETECTION: deterministic verification gate. CONTAINMENT: routed SEND_TO_CODER,
   never reached review. RECOVERY: fix loop / budget. DATA LOSS: **NONE**.

6. **Retry/attempt-budget exhaustion → stranded RUNNING run.** (RUN-000005/000006)
   ORIGIN: SUPERVISOR (recovery gate refused settlement-only re-entry). DETECTION:
   `GET …/recoverability` `attempt_accounting_reconstructable` fail. CONTAINMENT: run
   stayed RUNNING, no model re-called. RECOVERY: C68 `settlement_only`. DATA LOSS:
   **NONE** (the `RETRY_EXHAUSTED` escalation was the owed outcome).

7. **Client cancellation stranding durable state.** (hypothetical, RUN-000008
   *mis-attributed then corrected*) ORIGIN: SUPERVISOR (BaseException taxonomy).
   DETECTION: tests reproducing an observed `CancelledError`; **not** observed in the
   live RUN-000008 row. CONTAINMENT: n/a live (case reclassified to C67 recoverability).
   RECOVERY: C71 named handler (test-evidence). DATA LOSS: **NONE**.

8. **Human completion (`COMPLETED_BY_HAND`) with human commit not integrated →
   integration conflict.** (TS-109 / escalation fc9bf9b0) ORIGIN: MIXED (OPERATOR human
   fix, SUPERVISOR missing integration ordering). DETECTION: dependency baseline check;
   real merge conflict on integration. CONTAINMENT: human commit preserved as second
   parent; agent line not silently overwritten. RECOVERY: C73 `integrate_human_commit` +
   operator-resolved conflict contract → `e8b9eae`. DATA LOSS: **NONE** (human commit
   preserved unchanged).

9. **Test-harness destructive op reaches runtime DB.** ORIGIN: TEST/HARNESS.
   DETECTION: after the fact (history gone). CONTAINMENT: none at the time (this was the
   failure). RECOVERY: filesystem-artifact reconstruction (C75 importer); C74 fail-closed
   guard added to prevent recurrence. DATA LOSS: **YES** — the original PostgreSQL
   campaign history was lost (manifest: "NOT ORIGINAL DATABASE HISTORY").

10. **Backup tooling absent / not verifiable / host-only binaries.** ORIGIN:
    INFRASTRUCTURE + OPERATOR-PROC. DETECTION: recovery need after loss. CONTAINMENT:
    (post-C75) isolated `verify_*` restore under C74 guard. RECOVERY: `verify-backup` +
    retention. DATA LOSS: historical **YES**; post-C75 points **NONE**.

11. **Reconstruction-after-loss fidelity limit.** ORIGIN: INFRASTRUCTURE (loss) +
    TEST/HARNESS (reconstruction). DETECTION: manifest's own `omitted_history`
    (model_runs, run_events, reviews, verification_runs, training_examples, lessons,
    workflow_*). CONTAINMENT: `SYNTHETIC` classification tagging reconstructed identities;
    alembic-head requirement gate. RECOVERY: importer inserts evidence-backed rows.
    DATA LOSS: **PARTIAL** — some per-event history could not be recovered and remains
    reconstructed/absent (not re-derived to the original).

12. **Post-approval / pre-delivery supervisor crash + host/container worktree namespace
    mismatch.** (TS-110, TaskRun c31db56e) ORIGIN: MIXED (SUPERVISOR recovery gap +
    ENVIRONMENT path namespace). DETECTION: stranded durable state — task `APPROVED`,
    run `RUNNING`, no owner, `candidate_commit=NULL`, clean committed worktree at
    candidate; `WorktreeUnusable` for foreign-namespace metadata. CONTAINMENT: nothing was
    falsely integrated at crash time; candidate preserved only in the task worktree; the
    `_integration` disposable worktree was the only thing repaired. RECOVERY: C76
    `delivery_only` derivation → normal `deliver` node → one fast-forward integration to
    `4588bba`. DATA LOSS: **NONE** (approved candidate survived in Git; candidate
    delivery is evidenced by a single final fast-forward ref state and no extra
    model/run counts).

13. **Host-run `/health` reporting `source_revision=unknown/dev`.** ORIGIN:
    OPERATOR/PROCEDURE + ENVIRONMENT (a host-run process carries developer-build defaults
    when `SOURCE_REVISION` is not baked). DETECTION: `health.py`/`_build_meta.py`
    default + the final backup metadata `source_revision:"unknown/dev"`. CONTAINMENT:
    provenance established externally from a clean checkout/process instead (out-of-band),
    so the campaign could proceed on a known checkout. RECOVERY: not an in-band defect.
    DATA LOSS: **NONE**. *(Inference boundary: the campaign's reliance on external
    provenance is inferred from the presence of `RECOVERY_PROMPT.md`/fence conventions,
    not asserted by a code mechanism.)*

**Distinct campaign failures catalogued: 13.**

---

## PART 4 — Safeguards with evidence beyond design intent

Format: safeguard · intended invariant · test evidence · real-campaign evidence ·
what it contained · what it did NOT protect against.

1. **Execution-generation fencing.** Invariant: ≤1 effective executor per run. Test:
   C67 `acquire_execution`/`require_in_flight` + PG Barrier races; C68 two simultaneous
   settlements → one owner; C71 superseded-dispatch-cannot-settle; C76
   `test_generation_fencing_blocks_a_superseded_executor`. Real campaign: C67's first
   live recovery (gen 0→1); C76's TS-110 single-delivery outcome is corroborated by Git
   topology and durable whole-DB counts, while its gen 1→2 remains a canonical prior
   observation not independently separable here. Contained: duplicate executors / double
   settlement / stale-owner persistence. Not protected: a liveness claim is deliberately
   **not** encoded in the owner token (it is a held marker only).

2. **Settlement-only recovery (C68).** Invariant: spent budget with pending settle is
   recoverable to its owed terminal without a model. Test: 25 witness-based tests;
   PostgreSQL simultaneity. Real: discovered live on RUN-000005. Contained: permanent
   stranding after exhaustion. Not protected: does not integrate a candidate.

3. **Delivery-only recovery (C76).** Invariant: approved+committed candidate owed
   delivery. Test: `test_concern76.py` refusal battery + end-to-end. Real: TS-110 terminal
   event — durable Git `4588bba` FF from `e8b9eae`; final backup `model_runs=2`,
   `task_runs=8`. Contained: model-free re-delivery with a single observed fast-forward
   integration and no extra model/run counts. Not protected: refuses on broken **task**
   worktrees, derivation failure, held dispatch.

4. **Deterministic build/lint/test verification gate.** Invariant: only verified
   candidates integrate. Test: `test_verification_pipeline.py`, per-run
   `verification.json`. Real: TS-110 `passed:true (3 commands, 6 checks)`; RUN-000005
   `BUILD_FAILED` routed to coder. Contained: unverified code never reached review. Not
   protected: semantic correctness (reviewer's job).

5. **Reviewer separation + approval provenance.** Invariant: approval recorded durably.
   Test: review tests; C76 requires an approving review before `delivery_only`. Real:
   TS-110 `review.json decision=APPROVED`. Contained: delivery-only recovery only trusts
   a genuinely approved candidate. Not protected: the human's decision quality.

6. **Scope enforcement on the resulting Git diff.** Invariant: change ⊆ declared scope.
   Test: `test_scope_guard.py`, C70 scope-driven tests. Real: TS-110 `scope_decision:
   ALLOW`, `diff_lines:36`, `files_changed:2` within allowance; RUN-000007 SCOPE_VIOLATION
   caught. Contained: over-broad/destructive diffs. Not protected: within-scope but
   wrong implementation.

7. **Targeted-edit atomicity + exact-unique match.** Invariant: no partial/ambiguous
   edit. Test: C70 rollback tests; mutation A. Real: code-edits/3 in force (TS-110).
   Contained: silent near-match corruption. Not protected: refusing a change the model
   could have approximated (deliberate trade).

8. **Human-commit integration provenance (C73).** Invariant: no COMPLETE without human
   work in baseline; conflict = auditable merge preserving human commit. Test: 19 C73
   tests + conflict contract. Real: `e8b9eae` merge with `cbff2c4` preserved as 2nd
   parent, `provenance:"human"`. Contained: dependent tasks starting from a baseline
   missing human work. Not protected: correctness of the human fix itself.

9. **Candidate derivation & integration idempotency.** Invariant: a replayed delivery
   does not move the ref twice. Test: `test_integrate_candidate_is_idempotent_when_already_
   in_baseline`; C73 already-integrated check. Real: `contains_commit` guard on the live
   integration. Contained: double integration after a mid-delivery crash. Not protected:
   concurrent divergent integrations beyond the ref-move-last rule.

10. **Canonical ref readback.** Invariant: durable provenance read from the canonical
    ref, not assumed. Test: C76 derives from `agent/integration` / worktree HEAD and
    cross-checks `candidate_commit`. Real: `agent/integration`==`4588bba`==TS-110 branch
    tip observed directly. Contained: candidate/ref divergence mis-settlement. Not
    protected: a corrupted canonical ref itself.

11. **Test-database isolation (C74).** Invariant: destructive op requires proven isolated
    DB. Test: `test_concern74_db_safety.py` + rejection paths. Real: C70/C71/C72 runs used
    isolated `test_*`/`test_c7*_validation` DBs; C75 dropped `verify_*` under the guard.
    Contained: recurrence of the runtime-DB destruction. Not protected: the loss that
    already happened.

12. **Backup + restore verification (C75).** Invariant: recovery point trusted only after
    isolated restore+compare. Test: `test_database_backups.py`, reconstruction importer
    tests. Real: two backups exist, SHA-256 match, `verified_at` set,
    `source_counts_compared=true`, isolated `verify_a552f9a9394c`. Contained: unverified
    recovery points and host-binary unavailability. Not protected: reconstructed ≠
    original pre-loss rows.

13. **Reconstruction importer (C75).** Invariant: durable state can be re-derived from
    filesystem evidence to a required alembic head, tagging synthetic identities. Test:
    `test_reconstruction_importer.py` + C73 contract. Real: manifest produced the current
    reconstructed runtime (alembic `c1f4a7d29b60`, 1 project, 10 tasks, 8 runs). Contained:
    total state loss. Not protected: `omitted_history` detail (model_runs/run_events/etc.
    are reconstruction, not originals).

14. **Cancellation settlement naming (C71).** Invariant: cancellation settles durably,
    non-cancellation exceptions are not mislabelled. Test: 13 tests. Real: **none
    observed** (test-evidence only; RUN-000008 reclassified away). Contained (by tests):
    stranded RUNNING under cancellation. Not protected: an actual crash (only cooperative
    cancellation).

15. **Attempt / retry accounting charged at evidence boundaries.** Invariant: attempt
    charged when a model is asked; cycle when a reviewer answers; reconstruction reads
    pre-call artifacts so a lost DB row is not double-counted or missed. Test: C67/C68
    reconstruction + witness tests. Real: RUN-000005 reconstructed to next=3 then 4 with
    the lost attempt-2 counted once from its surviving artifact dir. Contained:
    accounting drift after partial failure. Not protected: an unrecorded call that leaves
    neither a row nor an artifact.

---

## PART 5 — Derived architectural invariants (clustering)

Confidence basis labels: **DESIGNED** (mechanism exists) · **TESTED** (unit/integration
test) · **CAMPAIGN-EXERCISED** (a real campaign event) · **NOT ESTABLISHED**. Not scored.

### A. MODEL CONTAINMENT
- Invariant: a model may only ever *propose*; every proposal passes deterministic scope,
  edit-protocol, and verification gates before it can affect durable state, and a
  representation must never make "remove the work" the cheapest legal response.
- Contributing concerns: C70, C72, C69 (option truthfulness), plus gates used by C73/C76.
- Implementation: `domain/edits.py`, `services/code_edits.py`, `domain/scope.py`,
  `services/verification.py`, `render_coding_instructions`.
- Tests: C70 (57 + mutation), C72 (added-first contract), C69.
- Campaign evidence: RUN-000007 deletion-to-fit (contained by scope guard); TS-110 clean
  36-line in-scope change under `code-edits/3`.
- Boundary: cannot certify semantics; only containment of invalid/destructive proposals.
- Basis: **CAMPAIGN-EXERCISED** (containment held on a real model failure).

### B. DURABLE EXECUTION
- Invariant: durable state transitions and external I/O are separated by transaction
  boundaries, and exactly one executor's writes may land, enforced by a monotonic
  generation fence — independent of liveness.
- Contributing concerns: C66, C67, C71.
- Implementation: pre/post-call commit + `require_in_flight(expected_generation)`,
  `acquire_execution`/`release_execution`, named cancellation handler.
- Tests: C66 `test_fix_loop_resume`/`test_db_session_cleanup`; C67 (65, PG races);
  C71 (13).
- Campaign evidence: RUN-000005 provider timeout recorded durably; C67 live gen0→1.
- Boundary: does not itself decide what a stranded run is *owed* (→ Recovery).
- Basis: **CAMPAIGN-EXERCISED** (C66/C67), **TESTED** for C71.

### C. RECOVERY / REPLAY SAFETY
- Invariant: the stranded-state classes modeled, tested, or campaign-exercised by
  C67/C68/C76 are either safely resumable, deterministically settleable, or
  delivery-recoverable — and each supported recovery mode is derived from evidence,
  fails closed, creates no new TaskRun/attempt, and reuses the normal terminal paths
  (no second copy of policy).
- Contributing concerns: C67, C68, C71 (settlement), C76.
- Implementation: `assess_recoverability`, `RecoveryMode{continue,settlement_only,
  delivery_only}`, idempotent `_commit_or_reconcile`/`integrate_candidate`.
- Tests: C68 (25), C71 (13), C76 (767-line battery + end-to-end), C67 (65).
- Campaign evidence: settlement_only discovered live (C68); delivery_only terminal on
  TS-110 (C76) with durable `model_runs=2`/`task_runs=8` and single FF integration.
- Boundary: does not claim exhaustive crash-state coverage; does not rebuild reviewed
  **task** worktrees; concurrency beyond the single guarded UPDATE is not proven.
- Basis: **CAMPAIGN-EXERCISED** (settlement_only + delivery_only both hit live).

### D. INTEGRATION / PROVENANCE
- Invariant: only approved, verified, in-scope work reaches `agent/integration`; the ref
  moves last; every integration records typed provenance (model candidate vs human commit
  vs integration-resolution), and dependent tasks start from a baseline that contains all
  prior accepted work.
- Contributing concerns: C73, C76, C70's scope gate.
- Implementation: `integrate_candidate`, `integrate_human_commit`,
  `integration_resolution_commit`, `contains_commit` idempotency, canonical ref readback.
- Tests: C73 (19 + conflict + invariant), C76.
- Campaign evidence: `e8b9eae` merge (human `cbff2c4` preserved 2nd parent) then TS-110
  `4588bba` FF — a full provenance chain in Git.
- Boundary: preserves and integrates human work faithfully; does not judge its
  correctness.
- Basis: **CAMPAIGN-EXERCISED** (the merge + FF are real durable artifacts).

### E. CONTROL-PLANE SURVIVABILITY
- Invariant: the operator/control plane now fails closed for destructive test-database
  operations, maintains verified recovery points, can verify backup restoration in
  isolated databases, and can reconstruct the subset of state represented by surviving
  evidence while keeping reconstructed history explicitly distinguishable from original
  PostgreSQL history.
- Contributing concerns: C74, C75, C71 (client-loss independence).
- Implementation: `db_safety.assert_test_database_safe`, `services/backups.py`,
  `reconstruction_importer.py`, cancellation settle.
- Tests: `test_concern74_db_safety.py`, `test_database_backups.py`,
  `test_reconstruction_importer.py`.
- Campaign evidence: **the original PostgreSQL campaign history was lost and the
  campaign was reconstructed to alembic `c1f4a7d29b60`; verified backups (isolated
  `verify_*` restores) exist around the terminal recovery.**
- Boundary: reconstruction is provably *reconstructed, not original* (manifest
  `omitted_history`); verified-dump ≠ verified-original-history.
- Basis: **CAMPAIGN-EXERCISED** for the DB-loss failure and the guard/backup response;
  the reconstruction subset is **TESTED** + attested by artifacts, not evidence of
  seamless database survival.

### F. ENVIRONMENT / NAMESPACE PORTABILITY
- Invariant: supervisor-owned disposable state (the `_integration` worktree) may be
  rebuilt within the current namespace; reviewed/task state must never be silently
  regenerated to paper over a namespace mismatch; and host-vs-container path portability
  must be a considered axis of delivery.
- Contributing concerns: C76; adjacent host-run provenance behaviour (`unknown/dev`).
- Implementation: `_usable_or_recreated_integration_worktree` (integration-only repair),
  `WorktreeUnusable`, task-worktree refusal, external provenance establishment.
- Tests: `test_concern76.py` namespace set (`…_is_unusable_without_repair`,
  `…_repairs_stale_integration_metadata`, `…_never_touches_an_unrelated_worktree`).
- Campaign evidence: TS-110's crash root cause was the container-path worktree
  incompatibility; the recovered integration touched only the disposable `_integration`
  worktree.
- Boundary: repairs only disposable supervisor worktrees; the general host↔container path
  issue for task worktrees is a refusal, not a fix.
- Basis: **CAMPAIGN-EXERCISED** (C76) for the integration-worktree case; host-run
  `unknown/dev` behaviour is **NOT ESTABLISHED** beyond a single observed metadata field.

---

## PART 6 — Case studies

### TS-109 — "Keep only the entries from one navigation source"
Project `d98cb1e7-75e4-401a-8b21-d0965b0b3115`. Durable evidence: `git` branches
`agent/TS-109-…-run1..run7` (all at `fc6abc5`), reconstruction manifest `ts109_runs`,
`e8b9eae`/`cbff2c4`, and concerns.md C66/C67/C68/C70/C71/C73.

- **Model attempts / failures (MODEL).** Seven runs. `RUN-20260928-000004` and
  `RUN-20260928-000007` → `SCOPE_VIOLATION` at attempt 2 (RUN-000007: model deleted 39
  existing tests to fit the whole-file ceiling — C70). `RUN-20260928-000005` and
  `RUN-20260928-000006` → `RETRY_EXHAUSTED` at attempt 3. `RUN-20260928-000005`
  additionally is the transaction-lifetime stranding of C66 (attempt-2 provider timeout)
  and the settlement_only discovery of C68.
  - *Model failures:* semantic/destructive edits, budget exhaustion.
  - *Supervisor behavior (correct):* verification gate routed `BUILD_FAILED`→SEND_TO_CODER;
    scope guard refused the 315-deletion edit; durable recording survived the provider
    timeout; recovery/gen-fence kept a single executor; accounting reconstructed the lost
    attempt-2 row from its surviving artifact directory (counted once).
- **Review / escalation.** Budget spent → fix-loop `settle(ESCALATED, RETRY_EXHAUSTED)` →
  `HUMAN_REVIEW`, one `OPEN` `HumanEscalation` (`fc9bf9b0-9683-41fa-aaa8-f8f53567b92a`),
  reason `RETRY_EXHAUSTED` ("3 of the task's 3 permitted attempts were made"). (This
  escalation is the one C69's option-truthfulness and C73's human path concern.)
- **Human completion (OPERATOR).** The person implemented `filterBySource` by hand and
  committed it to `master` as `cbff2c4bd919b860c73e3cb061bccff11789c37a`
  (C73 `COMPLETED_BY_HAND` + `human_commit`).
- **Historical provenance.** C73 records `cbff2c4` on the escalation's `human_commit`.
- **Integration conflict → C73 resolution.** `cbff2c4` conflicted with the agent line
  (baseline `fc6abc5`). Operator resolved the conflict; C73 wrote
  `e8b9eae300363a8a6020220c7f31a53729b4ad3b` as the `integration_resolution_commit` — a
  **two-parent merge** (`1st fc6abc5`, `2nd cbff2c4` **preserved unchanged**) with
  `provenance:"human"`, resolution scope `navigation-stack.ts` + its test. This is the
  `agent/integration` advance that carried the human work.
- **Final canonical integration.** `agent/integration` advanced to `e8b9eae` for TS-109;
  that became TS-110's `starting_commit`.
- **Distinguished:** model failures = SCOPE_VIOLATION/RETRY_EXHAUSTED/deletion-to-fit;
  supervisor = gating, durable recording, single-owner recovery, conflict as auditable
  merge; human = authored `cbff2c4`; integration = preserved the human commit and made it
  the baseline for TS-110.
- *(Provenance caveat: individual TS-109 run row counts/`run_events` are **reconstructed**
  per the manifest — the original DB history was lost.)*

### TS-110 — "Describe the oldest entry the stack still holds"
Task `4abc278d-57c4-515a-baef-874e1c89aa7a`; TaskRun `c31db56e-4962-4c3b-942b-20ae768b732a`
(`RUN-20260930-000001`); project `d98cb1e7-…`. Durable evidence:
`RUN-20260930-000001/*.json`, `git` `agent/TS-110-…-run1`==`agent/integration`==`4588bba`,
final restore-verified backup metadata.

- **Single model attempt → coder success.** attempt 1, `attempts_used:1`.
  Coder `m4ven/Qwen3-Coder-30B…` (model id `9618685d-…`), prompt
  `coder-prompt/3+code-edits/3`. Diff: `oldest()` added, 36 lines, 2 files,
  `scope_decision:ALLOW`, `rejected_edits:[]`, `unplanned_paths:[]`.
- **Verification success.** `verification.json`/`fix-loop.json`:
  `passed:true` (3 commands, 6 checks — build/lint/tests logs present under the run dir).
- **Reviewer approval.** `review.json` `decision=APPROVED`, 0 blocking issues; review
  summary: `oldest()` returns oldest or undefined, does not mutate the stack.
  → task `APPROVED`, `review_cycles:1`.
- **Delivery crash → stranded durable state.** (Prior recovery session established the
  stranded shape; C76's code docstring describes it.) task `APPROVED`, run still
  `RUNNING`, `execution_owner=NULL`, `candidate_commit=NULL`, clean committed worktree
  holding `4588bba`; root cause = host run could not resolve the container-path linked
  worktree metadata. *(This specific crash event's live rows are attested by the
  canonical-state description + the resulting durable artifacts, not by a run event this
  review read directly.)*
- **C76 assessment → delivery-only recovery.** `assess_recoverability` returned
  `recovery_mode=delivery_only`; `_assess_stranded_delivery_candidate` **derived** the
  candidate from the worktree HEAD and validated ancestry (from `starting_commit`
  `e8b9eae`), scope, approving review, passing gates, non-contradiction with recorded
  `candidate_commit` (NULL). Only the disposable `_integration` worktree was
  repaired/recreated for the current namespace.
- **generation 1 → 2.** This is the canonical prior observation. In this inspection, the
  recovery reacquisition path is directly supported by the C67/C76 implementation and
  tests; the final TS-110 outcome is corroborated by durable Git topology and whole-DB
  counts. The exact per-TS-110 generation transition was not independently re-derived
  because live row-level PostgreSQL evidence was unavailable.
- **No additional model calls.** Durable: final backup **`model_runs=2`** whole-DB — the
  coder + reviewer of attempt 1; recovery added zero model calls.
- **No additional attempt / TaskRun.** Durable: `attempts_used:1`, `review_cycles:1`;
  final backup **`task_runs=8`** with a single TS-110 run (only one
  `agent/TS-110-…-run1` branch exists); no replacement run.
- **Fast-forward integration.** `4588bba` has a **single parent** `e8b9eae` == the run's
  `starting_commit` == the TS-110 branch tip → `agent/integration` **fast-forwarded**
  (no merge commit).
- **Terminal settlement.** `outcome.json` `run_status:SUCCEEDED`, `outcome:accepted`,
  `failure_reason:null`; `candidate_commit:4588bba`.
- **Restore-verified final backup.** `orchestrator-20260930T215132Z-4a79e0da.dump`
  (SHA-256 `38101df1…`), alembic `c1f4a7d29b60`, `verified_at=20260930T215139Z`,
  restored into isolated `verify_a552f9a9394c`, `source_counts_compared=true`.

**What TS-110 demonstrates about recovery that a normal successful run would not:** a
run that only ever reaches `deliver` once proves the *happy path*. TS-110 proves the
supervisor can be interrupted **between approval and integration** — after durable
reviewed work already exists — and still land that exact work **without any further
model inference, without a new attempt, and without a new TaskRun**, as established by
Git topology, run artifacts, and whole-DB durable counts. The mechanism re-derives the
candidate from supervisor-owned Git state and re-enters the ordinary delivery node
idempotently; the canonical per-run generation/event details remain as bounded in §7.
It is the campaign's evidence for the Recovery/Replay-Safety invariant (C76) *and* for
the Environment/Namespace invariant (integration-only worktree repair) in one terminal,
durable-artifact-backed event.

---

## PART 7 — Evidence gaps (claims we might want but cannot yet prove)

Each: desired claim · evidence available · why insufficient. Not recommendations.

1. **Two simultaneous recovery operators against a live terminal-stranded run.**
   Desired: exactly one delivery-only settlement under real concurrency. Available:
   C68/C67 PostgreSQL Barrier tests (settlement/ownership) and C76 single-UPDATE fence.
   Insufficient: the concurrency tests exercise settlement_only/ownership, but there is
   **no documented simultaneous `delivery_only` acquisition race on a live-shaped run**;
   delivery_only's dual-acquisition is argued from the shared guard, not directly raced.

2. **Provider death at *each* durable checkpoint (create/pre-call/post-call/deliver).**
   Desired: any checkpoint's death yields correct reconstruction. Available: C66/C67/C68
   checkpoint reconstruction for pre/post-provider. Insufficient: death precisely
   *inside* the delivery transaction (the C76 shape) is only reproduced as a seeded
   fixture state, not driven by a real fault injected at that line.

3. **Exact per-TaskRun generation and event counts on the live runtime.**
   Desired: independently confirm TS-110 `execution_generation=2` and `run_events=16`.
   Available: canonical-state assertion; final backup shows whole-DB `run_events=17`,
   `model_runs=2`, `task_runs=8` (consistent but not per-run). Insufficient: this
   inspection environment had **no reachable PostgreSQL client and no live service on
   the documented port**; per-run columns cannot be read without restoring the dump
   (prohibited here), so gen 1→2 and the per-run event split remain
   **canonical-asserted / not independently separable**.

4. **Behaviour under multiple concurrent projects / shared integration namespace.**
   Desired: recovery/integration are per-project-correct under concurrency. Available:
   per-project `integration_worktree_path(project_id)` design + worktree namespacing.
   Insufficient: only one project (`d98cb1e7…`) existed in the campaign; no multi-project
   exercise recorded.

5. **Very large multi-file targeted change sets.** Desired: atomicity/scope hold at large
   N. Available: C70 atomic-set tests with a few edits. Insufficient: nothing at the
   scale/edge of many files/edits near ceilings was campaign-exercised.

6. **Repeated / cascading integration conflicts.** Desired: N conflicts resolve
   idempotently in sequence. Available: single real conflict (`e8b9eae`) + C73 tests.
   Insufficient: exactly one live conflict occurred; recurrence not exercised.

7. **Restart during human-resolution reconciliation (C73 reconcile endpoint).**
   Desired: a crash mid-`reconcile-human-commit` is recoverable/idempotent. Available:
   reconciliation is documented idempotent-rejecting-if-done. Insufficient: no campaign
   event or explicit crash-injection test for reconciliation is present in reviewed files.

8. **Full fidelity of reconstructed history vs originals.** Desired: reconstructed rows
   equal the lost originals. Available: importer contract tests + `SYNTHETIC` tags.
   Insufficient: manifest lists `omitted_history` (model_runs/run_events/reviews/…); by
   the manifest's own statement these are **not** the original rows.

9. **Cooperative cancellation actually observed in production.** Desired: a live client
   disconnect settled a run. Available: C71 tests (observed `CancelledError` in-harness).
   Insufficient: the sole candidate live run (RUN-000008) was **explicitly reclassified**
   as a non-cancellation `ModelTimeout`; no in-campaign cancellation settlement exists.

10. **Host-run provenance under a real (non-default) SOURCE_REVISION.** Desired: the
    host-run path reports the true revision. Available: `_build_meta` default
    `unknown/dev`; final backup shows `unknown/dev` was actually recorded. Insufficient:
    no campaign artifact shows a host-run process reporting a real SHA; the true-revision
    path is untested in this environment (build/deploy freshness is proven for the
    *container* path at `c748543…`, not the host path).

11. **Idempotency of delivery_only across a crash *after* ref-advance but *before* event
    write.** Desired: replay neither re-moves the ref nor double-writes events.
    Available: `contains_commit` + `_commit_or_reconcile` idempotency +
    `test_integrate_candidate_is_idempotent_when_already_in_baseline`. Insufficient: the
    precise crash window between ref move and terminal event is argued from the guards,
    not directly reproduced end-to-end against live durable state.

**Evidence gaps catalogued: 11.**

---

## PART 8 — Closing classification (no recommendations)

### A. Facts established by the campaign (durable artifacts in Git / run files /
verified backups)
- The orchestrator mechanism commits for C66–C76 are all ancestors of HEAD `b2e137f`.
- TS-110 candidate `4588bba` is `agent/integration` in `tracestack-clean`, with a single
  parent `e8b9eae` (= TS-110 `starting_commit`) → **fast-forward** integration.
- TS-109's human commit `cbff2c4` (== `master`) is preserved unchanged as the **second
  parent** of `e8b9eae`, a merge commit named the `integration_resolution_commit` for
  escalation `fc9bf9b0…` → human work carried into the baseline the way C73 specifies.
- The TS-110 run artifacts record attempt 1, verification pass (6 checks), reviewer
  `APPROVED`, `run_status SUCCEEDED`, 2 model calls, in-scope 36-line 2-file change,
  `rejected_edits:[]`.
- The final runtime backup is restore-verified into an isolated `verify_*` DB at alembic
  `c1f4a7d29b60`, `source_counts_compared=true`, `model_runs=2`, `task_runs=8`,
  `human_escalations=1`, `reviews=1`.
- The original PostgreSQL campaign history was **lost** (manifest statement) and the
  current runtime is **reconstructed**; synthetic identities are tagged as such.
- Host-run `/health`/backup metadata carried `source_revision=unknown/dev`.

### B. Facts strongly supported but not fully campaign-exercised
- C71 cancellation settlement: strong in-harness evidence (13 tests, no sleeps), but the
  one candidate live run was **reclassified away** from cancellation.
- Concurrent-operator recovery under `delivery_only`: supported by the shared
  single-`UPDATE`/generation guard and C67/C68 PG races, not directly raced in the
  delivery_only shape.
- Reconstruction importer fidelity: contract-tested and artifact-attested, but by its own
  statement not equal to original history (`omitted_history`).

### C. Things that remain unknown (given available evidence)
- Exact per-TaskRun `execution_generation` (claimed 1→2) and the per-TS-110 `run_events`
  split (project-wide count is 17) — not separable without restoring the dump.
- Whether any real (not seeded) fault at the delivery-transaction line was exercised.
- Behaviour under multiple concurrent projects or repeated integration conflicts.
- A host-run process reporting a true (non-default) source revision.

### D. Observed issues already adequately contained by current safeguards
- Whole-file deletion-to-fit (C70 scope guard + representation) — the destructive edit
  never integrated.
- Over-limit targeted edits (C70/C72 parser) — atomic refusal, no partial write.
- Provider-timeout transaction stranding (C66) — failure durably recorded, run
  well-formed; the run was later recovered and settled (C67/C68).
- Post-approval/pre-delivery crash (C76) — Git topology shows a single fast-forward of
  the approved candidate, and durable whole-DB counts show model-free delivery without a
  new run/attempt.
- Human/agent integration conflict (C73) — resolved as an auditable merge preserving the
  human commit unchanged.
- Runtime-DB destruction recurrence (C74) + recovery points (C75) — isolated
  `verify_*` restores, verified SHA-256, retention.

### E. Observed issues that remain outside current safeguards
- The **general host↔container worktree namespace problem for *task* worktrees**: C76
  repairs only the disposable `_integration` worktree and *refuses* to rebuild a
  namespace-broken task worktree — the underlying path-portability defect for task
  worktrees is a refused condition, not a fixed one.
- **Original-history fidelity after DB loss**: no safeguard restores lost per-event
  history; the importer only reconstructs evidence-backed rows and marks the rest omitted.
- **Cooperative-only cancellation**: a hard process crash (not `CancelledError`) is not
  settled by C71; it relies on C67/C76 recovery of the stranded state instead.
- **Model semantic correctness**: containment gates (scope/verification/review) bound
  shape and behaviour, but no safeguard guarantees a within-scope, passing implementation
  is semantically right — that remains reviewer/human judgement.

---

### Method & integrity notes for this review
- Read-only throughout: git object/ref/worktree queries, `git log/show/merge-base`,
  file reads, `sha256sum`, and JSON metadata reads. No file edited, no `git add`/commit/
  push, no checkout of another revision in the real repository, no ref reset, no
  TraceStack/DB/worktree mutation, no backup restore into runtime, no recovery/dispatch/
  model invocation, no configuration change, no Concern 77, no implementation proposal.
- The live PostgreSQL runtime was **not reachable** from this environment
  (`psql` absent; no service on the documented host port), so live row-level facts were
  sourced from the **restore-verified backup metadata JSON** and filesystem run
  artifacts rather than the runtime DB.
- Durable evidence sources are named inline (concerns.md line ranges, commit SHAs, run
  artifact paths, git refs, backup metadata fields). Commit-message text was used only
  where no stronger artifact existed, and never as sole proof of a mechanism.
