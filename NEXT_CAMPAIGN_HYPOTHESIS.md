# Next-Campaign Hypothesis — Post-TraceStack Retrospective, Phase 2

> **Artifact type:** Analysis only. This document proposes nothing to implement.
> It contains no code, no patches, no Concern 77, no model/hardware recommendations,
> no repository selection, and no priority rankings presented as conclusions.
>
> **Primary and only evidence:** `TRACESTACK_CAMPAIGN_RETROSPECTIVE.md` and the durable
> sources it cites. No new runtime, DB, Git, worktree, model, or provider inspection was
> performed for this document.
>
> **Framing question (per instruction):** not "what should we fix next?" but "what should
> a second real development campaign *test about the orchestrator* that TraceStack did not
> establish?"
>
> **Evidence-integrity rules carried forward:** campaign-exercised vs test-only vs
> unestablished are kept separate; a test-only property is never described as campaign-proven;
> a gap is never re-described as a defect; reconstructed runtime facts are treated as
> reconstructed (the original PostgreSQL campaign history was lost, per the retrospective).

---

## 1. Established TraceStack baseline (six architectural areas)

For each area: **campaign-exercised** (a real campaign event), **test-only** (deterministic
/harness evidence but no live campaign event), **explicitly unestablished** (neither).

### 1.1 Model Containment
- **Campaign-exercised:** destructive whole-file "deletion-to-fit" (RUN-20260928-000007,
  315 deletions / 413-line diff) was refused by the scope guard before integration; a real
  within-scope 36-line, 2-file change landed under `code-edits/3` with zero rejected edits
  (TS-110). Exact-unique-match refusal and atomic change-set refusal were exercised as the
  containment boundary on a *live* model failure.
- **Test-only:** large multi-file / many-edit containment (C70's 57 tests + 5 mutations);
  pre-generation constraint disclosure (C72 added-first contract); option-text truthfulness
  (C69).
- **Explicitly unestablished:** containment at **large N** — many `replace` edits across
  several files near the emission (8 KB), diff (150-line), and result-size ceilings; whether
  a *real model*, under `coder-prompt/3`, still avoids deletion-to-fit when the tempting
  surface is many unrelated tests spread across multiple files rather than one 11 KB file.

### 1.2 Durable Execution
- **Campaign-exercised:** a provider `ModelTimeout` that outlived the DB transaction was
  recorded durably without masking and without stranding the transaction (C66, RUN-000005);
  the first live execution-ownership transfer fenced the old owner (C67, gen 0→1).
- **Test-only:** `asyncio.CancelledError` settlement to `WORKFLOW_CANCELLED` (C71, 13 tests,
  no sleeps). **No live cancellation was ever observed** — the candidate run RUN-20260928-000008
  was explicitly reclassified away from cancellation.
- **Explicitly unestablished:** cooperative cancellation *seen in production*; and durable
  recording at interruption points not sampled live (the delivery-transaction interior and
  the "neither row nor artifact" call — see §9).

### 1.3 Recovery / Replay Safety
- **Campaign-exercised:** `settlement_only` discovered and applied on a real spent-budget run
  (C68, RUN-000005); `delivery_only` applied on a real post-approval delivery crash, landed by
  a single fast-forward with `model_runs=2` and `task_runs=8` proving zero post-approval model
  calls and no new TaskRun (C76, TS-110).
- **Test-only:** the C76 refusal battery (wrong ancestry / scope / unapproved / candidate
  disagreement / dispatch holds run / unusable worktree), generation-fence, and idempotent
  `integrate_candidate` (C67/C68/C76 PostgreSQL Barrier races for *settlement/ownership*).
- **Explicitly unestablished:** a **simultaneous** two-operator `delivery_only` acquisition on
  a live-shaped run; a controlled fault injected precisely *inside* the delivery transaction
  (the one C76 exercised was uncontrolled and single); delivery_only idempotency across the
  narrow window *after* ref-move but *before* the terminal event write; recovery-mode selection
  when the interruption lands at a checkpoint that yielded neither a durable row nor a surviving
  artifact.

### 1.4 Integration / Provenance
- **Campaign-exercised:** the human commit `cbff2c4` (== `master`) preserved unchanged as the
  **second parent** of `e8b9eae`, the operator-resolved `integration_resolution_commit` for
  escalation `fc9bf9b0`; `agent/integration` advanced through that merge, then fast-forwarded
  to `4588bba`. A real human/agent merge *conflict* occurred and was resolved.
- **Test-only:** C73's 19 tests (baseline containment for dependents, human-vs-model
  provenance, nonexistent-commit and merge-conflict fail-closed, no fake automated candidate).
