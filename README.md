# Local AI Development Orchestrator

A Docker-first local orchestrator that moves a project through bounded coding
tasks: build context, invoke a local coding model, verify the result
deterministically, submit the diff for structured review, route feedback back
to the coder, and commit only what passes.

The specification lives in [`local-ai-dev-orchestrator-build.md`](local-ai-dev-orchestrator-build.md).
The governing rule (section 52):

> The model should never be responsible for remembering the workflow, proving
> that its own work succeeded, or deciding what safety boundaries apply.

## Status

**Phases A–L are complete. Phase M's deterministic first-project acceptance
gate is complete; the configured-model quality trial remains operator work.**

Phase A: repository scaffold, Docker Compose, configuration, domain models,
SQLAlchemy mappings and repositories, Alembic migrations, health API.

Phase B: project CRUD, manifest parser, task importer, dependency validation,
task state machine, next-ready-task selection, run creation. A manifest can be
imported and the correct next task selected -- end to end over the API.

Phase C: `GitService`, clean-state validation, task branches, per-run
worktrees, diff capture, commit, rollback, checkpoints and push policy. A task
run can take an isolated worktree, change a repository, capture the diff,
commit on its own branch and clean up without the managed repository ever
moving.

Phase D: the Node/Python/Java worker images, the `WorkerService`, the command
policy, per-command timeouts, output capture with redaction, log artifacts and
guaranteed cleanup. The orchestrator executes a project's configured
verification commands inside an isolated worker that can only see that run's
worktree.

Phase E: the `ModelProvider` contract, an OpenAI-compatible adapter, provider
configuration from the environment and from registered `models` rows, a
connection test, structured-response handling and classified timeouts/errors.
A bounded prompt reaches the configured local model and comes back as a
validated object.

Phase F: task-declared files, repository file selection, interface and test
discovery, configuration and ADR loading, the context budget, the run artifact
store and `context-manifest.json`, and the lesson hook. A fixture task produces
a deterministic context package, hashed and recorded against the run.

Phase G: the coding and planning prompts, plan mode with validation before any
code is written, the structured edit contract and the applier that enforces the
task's write allowance, the completion report, and the scope guard. A fixture
task changes its own isolated worktree without touching a forbidden path.

Phase H: the project verification profile, the pipeline in section 17's order,
failure classification per category, the deterministic feedback that goes back
to the coder, the diff-size and protected-path checks, and the security scan
over the candidate diff. An intentionally broken fixture fails before a
reviewer is ever invoked.

Four concerns that stood between phase H and phase I were cleared before
starting the reviewer: every model call now leaves a `model_runs` row against
a real `models` row (3), context artifacts are prefixed per attempt so a
review cycle can show what its coder saw (10), a task's `verify` list adds to
the project's suite instead of replacing it (18), and `verified` is a distinct
claim from `passed` so an unverified candidate cannot reach a reviewer (19).

Phase I: the `ReviewProvider` contract and its model-backed implementation,
the structured review schema and the tolerant parse that reconciles a review
with its own findings, the reviewer prompt, the bounded review package, and
the routing that turns a verdict into the task's next state -- approval,
change request or human escalation, with section 37's human-approval gate
applied to acceptance. A fixture review routes correctly for all three
decisions, and a reviewer that fails produces no verdict at all.

Phase J: the fix loop -- the correction prompt reaching the coder,
re-verification, re-review, the attempt and cycle counters, issue resolution
across cycles, and the escalation that ends a run nobody could finish. A
deliberately flawed fixture is corrected and approved; an unfixable one
escalates with its whole history, and never on a fourth attempt.

Seven concerns were cleared during phase J. Six were things it could not be
built correctly on top of: a coding attempt is now refused outright when a file
it may rewrite was clipped out of its context, because asking for the complete
contents of a file the coder has only partly seen destroys the rest (1); every
path through a run now ends somewhere instead of parking a task in `CODING` or
`VERIFYING` (9, 23); the correction text reaches a model (26); a finding a later
reviewer stops raising is marked resolved, so the open list shrinks as well as
grows (27); and each turn's artifacts are filed under the cycle they belong to
(33). The seventh was not in phase J's way at all: a review package sent to a
reviewer that is not on this machine is now redacted first (28).

Phase K: a durable LangGraph wraps the existing services without absorbing
their business logic. Its SQLAlchemy checkpointer persists the graph cursor in
PostgreSQL, each code/verify/review turn is committed as a recovery unit, pause
requests are honoured at safe boundaries, and incomplete runs are reconciled
from database and worktree state after restart. Approval now commits, tags,
optionally pushes, completes, and releases the worktree. Human escalation
options carry explicit intents, so accepting, retrying, requesting changes,
completing by hand, or abandoning causes the selected effect without parsing
free text. The task runtime and whole-worker ceilings are enforced before new
work starts and across verification commands.

Phase L: the lessons system, experience capture from settled runs, and the
metrics that read both. A verified review/fix cycle proposes lessons; a person
approves them; approved lessons are retrieved for later runs of the same
project. Accepted runs are preserved as training examples. Both directions of
failure leave a queryable history. Known gaps and their reasoning remain in
[`concerns.md`](concerns.md); earlier blockers are marked resolved there
rather than removed.

Phase M: a disposable Python repository now runs through a single acceptance
exercise in `tests/integration/test_phase_m.py`. Ten low-risk tasks complete;
the scenario includes multiple intentional verification failures, a
review/fix cycle, retry exhaustion, pause/restart/resume, rollback, runner and
database-engine reconstruction, and worktree cleanup. Separately, the real
Python worker image was built and removed cleanly after a hardened container
run, and the Compose PostgreSQL/orchestrator pair retained its schema across a
restart. The exercise uses scripted coder/reviewer responses so failures are
repeatable; it validates orchestration and safety, not the quality of whatever
local models an operator configures (concern 47).

