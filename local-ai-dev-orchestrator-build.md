# Local AI Development Orchestrator --- build.md

## 0. Purpose

Build a Docker-first local AI software-development orchestrator that can
take a structured project build plan, select the next eligible task,
assemble the minimum useful repository context, send the task to a local
coding model, independently build/test/lint/security-check the result,
commit the candidate changes to Git, submit the bounded change set to a
stronger review provider, route review feedback back to the coding
model, learn reusable lessons from accepted review findings, and
continue until the project is complete or a human decision is required.

This system is not a chatbot and the LLM is not the workflow engine.
Deterministic application code owns task state, Git, command execution,
limits, retries, persistence, security boundaries, and transitions.
LangGraph coordinates the stateful agent workflow.

The first useful release must prove this loop:

``` text
Load Task
   ↓
Build Context
   ↓
Local Coder
   ↓
Build / Lint / Tests
   ↓
Git Diff
   ↓
Reviewer
   ↓
┌───────────────┐
│               │
APPROVE     CHANGES REQUESTED
│               │
│               └──→ Local Coder → Verify → Review
│
Commit / Push
   ↓
Complete Task
   ↓
Next Task
```

------------------------------------------------------------------------

# 1. Core Design Principles

1.  **LLMs are workers, not supervisors.** Application code owns
    workflow state.
2.  **Git is the source-code contract.** Every coding task has a known
    starting SHA and an auditable diff.
3.  **Verification is deterministic.** Never trust an agent merely
    saying that tests passed.
4.  **Small bounded tasks.** A coder receives one task and only the
    repository context needed for it.
5.  **Review against requirements.** Review is performed against
    explicit acceptance criteria, not general code aesthetics.
6.  **Safe failure.** Failed attempts must not leave the main repository
    in an unknown state.
7.  **Human escalation is a feature.** The system stops rather than
    inventing architectural decisions.
8.  **Provider independence.** Local coder and reviewer providers must
    be replaceable.
9.  **Record experience from day one.** Preserve prompts, context
    metadata, attempts, reviews, fixes, outcomes, and reusable lessons.
10. **No silent local-to-cloud fallback.** A provider change must be
    explicit in configuration/policy.
11. **Containers isolate coding workers.** Autonomous code execution
    must not receive unrestricted host access.
12. **Start small.** Do not add distributed infrastructure until the
    single-machine workflow proves it is necessary.

------------------------------------------------------------------------

# 2. Recommended Technology Stack

## Backend / Orchestrator

-   Python 3.12+
-   FastAPI
-   Pydantic
-   SQLAlchemy 2.x
-   Alembic
-   LangGraph
-   PostgreSQL
-   psycopg
-   Git CLI behind a GitService abstraction
-   Docker Engine API/SDK or controlled Docker CLI behind a
    WorkerService abstraction

## Local AI

Support an OpenAI-compatible provider abstraction.

Initial targets:

-   llama.cpp server
-   Ollama
-   other OpenAI-compatible local endpoints

Do not make Ollama-specific concepts part of the domain model.

## Reviewer

Implement a `ReviewProvider` abstraction.

Possible implementations:

-   OpenAI-compatible remote reviewer
-   OpenAI-compatible local reviewer
-   future provider adapters

The workflow must consume the same structured `ReviewResult` regardless
of provider.

## Frontend

Do not build the dashboard in V1.

Later:

-   Angular
-   REST API to FastAPI
-   optional WebSocket/SSE run-status updates

## Infrastructure

-   Ubuntu host
-   Docker Compose for long-lived services
-   disposable Docker worker containers for coding tasks
-   local model server may initially run directly on Ubuntu for simpler
    GPU management

Do not introduce Kubernetes, Kafka, RabbitMQ, Redis, Temporal, or
separate vector infrastructure in V1.

------------------------------------------------------------------------

# 3. High-Level Architecture

``` text
                        USER
                          │
                          ▼
                 Project / build.md
                          │
                   build.tasks.yaml
                          │
                          ▼
              ┌─────────────────────┐
              │    ORCHESTRATOR     │
              │ FastAPI + LangGraph │
              └──────────┬──────────┘
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
     PostgreSQL       GitService    ResourceManager
          │              │              │
          └──────────────┼──────────────┘
                         ▼
                    Task Scheduler
                         │
                         ▼
                   Context Builder
                         │
                         ▼
               Disposable Worker
                         │
                         ▼
                  Local Coder LLM
                         │
                         ▼
              Build / Lint / Tests
                         │
                         ▼
                      Git Diff
                         │
                         ▼
                   Review Provider
                         │
              ┌──────────┼──────────┐
              ▼          ▼          ▼
           APPROVE      FIX       ESCALATE
              │          │          │
              │          └→ Coder   └→ Human
              ▼
       Experience Capture
              │
       ┌──────┼──────────┐
       ▼      ▼          ▼
    Reviews Lessons  Model Metrics
              │
              ▼
         Commit / Push
              │
              ▼
           Next Task
```

------------------------------------------------------------------------

# 4. Repository Layout

Use a monorepo-style layout:

``` text
local-dev-orchestrator/
├── README.md
├── build.md
├── pyproject.toml
├── .env.example
├── .gitignore
├── docker-compose.yml
├── alembic.ini
│
├── apps/
│   └── orchestrator/
│       ├── main.py
│       ├── api/
│       ├── config/
│       ├── db/
│       ├── domain/
│       ├── repositories/
│       ├── services/
│       ├── providers/
│       ├── agents/
│       ├── workflow/
│       └── schemas/
│
├── workers/
│   ├── node/
│   │   └── Dockerfile
│   ├── java/
│   │   └── Dockerfile
│   └── python/
│       └── Dockerfile
│
├── migrations/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
│
├── scripts/
│
└── data/
    ├── artifacts/
    ├── runs/
    └── training/
```

Keep LangGraph-specific code under `workflow/`. Domain and service
classes must not require LangGraph.

------------------------------------------------------------------------

# 5. Project Manifest

Each managed repository should contain a human-readable `build.md` and a
machine-readable `build.tasks.yaml`.

Example:

``` yaml
version: 1

project:
  id: tracestack
  name: TraceStack
  repository: /workspace/tracestack
  default_branch: main

runtime:
  worker_profile: node
  max_parallel_tasks: 1

model_policy:
  default_coder: qwen-coder-14b
  high_complexity_coder: qwen-coder-30b
  reviewer: primary-reviewer

verification:
  milestone_interval: 5

protected_paths:
  - ".git/**"
  - ".env"
  - "secrets/**"

tasks:
  - id: TS-001
    section: 1
    title: Scaffold extension
    status: pending
    complexity: low
    depends_on: []

    limits:
      max_attempts: 3
      max_review_cycles: 3
      max_runtime_minutes: 30
      max_files_changed: 12
      max_diff_lines: 1200

    verify:
      - npm run compile
      - npm test

  - id: TS-002
    section: 2
    title: Navigation core
    status: pending
    complexity: medium
    depends_on:
      - TS-001

    verify:
      - npm run compile
      - npm test
```

The database is authoritative for runtime status after import. The
manifest describes the intended project/task graph and can be
re-synchronized deliberately.

------------------------------------------------------------------------

# 6. Task Specification Contract

Every implementation task should have:

-   unique task ID
-   title
-   goal
-   prerequisites/dependencies
-   files to inspect
-   files allowed to modify/create
-   implementation requirements
-   acceptance criteria
-   required tests
-   verification commands
-   constraints/non-goals
-   risk level
-   maximum attempts
-   maximum review cycles
-   completion-report requirements
-   explicit stop boundary

A task should be executable without requiring the coder to infer the
next feature.

------------------------------------------------------------------------

# 7. Core Domain Models

Implement domain models independent of FastAPI, SQLAlchemy, LangGraph,
Docker, and any LLM SDK.

Minimum models:

## Project

``` text
id
name
repository_path
default_branch
worker_profile
status
created_at
updated_at
```

## Task

``` text
id
project_id
external_task_id
title
section
instructions
complexity
risk_level
status
depends_on
max_attempts
max_review_cycles
max_runtime_minutes
max_files_changed
max_diff_lines
created_at
updated_at
```

## TaskRun

``` text
id
task_id
run_number
attempt_number
status
coder_model_id
worker_image
starting_commit
candidate_commit
started_at
completed_at
context_hash
prompt_version
failure_reason
```

## VerificationRun

``` text
id
task_run_id
verification_type
command
exit_code
stdout_artifact
stderr_artifact
duration_ms
status
created_at
```

## Review

``` text
id
task_run_id
reviewer_provider
reviewer_model
decision
confidence
risk
summary
created_at
```

## ReviewIssue

``` text
id
review_id
severity
category
file
line
requirement_id
problem
required_fix
resolved
```

## Lesson

``` text
id
project_id nullable
language nullable
framework nullable
category
title
lesson
source_review_issue_id
confidence
times_retrieved
times_applied
created_at
updated_at
```

## Model

``` text
id
provider
model_name
role
endpoint
enabled
metadata
```

## ModelRun

``` text
id
task_run_id
model_id
purpose
started_at
completed_at
input_tokens nullable
output_tokens nullable
duration_ms
status
artifact references
```

## HumanEscalation

``` text
id
task_id
task_run_id
reason
summary
options
status
resolution
created_at
resolved_at
```

## Milestone

``` text
id
project_id
name
after_task_count
status
starting_commit
ending_commit
created_at
completed_at
```

------------------------------------------------------------------------

# 8. PostgreSQL Responsibilities

PostgreSQL stores operational state and searchable history:

-   projects
-   tasks and dependencies
-   task runs
-   model configuration metadata
-   model runs
-   verification results
-   reviews
-   review issues
-   lessons
-   lesson usage
-   milestones
-   human escalations
-   run events
-   artifact metadata

Do not store very large source snapshots, huge logs, or giant patches
directly in normal table columns.

Store large artifacts under `data/` initially and save their paths plus
hashes in PostgreSQL.

Later, object storage may replace local artifact storage without
changing domain contracts.

------------------------------------------------------------------------

# 9. Artifact Store

Every run gets a stable identifier:

``` text
RUN-20260919-000042
```

Suggested structure:

``` text
data/runs/RUN-20260919-000042/
├── task.json
├── context-manifest.json
├── prompt.txt
├── coder-response.txt
├── starting-sha.txt
├── candidate.patch
├── build/
├── tests/
├── lint/
├── security/
├── review.json
├── correction-prompt.txt
├── final.patch
└── outcome.json
```

Store hashes for important artifacts.

The run must be reconstructable enough to answer:

-   What task was attempted?
-   Which model/version handled it?
-   What context was supplied?
-   What commit did it start from?
-   What changed?
-   What commands ran?
-   What failed?
-   What did the reviewer say?
-   What was corrected?
-   What commit was accepted?

------------------------------------------------------------------------

# 10. Git Service

Create a deterministic `GitService`.

Required operations:

``` text
get_current_branch
get_head_sha
ensure_clean_worktree
create_task_branch
checkout_branch
create_worktree
remove_worktree
get_status
get_diff
get_changed_files
get_diff_line_count
commit
push
reset_hard_to_sha
tag_checkpoint
```

Rules:

1.  Never allow an LLM to construct arbitrary Git commands and execute
    them directly.
2.  The orchestrator chooses branch names.
3.  Record starting SHA before any work.
4.  Refuse to begin against an unexpectedly dirty workspace unless
    policy explicitly allows it.
5.  Never force-push by default.
6.  Never automatically push directly to the protected default branch.
7.  Each task must be traceable to a task branch/commit.
8.  A failed task can be reset to its known starting SHA.
9.  Commit messages should include the task ID.

Example:

``` text
agent/TS-004-navigation-tree
```

Commit:

``` text
TS-004: implement navigation tree
```

------------------------------------------------------------------------

# 11. Worker Container Model

Long-lived containers:

-   orchestrator
-   PostgreSQL
-   future dashboard

Disposable containers:

-   one coding worker per active task/worktree

Initial worker profiles:

## Node Worker

Include:

-   Node LTS/current project-compatible version
-   npm
-   npx
-   Git
-   common build utilities

## Java Worker

Include:

-   configured JDK versions
-   Maven
-   Gradle
-   Git

## Python Worker

Include:

-   Python
-   pip/uv as selected
-   Git

Worker policy:

-   repository/worktree is the only required writable project mount
-   no Docker socket by default
-   no host root filesystem
-   no `sudo`
-   restricted network by default
-   explicit command allow/policy layer
-   runtime timeout
-   CPU/RAM limits
-   optional GPU access only when genuinely needed
-   secrets injected individually, never wholesale host environment

Destroy worker after task completion/failure unless retained temporarily
for debugging.

------------------------------------------------------------------------

# 12. Command Execution Service

Create a `CommandRunner` separate from the LLM.

Responsibilities:

-   run approved commands
-   stream/capture stdout/stderr
-   enforce timeout
-   enforce output-size limits
-   redact secrets
-   record exit code
-   save logs as artifacts
-   classify common failures

The agent may request an operation, but the service decides whether it
is permitted.

Do not expose a raw unrestricted host shell to the model.

------------------------------------------------------------------------

# 13. Model Provider Abstraction

Create a generic model interface.

Conceptually:

``` python
class ModelProvider(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResponse:
        ...
```

`ModelRequest` should support:

-   system instructions
-   task instructions
-   bounded repository context
-   previous review feedback
-   structured-output schema when required
-   temperature/settings
-   timeout

Implement an OpenAI-compatible local provider first.

Provider configuration must include:

``` text
provider_id
base_url
model_name
role
timeout
context_window
enabled
```

No silent fallback between local and cloud providers.

------------------------------------------------------------------------

# 14. Coding Agent

The coding agent receives:

1.  exact task specification
2.  project conventions
3.  relevant architectural decisions
4.  bounded source context
5.  relevant tests
6.  relevant lessons
7.  previous review findings when retrying
8.  explicit allowed scope
9.  explicit completion format

For medium/high-complexity tasks, require a plan before code
modification.

Plan example:

``` json
{
  "filesToInspect": [],
  "filesToModify": [],
  "filesToCreate": [],
  "approach": [],
  "risks": [],
  "expectedTests": []
}
```

Validate the plan before allowing execution.

Reject or escalate suspicious plans such as a small task proposing
dozens of unrelated file changes.

------------------------------------------------------------------------

# 15. Context Builder

The Context Builder is a first-class subsystem.

Goal: give the coder the smallest useful slice of the repository.

Inputs:

-   task
-   task-declared files
-   repository structure
-   relevant interfaces/types
-   relevant tests
-   project configuration
-   architecture decisions
-   previous task output when necessary
-   retrieved lessons

Priority:

1.  exact task instructions
2.  files explicitly named by the task
3.  interfaces/types used by those files
4.  relevant tests
5.  configuration/build files
6.  relevant architecture decisions
7.  recent relevant accepted changes
8.  relevant lessons

Do not automatically send the entire repository.

Record a `context-manifest.json` containing file names, hashes, reason
included, and truncation information.

Implement configurable context budgets.

------------------------------------------------------------------------

# 16. Project Memory / Architecture Decisions

Use explicit project memory rather than conversational memory.

Suggested project structure:

``` text
.ai/
├── project.md
├── architecture.md
├── decisions/
│   ├── ADR-001.md
│   └── ADR-002.md
├── policies/
├── task-overrides/
└── local-notes/
```