- **Explicitly unestablished:** **repeated / cascading** integration conflicts; dependent-task
  ordering across a non-trivial dependency structure (TraceStack was effectively a single
  linear TS-109→TS-110 hand-off); conflict recurrence and idempotency across a sequence.

### 1.5 Control-Plane Survivability
- **Campaign-exercised:** the *failure* — the original PostgreSQL history was lost — and the
  response: fail-closed test-DB isolation (C74) now guarding destructive ops, and verified
  recovery points restored into an isolated `verify_a552f9a9394c` DB (`source_counts_compared
  =true`, alembic `c1f4a7d29b60`) both before (`e190ea70`) and after (`4a79e0da`) the terminal
  recovery (C75).
- **Test-only:** the reconstruction importer contract (`test_reconstruction_importer.py`,
  `test_reconstruction_c73_contract.py`) producing the reconstructed runtime.
- **Explicitly unestablished:** fidelity of reconstructed rows to the destroyed originals
  (manifest `omitted_history`: model_runs / run_events / reviews / …); a host-run process
  reporting a *true* `SOURCE_REVISION` (only the `unknown/dev` default was observed); a restart
  landing mid human-resolution **reconciliation** (crash-window idempotency of
  `reconcile-human-commit`).

### 1.6 Environment / Namespace Portability
- **Campaign-exercised:** the crash root cause — a linked worktree whose absolute `.git`
  metadata was written under a container path a host run cannot resolve — and the bounded
  repair: only the disposable, supervisor-owned `_integration` worktree is rebuilt; a broken
  **task** worktree is a refusal, not a regeneration (C76).
- **Test-only:** `test_concern76.py` namespace set (`…_is_unusable_without_repair`,
  `…_repairs_stale_integration_metadata`, `…_never_touches_an_unrelated_worktree`,
  `…_with_unresolvable_baseline_raises_without_destroy`).
- **Explicitly unestablished:** portability with **multiple concurrent projects** sharing one
  orchestrator host (`WORKTREE_ROOT/<project_id>/` isolation under real parallel load —
  TraceStack had exactly one project, `d98cb1e7`); the general host↔container path issue for
  *task* worktrees (deliberately a refusal today); host-run provenance under a real revision.

---

## 2. Analysis of all 11 evidence gaps

Dimensions per gap: **A** invariant challenged · **B** best testing vehicle ·
**C** does a real coding model materially contribute? · **D** can TraceStack itself exercise
it, or is reuse weak/redundant? · **E** what would be *genuinely new* vs rediscovery of a known
limitation. Not ranked here.

**Gap 1 — simultaneous `delivery_only` recovery operators on a live-shaped run.**
- A: Durable Execution / Recovery-Replay (generation fence).
- B: **concurrency test** (Barrier-style dual acquisition), not a coding campaign.
- C: No. A model only needs to *produce* the approved stranded candidate, which TS-110 already
  did once; the dual-owner race is deterministic.
- D: Reusing TraceStack adds nothing for the race; the fence is already proven for
  settlement/ownership (C67/C68 PG races).
- E: Largely **rediscovery** — new only in that the specific branch (`delivery_only`) has never
  been *raced*; the underlying fence property is known.