## Layout

| Path | Contents |
| --- | --- |
| `apps/orchestrator/domain/` | Framework-free models, enums, state machine, policies |
| `apps/orchestrator/db/` | SQLAlchemy mappings and session management |
| `apps/orchestrator/repositories/` | The only translation layer between rows and domain objects |
| `apps/orchestrator/api/` | FastAPI routers (resources, never workflow internals) |
| `apps/orchestrator/providers/` | Model provider adapters behind one contract |
| `apps/orchestrator/agents/` | Prompt assembly and the model-facing steps |
| `apps/orchestrator/services/` | Deterministic services |
| `apps/orchestrator/workflow/` | LangGraph coordination (Phase K) |
| `workers/` | Disposable coding-worker images, one per profile |
| `migrations/` | Alembic revisions |
| `data/runs/` | One directory per run: context manifest, prompts, logs, diffs |
| `data/` | Run artifacts, referenced from the database by path and hash |
| `workspace/` | Managed repositories and, under `worktrees/`, one checkout per run |

Domain and service classes must not import LangGraph.

## Setup

Requires `uv` and Python 3.12.

```bash
./scripts/dev.sh setup
cp .env.example .env
./scripts/dev.sh test
```

### With Docker

```bash
docker compose up -d postgres
./scripts/dev.sh migrate
./scripts/dev.sh serve
```

This host-orchestrator arrangement is the safer default. Running the
`orchestrator` Compose service also works: it receives the host Docker socket
and uses `HOST_WORKTREE_ROOT` to translate `/workspace/worktrees` into the bind
source the host daemon understands. Access to that socket is effectively
Docker-daemon authority, so enable the full service only on a trusted machine:

```bash
docker compose up -d postgres orchestrator
```

The Compose orchestrator runs `alembic upgrade head` before it starts serving.
`/health` checks that every mapped application table exists, so a reachable but
unmigrated PostgreSQL instance reports `503` instead of a false ready state.

### Without Docker

The test suite runs entirely on SQLite and needs no database server. SQLite is
a test convenience only; PostgreSQL is the supported runtime backend. To run
the tests against PostgreSQL instead:

```bash
TEST_DATABASE_URL=postgresql+psycopg://orchestrator:orchestrator@localhost:5432/orchestrator \
  ./scripts/dev.sh test
```

## Managing a project

Each managed repository carries a `build.tasks.yaml` manifest (build.md
section 5) declaring the project and its task DAG. Register the repository,
then import the manifest:

```bash
curl -X POST localhost:8000/projects \
  -H 'content-type: application/json' \
  -d '{"name": "TraceStack", "repository_path": "/workspace/tracestack"}'

curl -X POST localhost:8000/projects/$PROJECT_ID/import-tasks
curl localhost:8000/projects/$PROJECT_ID/next-task
curl -X POST localhost:8000/projects/$PROJECT_ID/run
```

The run endpoint selects one eligible task and waits for its durable workflow
invocation to reach completion, escalation, failure, or a pause boundary. V1
still enforces project concurrency of one.

A task may declare what the coder should read and what it may write
(section 6). The context builder loads the first two lists; the scope guard
measures the diff against the writable ones and refuses a write outside them:

```yaml
- id: TS-004
  title: Implement navigation tree
  files:
    inspect: ["src/widgets/tree.ts"]
    modify: ["src/navigation.ts"]
    create: ["src/navigationTree.ts"]
```

Paths are repository-relative and validated at parse time: an absolute path,
a `~`, or anything containing `..` is rejected rather than normalised, because
these lists end up as a reading list and a write allowance. A directory or a
glob expands against the tracked files.

The project declares the commands that verify its work (section 18). The
orchestrator runs exactly these, and a model never supplies one:

```yaml
verification:
  build: ["npm run compile"]
  lint:  ["npm run lint"]
  tests: ["npm test"]
  security: ["npm audit --omit=dev"]   # optional; section 19
```

A task's own `verify` list (section 6) is **added** to the test category, never
substituted for it: a task can ask for more verification than the project
requires and never for less. Commands the profile already runs are dropped
rather than repeated, so the example task in build.md section 5 --
`verify: [npm run compile, npm test]` against a project that already runs both
-- costs nothing extra. Concern 18 has the reasoning.

Two rules govern importing:

- **Parsing is strict.** An unknown key, a dependency on a missing task, or a
  dependency cycle is rejected before anything is written. A typo such as
  `verifiy:` would otherwise import a task with no verification commands, and
  a command no worker is permitted to run is refused at import rather than on
  the first attempt at the task.
- **The database is authoritative for runtime status.** A re-import creates
  missing tasks and refreshes declarative fields (title, limits, dependencies,
  verification commands) but never resets progress, never touches a task with a
  run in flight, and never deletes a task that has left the manifest -- it is
  reported as orphaned so its run history survives.

## Git workspaces

Each task run gets its own branch and its own worktree; the managed
repository's own working tree is never checked out, reset or committed to.

```text
agent/TS-004-navigation-tree          branch, named by the orchestrator
TS-004: implement navigation tree     commit message
$WORKTREE_ROOT/<project>/ts-004-run1  isolated checkout for run 1
```

The rules from build.md section 10 are enforced in code, not in a prompt:

- **No model ever builds a Git command.** `GitService` exposes a fixed set of
  operations with fixed argument vectors, no shell and no escape hatch.
- **A dirty repository aborts the run** unless `GIT_ALLOW_DIRTY_START=true`.
- **The starting SHA is recorded before any work**, so a failed attempt can
  always be reset to it and an escalated one can be tagged for a human.
- **The default branch is protected**: no commit, no reset, no push.
- **Worktrees may only be created under `WORKTREE_ROOT`**; that boundary is
  what keeps a coding worker away from the rest of the machine.
- **Pushing is off by default**, force-pushing needs a second switch, and a
  skipped push is never a task failure.
- **Repository hooks do not run**: project code executes in a worker
  (phase D), never in the orchestrator process.

## Model providers

A provider is an endpoint the orchestrator may call, plus the role it is
allowed to play. Two come from the environment -- the local coder and the
reviewer -- so a fresh installation runs without a database row; more are
registered against the `models` table.

```bash
curl localhost:8000/models                     # what would actually be called
curl -X POST localhost:8000/models/connection-test   # would it answer?
```

The connection test probes `GET /models` on each endpoint. It never runs
inference, and it reports rather than raises: an endpoint that is up but
serving a different model than configured comes back `healthy: false` with the
reason, which is the misconfiguration worth catching before a run starts and
not during one.

Four rules are enforced in code (build.md sections 13 and 49):

- **No silent fallback.** Selection returns the provider configured for the
  role or raises. A reviewer is never substituted for a missing coder, and a
  manifest asking for a model this installation does not serve stops the run
  rather than quietly getting a different one -- otherwise the `model_id`
  recorded against the attempt would be a lie.
- **Every failure is classified.** Timeout, unreachable, rejected, invalid
  response and oversized prompt each map to a `FailureReason`, which maps to
  exactly one action. The provider itself never retries: a retry has a ceiling
  the workflow owns.
- **Text is not trusted to be JSON.** A schema request survives a `<think>`
  block, a Markdown fence and surrounding commentary; anything still not
  valid, or truncated at the token limit, is an `INVALID_MODEL_RESPONSE` that
  goes back to the coder as evidence.
- **Credentials stay out of the database and the logs.** A registered model
  stores the *name* of an environment variable, never a key, and no prompt or
  response body is echoed into an error message.

`LOCAL_MODEL_CONTEXT_WINDOW` must track the endpoint's served `n_ctx`, not the
model's trained maximum. A prompt that would not leave room for an answer is
refused before the request is sent -- that is a backstop for a context-builder
bug (phase F), not a budget.

## Context

The context builder gives the coder the smallest useful slice of the
repository (build.md section 15). It reads the run's worktree, not the managed
repository, so the coder sees the tree it is about to change.

Candidates are gathered in the specification's priority order and fitted into
a budget:

| Rank | Included | Why it is a candidate |
| --- | --- | --- |
| 1 | Task specification | Always; generated from the database record |
| 2 | Task-declared files | Named by `files:` in the manifest |
| 3 | Interfaces and types | Imported by a declared file, or a type-shaped keyword match |
| 4 | Relevant tests | Named after a declared file, or a keyword match |
| 5 | Configuration | `package.json`, `pyproject.toml`, ... near the repository root |
| 6 | Architecture decisions | Binding records under `.ai/decisions/` |
| 7 | Recent changes | Commits touching the declared files |
| 8 | Lessons | Retrieved for this project (sections 32-33) |
| 9 | Repository map | Directory counts, for orientation |

Four rules hold the subsystem together:

- **The repository is never sent by default.** Every item is present because
  something put it there, and `context-manifest.json` records the file name,
  its hash, the reason it was included and whether it was clipped.
- **The package is deterministic.** The same commit, task and settings render
  the same text and therefore the same `context_hash`, which is what makes a
  run reconstructable and lets a failure be attributed to a prompt rather than
  to a lucky ordering.
- **Nothing is cut silently.** A clipped file says so in the prompt and in the
  manifest; a dropped one is listed with the limit that dropped it. A coder
  that failed for lack of context can be shown why.
- **No model chooses its own context.** Selection is code. A model that could
  ask for more would make the budget advisory.

The budget follows the endpoint that will serve the prompt: with
`CONTEXT_MAX_TOKENS=0` it is `LOCAL_MODEL_CONTEXT_WINDOW * CONTEXT_WINDOW_SHARE`,
leaving room for the system prompt, review feedback on a retry, and the answer.
A budget too small to hold the task specification is a configuration error and
is raised as one.

Project memory is explicit, not conversational (section 16):

```text
.ai/
├── project.md          included whole
├── architecture.md     included whole
└── decisions/ADR-001.md   retrieved when relevant and not superseded
```

ADR parsing is deliberately tolerant, unlike manifest parsing: a manifest is a
contract, but an ADR is a human note, and refusing to run a task because
someone wrote `Status :` would be the worse failure. What could not be read is
reported in the manifest's `warnings`, and a record with no status still binds.

## The coding agent

One attempt runs in a fixed order and stops (build.md section 14):

```text
build context → plan → validate the plan → ask for edits
              → apply what policy permits → capture the diff
              → measure it against the task's scope → completion report
```

Nothing in that sequence verifies, reviews or commits. A medium- or
high-complexity task must plan first; a low-complexity one goes straight to
code.

**The coder returns edits, not commands and not a patch.** It answers with a
JSON object naming each file, an operation (`create`/`update`/`delete`) and the
file's complete new contents. It never runs a command, never uses a shell and
never touches Git, so the decision of what may be written stays with the
orchestrator rather than being delegated by default. Whole-file content is
chosen over a unified diff because a wrong hunk header from a local model fails
quietly and sends the model off to debug a patch format instead of the task;
the cost is that a small change to a large file rewrites the whole file, which
is [concern 1](concerns.md).