Architecture Decision Records should contain:

``` text
ID
Title
Status
Context
Decision
Consequences
Date
```

The Context Builder retrieves relevant decisions.

The reviewer must check for architecture drift against applicable
decisions.

------------------------------------------------------------------------

# 17. Verification Pipeline

Run verification before expensive AI review.

Order:

``` text
scope validation
      ↓
compile/build
      ↓
lint/static analysis
      ↓
targeted tests
      ↓
security checks
      ↓
diff policy checks
      ↓
AI review
```

If build/test/lint fails, send the deterministic failure back to the
coder before reviewer invocation.

Never accept a model's statement that a command passed. The orchestrator
must execute it.

------------------------------------------------------------------------

# 18. Verification Profiles

Each project defines commands.

Example Node:

``` yaml
verification:
  build:
    - npm run compile
  lint:
    - npm run lint
  tests:
    - npm test
```

Example Java:

``` yaml
verification:
  build:
    - ./mvnw -q -DskipTests package
  tests:
    - ./mvnw test
```

Commands must be project-controlled/configured rather than invented
freely by the model.

------------------------------------------------------------------------

# 19. Security Verification

V1 security checks can be modest but the abstraction should exist.

Possible checks:

-   dependency audit where appropriate
-   secret scanning
-   protected-path checks
-   forbidden file detection
-   unexpected binary detection
-   diff-size checks
-   suspicious generated files
-   configuration policy checks

Security-sensitive tasks may always require human review.

------------------------------------------------------------------------

# 20. Scope Guard

Before review, compare actual changes against task scope.

Check:

-   number of files changed
-   diff lines
-   protected paths
-   allowed paths
-   deleted files
-   dependency changes
-   lockfile changes
-   migrations
-   authentication/security files
-   payment files
-   CI/deployment files

Policy can return:

``` text
ALLOW
REQUIRE_REVIEW
BLOCK
```

------------------------------------------------------------------------

# 21. Review Provider

Reviewer receives a bounded package:

-   task requirements
-   acceptance criteria
-   relevant architecture decisions
-   starting SHA
-   candidate SHA or patch
-   changed-file list
-   deterministic verification results
-   applicable lessons/policies
-   only additional source required to understand the diff

Reviewer should not receive unrelated repository contents.

Structured response:

``` json
{
  "taskId": "TS-004",
  "decision": "CHANGES_REQUESTED",
  "confidence": 0.93,
  "risk": "MEDIUM",
  "summary": "Implementation is mostly correct but misses selection restoration.",
  "issues": [
    {
      "severity": "HIGH",
      "category": "requirement",
      "file": "src/providers/navigation-tree-provider.ts",
      "line": 84,
      "requirementId": "TS-004-R7",
      "problem": "Saved selection is not restored.",
      "requiredFix": "Restore the stored selection after opening the document."
    }
  ]
}
```

Allowed decisions:

``` text
APPROVED
CHANGES_REQUESTED
HUMAN_REVIEW_REQUIRED
```

Confidence must not override mandatory human-review policy.

------------------------------------------------------------------------

# 22. Review Responsibilities

Reviewer checks two dimensions:

## Task Compliance

-   acceptance criteria satisfied
-   required tests exist
-   implementation behavior matches task
-   no requested requirement omitted
-   no unrequested major feature added

## Architecture Compliance

-   established ADRs followed
-   layering respected
-   no forbidden dependencies
-   no unnecessary coupling
-   provider boundaries preserved
-   no architectural shortcuts that create later lock-in

Reviewer should distinguish:

-   blocking issues
-   non-blocking observations
-   optional future improvements

Only blocking issues should force a retry.

------------------------------------------------------------------------

# 23. Fix Loop

When review requests changes:

1.  store review and issues
2.  create a correction prompt containing only actionable blocking
    issues
3.  provide the coder with required relevant context
4.  coder modifies candidate worktree
5.  rerun deterministic verification
6.  regenerate diff
7.  rerun review
8.  increment review cycle

Default limits:

``` text
max coding attempts: 3
max review cycles: 3
```

After the limit, create a human escalation.

Never loop indefinitely.

------------------------------------------------------------------------

# 24. Human Escalation

Human escalation should be concise and decision-oriented.

Example:

``` text
TASK TS-017 — HUMAN REVIEW REQUIRED

Reason:
Three implementation attempts failed.

Requirement:
Capture native Go-to-Definition navigation.

Current blocker:
The selected VS Code event path cannot reliably distinguish
ordinary cursor movement from definition navigation.

Attempts:
1. active editor listener
2. selection listener
3. document navigation heuristic

Reviewer concern:
The implementation would record false navigation edges.

Options:
A. Use TraceStack-owned Go To Definition command.
B. Accept heuristic native detection.
C. Defer native interception.

Current repository:
Restored to known-good SHA abc123.
```

The human should not have to reconstruct the history manually.

------------------------------------------------------------------------

# 25. Retry and Recovery

Every task begins from a known-good SHA.

If a task fails beyond policy:

``` text
restore/reset disposable worktree
mark run failed
preserve artifacts
create escalation
do not merge/push unsafe candidate
```

Independent tasks may continue only if dependency and
repository-isolation rules prove they are unaffected.