**Gap 2 — provider/process death at each durable checkpoint (esp. inside the delivery txn).**
- A: Durable Execution / Recovery-Replay.
- B: **fault-injection test** (crash at a named line, then resume).
- C: No. Harness needs to seed real durable state to crash *over*; timing is deterministic.
- D: TraceStack's delivery crash was uncontrolled and single; it cannot be *re-timed*.
- E: **New** for checkpoints not yet observed live, but a campaign cannot reliably hit them —
  deterministic injection is the correct instrument.

**Gap 3 — exact per-TaskRun generation and per-run event counts on the live runtime.**
- A: none (observability / evidence completeness, not an invariant).
- B: **operational drill / read the restored dump** (the retrospective's environment had no
  reachable PostgreSQL client and no live service).
- C: No.
- D: N/A — closing this is just reading durable state that already exists.
- E: **Not new information** — it confirms a canonical assertion already bounded in the
  retrospective; it is completeness, not a hypothesis.

**Gap 4 — behaviour under multiple concurrent projects / shared integration namespace.**
- A: Environment/Namespace Portability + Durable Execution + Integration.
- B: **real coding campaign** (partly with a concurrency component).
- C: **Yes** — genuinely needs real, distinct code landed in two or more repos concurrently.
- D: TraceStack was single-project; **reuse would be redundant/insufficient** — a *different
  second project* is required, which is a campaign, not the same one.
- E: **New** — per-project worktree/ref isolation and non-contaminated integration under real
  parallel load has never been exercised.

**Gap 5 — very large multi-file targeted change sets near ceilings.**
- A: Model Containment (scope + edit protocol + representation).
- B: **real coding campaign** (with a supporting deterministic apply/rollback test).
- C: **Yes** — the open question is *how a real model composes* a large multi-file change set
  under pre-disclosed limits without deletion-to-fit; a fixture cannot answer that.
- D: TraceStack's largest exercised file was ~11 KB single-file; multi-file/many-edit at scale
  was not exercised, and the same project cannot scale it — needs larger multi-module surfaces.
- E: **New** — containment-holds-at-scale for a real model (atomic all-or-none, distinct-path
  accounting, no silent partial application) is unestablished.

**Gap 6 — repeated / cascading integration conflicts.**
- A: Integration / Provenance.
- B: **deterministic/operational** (seed successive conflict states), only marginally campaign.
- C: Marginal — conflict *resolution* is operator/merge, deterministic; models only supply
  conflicting candidates.
- D: TraceStack produced exactly one conflict (`e8b9eae`); the sequence/idempotency question is
  reproducible deterministically.
- E: **New** as a *sequence* property, but a campaign's marginal model value makes deterministic
  testing the better instrument.

**Gap 7 — restart mid human-resolution reconciliation.**
- A: Control-Plane Survivability + Recovery.
- B: **fault-injection / operational drill** (crash inside `reconcile-human-commit`).
- C: No.
- D: TraceStack reconciled once (`fc9bf9b0`) without a crash window; not repeatable as a timed
  campaign event.
- E: **New** (crash-window idempotency), but deterministic injection is the right tool.

**Gap 8 — full fidelity of reconstructed history vs originals.**
- A: Control-Plane Survivability.
- B: **not worth testing yet / intrinsically untestable** for the specific claim "equals the
  lost originals" — the originals were destroyed.
- C: No.
- D: No campaign can recover data that no longer exists.
- E: **Not obtainable** as a campaign result; already an accepted, documented boundary
  (`omitted_history`). Treating it as a target would be converting a known limitation into a
  pseudo-defect.

**Gap 9 — cooperative cancellation actually observed in production.**
- A: Durable Execution (C71).
- B: **operational drill** (deliberately disconnect a live client mid-run); weakly a campaign.
- C: Marginal — a model only needs to keep the run long enough to disconnect.
- D: TraceStack deliberately did *not* observe a real `CancelledError` (RUN-000008 reclassified);
  a controlled disconnect could, but it is an ops action.