Five rules are enforced in code:

- **A plan can be refused.** It is validated against the task's scope before
  any code is requested. A plan that would write a protected or undeclared
  path is *rejected* with reasons the coder gets on its next attempt; a plan
  that is merely suspiciously wide -- section 14's "small task proposing dozens
  of unrelated file changes" -- is *escalated*, because only a human can say
  whether the task was under-specified or misread.
- **The allowance is enforced at the write, not in the prompt.** Every edit
  passes a structural check (repository-relative, no `..`), the scope guard, and
  a filesystem check that refuses to write through a symbolic link, before a
  byte is written. A refused edit never touches the tree; it is recorded as
  evidence and fails the attempt with `SCOPE_VIOLATION`.
- **A change set wider than the task allows is refused whole.** Applying the
  first twelve of thirty edits leaves a half-implemented candidate that would
  spend a verification cycle proving it does not work.
- **The completion report keeps the claim and the measurement apart.** What the
  coder says it did is recorded verbatim and believed by nobody; what was
  actually written, refused and measured comes from the applier and the diff. A
  test the coder claims but never wrote is reported as a discrepancy and
  travels with the candidate to the reviewer.
- **The agent returns the coder's failures and raises the endpoint's.** A
  refused plan, unparseable edits or an out-of-scope write come back as a
  `CodingAttempt` carrying a `FailureReason` and the feedback for the next
  attempt. A timeout or an unreachable endpoint raises: that is not the model's
  output, its policy is retry, and the retry ceiling belongs to the workflow.

The scope guard (section 20) is a separate, pure module, used three times: to
check a plan, to permit each write, and to measure the finished diff. It
returns `ALLOW`, `REQUIRE_REVIEW` or `BLOCK`, and one `BLOCK` blocks. Beyond
the task's own file lists and size limits it knows about deletions, binary
files, and the categories section 20 names by hand -- lockfiles, dependency
manifests, migrations, security, payment and CI/deployment files. A change in
one of those the task never declared is `REQUIRE_REVIEW`: not wrong, but never
incidental.

Some paths are never writable whatever a manifest says: `.git/`, `.env` and
friends, keys, the project's own `protected_paths`, and `build.tasks.yaml`
itself -- a coder that can edit its own task can edit its own verification
commands.

A task that declared no file list is not thereby allowed everything: it loses
the allowance check, because there is nothing to check against, and keeps the
size limits, the protected paths and the sensitive-category checks.

## Workers and commands

A project's verification commands are run by the orchestrator, never by a model,
and never in the orchestrator's own process (build.md sections 11 and 12). One
disposable worker is created per task run, holding exactly one writable mount:
that run's worktree.

```bash
./scripts/dev.sh workers          # build the worker images
./scripts/dev.sh workers node     # or just one profile
```

The container is created with the restrictions section 11 lists, and they are
asserted in the test suite rather than trusted:

```text
--volume <the run's worktree>:/workspace:rw   the only writable project mount
--network none                               restricted network by default
--read-only --tmpfs /tmp                      nothing outside the worktree persists
--cap-drop ALL --security-opt no-new-privileges
--cpus / --memory / --pids-limit              resource ceilings
--user <the orchestrator's own uid>           never root; files stay host-owned
```

No Docker socket, no host root filesystem, no `sudo`. `WORKER_BACKEND=subprocess`
runs the same policy and the same capture in a host process instead; it has
**weaker isolation than a container** and exists so the loop can be developed on
a machine without Docker. `/health` reports which backend is configured and
whether it is usable, so a host that cannot verify anything says so before the
first task rather than during it.

**A command is an argument vector, not a shell line.** `CommandPolicy` parses
each configured command and refuses anything that needs a shell:

```text
npm test                     ✓ runs as ["npm", "test"]
npm run compile && npm test  ✗ list two commands; there is no shell to run `&&`
pytest tests/*.py            ✗ nothing would expand the glob
sudo npm test                ✗ never permitted
curl https://example.com     ✗ never permitted
git status                   ✗ every Git operation belongs to GitService
mvn test                     ✗ not on the node profile's list
```

Five rules hold this together:

- **The policy is per profile, with a floor nothing lifts.** A Node project may
  run `npm`; `WORKER_EXTRA_EXECUTABLES` adds to its list, and cannot add
  `sudo`, `docker`, `curl` or `git` -- an operator who wants Git in a worker
  wants something section 10 forbids.
- **A manifest's commands are checked at import time.** A command no worker will
  ever run is a manifest defect, and the moment to say so is the import, not the
  first attempt at the task.
- **A failing command is data, not an exception.** A non-zero exit is the
  pipeline working; it comes back as a `CommandResult` for the caller to
  classify. Only "we could not find out what it would do" -- no runtime, no
  container, a timeout -- raises.
- **Output is bounded and drained.** Each stream is captured up to
  `WORKER_MAX_OUTPUT_BYTES` and clipped with a visible marker, while still being
  read to the end: a command whose pipe fills up would otherwise hang, turning
  an output limit into a deadlock.
- **Secrets are injected individually and masked on the way out.** A worker gets
  a built environment, never this process's own; a credential-shaped name in
  `WORKER_ENV_PASSTHROUGH` is refused; and whatever a build script prints is
  redacted as it is captured, before it reaches the disk or a reviewer.