------------------------------------------------------------------------

# 26. Task Dependency Graph

Tasks form a DAG.

States:

``` text
PENDING
READY
PLANNING
CODING
VERIFYING
REVIEW_PENDING
REVIEWING
CHANGES_REQUESTED
APPROVED
COMPLETE
BLOCKED
FAILED
HUMAN_REVIEW
PAUSED
```

A task is `READY` only when all dependencies are `COMPLETE`.

V1 executes one task at a time.

Parallel scheduling comes later.

------------------------------------------------------------------------

# 27. LangGraph Workflow

LangGraph coordinates workflow state but must call domain/services for
actual operations.

Initial nodes:

``` text
load_task
prepare_workspace
build_context
plan_task
validate_plan
run_coder
validate_scope
run_build
run_lint
run_tests
run_security
prepare_review
run_review
route_review
capture_lessons
commit_candidate
push_candidate
complete_task
create_escalation
```

Conditional edges handle:

-   plan rejected
-   verification failed
-   review changes requested
-   approval
-   retry exhausted
-   mandatory human gate
-   pause request

Persist sufficient workflow state so runs can resume after orchestrator
restart.

------------------------------------------------------------------------

# 28. Pause / Resume

Support:

``` text
pause project
pause task
resume project
resume task
```

Pause must not corrupt worker state.

Prefer pausing at safe boundaries.

On restart, PostgreSQL is used to identify incomplete runs and determine
whether they can safely resume or need workspace reconciliation.

Never assume an old worker container still exists.

------------------------------------------------------------------------

# 29. Milestones

Milestones are stronger verification gates.

Default suggestion: every 5 completed tasks, configurable per project.

Milestone validation:

-   clean repository state
-   full build
-   full lint
-   full test suite
-   integration tests if configured
-   architecture review
-   security scan
-   checkpoint/tag

Only continue automatically when milestone policy passes.

------------------------------------------------------------------------

# 30. Resource Manager

V1 may only expose resource information and enforce concurrency = 1.

Design for:

-   available system RAM
-   CPU load
-   GPU inventory
-   VRAM
-   loaded model(s)
-   active workers
-   active builds
-   configured model requirements

Later the scheduler can decide whether two independent tasks may run
simultaneously.

Do not build a complicated GPU scheduler in V1.

------------------------------------------------------------------------

# 31. Model Routing

Model selection should be policy-driven.

Example:

``` yaml
model_policy:
  default_coder: qwen-coder-14b

  routes:
    low:
      coder: qwen-coder-7b
    medium:
      coder: qwen-coder-14b
    high:
      coder: qwen-coder-30b

  escalation:
    promote_after_failed_attempts: 2

  reviewer:
    provider: primary-reviewer
```

Store which model handled every attempt.

Do not automatically promote to a cloud coding model unless explicitly
allowed.

------------------------------------------------------------------------

# 32. Lessons System

A lesson is a reusable engineering instruction extracted from a verified
review/fix cycle.

Example:

``` yaml
lesson_id: ANGULAR-004
category: lifecycle
language: typescript
framework: angular

lesson: >
  When creating manual subscriptions in Angular components,
  use the project's established cleanup/lifecycle pattern.

source_task: FORM-027
source_review_issue: REVIEW-027-2
confidence: high
```

Rules:

1.  Do not create a lesson from every review comment.
2.  Prefer recurring/generalizable issues.
3.  A lesson must be traceable to its source.
4.  Project-specific lessons should not automatically become global
    lessons.
5.  Retrieve only a small relevant set for a task.
6.  Track retrieval and usefulness.

------------------------------------------------------------------------

# 33. Lesson Retrieval

V1:

-   language
-   framework
-   category
-   keyword/tag matching

Later:

-   add pgvector
-   embed lessons/task descriptions
-   semantic retrieval
-   hybrid metadata + vector ranking

Do not require pgvector for the first autonomous loop.

------------------------------------------------------------------------

# 34. Training Dataset Capture

Record training-quality history from day one, but do not fine-tune in
V1.

For accepted tasks preserve:

``` text
task instructions
context manifest
prompt
original model response
original patch
verification results
review feedback
correction prompts
corrected patch
final accepted patch
model metadata
outcome
```

Suggested artifact path:

``` text
data/training/<project>/<task>/<run>/
```

The database indexes the artifacts.

Do not automatically train on every accepted example. A future
dataset-curation process should select high-quality examples.

------------------------------------------------------------------------

# 35. Model Evaluation

Design metrics now.

Track:

-   tasks attempted
-   first-pass verification rate
-   first-pass review approval
-   average coding attempts
-   average review cycles
-   failure categories
-   human escalation rate
-   average runtime
-   token usage when available
-   reviewer cost when available
-   success by language/framework/task category

Later support evaluation mode:

``` text
same bounded task
   ├── Model A
   └── Model B
        ↓
same verification
        ↓
independent review
        ↓
record comparative outcomes
```

Do not use benchmark scores as the only routing evidence. Prefer actual
performance on managed projects.

------------------------------------------------------------------------

# 36. Secrets and Redaction

Implement a `SecretService`/redaction layer.

Requirements:

-   never persist raw API keys in run artifacts
-   redact secrets from logs
-   redact secrets from reviewer payloads
-   do not include `.env` contents in model context
-   inject only required secrets into a worker
-   support environment-variable based secrets in V1
-   leave interface open for a dedicated secrets manager later

Protected files should include `.env*` by default.

------------------------------------------------------------------------

# 37. Human Approval Policies

Always require human approval before automatically accepting changes
involving configured high-risk areas.

Default categories:

-   authentication/authorization
-   secrets/credential handling
-   production deployment
-   destructive database migration
-   payment/Stripe logic
-   deleting significant files/data
-   major dependency upgrades
-   infrastructure credentials
-   CI/CD production release configuration

Projects can extend this policy.

------------------------------------------------------------------------

# 38. Notifications

Do not make notifications a V1 blocker.

Create an event interface so providers can be added later.

Useful events:

``` text
project_started
milestone_completed
project_completed
human_review_required
task_failed
review_limit_reached
resource_blocked
```

Potential future channels:

-   email
-   Slack
-   Discord
-   webhook

------------------------------------------------------------------------

# 39. API Design

FastAPI endpoints should expose resources, not LangGraph internals.

Suggested V1 endpoints:

``` text
POST   /projects
GET    /projects
GET    /projects/{project_id}

POST   /projects/{project_id}/import-tasks
GET    /projects/{project_id}/tasks

POST   /projects/{project_id}/run
POST   /projects/{project_id}/pause
POST   /projects/{project_id}/resume

GET    /tasks/{task_id}
GET    /tasks/{task_id}/runs
GET    /runs/{run_id}
GET    /runs/{run_id}/events
GET    /runs/{run_id}/reviews

POST   /escalations/{id}/resolve

GET    /models
GET    /lessons
GET    /health
```

Use typed request/response schemas.

------------------------------------------------------------------------

# 40. Run Event Stream

Record meaningful workflow events:

``` text
TASK_SELECTED
WORKSPACE_CREATED
CONTEXT_BUILT
PLAN_CREATED
CODING_STARTED
CODING_COMPLETED
BUILD_STARTED
BUILD_FAILED
TESTS_PASSED
REVIEW_STARTED
CHANGES_REQUESTED
FIX_STARTED
APPROVED
LESSON_CAPTURED
COMMIT_CREATED
PUSH_COMPLETED
TASK_COMPLETED
HUMAN_REVIEW_REQUIRED
```

This becomes the foundation for the future dashboard and audit trail.

------------------------------------------------------------------------

# 41. Future Dashboard

Do not build until backend workflow is reliable.

When added, dashboard should show:

``` text
Projects
Active runs
Task DAG/status
Current model
Current worker
Verification status
Review status
Human escalations
Milestones
Model performance
Lessons
Run history
Reviewer usage/cost
```

A useful project view:

``` text
TraceStack

TS-001  COMPLETE
TS-002  COMPLETE
TS-003  REVIEWING
TS-004  BLOCKED
TS-005  PENDING

Current run:
RUN-20260919-0042

Coder:
qwen-coder-14b

Verification:
Build  PASS
Lint   PASS
Tests  PASS

Review:
IN PROGRESS
```

------------------------------------------------------------------------

# 42. V1 Scope

V1 must prove one reliable end-to-end autonomous loop.

Implement:

-   Python/FastAPI service
-   PostgreSQL
-   Alembic migrations
-   Docker Compose
-   one local OpenAI-compatible model provider
-   project registration
-   task import from YAML
-   sequential dependency-aware task selection
-   Git worktree/task branch handling
-   Node worker profile first
-   bounded context builder
-   coding-agent invocation
-   deterministic command runner
-   build/lint/test verification
-   diff/file-count policy
-   structured review provider
-   review/fix loop
-   retry limits
-   human escalation
-   run artifact storage
-   task/run/review persistence
-   commit and optional push after approval
-   pause/resume at safe boundaries
-   basic lessons capture
-   health/status APIs
-   unit and integration tests

V1 does **not** need:

-   Angular dashboard
-   parallel agents
-   Java/Python workers beyond interfaces/placeholders
-   pgvector
-   fine-tuning
-   Temporal
-   Kubernetes
-   queues
-   Redis
-   complex GPU scheduler
-   automatic PR merging
-   autonomous production deployment

------------------------------------------------------------------------

# 43. V2 Scope

After V1 proves reliable:

-   Angular dashboard
-   Java worker
-   Python worker
-   richer context dependency discovery
-   milestone gates
-   architecture-review pass
-   security scanning improvements
-   notification providers
-   model routing by task complexity
-   model promotion after repeated failures
-   richer lesson retrieval
-   pgvector semantic lesson retrieval
-   pull-request creation
-   resource monitoring
-   multiple project support improvements

------------------------------------------------------------------------

# 44. V3 Scope

Only after real usage justifies it:

-   safe parallel task execution
-   GPU/model resource scheduler
-   multi-model evaluation mode
-   local reviewer model
-   training dataset curation
-   fine-tuning pipeline
-   automated model-quality comparison
-   advanced repository semantic indexing
-   organization/team support
-   remote workers
-   durable distributed workflow engine such as Temporal if single-host
    LangGraph execution becomes insufficient

------------------------------------------------------------------------

# 45. Implementation Order

Build in this order.