- E: **New** (first *live* cancellation settlement), but the mechanism is already test-proven;
  the only gain is observational, favoring a drill over a coding campaign.

**Gap 10 — host-run provenance under a real `SOURCE_REVISION`.**
- A: Control-Plane Survivability / environment provenance.
- B: **operational / configuration** (bake a real revision, observe health + backup metadata).
- C: No.
- D: This review's host-run showed only `unknown/dev`; not a coding-campaign concern.
- E: **New** only in the trivial sense of confirming the true-revision path; deterministic.

**Gap 11 — `delivery_only` idempotency across a crash after ref-move, before event write.**
- A: Recovery / Replay Safety.
- B: **fault-injection test** (precise sub-window crash, then re-recover).
- C: No.
- D: TS-110 is the `delivery_only` path but the internal crash window is not controlled in a
  campaign.
- E: **New** (proving no double-ref-move / no double-event), deterministic.

**Gap → vehicle tally (primary):** real coding campaign — Gaps 4, 5 (2). Deterministic /
fault-injection / concurrency / operational — Gaps 1, 2, 3, 6, 7, 9, 10, 11 (8). Not worth
testing yet / intrinsically untestable — Gap 8 (1).

---

## 3. Separating campaign questions from engineering tests

### SET A — questions that require or strongly benefit from a second real coding campaign
- **A1 (Gap 4):** per-project worktree/ref isolation and non-contaminated `agent/integration`
  advancement when *distinct real projects* integrate concurrently through one orchestrator host,
  including a post-approval delivery crash in one project while another is mid-run.
- **A2 (Gap 5):** whether a *real coding model*, under `coder-prompt/3`/`code-edits/3`, composes a
  large multi-file targeted change set near ceilings without deletion-to-fit, partial application,
  or miscounting distinct paths — i.e. whether containment holds on real model output at scale.
- **A3 (Gap 2/E, partial):** that a *longer* sequence of real, flaky-provider-driven runs samples
  interruption at durable checkpoints beyond the two TraceStack happened to hit, and each still
  converges to exactly one terminal outcome. (Model contributes the work and the natural timing of
  the faults; the checkpoint *coverage* is what a real, longer campaign adds.)

### SET B — questions better answered by deterministic engineering tests / fault injection /
concurrency / operational drills
- **B1 (Gaps 1, 11, 2):** dual-operator `delivery_only` acquisition; delivery-transaction interior
  crash; ref-move-before-event-write idempotency → concurrency + fault-injection tests.
- **B2 (Gaps 6, 7):** cascading integration-conflict sequences; crash inside `reconcile-human-commit`
  → deterministic / operational tests.
- **B3 (Gaps 3, 9, 10):** per-run generation/event verification; live cancellation settlement;
  host-run true `SOURCE_REVISION` → read-the-dump / operational drills / config verification.

### Why the separation
TraceStack's established value is that it produced **real durable artifacts** for properties that
fixtures can only approximate — the merge topology (`e8b9eae`/`4588bba`), the model-free
`delivery_only` counts, the live `ModelTimeout` durable recording. The remaining properties split
into two kinds. A *minority* are **model-behavioral and scale-dependent**: they depend on what a
real model emits (a large multi-file change set) or on *how many genuinely different repos are
being driven concurrently* — neither of which a single-project fixture can substitute. That is the
only honest justification for a second real campaign. The *majority* of the gaps are about
**precisely-timed internal state transitions** (crash at line X, two owners racing, ref-vs-event
window), where a campaign is a poor instrument: it cannot control the timing, and a deterministic
fault-injection or concurrency test proves the property more sharply and repeatably. Using a
campaign merely to "exercise the recovery subsystem again" would mostly re-produce the two
checkpoints TraceStack already landed — redundancy, not information.

---

## 4. Candidate campaign hypotheses (at most three, falsifiable)