A command that exceeds its timeout has its whole process group killed, and the
worker is destroyed with it: killing `docker exec` does not stop what it started
inside the container. A timeout records **no exit code**, because it produced no
verdict -- recording 0 would let a caller that checks only the exit code read a
killed test suite as a pass.

## Verification

Nothing reaches a reviewer until the orchestrator has run the project's own
commands against the candidate and seen them pass (build.md section 17). The
governing sentence is short: *never accept a model's statement that a command
passed.* The coder's completion report is never read as evidence; it is kept
beside the pipeline's own record so a later reader can compare claimed against
measured.

```text
scope validation  →  build  →  lint  →  targeted tests  →  security  →  diff policy  →  review
```

- **Scope runs first, before a worker exists.** A candidate that already broke
  its write allowance does not get a container.
- **The pipeline stops at the first failing category.** A candidate that does
  not compile has nothing useful to say about its own tests.
- **Diff policy runs last, after the commands.** A `dist/` that appeared during
  the build is in the worktree whether or not the coder wrote it, and the first
  scope check could not have seen it (concern 20).
- **A category with no command is `SKIPPED`, never `PASSED`.** A project with no
  lint step has not passed lint, and the row says so.
- **`passed` and `verified` are different claims.** `passed` means nothing that
  ran failed; `verified` means nothing failed *and* the orchestrator executed at
  least one command. A project with no profile passes trivially, having proven
  nothing, so a caller deciding whether a candidate has earned a reviewer's time
  gates on `verified`. The importer warns about tasks nothing would verify.
- **`REQUIRE_REVIEW` is a third outcome.** An undeclared change to an
  authentication or payment file is not a defect the coder can fix by trying
  again; it rides on the report for a human to route, and it does not fail the
  run.

Every check leaves a row in `verification_runs` -- what ran, its exit code, its
duration, and the path to its log -- plus `verification.json` for the run as a
whole. A category's commands each get a log under `build/`, `lint/`, `tests/`
or `security/`.

**Failures are classified, never generic** (section 49). Each category maps to
one `FailureReason`, and each reason to one action:

```text
BUILD_FAILED / LINT_FAILED / TEST_FAILED   → SEND_TO_CODER
SCOPE_VIOLATION / SECURITY_FAILED          → ROLLBACK
```

What goes back to the coder is the command, what it returned, and the tail of
what it printed -- the compiler's actual complaint, not a summary of it. A
timeout is recorded as `TIMEOUT` with **no exit code**, because a killed suite
produced no verdict and recording `0` would let a caller that checks only the
exit code read it as a pass.

Security verification (section 19) is deliberately modest: the scope guard owns
protected paths, diff size and deletions; a dependency audit is a command the
project configures; and the scan here reads the *added* lines of the diff for
credential shapes, refuses committed build output and dependency trees, and
flags a binary nobody can review. Read a clean result as "nothing obvious", not
as "no secrets" -- concern 21 says why, and a clipped diff is reported as
unscanned rather than passed quietly.

## Review

A candidate that passed verification is submitted to a `ReviewProvider`
(build.md sections 21 and 22). The reviewer is a boundary, not an endpoint:
`ModelReviewProvider` wraps any `ModelProvider`, so a remote and a local
OpenAI-compatible reviewer differ only in a base URL, and a future adapter
that is not a chat model returns the same `ReviewResult` the workflow already
consumes.

**The package is bounded.** Section 21's rule is that the reviewer must not
receive unrelated repository contents, so a review package is not the coder's
context with a diff attached. It holds the task and its acceptance criteria,
the candidate diff and changed-file list, the starting SHA, the deterministic
verification results, the coder's completion report, open findings from
earlier cycles, the binding ADRs, and only the source the diff cannot be read
without -- a file the task declared read-only, or one a changed file imports.
Nothing is included by keyword match.

When it does not fit, the order of sacrifice is fixed: lessons, then
supporting source, then architecture decisions, and only then is the diff
clipped. A package that had to clip says so in the manifest *and* in the
prompt, and tells the reviewer to answer `HUMAN_REVIEW_REQUIRED` rather than
approve a change it has not fully seen.

**The reviewer's word is not the last word.** A review is reconciled against
its own findings before it is routed:

| The reviewer said | And | The orchestrator does |
| --- | --- | --- |
| `APPROVED` | lists a CRITICAL/HIGH/MEDIUM issue | reads it as `CHANGES_REQUESTED` -- the issues decide |
| `CHANGES_REQUESTED` | lists no issue at all | escalates: there is nothing to send the coder |
| `CHANGES_REQUESTED` | lists only LOW/INFO issues | sends them anyway -- the severities may be under-graded |
| `APPROVED` | confidence below `REVIEW_MIN_CONFIDENCE` | escalates |
| `APPROVED` | the diff touches a gated area | escalates (section 37) |
| `CHANGES_REQUESTED` | the review budget is spent | escalates as `RETRY_EXHAUSTED` (section 23) |

Only blocking issues (CRITICAL, HIGH, MEDIUM) force a retry, which is section
22's rule; LOW and INFO are recorded and cost nothing.

**Human approval gates acceptance, not iteration** (section 37). A change
touching authentication, payments, migrations, CI/CD or dependency manifests
is never accepted automatically -- but a *change request* on the same diff
still goes back to the coder, because nothing is being accepted and gating
every cycle would spend a person's attention watching a model iterate.
Confidence cannot buy a way past the gate; section 21 says so directly.