## Phase A --- Foundation

1.  Repository scaffold
2.  Docker Compose
3.  PostgreSQL
4.  configuration
5.  domain models
6.  SQLAlchemy models/repositories
7.  Alembic
8.  health API
9.  tests

Exit condition: orchestrator and PostgreSQL start cleanly and
persistence tests pass.

## Phase B --- Project and Task Management

1.  project CRUD
2.  manifest parser
3.  task importer
4.  dependency validation
5.  task state machine
6.  next-ready-task selection
7.  run creation

Exit condition: a project manifest can be imported and the correct next
task is selected.

## Phase C --- Git Workspace

1.  GitService
2.  clean-state validation
3.  task branch creation
4.  worktree creation
5.  diff capture
6.  rollback
7.  commit
8.  push configuration

Exit condition: an integration test can create an isolated task
worktree, modify a fixture repo, capture diff, commit, and clean up.

## Phase D --- Worker Runtime

1.  Node worker image
2.  WorkerService
3.  command policy
4.  timeout
5.  log capture
6.  artifact storage
7.  cleanup

Exit condition: orchestrator can execute configured verification
commands inside an isolated worker.

## Phase E --- Local Model

1.  provider interface
2.  OpenAI-compatible provider
3.  model configuration
4.  connection test
5.  structured response handling
6.  timeouts/errors

Exit condition: a configured local model can receive a bounded prompt
and return a valid response.

## Phase F --- Context Builder

1.  task parser
2.  repository file selection
3.  task-declared source loading
4.  relevant test/config loading
5.  ADR loading
6.  context budget
7.  manifest/hashes
8.  lesson hook

Exit condition: a deterministic context package is produced for a
fixture task.

## Phase G --- Coding Agent

1.  coding prompt
2.  plan mode
3.  plan validation
4.  code-edit execution mechanism
5.  completion report
6.  scope check

Exit condition: a fixture task can cause an isolated worktree change
without touching forbidden paths.

## Phase H --- Verification

1.  build command
2.  lint command
3.  targeted tests
4.  failure classification
5.  retry path
6.  diff limits
7.  protected-path checks

Exit condition: intentionally broken fixture implementations fail before
review.

## Phase I --- Reviewer

1.  ReviewProvider interface
2.  structured review schema
3.  reviewer prompt
4.  diff/context package
5.  approval
6.  change request
7.  human-review response

Exit condition: fixture reviews route correctly.

## Phase J --- Fix Loop

1.  correction prompt
2.  unresolved issue tracking
3.  retry counters
4.  re-verification
5.  re-review
6.  retry exhaustion

Exit condition: a deliberately flawed fixture can be corrected and
approved, while an unfixable fixture escalates.

## Phase K --- LangGraph Integration

1.  graph state
2.  nodes wrapping existing services
3.  conditional edges
4.  checkpoint/persistence strategy
5.  pause
6.  resume
7.  recovery reconciliation

Exit condition: the complete V1 loop executes through LangGraph without
business logic being embedded in graph nodes.

## Phase L --- Experience Capture

1.  run artifacts
2.  review history
3.  lesson candidate extraction
4.  lesson approval/rules
5.  lesson retrieval
6.  model metrics
7.  training artifact capture

Exit condition: accepted and rejected runs leave a useful, queryable
history.

## Phase M --- First Real Project

Use a small non-production repository.

Do not start with a payment/authentication/production-sensitive
repository.

Run at least:

-   10 low-risk tasks
-   multiple intentional verification failures
-   at least one review/fix loop
-   at least one retry exhaustion
-   pause/restart/resume
-   rollback
-   orchestrator restart
-   database restart
-   worker cleanup verification

Only after this should the system be trusted with larger projects.

------------------------------------------------------------------------

# 46. Required Automated Tests

Minimum categories:

## Unit

-   task dependency calculation
-   state transitions
-   retry limits
-   manifest validation
-   context-budget enforcement
-   protected-path matching
-   diff-size policy
-   review schema validation
-   lesson filtering
-   secret redaction
-   model routing
-   escalation policy

## Integration

-   PostgreSQL repositories
-   Alembic migration up/down in test environment
-   Git fixture repository
-   branch/worktree lifecycle
-   worker creation/destruction
-   command timeout
-   artifact writing
-   local model mock provider
-   reviewer mock provider
-   LangGraph state transitions

## End-to-End Fixture

Create a tiny fixture repository with a deliberately simple coding task.

Test:

``` text
import task
→ select task
→ create worktree
→ build context
→ coder mock writes change
→ tests pass
→ reviewer approves
→ commit
→ complete task
```

Also create a rejection fixture:

``` text
coder produces defect
→ deterministic checks pass
→ reviewer requests change
→ coder fixes
→ verify
→ reviewer approves
→ complete
```

And escalation fixture:

``` text
coder repeatedly fails
→ retry ceiling
→ human escalation
→ repository restored/safe
```

------------------------------------------------------------------------

# 47. Observability

Use structured logs.

Every log/event should include when applicable:

``` text
project_id
task_id
run_id
attempt
worker_id
model_id
event_type
timestamp
```

Do not log secrets or full sensitive source unnecessarily.

Provide `/health` and basic readiness checks for PostgreSQL and
configured services.