### H1 — Per-project namespace & integration isolation under concurrent real work
**HYPOTHESIS:** If two or more *distinct* projects integrate concurrently through one orchestrator
host — each producing approved candidates, with at least one suffering a post-approval
delivery crash — the orchestrator will land each project's accepted work on **its own**
`agent/integration`, with per-project worktree isolation and **zero cross-project ref or
worktree contamination**, despite shared-supervisor-state and namespace stress.

**WHY TRACESTACK DID NOT ESTABLISH IT:** the campaign ran exactly one project (`d98cb1e7`);
per-project `WORKTREE_ROOT/<project_id>/` isolation and `agent/integration` separation were never
exercised against a second concurrent project (Gap 4; §1.6 unestablished).

**WHAT THE CAMPAIGN WOULD HAVE TO EXERCISE:** parallel live runs across ≥2 repositories; a
delivery interruption in one project while another is mid-integration; recovery (`delivery_only`
or `continue`) invoked for the crashed project's run.

**WHAT WOULD FALSIFY IT:** a durable artifact showing (a) an `agent/integration` advanced by a
candidate belonging to a *different* project, (b) a worktree under one project's namespace used
or repointed by another, (c) a recovery/repair that touched another project's disposable
`_integration` worktree or ref, or (d) a cross-project candidate-commit provenance mismatch.

**WHAT WOULD NOT COUNT AS A FALSIFICATION:** a model emitting semantically-wrong-but-in-scope
code (a model limitation, containment already bounded); a provider timeout yielding a
well-formed stranded run that the assessment correctly reports as recoverable/refused (designed
C66/C67/C68 behavior); an intentional refusal to rebuild a namespace-broken *task* worktree (the
documented C76 boundary, not a cross-contamination failure).

**EVIDENCE REQUIRED:** per-project Git topology (each `agent/integration` SHA + parent lineage);
worktree layout and per-project paths; run_events showing recovery authorizations scoped to the
correct project; `model_runs` counts proving no post-approval inference per project;
completion-report/candidate records cross-checked against the owning project id; restore-verified
backup confirming isolated integration refs.

**Model contribution:** YES (needs real code in multiple repos).

### H2 — Containment holds for large, real, multi-file change sets
**HYPOTHESIS:** If a task legitimately requires a large multi-file change set (many targeted
`replace` regions across several files, each carrying substantial unrelated content, near the
8 KB emission / 150-line diff / result-size ceilings) authored by a real model under
`coder-prompt/3`, then the orchestrator will either apply the entire change set **atomically and
within scope** or **refuse it entirely with zero partial application and zero deletion-to-fit**,
across every such attempt.

**WHY TRACESTACK DID NOT ESTABLISH IT:** C70's destructive-edit evidence was on a *single*
~11 KB file, and the surviving containment tests are fixtures; large-N, multi-file, real-model
change sets near ceilings were not exercised (Gap 5; §1.1 unestablished).

**WHAT THE CAMPAIGN WOULD HAVE TO EXERCISE:** tasks whose honest solution touches several files
while each file also holds unrelated tests/branches the task says nothing about; many `replace`
regions approaching the emission ceiling; attempts whose scope/diff accounting could break only
at scale.