**A reviewer that fails produces no verdict.** An unreachable endpoint raises
`ReviewerUnavailable` (retryable), an unparseable answer raises
`InvalidModelResponse`, and a review cut off by the output limit is discarded
rather than salvaged -- an "approval" whose issues list was truncated mid-way
is not an approval. There is no default decision anywhere in the path.

An escalation is written in section 24's shape -- the reason, the requirement,
the blocker, the attempts, the reviewer's own concern, the options and the
known-good SHA -- stored in `human_escalations`, and answerable over the API:

```text
GET  /runs/{run_id}/reviews
GET  /escalations
POST /escalations/{escalation_id}/resolve
```

Each displayed option now has a stable key and machine-readable intent. Resolve
with both the explanation and the selected key; the workflow validates that the
option was actually offered, records it, and applies exactly that effect:

```json
{"resolution":"The reviewer concern is acceptable here.","option_key":"A"}
```

A legacy escalation whose stored options predate intents can still be closed,
but cannot trigger repository changes automatically.

**The package is redacted before it leaves this machine** (section 36). Worker
output and log artifacts already were; the package carries the raw diff and the
raw contents of supporting files, which is the largest thing the orchestrator
sends anywhere. `REVIEW_REDACT_PACKAGE` unset means *on unless the reviewer is
on this host* -- an empty `REVIEW_BASE_URL` counts as local, because there is no
endpoint to leak to. The inputs are masked rather than the rendered text, so the
stored package, its hash and the prompt actually sent are the same bytes; the
manifest records `redacted` either way, so a finding about a masked line is
readable later as one. Masking costs the reviewer the ability to comment on that
line, which is why it is a setting and not a rule.

## The fix loop

`run_fix_loop` is the whole of a task's work, not only its corrections: one turn
is a coding attempt, its verification and -- if there is anything to review -- its
review, and a task that is right first time passes through one turn and comes
back approved. Section 23's order, with section 23's own governing sentence:
**never loop indefinitely.**

```text
code -> verify -> review -> approved
          |         |
          |         +-- changes requested -> next turn, with the findings
          +-- build/lint/test failed -> next turn, with the command output
```

**One turn spends exactly one coding attempt**, so the loop is bounded by
`max_attempts` structurally -- a `for` over the attempts the task allows, not a
ceiling check that could be got wrong. The review-cycle ceiling is enforced
where the cycle is counted, inside `route_review`, for the same reason. A caller
may lower a run's budget below the manifest's; it cannot raise it.

**What ends a run comes from `domain.failure_policy`** -- one action per failure
class, so no failure reaches a generic handler:

| Outcome | Action | What the loop does |
| --- | --- | --- |
| build, lint or tests failed | `SEND_TO_CODER` | next turn, carrying the real command and its real output |
| review requested changes | `SEND_TO_CODER` | next turn, carrying the actionable blocking issues |
| edits unusable, nothing applied | `SEND_TO_CODER` | next turn, carrying what could not be applied |
| scope violation, secret in the diff | `ROLLBACK` | reset the worktree, run `FAILED`, no reviewer, no escalation |
| attempts spent | `ESCALATE` | escalation in section 24's shape, run `FAILED`, candidate preserved |
| review cycles spent | `ESCALATE` | the reviewer's own escalation, which can say more |
| a writable file was clipped out of context | `ESCALATE` | refused before the coder is called at all (concern 1) |

**The loop writes no feedback of its own.** A deterministic failure travels as
`VerificationReport.feedback` and a review as `ReviewRouting.feedback`; the loop
chooses which the next attempt is given. A correction prompt assembled by the
thing counting the retries would be describing its own summary of the evidence
rather than the evidence.

**A correction attempt does not re-plan.** The approach was planned and
validated on the first attempt, and the reviewer's findings are what the fix is
following; re-planning would spend a call on a plan nobody asked for and risk
refusing the attempt over an approach that is not the subject.

**An issue is closed by a reviewer, not by an attempt.** After each cycle, the
earlier findings the new review did *not* raise again are marked resolved -- the
coder's claim to have fixed something is exactly the kind of claim this system
does not believe. The identity used is the requirement the finding cites, the
file it points at and the category it was filed under, never the prose, which a
reviewer recomposes every cycle.

**The loop itself still does not commit, tag, push or complete.** It stops at
`APPROVED`; the graph's delivery node then performs those operations in order.
Keeping that boundary means the agent loop cannot decide to land its own work.
An escalated run is marked `FAILED` and its candidate is preserved until the
human answers; the resolution handler then either lands it or releases the
worktree.

## Experience capture

### Lessons

A lesson is an instruction the system has learned from a review finding that a
later reviewer did **not** re-raise. That is the whole bar: a finding the coder
was never told is fixed is not evidence of anything, so only resolved findings
are ever considered. A run that passed first time has no fix cycle and proposes
nothing.

Proposal is deterministic and involves no model call. Findings are filtered to
the severities and categories worth teaching from, then grouped by category and
file, so several findings about one thing become one instruction rather than
three near-duplicates. A finding's `required_fix` is usually written as a
description ("the null check in the loader is missing") and is converted to an
imperative ("Add the null check in the loader."), because a rule phrased as a
defect is ambiguous about what to do.

The same finding arriving twice is **one lesson with two occurrences**, not two
lessons. The lesson is found by the triple that identifies a finding -- its
requirement, its file and its category -- the count rises, and confidence is
re-derived from that count. Occurrences are counted per distinct run, so one
review cycle filing the same requirement twice, or a run being proposed from
twice after a restart, cannot inflate the count. Confidence is not a measure of
how right a lesson is; nothing available can be. It is how often the same thing
has been found, and it is the only evidence behind rule 2.