------------------------------------------------------------------------

# 48. Configuration

Use environment configuration for infrastructure and project manifests
for project-specific behavior.

Example `.env.example`:

``` text
DATABASE_URL=
ARTIFACT_ROOT=/data
DEFAULT_LOCAL_MODEL_BASE_URL=
DEFAULT_LOCAL_MODEL=
REVIEW_PROVIDER=
REVIEW_BASE_URL=
REVIEW_MODEL=
GIT_PUSH_ENABLED=false
```

Never commit actual credentials.

------------------------------------------------------------------------

# 49. Failure Philosophy

Classify failures.

Examples:

``` text
MODEL_UNAVAILABLE
MODEL_TIMEOUT
INVALID_MODEL_RESPONSE
BUILD_FAILED
LINT_FAILED
TEST_FAILED
SECURITY_FAILED
SCOPE_VIOLATION
GIT_CONFLICT
REVIEW_CHANGES_REQUESTED
REVIEWER_UNAVAILABLE
RETRY_EXHAUSTED
HUMAN_DECISION_REQUIRED
WORKER_FAILURE
RESOURCE_UNAVAILABLE
```

Each class should have a deterministic policy:

-   retry
-   send back to coder
-   rollback
-   pause
-   escalate

Do not use one generic exception path for everything.

------------------------------------------------------------------------

# 50. Definition of Done for V1

V1 is complete only when the following scenario works without manual
prompt copying:

1.  User registers a local repository.
2.  User imports `build.tasks.yaml`.
3.  System determines the next eligible task.
4.  System creates an isolated Git task workspace.
5.  Context Builder assembles bounded task context.
6.  Local coding model receives the task.
7.  Candidate code is produced in the isolated workspace.
8.  Orchestrator independently runs build/lint/tests.
9.  Failed deterministic verification is routed back to the coder within
    limits.
10. Passing candidate produces a bounded Git diff.
11. Reviewer receives requirements + diff + verification evidence.
12. Reviewer can approve or request changes using structured output.
13. Requested changes return to the coder.
14. Corrected code is reverified and rereviewed.
15. Approved work is committed.
16. Push occurs only if enabled.
17. Task is marked complete.
18. Next dependency-eligible task becomes ready.
19. Review history and run artifacts are persisted.
20. A reusable lesson can be captured from the run.
21. Retry exhaustion creates a human escalation.
22. Restarting the orchestrator does not lose workflow state.
23. A worker cannot access unrestricted host files.
24. Secrets are not written into prompts/logs/artifacts.
25. The system can pause and safely resume.

------------------------------------------------------------------------

# 51. Non-Goals for the Initial Build

Do not turn V1 into:

-   an IDE
-   a general-purpose autonomous shell agent
-   a cloud deployment platform
-   a replacement for Git
-   a replacement for CI/CD
-   a multi-user SaaS product
-   a model-training platform
-   a distributed cluster manager

The immediate goal is reliable autonomous movement through bounded
development tasks on one local machine.

------------------------------------------------------------------------

# 52. Key Architectural Rule

The central rule for the entire project is:

> The model should never be responsible for remembering the workflow,
> proving that its own work succeeded, or deciding what safety
> boundaries apply. The orchestrator supplies bounded context, executes
> deterministic checks, records state, enforces limits, and decides what
> the model is allowed to do.

A second rule applies to learning:

> The system should improve before the model is fine-tuned. Capture
> verified mistakes and accepted corrections, retrieve relevant lessons
> for future tasks, measure whether those lessons improve outcomes, and
> only later use curated history for model fine-tuning.

------------------------------------------------------------------------

# 53. Future Training Path

Do not implement training initially.

The intended evolution is:

``` text
LEVEL 1
Prompt lessons
    ↓
LEVEL 2
Retrieved project/model lessons
    ↓
LEVEL 3
Curated accepted examples
    ↓
LEVEL 4
Fine-tuned local coder/reviewer
```

Before fine-tuning, require enough high-quality data to compare:

``` text
baseline first-pass approval
lesson-assisted first-pass approval
fine-tuned first-pass approval
```

Fine-tuning is only worthwhile if measured project performance improves.

------------------------------------------------------------------------

# 54. First Production-Safe Boundary

Before allowing unattended operation on important repositories, require:

-   at least 10 successful fixture/low-risk tasks
-   rollback tested
-   pause/resume tested
-   retry exhaustion tested
-   worker isolation verified
-   secret redaction verified
-   protected paths verified
-   reviewer structured-output failures tested
-   local model outage tested
-   reviewer outage tested
-   Git push disabled by default and tested
-   database backup procedure documented
-   artifact retention policy documented
-   human approval policies enabled

------------------------------------------------------------------------

# 55. Final Target

The finished system should let the user do this:

``` text
1. Design a project.
2. Add build.md and build.tasks.yaml.
3. Register the repository.
4. Start the project.
5. Let the orchestrator move through safe tasks.
6. Receive a notification only when a real decision is required.
7. Inspect every change through Git and run history.
8. Accumulate lessons from review failures.
9. Measure which local models perform best for which work.
10. Eventually use curated history to improve local models.
```

The system succeeds when it reduces manual prompt passing without
sacrificing engineering control, auditability, or repository safety.