**WHAT WOULD FALSIFY IT:** a durable record of a *partially applied* change set (some edits land,
others don't) reaching integration; a within-scope change that **deleted unrelated pre-existing
work yet passed gates**; a distinct-path `max_files_changed` miscount letting an over-file change
land; or a scope/refusal computation inconsistent with the resulting diff.

**WHAT WOULD NOT COUNT AS A FALSIFICATION:** a model *choosing* to decline or split a large change
(a model behavior, not a containment breach); an over-limit change **atomically refused** with no
partial write (that is containment *working*); a semantic-but-in-scope mistake that review
appropriately flags.

**EVIDENCE REQUIRED:** `completion-report.json` applied vs rejected paths and `rejected_edits:[]`
consistency; scope decision + diff statistics from the real candidate; per-attempt model-call
counts; verification/review outcomes; proof that no unrelated content was removed (diff deletes vs
task scope); run_events demonstrating refusal-atomicity rather than partial application.

**Model contribution:** YES (the open question is real-model composition at scale).

### H3 — Recovery converges to exactly one terminal outcome across varied real interruption points
**HYPOTHESIS:** If a longer queue of real runs is interrupted by natural provider/environment
faults at *varied* durable checkpoints (pre-call, post-call, verify, review, and delivery), the
orchestrator will drive each affected TaskRun to **exactly one** terminal outcome — chosen as
`continue` / `settlement_only` / `delivery_only` by the assessment — **without manufacturing an
extra attempt, an extra model call after approval, or an extra TaskRun**, despite the interruption
point varying run to run.

**WHY TRACESTACK DID NOT ESTABLISH IT:** TraceStack landed only two recovery modes, each at one
specific checkpoint (C68 settlement after exhaustion; C76 delivery after approval). Arbitrary or
other-checkpoint live interruptions were not observed; the one delivery crash was a single
uncontrolled instance (Gap 2 / Gap 11; §1.3 unestablished).

**WHAT THE CAMPAIGN WOULD HAVE TO EXERCISE:** enough real volume, under a flaky/slow provider, to
naturally produce stranding at multiple distinct checkpoints; then recovery of each via the
ordinary assessment and normal nodes.

**WHAT WOULD FALSIFY IT:** a checkpoint that strands a run with **no supported `recovery_mode`**
(not a correct fail-closed refusal, but an *unclassified* stranding); a recovery that yields a
duplicate model call, attempt, or TaskRun; a double ref-move or double terminal event for a single
interruption; or a run that converges to two different terminal outcomes.

**WHAT WOULD NOT COUNT AS A FALSIFICATION:** an interruption the assessment *correctly fails
closed* on (a designed refusal pending operator action); a genuine provider timeout recorded
durably and later recovered model-free; a model's own semantic failure that the gates contain.

**EVIDENCE REQUIRED:** a per-interruption table mapping checkpoint → assessed `recovery_mode` →
observed terminal outcome; per-run `execution_generation` transitions; `model_runs` counts proving
zero post-approval inference; run_events showing exactly one terminal/one authorization event per
run; `task_runs` count unchanged by recovery; durable proof of single integration (Git parentage).

**Model contribution:** PARTIAL — a real model supplies the work and the *natural timing* of
faults; but the precise sub-window/idempotency claims overlap heavily with deterministic
fault-injection (B1), so this hypothesis should be paired with, not substituted by, those tests.

---

## 5. Required project characteristics per hypothesis (no repositories named)

### For H1 (multi-project isolation)
- **≥2 independent repositories/projects** with disjoint module sets and no shared source path,
  so per-project worktree/ref isolation is actually tested rather than assumed.
- Toolchains that produce **real, verifiable diffs** for each project (so an integration advance
  is observable and cross-contamination would be detectable).
- Sufficient per-project size that at least one task's approved candidate is non-trivial, giving a
  meaningful post-approval delivery to interrupt.
- An operational setup able to keep both projects **live concurrently** against one orchestrator
  host, with per-project `WORKTREE_ROOT/<project_id>/` namespacing exercised in parallel.
- Deliberate capacity to interrupt delivery in one project while the other is mid-run.
- Note: "larger single repo" does **not** substitute here; the axis is *project multiplicity*,
  not size.

### For H2 (large multi-file containment)
- A language/toolchain with **sizable existing source files** (so whole-file re-emission would be
  costly and `replace` is the natural expression).
- Tasks whose honest solution spans **multiple files**, where each file also contains a
  **substantial body of unrelated tests/branches/imports** the task does not mention — making
  deletion-to-fit both tempting and detectably wrong.
- Change surfaces that can approach the **8 KB emission**, **150-line diff**, distinct-path
  `max_files_changed`, and `context_max_file_bytes` result ceilings *simultaneously*.
- A deterministic test/build/lint harness per project so verification-gate behavior is observable
  independent of the model.
- **Independent modules** to avoid confounding H2 with multi-project effects; keep it single-project.
- Stateful/stateless not a primary axis; the axis is **edit-set size and unrelated-content density**.

### For H3 (varied-checkpoint recovery)
- A **longer task queue** (more real runs) than TraceStack, to raise the chance of sampling
  multiple distinct interruption checkpoints across the lifecycle.
- A provider/environment configuration that **naturally exhibits slowness/timeouts** (TraceStack's
  ~600 s local endpoint was the source of its real interruptions) — the fault should arise from
  real operation, not be scripted, so checkpoint coverage is honest.
- Any language/toolchain is acceptable; the axis is **volume and environmental instability**, not
  repo shape.
- Preferably the same stable repo family to keep model-behavior variables controlled, so
  convergence claims are attributable to the supervisor, not the project.

---

## 6. Is a second real campaign justified now?

**Conclusion: YES — but narrowly scoped, to Set A only (H1, H2, and — with a caveat — H3).**

Reasoning by evidence type, not enthusiasm:
- A campaign is justified **only where the evidence a deterministic test cannot produce is
  precisely the evidence that is missing.** That holds for exactly two properties:
  (1) what a *real model* emits for a *large multi-file* change set near ceilings (H2 / Gap 5), and
  (2) whether per-project namespace/ref isolation survives *several genuinely distinct repos*
  running concurrently (H1 / Gap 4). Neither can be substituted by reusing TraceStack (single
  project, single/one-file scale) — reproduction would be redundant.
- The **majority of gaps (1, 2, 3, 6, 7, 8, 9, 10, 11)** concern *precisely-timed internal state
  transitions*, *recovered-but-destroyed data*, or *observational confirmation* — for which a
  campaign is a weak instrument and deterministic fault-injection / concurrency / operational
  drills are stronger. Running a campaign to reach them would be "using a campaign merely because
  a gap exists," which the framing explicitly rejects.
- Gap 8 ("reconstructed == original history") is **intrinsically untestable** (the originals are
  gone) and is already an accepted boundary; it must not be promoted into a campaign target.

Therefore: a second real campaign is warranted now **only** if it is designed around the model-
material, non-redundant properties H1 and H2 (H3 optional and paired with B1). If a campaign
cannot be scoped to those — i.e. if it would mostly re-sample TraceStack's two landed checkpoints
or attempt to prove deterministic timing properties — then the correct conclusion would instead be
**NOT YET** (resolve B1/B2/B3 first). This document finds that a properly scoped H1/H2 campaign
*does* exist, so the answer is the qualified YES above, not an unqualified one.

---

## 7. Evidence required to call a future campaign successful

Success must be certified from **durable, cross-checkable artifacts**, not from the POST/response
narrative:
1. **Git topology per project:** each project's `agent/integration` SHA and its **parent lineage**
   showing only that project's candidates (H1); expected fast-forward vs merge stated and matching.
2. **Zero post-approval inference:** whole-DB and per-run `model_runs` counts consistent with
   attempts charged at evidence boundaries; recovery adds none (all hypotheses).
3. **One terminal outcome per interrupted run:** run_events showing exactly one terminal / one
   recovery-authorization event, `execution_generation` transitions monotonic, `task_runs` unchanged
   by recovery (H3, H1).
4. **No partial application / no deletion-to-fit:** completion-report applied-vs-rejected paths,
   `rejected_edits:[]`, scope decision consistent with the real diff, and proof unrelated pre-existing
   content survived (H2).
5. **No cross-project contamination:** worktree layout under per-project namespaces and recovery
   authorizations scoped to the owning project id; no ref touched by another project's candidate
   (H1).
6. **Checkpoint coverage table:** an explicit mapping, per stranded run, of interruption checkpoint
   → assessed `recovery_mode` → observed terminal outcome, demonstrating variety beyond the two
   TraceStack landed (H3).
7. **Restore-verified final state:** a new backup verified into an isolated `verify_*` DB with
   `source_counts_compared=true`, recorded alembic head, SHA-256; and SYNTHETIC tagging preserved if
   any reconstruction recurs (control-plane integrity).
8. **Negative controls preserved:** deliberate refusals (namespace-broken *task* worktree,
   derivation-failed candidate) observed as *correct* fail-closed outcomes, not counted as failures.

A campaign is **not** "successful" merely because it ran to completion; it is successful only if
the specific falsification signatures in §4 are **absent** and the positive durable signatures above
are **present** for each hypothesis exercised.

---

## 8. Explicit non-goals

- Not proposing any code, test, or schema change (no Concern 77, no implementation plan).
- Not selecting or recommending a repository, language, or toolchain project (only characteristics).
- Not recommending a model, provider, or hardware change.
- Not ranking hypotheses or gaps by priority/severity, and not scoring them.
- Not treating any evidence gap as a defect; they are unproven properties.
- Not using a campaign to answer deterministic/operational questions (B1/B2/B3).
- Not re-designing TraceStack to repeat what it already established.
- Not asserting any safeguard beyond its stated evidence tier (campaign/test/unestablished).

---

## 9. Evidence uncertainties carried into Phase 2

- **Single data points:** most exercised campaign properties rest on n=1 live events (one delivery
  crash, one merge conflict, one spent-budget settlement). Repeated statistical behavior is
  unestablished; this is why H1/H3 emphasize *variety and multiplicity* rather than re-confirmation.
- **Reconstructed runtime:** the live PostgreSQL history was lost; durable row-level facts cited in
  the retrospective come from restore-verified backup metadata and filesystem artifacts and are
  labelled reconstructed. Per-run `execution_generation` (claimed 1→2) and the per-TS-110
  `run_events` figure (canonical 16 vs project-wide backup 17) were **not independently re-derived**
  (Gap 3), so any Phase-2 confidence in those exact per-run values inherits that bound.
- **Model specificity:** TraceStack used one coder/reviewer model configuration. H2's conclusions
  about real-model behavior at scale are **model-conditional**; changing the model would confound
  the comparison, so a future campaign should hold the model constant and attribute scale effects to
  the orchestrator, not the model swap.
- **Host-run provenance:** only the `unknown/dev` default was observed; the true-`SOURCE_REVISION`
  path is unestablished (Gap 10), so any host-run provenance inference in future artifacts remains
  partially attested.
- **Cancellation:** no live cooperative cancellation was ever observed (RUN-20260928-000008 was
  reclassified); its settlement property is **test-only**, and a future claim of "cancellation works
  in production" would still be unestablished until an operational drill (B3) observes it.
- **Timing non-determinism:** a real campaign cannot control *which* checkpoint a fault hits, so
  H3's checkpoint-coverage claim can only be argued from *observed sampling*, never proven to be
  exhaustive; deterministic fault-injection (B1) remains the sharper instrument for the specific
  sub-window questions.

---

### Self-review note (this document)
All 11 retrospective evidence gaps are addressed in §2. No gap is re-described as a defect. Test-only
properties (C71 cancellation, reconstruction importer, C69 option text, delivery_only refusal battery)
are kept in the "test-only / unestablished" tiers and are **not** claimed as campaign-proven. Each
candidate hypothesis in §4 is falsifiable with a specific durable failure signature and an explicit
"not-a-falsification" clause. No candidate reproduces TraceStack's single-project / single-file /
two-checkpoint scenario. No implementation recommendation, Concern 77, model/hardware recommendation,
repository recommendation, or disguised priority ranking appears. Only this file was created; the
orchestrator runtime, database, Git refs, worktrees, and model providers were not touched.