Every candidate is traceable before it can be trusted: a lesson that cites no
review issue cannot be approved, and approval cannot be reached by writing a row
directly with the fields omitted. Each lesson carries the review issue, task and
run it came from, the requirement it cites and the file it was raised against,
and those are returned by the API, so the text a person approves can be checked
against the finding that produced it.

A project's lessons stay in that project. Approval never changes scope, and no
code path sets a lesson's project to `None`, so a project lesson cannot become
a global one.

The lifecycle:

```text
proposed -> approved -> retired
    |                    
    +------> rejected
```

`retired` is the state for a lesson that *was* approved and used and later
stopped being true. It is deliberately not `rejected`: the counters on it are
evidence about guidance that was being followed, and a rejected proposal is not.

```text
GET  /lessons?project_id=&status=&limit=      the queue, proposed by default
GET  /lessons/{lesson_id}                      with its source
POST /lessons/{lesson_id}/approve              {"approved_by": "..."}
POST /lessons/{lesson_id}/reject               {"reason": "..."}
POST /lessons/{lesson_id}/retire               {"reason": "..."}
GET  /lessons/{lesson_id}/usefulness           sent, applied, and the ratio
POST /runs/{run_id}/lessons/propose
```

Only `approved` is retrieved for a coder. That is enforced in the repository's
default rather than by convention, so adding a retrieval site cannot accidentally
put an unvetted lesson in a prompt. Retrieval is bounded and deterministic, in
this order: language, framework, category, keyword and tag overlap with the
task's own words, then recurrence, then prior usefulness. An approved lesson
gets a one-point floor so a relevant proposal outranks a generic approved
lesson -- the floor stops a keyword match from being the only thing that
matters, not the reverse.

Retrieval and application are counted separately. A lesson sent to a coder and
one they acted on are different facts, and the ratio between them is the only
honest measure of whether the guidance is landing.

### Training examples

An accepted run is preserved: its artifacts are copied into
`data/training/<run-id>/` with a `manifest.json` naming what was kept and a
SHA-256 of it, and a `training_examples` row indexes it. Only `SUCCEEDED` runs
are captured. A rejected run's artifacts stay under its run directory and are
never filed as training data -- training on the run that got rejected teaches
the rejection.

Capture is idempotent per run and preserves a curation decision: delivery can be
re-entered after a restart, and re-entry must not re-file an example somebody
has already ruled on. Every example starts `captured`; section 34 says not to
train on every accepted example, so selection is a separate, manual act
(`selected` or `excluded`) and phase M owns it.

```text
GET  /projects/{project_id}/training?status=   a project's captured examples
GET  /runs/{run_id}/training                   the example for one run, or null
POST /runs/{run_id}/training/capture           backfill an accepted run
```

A `status` filter stays inside the project it was asked about.

### Outcome records

Every settled run -- accepted **or** rejected -- gets an `outcome.json` in its
run directory: the outcome and why it failed, attempts, review cycles, the token
cost, and the full review history including every finding. The database stays
authoritative; the file is the same numbers, denormalised so a failed run is
readable on its own once the database is gone. The fix loop writes the rejected
version, delivery writes the accepted one, and the payload shape is identical
either way so a query over outcomes does not have to know which path produced
the run.

### Metrics

Section 35's figures, all scoped to one project unless the endpoint says
otherwise:

```text
GET /projects/{project_id}/metrics            runs, reviews, lessons, training, models
GET /tasks/{task_id}/metrics                  the same run figures for one task
GET /models/metrics?project_id=               per-model cost, tokens and purpose split
GET /projects/{project_id}/lesson-metrics     counts by status, and unused approved ones
GET /projects/{project_id}/training-metrics   captured, and captured-but-unselected
GET /tasks/{task_id}/review-history           every review of a task, across its runs
GET /projects/{project_id}/review-history
GET /projects/{project_id}/recurring-findings what this project keeps getting wrong
```

Rates are only computed over runs that finished. A run still in flight is not a
failure, and an abandoned run stays in the denominator because a run nobody
finished is a run the orchestrator did not deliver. `first_pass_rate` is
reported beside `success_rate` because a project where most tasks pass on
attempt 3 and one where most pass on attempt 1 have the same success rate, and
only the first is producing work that was right the first time.

`recurring-findings` groups by the same identity the lesson system uses, and
only reports findings seen more than once: a review raising something once is an
opinion, and a list of every finding ever raised would bury the two that have
now happened four times each. Each entry says how many times it was seen, how
many of those are still open, and the severity mix.

A project view does not include another project's numbers, and a
`/projects/{id}/...` route for a project that does not exist is a 404 rather than
an empty report. The cross-project endpoints (`/lessons?project_id=...`,
`/models/metrics?project_id=...`) treat `project_id` as a filter instead, and
answer with an empty result for a project nothing matches.

## LangGraph workflow, pause, and recovery

`WorkflowRunner` executes one task at a time through persisted nodes:

```text
load task -> prepare/attach worktree -> safe pause boundary
          -> code/verify/review loop
          -> deliver approved candidate | preserve escalation | release failure
```

The graph state contains identifiers and routing facts only. Context assembly,
model calls, verification, review policy, Git operations, task transitions, and
human-resolution effects remain in domain/services and are independently
testable. `workflow_checkpoints` and `workflow_writes` contain LangGraph's opaque
cursor data; normal task, event, review, verification, and artifact tables remain
the authoritative history.

Pause and resume are available for both scopes:

```text
POST /projects/{project_id}/pause
POST /projects/{project_id}/resume
POST /tasks/{task_id}/pause
POST /tasks/{task_id}/resume
```

An active task finishes its current service call before honouring a request; an
old worker container is never assumed to exist. `inspect_incomplete_runs`
classifies incomplete runs as resumable, paused, waiting for a human, or missing
their recorded worktree. `WorkflowRunner.recover_incomplete()` resumes only the
safe class. Delivery is idempotent across the unavoidable Git/database boundary:
if Git committed immediately before a crash, recovery reconciles the clean
worktree HEAD into the run instead of trying to make an empty second commit.

Completed, rolled-back, and answered worktrees are released. `/health` reports
the total, releasable, and unclaimed counts; unknown directories are reported
but never deleted.

## Run artifacts

Every run gets a stable identifier and a directory (section 9):

```text
data/runs/RUN-20260926-000001/
├── context-manifest.json   what the coder was given, and why
├── context.md              the text itself
├── plan-prompt.txt         the planning request, exactly as sent
├── plan-response.txt       what came back, before parsing
├── plan.json               the plan and the validation verdict
├── prompt.txt              the coding request, exactly as sent
├── coder-response.txt      what came back, before parsing
├── candidate.patch         the diff the attempt produced
├── completion-report.json  claimed beside measured
├── verification.json       every check, its verdict and its evidence
├── review-package.txt      exactly what the reviewer was shown
├── review-package.json     its manifest: hash, sources, what was omitted
├── review-prompt.txt       the review request, exactly as sent
├── review-response.txt     what came back, before parsing
├── review.json             the verdict, its issues and how it was routed
├── escalation.txt          the human-facing summary, when one was needed
├── fix-loop.json           every turn of the loop, and what ended it
├── outcome.json            the settled outcome: what happened, at what cost
├── build/01-npm-run-compile.log
├── tests/01-npm-test.log   one log per command, under its category
└── attempt-2-cycle-2/      the same names again, for the next turn
```

Every model call also leaves a `model_runs` row -- purpose (`PLAN`, `CODE`,
`FIX`, `REVIEW`), status, tokens, duration and both artifact paths -- pointing
at a `models` row. A provider configured from the environment is registered
into `models` on its first call, so a call is attributable whether or not an
operator registered the endpoint. A failed call is recorded too: how often an
endpoint times out is a question sections 34 and 35 need answerable.

The database holds a path, a size and a SHA-256; the bytes stay on disk. The
first turn of a run writes section 9's plain names; every turn after it writes
the same names under `attempt-N-cycle-M/`, where `M` is the cycle the work
belongs to -- the one whose review will judge it, not the one already finished
(concern 33). Collision-freedom does not depend on the cycle: every coding
attempt advances `N`, so two turns cannot share a directory even when a
verification failure means no cycle was spent between them.

`fix-loop.json` is the index over all of it: one entry per turn, naming the
stage it stopped at, the failure reason, the directory its files are in and the
issues that cycle closed. It is written at the run's root because it is the one
artifact about the run as a whole. `outcome.json` is written alongside it once
the run settles, on both paths -- an accepted run and a run nobody could finish
both get one, and the accepted run's artifacts are then copied into
`data/training/<run-id>/` with a `manifest.json` naming what was kept. The
final committed patch is still not written beside them; that gap is tracked in
[`concerns.md`](concerns.md).

## Configuration

Infrastructure comes from the environment (`.env`); project-specific behaviour
comes from each managed repository's `build.tasks.yaml`. See `.env.example`.

Three defaults are deliberate and should stay that way until the loop is proven:
`GIT_PUSH_ENABLED=false`, `WORKER_BACKEND=docker` and `WORKER_NETWORK=none`.
The last one means a verification command cannot install dependencies -- the
repository must already have them (concern 12).

`CONTEXT_MAX_TOKENS=0` is a third: leaving the budget derived from the served
window means raising `LOCAL_MODEL_CONTEXT_WINDOW` for a bigger endpoint is one
change, not two that can disagree. `REVIEW_MAX_PACKAGE_TOKENS=0` works the same
way against `REVIEW_CONTEXT_WINDOW`.

`REVIEW_HUMAN_APPROVAL_ENABLED=true` is the fourth, and the one with the
sharpest edge: turning it off lets the orchestrator accept changes to
authentication, payments, migrations, CI/CD and dependency manifests with no
person in the loop. It exists for a sandbox project and nothing else.

`REVIEW_REDACT_PACKAGE` is the fifth and is deliberately *unset*, which means
"on unless the reviewer is on this machine". Setting it either way is an
operator's call: `false` against a remote endpoint sends a repository's raw diff
to a third party, and `true` against a local one costs the reviewer the ability
to comment on a masked line for no gain.

The limits that bound a run -- attempts, review cycles, runtime, files changed,
and diff lines -- are **not** here. They come from each task in
`build.tasks.yaml`, because they are properties of a task and not of an
installation. `max_runtime_minutes` is checked before every loop turn; an
already-running command is allowed to settle safely. `WORKER_TIMEOUT_SECONDS`
also caps the shared verification worker across all of its commands.

## Tests

```bash
./scripts/dev.sh test              # everything
./scripts/dev.sh test -m "not integration"   # unit only
./scripts/dev.sh lint
```

The Phase M deterministic acceptance gate can also be run on its own:

```bash
./scripts/dev.sh test tests/integration/test_phase_m.py
```

It uses an isolated SQLite database and subprocess workers for portability.
Before trusting an important repository, also build the applicable Docker
worker images, restart the real PostgreSQL/orchestrator services, and run the
same low-risk workload with the configured coder and reviewer models.
# development-orchestrator
