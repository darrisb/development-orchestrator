# Known issues and concerns

Written while building phases A–M. Each entry says what is wrong, why it
matters, and the smallest change that would fix it. Ordered by how likely it is
to bite during the first real project (build.md phase M); concerns 18–24 came
out of phase H, 25–33 out of phase I, 34–40 out of phase J, and 41–45 out of
phase L.

**48–54 are different in kind from everything above them.** Every earlier entry
was found by reading the code or by driving it with scripted responses. These
seven were found in the real execution path, and six of them were invisible to
the whole test suite: the transaction-timestamp collapse (49) needs a
transaction long enough to notice, the confidence bypass (50) had a test
asserting the defective behaviour was correct, the integration-baseline
defect (51) needed a *second* task that depended on a first, and each of 52–54
needed a real failure rather than a scripted answer — a process that dies
mid-correction, a provider that times out, a second task to integrate. The
lesson is recorded here rather than implied: a green suite said nothing about
any of them, and 51 in particular passed every per-candidate gate while
producing four accepted candidates that would not merge together.

Resolved entries are kept in place and marked, rather than deleted or
renumbered: the reasoning is referenced from code comments and tests, and the
numbers are how they are referenced. **Resolved: 1–12, 16, 18–20, 22–29, 32–36,
38–39, 41, 43, 46, 48–56, 58–64.** **Partly resolved: 30, 45** -- each says which half.
**Open: 13, 14, 15, 17, 21, 31, 37, 40, 42, 44, 47.**

Every open entry is now a documented limitation rather than an unfinished fix.
Nine of the eleven say so themselves -- 13, 14, 15, 17, 21, 31, 37, 40, 42 and
44 either propose no fix or propose one whose cost is not worth paying until a
real project has produced numbers -- and the other two are boundaries rather
than defects: 47 is an operator validation gate, and what is left of 45 is a
judgement about what deserves capturing that no rule can be written for yet.

Phase J closed the retry and review loop; Phase K closed the delivery, human
response, runtime, transaction and worktree lifecycle gaps around it: approved
candidates land (25), answered escalations have explicit effects (32), finished
worktrees are released and counted (34), runtime ceilings are enforced (8, 16,
24, 35), and each loop turn is committed independently of the graph cursor (36).

Phase L added the lessons system, training capture and section 35's metrics,
which closed no numbered concern by itself: every one of the six rules in
section 32 is enforced structurally rather than by a fix, and the
retrieval/application counters section 32 rule 6 asks for now exist. What phase
L did instead was make five limitations measurable (41–45).

Phase M closed the rest. Two scope gaps (4 and 6) and the Compose readiness
blocker (46) went first, then the policy-configuration group that concerns 5, 22
and 29 had each found in a different place -- a pattern list that is right for
most projects and wrong for some, with no way for the wrong ones to say so. The
answer is the same in all three: the manifest carries the exception, in the safe
direction, beside `protected_paths`.

The rest of the phase M pass was an audit of the fixes themselves, and it is
worth saying what that found, because "implemented" and "working" turned out to
be different claims. The per-project overrides, the dependency pre-population,
the convergence guard, the feedback history, the provisional outcome file and
the retention and ranking rules were all written without tests. Testing them
found two real defects -- a `.gitignore` directory entry that the dependency
check could not match (12) and two import patterns that read nonsense as import
targets (30) -- and established that concern 20's danger had already been
removed by something written for another reason (the candidate-patch restore),
which in turn removed concern 29's cause. Both are now pinned by tests, because
a property that holds by accident is a property that stops holding.

A second pass over the same real run — the one whose first pass produced
concern 51 — found 52, 53 and 54, and they share a cause: the loop kept the
state of a run in the process running it. The consequence was a resume that
restarted the run (52), a record that could not survive the failure it was
describing (53), and a cumulative gate whose verdict existed only in an event
payload (54). The fix is the same shape in all three: a run's state is
reconstructed from the records that must exist anyway, a call commits its own
row before the exception leaves, and a gate writes through the same repository
every other gate uses. All three are pinned by tests, and 52's are pinned by a
real process restart rather than by a simulation of one.

## 1. A clipped file plus whole-file edits can destroy code — **resolved**

**Was the sharpest issue in phase G.** The context builder clips any single file to
`CONTEXT_MAX_ITEM_TOKENS` (2000 tokens, ~7 KB) and marks the clip. The coding
agent then asks for *the complete new contents* of every file it changes. For a
file larger than the clip, the coder has never seen the end of the file and is
being asked to return it whole: it will invent or silently drop the part it
could not see, and the result is a large deletion in the diff rather than an
error.

Today this is contained rather than fixed: the change lands only in the run's
worktree, `max_diff_lines` will often block it, and nothing is committed
without review. But it wastes attempts and reads like a model defect.

*Resolved* as prescribed, and it had to be before phase J: containment relied
on `max_diff_lines` catching the damage, and a loop that retries three times is
three chances for a deletion to come in under the limit.
`run_coding_attempt` now measures the context package's `truncated_paths`
against the task's writable allowance and refuses before the coder is called at
all. The refusal is `HUMAN_DECISION_REQUIRED`, so the failure policy escalates
it rather than retrying: no feedback the coder could act on would change the
outcome, and a person has to either raise `CONTEXT_MAX_ITEM_TOKENS` or split
the task.

One gap is named rather than closed. A task that declares **no** allowance is
not checked, because with no allowance every file in the package is nominally
writable and refusing on any clipped item would refuse most tasks against a
repository with large files. So the protection is something a task earns by
saying what it changes. A larger per-item budget for writable files, or a
patch-based edit mode, is still the better long-term answer.

## 2. The orchestrator cannot start workers from inside its own container — **resolved**

`docker-compose.yml` runs the orchestrator in a container with no Docker socket
and with the workspace bind-mounted at `/workspace`. Neither part works for
creating workers: without a socket it cannot ask the daemon for a container at
all, and if it could, the worktree path it would pass as a bind mount
(`/workspace/worktrees/...`) is a path inside *its own* container, which the
daemon would resolve against the host and get something else or nothing.

So today the orchestrator has to run on the host (`./scripts/dev.sh serve`) for
tasks to be executed; Compose is for PostgreSQL. The file says so in a comment.

*Fix:* a `HOST_WORKSPACE_ROOT` setting that the worker service uses to translate
a container path back to a host path, plus a mounted socket — and then the
orchestrator container is as privileged as the daemon, which is a decision to
take deliberately rather than by default. Running the orchestrator on the host
is the safer arrangement and may simply be the answer.

*Resolved* with the deliberate option the original entry described.
`HOST_WORKTREE_ROOT` translates the already-validated container worktree path
to the host spelling used by the daemon; the image includes the Docker CLI; and
Compose mounts the host socket. The security consequence is not hidden: the
socket grants daemon-level authority, Compose and the README call it out, and
`docker compose up -d postgres` remains the safer host-orchestrator setup.

## 3. Nothing records a `model_runs` row — **resolved**

`ModelRunRow.model_id` is a non-nullable foreign key to `models`, and the
providers configured from the environment (the default coder and the reviewer)
had no `models` row, so a call against them had nowhere to be recorded and
`task_runs.coder_model_id` stayed null. Sections 34 and 35 depend on that
table.

*Resolved* the way this entry originally proposed, by keeping the schema
honest rather than loosening it: `services/model_runs.py` upserts a `models`
row for an environment-configured provider the first time it is called, keyed
by the table's own natural key (provider, model name, role) and marked
`source: environment`. Every call the coding agent makes -- plan, code, fix --
now leaves a `model_runs` row with its purpose, status, tokens, duration and
both artifact paths, and a call that *fails* is recorded before the error is
re-raised, so "how often does this endpoint time out" is answerable from the
data. `task_runs.coder_model_id` is set before the first call rather than
after the last, so a run that dies mid-attempt still says who was asked.

A nullable `model_id` was the other option and was rejected: it would have
made "we do not know which model wrote this" representable, and section 34
depends on that never being true.

## 4. A rename out of a protected path is not detected — **resolved**

`evaluate_scope` measures `FileChange.path` and ignores `original_path`. Git
reports a rename under its new path, so moving `.env` to `config/env.txt` would
be measured as an ordinary addition. The applier cannot produce this (it only
creates, overwrites and deletes whole paths), but a future worker that runs
project scripts could.

*Fix:* run the protected-path and allowance checks over `original_path` as well
whenever it is set.

*Resolved* in Phase M. The scope guard treats a rename as writes to both the
reported destination and `original_path`, applying protected-path, allowance,
inspection-only, and sensitive-category checks to the source as well. A rename
from `.env` to an allowed ordinary path is now a blocking scope violation.

## 5. Sensitive-path patterns will produce false escalations — **resolved**

`SENSITIVE_PATTERNS[SECURITY]` included `**/*token*.*` and `**/*session*.*`,
which match ordinary files such as `src/tokenizer.ts` or
`src/sessionStore.ts`. An undeclared match is `REQUIRE_REVIEW`, which halts a
run waiting for a human. A guard that escalates routine work gets switched off.

*Resolved* at both ends the fix named. `SENSITIVE_PATTERNS[SECURITY]` is
directory-shaped -- `**/tokens/**`, `**/sessions/**` -- so `src/tokenizer.ts`
and `src/sessionStore.ts` match nothing, and a project whose layout still
collides declares `sensitive_path_exceptions` in its manifest beside
`protected_paths`. An exception removes a path from *every* category rather
than one: a project saying "this directory is not what it looks like" means it
of the whole guard, and a per-category exception would be a list nobody could
reason about.

## 6. A declared bare filename widens the allowance — **resolved**

`path_matches_declaration` reuses `matches_pattern`, which matches a pattern
containing no `/` against a file's *name at any depth*. A task declaring
`config.ts` therefore also permits writing `src/deep/nested/config.ts`. This is
deliberate in the context builder (finding a file to read) and wrong as a write
allowance.

*Fix:* require a declaration used as an allowance to match the full
repository-relative path, with directories and globs as the only widening.

*Resolved* in Phase M by separating read-selection matching from write-policy
matching. A bare `config.ts` allowance now names only the root file; a directory
or an explicit glob such as `src/**` is required to authorize descendants.

## 7. A plan is validated but never enforced — **resolved**

The plan is checked against the task's scope before coding, and the approved
plan is sent back with the coding request — but nothing compares the edits that
arrive against the plan that was approved. A coder can have a two-file plan
approved and then write three different (allowed) files.
`deviationsFromPlan` is self-reported and therefore not evidence.

*Resolved.* `CompletionReport.planned_paths` and `unplanned_paths` compare the
edits that arrived against the plan that was approved, in the report's measured
half, and the comparison reaches the reviewer twice: as a discrepancy in the
review package and as a pending human-review reason. `REQUIRE_REVIEW`, not
`BLOCK`, as the fix suggested -- a coder that found a better route while
staying inside its allowance is looked at, not rolled back.

## 8. `max_runtime_minutes` is not enforced anywhere — **resolved**

`TaskLimits.max_runtime_minutes` exists and is imported from the manifest, but
no code reads it. A coding attempt is bounded only by the provider's own timeout
and a command by `WORKER_COMMAND_TIMEOUT_SECONDS`; the run as a whole has no
wall-clock ceiling. See also concern 16.

*Resolved* in Phase K. The deadline is derived from the persisted run start and
checked before every loop turn, so process restarts and pause/resume do not reset
the budget. A command already in progress is allowed to settle rather than being
killed while it may be writing the worktree.

## 9. The task is left in `CODING` whatever happens — **resolved**

The coding agent moved the task to `PLANNING`/`CODING` and deliberately did not
move it on, because verification (H), review (I) and the fix loop (J) own what
happens next — and none of them existed.

*Resolved* by the fix loop, which is the caller that now owns the end of every
path: a candidate that breaks its scope ends the run `FAILED`, an attempt
refused before it starts escalates to `HUMAN_REVIEW`, and anything the coder can
act on turns the loop. The agent itself is unchanged and still does not move the
task on; it is a caller that was missing, not a rule.

Two things about it stayed as they were. An illegal transition is still logged
and swallowed rather than raised, so a state-machine mistake will be quiet — but
the loop reads both rows back from the database after every turn before it makes
its terminal transition, because the copies it holds go stale the moment an
agent moves them, and a stale status is exactly how a finished run would have
been left parked in `VERIFYING`. And `run_coding_attempt` on its own still
leaves the task in `CODING`; it is only inside the loop that a run always
lands somewhere.

## 10. A second attempt in one run overwrites the context manifest — **resolved**

Coding artifacts were prefixed per attempt (`attempt-2-cycle-1/prompt.txt`) but
`context_builder` wrote `context-manifest.json` and `context.md` unprefixed, so
two attempts in one run kept two prompts and one context manifest.

*Resolved* by giving the context builder the same prefix. The helper now lives
in `artifact_store.attempt_prefix` and is shared by everything that writes a
run artifact, so an attempt's context, prompts, logs and verification report
land together under `attempt-N-cycle-M/`. This mattered more than it looked:
a review cycle whose coder was given different context cannot explain its own
outcome if the earlier context is gone.

One thing stayed as it was, deliberately. The `artifacts` table keys on
(run, kind), so the *row* for `context-manifest.json` still points at the most
recent attempt's file while every attempt's file survives on disk. That is the
same rule the coding artifacts already follow.

## 11. `CONTEXT_MAX_ITEM_TOKENS` and `MAX_EDIT_BYTES` are unrelated numbers — **resolved**

One bounds what a file looks like on the way in (2000 tokens), the other what
it may be on the way out (512 KB). They describe the same file at two moments
and can disagree by two orders of magnitude. See concern 1; they should be
derived from one another.

*Resolved.* `domain.edits.max_edit_bytes_for_context` computes the output
ceiling from `CONTEXT_MAX_ITEM_TOKENS` through `domain.tokens`' single ratio,
times an explicit `EDIT_SIZE_HEADROOM` -- a rewritten file may be larger than
the one the budget could show, because adding a guard clause makes a file
longer, but by a stated factor rather than by two orders of magnitude. The bare
`* 4` that stood in for the derivation is gone: it was the same drift in
miniature, since `CHARS_PER_TOKEN` is 3.5 and nothing tied the two together.

## 12. A worker with no network cannot install anything — **resolved**

`WORKER_NETWORK=none` is the default, because section 11 asks for a restricted
network and a verification command that can reach the internet can also
exfiltrate the repository. The consequence is that `npm ci`, `pip install` and
`./mvnw` resolving a new dependency all fail in a worker: the repository must
already contain everything its verification commands need.

For a project with a committed lockfile and installed `node_modules` this is
fine. For a fresh clone it is not, and the failure will read as a broken worker
rather than as a policy.

*Resolved* by the first of the two, which keeps the network policy absolute: a
project declares `dependency_paths` in its manifest and `prepare_workspace`
copies them into each new worktree. The copy is refused unless Git ignores the
path -- a tracked path is already in the worktree, and copying over it would
mean the worker ran against something other than the commit it was given -- and
refused for any path that leaves the repository.

Closing this found one defect worth naming: a directory-only `.gitignore` entry
(`node_modules/`, which is how everyone writes it) does not match the bare name
`node_modules` when the path is absent from the worktree, which it always is at
that point, because Git cannot tell an absent path is a directory. The natural
manifest entry was therefore refused with a message about Git not ignoring a
path Git does ignore. The check now asks with the form that matches.

## 13. A worktree alone is not a usable Git checkout

A linked worktree's `.git` is a *file* pointing into the managed repository's
`.git/worktrees/`. The worker only mounts the worktree, so anything inside it
that reads repository metadata — a test that shells out to `git describe`, a
build that stamps a commit, `husky` — will fail to find a repository.

This is a deliberate consequence of the mount rule (a worker must not see the
managed repository) and `git` is not a permitted command anyway, but the failure
mode is obscure: "not a git repository" from a build tool, not from us.

*Fix:* nothing cheap. A `.git` file rewritten to a mounted read-only copy of the
metadata would work and is more machinery than V1 needs. Worth knowing about
before debugging it from scratch.

## 14. The command policy refuses some legitimate arguments

Tokens containing shell metacharacters are refused, and after `shlex` has
stripped the quotes there is no way to tell `"*"` from `*`. So
`jest --testPathPattern=src/.*\.test\.ts` is refused although it is perfectly
safe, and the operator's only recourse is to move the pattern into a package
script.

That is the right direction to fail in, but it will be hit. *Fix:* if it becomes
a nuisance, check the metacharacters against the raw string's *unquoted* regions
rather than against the parsed tokens.

## 15. A killed `docker exec` leaves the worker unusable, by design

Killing the local `docker exec` process does not stop the process it started
inside the container, so a timed-out command destroys the whole worker rather
than leaving something running that nobody is waiting for. The result is correct
but coarse: a verification profile whose second command times out cannot run its
third in the same worker, and the caller has to start a new one.

Phase H's pipeline stops at the first failure anyway, so this costs nothing
today. It will matter if a later phase wants to run independent checks in
parallel or continue past a timeout.

## 16. Nothing enforces the whole-run worker ceiling — **resolved**

`WORKER_TIMEOUT_SECONDS` (1800) is meant to bound a whole run's worth of
commands, and only the per-command ceiling is actually applied. Ten commands
that each take 890 seconds would all pass their own limit. Related to concern 8:
neither the run ceiling nor `max_runtime_minutes` has an owner yet, and the
natural one is the verification pipeline or the workflow.

*Resolved* by sharing one monotonic `WORKER_TIMEOUT_SECONDS` deadline across
all command categories in a verification worker. Each command receives only the
remaining time, so ten individually legal commands cannot each claim the full
per-run budget.

## 17. Whole-file edits are expensive by design

The edit contract trades output tokens for reliability: a one-line change to a
600-line file costs 600 lines of generation. This is a deliberate choice —
unified diffs from local models fail in ways that are hard to attribute — but
it makes a task that does not declare its files much more expensive than one
that does, and it puts a real ceiling on the size of file this orchestrator can
work on. Worth revisiting once a real project has produced numbers.

## 18. A task's `verify` list silently replaces the test suite — **resolved**

`resolve_profile` narrowed the project's `tests` category to the task's own
`verify` list, which reads like section 17's "targeted tests" and is a trap
the specification itself walks into: build.md section 5's example task writes

```yaml
verify:
  - npm run compile
  - npm test
```

so a task that *replaced* the suite would run the compiler as a "test", and a
task whose list happened to hold only `npm run compile` would run no tests at
all while the report said `TESTS: PASSED`.

*Resolved* by making a task's commands **additive**: they are appended to the
project's test category, and any command the profile already runs is dropped
rather than repeated. A task can therefore ask for more verification than the
project requires and never for less, and the example above now costs nothing
extra instead of running the compiler twice and the tests never.

The other option this entry offered -- a per-category block on the task
(`verify: {tests: [...]}`) -- is the more honest model and is still worth
having. It needs a manifest schema change, a `tasks` column and a migration,
so it is a deliberate decision rather than a fix, and additive is the safe
default in the meantime.

## 19. A project with no verification profile "passes" — **resolved**

`VerificationReport.passed` means "nothing that ran failed", so a project that
declares no `verification:` block ran nothing, failed nothing, and passed --
and a workflow checking only `passed` would have sent a completely unverified
candidate to the reviewer.

*Resolved* at both ends. `VerificationReport.verified` is now a separate
property meaning *nothing failed **and** the orchestrator executed at least
one command*, with `commands_run` and `unverified_categories` beside it, and
the report's own summary says "nothing was verified" rather than "passed" in
that case. Phase I and the workflow should gate on `verified`, not on
`passed`. At the other end the importer now warns -- in `ImportReport.warnings`,
which the API returns -- when the manifest declares no build and no tests and
a task declares no `verify` of its own, naming the tasks that would be
unverifiable.

Still a warning rather than a refusal: a project may legitimately be imported
before its build is wired up, and refusing would make the orchestrator harder
to adopt than it needs to be.

## 20. A build that writes into the worktree fails the diff-policy check — **resolved**

Section 17's diff-policy check runs *after* the commands, which is the point:
a `dist/` created during `npm run compile` is in the worktree whether or not
the coder wrote it. But the check cannot tell a generated file from a written
one, so a project whose build writes output that is **not** in `.gitignore`
turns every otherwise-passing candidate into a `SCOPE_VIOLATION` or a security
finding whose real fix is a `.gitignore` entry, not a change to the code. The
failure routes to `ROLLBACK` (section 49), so the coder is not even told.

Most projects gitignore their build output and are unaffected —
`git add --all` respects `.gitignore`, so those files never reach the diff.
The ones that do not will look broken in a confusing way.

*Resolved*, and by neither of the two the entry proposed: what protects the
candidate is that `_run_command_categories` resets the worktree and reapplies
the *measured* candidate patch in a `finally` block, before `DIFF_POLICY`
looks. The commands' side effects are discarded rather than classified, so
there is nothing for the guard to mistake for the coder's work and no pattern
list to get right. `DIFF_POLICY` is then also the check that would notice if
the restore had not worked, which is the better reason for it to run twice.

Worth knowing about the shape of the evidence: `dist/` is in
`GENERATED_PATTERNS` and `src/version.py` is not, and a build writing either
one leaves a passing candidate. A test pins the second case, because a pass
there is the only one that distinguishes the restore from the pattern list.

## 21. Secret scanning is a text heuristic, and reads only added lines

Section 19 asks for modest checks, and these are modest. The scan reads added
lines of the unified diff and matches the redactor's credential shapes, so it
finds a pasted API key and a PEM header. It does not find a secret that is
base64-encoded, split across lines, assembled at runtime, inside a binary file,
or longer than `MAX_SCANNED_LINE_LENGTH` on one line. A clipped diff is
reported as incomplete rather than scanned quietly, which is the right
behaviour and still means the run record says "a human should read the rest".

*Read the result as "nothing obvious", never as "no secrets".* Real secret
scanning is a dedicated tool in the project's `verification.security` commands.

## 22. The generated-file patterns have no per-project override — **resolved**

`domain.security.GENERATED_PATTERNS` blocks `vendor/`, `dist/`, `build/`,
`target/` and friends. A Go project that vendors its dependencies, or any
repository that deliberately versions its build output, cannot pass
verification and has no way to say so — the list is a module constant, not
configuration. This is concern 5's problem in a second place: a pattern list
that is right for most projects is wrong for some, and only the wrong ones
find out.

*Resolved* exactly that way: `generated_path_exceptions` travels manifest ->
project -> verification pipeline -> `scan_candidate`. A Go repository that
vendors its dependencies declares `vendor/**` and passes. The ordering matters
and is tested: the forbidden-file check runs *ahead* of the generated-path
check, so a declared generated path is still not a place to put a private key.

## 23. A failing candidate is left in `VERIFYING` — **resolved**

The same shape as concern 9. The pipeline never moved a failing task, because
whether a deterministic failure goes back to the coder, escalates or ends the
run is `domain.failure_policy`'s answer and a caller's decision.

*Resolved* by the fix loop asking `failure_policy.action_for` and acting on the
answer: `SEND_TO_CODER` sends `VerificationReport.feedback` — the real command
and its real output — into the next attempt, `ROLLBACK` resets the worktree and
ends the run, and a `SEND_TO_CODER` with no attempts left becomes
`RETRY_EXHAUSTED`, which is a different failure with a different policy and is
recorded as one. The pipeline is unchanged, and calling `verify_candidate` on
its own still leaves a failing task in `VERIFYING`, which is the honest
behaviour for a function that does not own the decision.

## 24. Nothing bounds a whole verification pass — **resolved**

Concern 16, now with a concrete owner. Build, lint, tests and security each get
`WORKER_COMMAND_TIMEOUT_SECONDS` (900), so a profile with four slow commands
can occupy a worker for an hour while every individual command passes its
limit. The verification pipeline is the natural place to enforce
`WORKER_TIMEOUT_SECONDS` and the task's own `max_runtime_minutes`, and it does
not.

*Resolved* with the same shared monotonic deadline as concern 16. The worker
ceiling applies across build, lint, tests, and project security commands; the
task wall-clock ceiling is owned by the outer workflow, which refuses to start
the next turn once it is spent.

## 25. An approved candidate is left in `APPROVED`, uncommitted — **resolved**

The same shape as concerns 9 and 23, at the other end of the loop — and the one
of the three that phase J did **not** close. The fix loop now reaches `APPROVED`
by itself, and stops there deliberately: merging is section 10's business and
the workflow's decision.

So an approved run leaves its work uncommitted in the worktree on the task
branch, and the run row is left `RUNNING` with no `completed_at` — which is the
truth (the work is not delivered) but means an approved run looks in-flight to
anything counting active runs. `GitService` can commit, tag and push all of it;
nothing calls it. This is the single largest thing phase K has to add, and until
it does, "approved" means "a reviewer accepted it", not "it landed".

*Resolved* by the graph's delivery node. It admits only an `APPROVED` task,
commits with the orchestrator's fixed message, tags the checkpoint, pushes only
when policy enables it, marks the run succeeded and task complete, and releases
the worktree. Delivery also reconciles the Git/database transaction gap: a
clean HEAD newer than the recorded starting SHA is recorded as the candidate
after a restart instead of producing a second, empty commit.

## 26. The correction text is produced and nobody sends it — **resolved**

`ReviewRouting.feedback` held exactly what section 23 asks for and
`run_coding_attempt` already took a `review_feedback` argument. Phase J is the
wire, and `agents/fix_loop.py` is it.

The loop chooses which evidence the next attempt is given and writes neither of
them: a deterministic failure travels as the pipeline's `feedback` and a review
as the routing's. A correction prompt assembled by the thing counting the
retries would be describing its own summary of the evidence rather than the
evidence, which is the failure mode this system is built to avoid.

One thing the wire does not carry is in concern 39: the coder is given the *last*
cycle's findings, not every open one.

## 27. Review issues are never marked resolved — **resolved**

`ReviewRepository.mark_issue_resolved` existed and nothing called it, so the
open list only grew: by cycle three the reviewer was reading every finding ever
raised against the run, including the ones it could see were fixed.

*Resolved* the honest way rather than the easy way. Nothing is closed because an
attempt was made — the coder's claim to have fixed something is exactly the kind
of claim this system does not believe. An issue is closed when the *next*
reviewer, which was shown every open finding in its package, reads the new diff
and does not raise it again. `domain.review.unreraised_issues` decides what
counts as "again" and `fix_loop._close_unreraised_issues` writes the rows.

The identity used for the comparison is the requirement the finding cites, the
file it points at and the category it was filed under — never the prose, which a
reviewer recomposes every cycle. The cost of that choice is concern 40.

## 28. The review package is not redacted — **resolved**

Everything else that leaves the orchestrator was passed through
`domain.redaction` — worker stdout, worker stderr, log artifacts — and the
review package was not, although it carries the raw diff and the raw contents of
supporting files to whatever `REVIEW_BASE_URL` points at.

*Resolved* as prescribed. `domain.review_package.redact_package` masks every text
field, `Settings.redact_review_package()` decides whether to, and the package
manifest records `redacted` either way. The default is derived and derived
conservatively: on unless the reviewer is recognisably on this machine, with an
empty `REVIEW_BASE_URL` counting as local because there is no endpoint to leak
to. `REVIEW_REDACT_PACKAGE` overrides the guess in both directions, because
masking costs the reviewer the ability to comment on a masked line and only an
operator knows whether that trade is worth it.

Two details are load-bearing. The *inputs* are masked, not the rendered text, so
the package's `content_hash`, its stored artifact and the prompt actually sent
are the same bytes — hashing one thing and sending another would break the only
link between a reviewer's answer and what it saw (section 34). And
`unresolved_issues` is left alone: those came from earlier reviews that read a
package through this same function, so their text is masked at source, and
rewriting stored findings here would mean the issue quoted to the reviewer no
longer matched the issue in the database.

`host.docker.internal` is deliberately **not** treated as local. It resolves to
this host from inside a container, but it is also exactly how a container reaches
a proxy it does not control, so allowing it is an operator's explicit choice.

## 29. The section 37 gate will fire on builds that touch a lockfile — **resolved**

Concerns 20 and 37-policy meet here. A build that runs a package manager
writes `package-lock.json` into the worktree; the post-command diff check would
have seen it (concern 20), and `LOCKFILE` and `DEPENDENCY_MANIFEST` are both in
`APPROVAL_GATED_CATEGORIES` because section 37 lists "major dependency
upgrades". So a task that changed no dependency can still land on a human's
desk because its build refreshed a lockfile.

Including them is the conservative reading and it is the right default — the
guard cannot tell a patch bump from a major one. But on a real project this
will be the most common escalation, and it is the one most likely to train an
operator to approve without looking.

*Resolved* twice over, from both directions this entry was worried about.

The cause is gone: concern 20's restore means a build that refreshes
`package-lock.json` never puts it in the diff at all, so the escalation this
entry predicted would be the most common one does not arise. What is left is a
task that really does change a lockfile, which is what the gate is for.

And the inverse the fix asked for exists: `approval_gated_categories` on the
manifest narrows the policy per project, the same way `protected_paths`
extends it. It is opt-in in the safe direction -- omitted means the
conservative default, and the default still gates a declared lockfile change,
because the guard cannot tell a patch bump from a major one.

## 30. Supporting-source selection only understands some languages — **partly resolved**

`_supporting_sources` includes what a changed file imports, resolved with
`domain.relevance.extract_import_targets` — which reads ES-module, CommonJS
and Python import syntax. For a Java or Go repository it finds nothing, and
the reviewer sees the diff plus whatever the task declared as read-only and
nothing else.

It also resolves one level deep and never includes *callers*: a change to a
function's signature is reviewed without the code that calls it, so "this
breaks its caller" is a finding the reviewer is structurally unable to make.

*The language gap is resolved.* `extract_import_targets` reads Java and Kotlin
`import a.b.C;` and Go imports as well as ES-module, CommonJS and Python, so a
Java or Go repository is no longer reviewed with the diff and nothing around
it.

Closing it found two defects in the patterns themselves. Java's
`import static org.junit.Assert.assertEquals;` also matched Python's bare-import
pattern and yielded the keyword `static` as a target; and Go's specifier
pattern matched any line whose content began with a quoted string, so
`"name": "thing",` in a JSON fixture was read as an import. Neither could
resolve to a file, so neither was visible -- which is the point: a selector
that quietly considers nonsense is a selector nobody notices is wrong. Go
specifiers are now read only from a real `import` statement or inside a
parenthesised import block.

*The caller half remains open*, and is a V2 item rather than a fix: a change to
a function's signature is still reviewed without the code that calls it, so
"this breaks its caller" is a finding the reviewer is structurally unable to
make. That wants section 43's repository-wide symbol index.

## 31. Confidence is a self-reported number treated as a measurement

`REVIEW_MIN_CONFIDENCE` gates acceptance on a float the reviewer wrote about
itself. Nothing calibrates it, and local models are not well calibrated: a
model that always answers `0.95` disables the gate, and one that always
answers `0.5` escalates everything. The threshold was chosen by reasoning, not
from data.

Section 35's model evaluation is where this gets an answer — once there are
accepted and rejected runs to compare, the confidence distribution of reviews
that were later overturned is measurable. Until then, treat the number as a
weak signal, and the section 37 category gate as the real protection: that one
does not depend on the model saying anything.

## 32. An answered escalation does not restart anything — **resolved**

`POST /escalations/{id}/resolve` records the human's answer and moves nothing,
deliberately: what "A. Accept the candidate" means for a run is a workflow
decision. The consequence today is that answering an escalation leaves the
task in `HUMAN_REVIEW` and the answer in a column nobody reads. Phase K needs
to pick resolved escalations up; the options the orchestrator offers are free
text, so it will also need them to carry a machine-readable intent rather than
being parsed back out of a sentence.

Phase J made this reachable rather than theoretical — the loop escalates on its
own now, and `domain.escalation.run_escalation_options` writes a second set of
A/B/C options nobody can act on programmatically. It also decided one thing on
phase K's behalf that phase K may want to revisit: an escalated run is marked
`FAILED` (section 25's "mark run failed"), so continuing from a human's answer
means opening a **new** run rather than resuming the escalated one, and the task
has to be walked back to `READY` for `create_run` to accept it.

*Resolved* without parsing prose. Each offered option now carries an
`EscalationIntent`, the API answer names the option key, and the workflow maps
that intent to one explicit effect. Acceptance lands the preserved candidate;
requested changes and retry reopen the task for a new run; completion by hand
completes without inventing a commit; abandonment fails it. The old worktree is
released once the answer no longer needs it, and requested-change text is read
back as the next run's initial coder feedback.

## 33. A review cycle's artifacts sit in the previous cycle's directory — **resolved**

`attempt_prefix` was computed from the run's *current* `review_cycle`, which
counts the cycles that have **finished** — the counter is deliberately not
incremented until a reviewer has answered — so every cycle's work was filed
under the previous cycle's name.

*Resolved* by making the label the cycle the work belongs to: the default is
`review_cycle + 1`, the cycle in progress, and `attempt_prefix(run, cycle=...)`
lets a caller that knows better say so (the review agent does). It matters more
now than it did: a fix loop produces several of these directories per run, and
the loop's own `fix-loop.json` points a reader at them by name.

Collision-freedom does not depend on the cycle at all, which is why this was
safe to change: every coding attempt advances `attempt_number`, so two turns
cannot share a directory even when a verification failure means no cycle was
spent between them.

## 34. Worktrees are never released, so they accumulate one per run — **resolved**

`release_workspace` exists and the fix loop does not call it, for a reason that
is right in each individual case and wrong in aggregate. An escalated run's
worktree is the only live copy of the candidate a person is being asked to judge
(the diff survives as an artifact, but not a working tree), and an approved run's
worktree still holds work nothing has committed yet (concern 25). Deleting
either would destroy something. So nothing deletes them, and
`WORKTREE_ROOT` grows by one checkout of the repository per run, forever.

On a small fixture repository this is invisible. On a real project with ten
low-risk tasks a day it is the thing that fills the disk first, and it will do
it quietly, because a full disk surfaces as a Git error inside an unrelated run.

*Fix:* phase K owns the lifecycle, and the rule is not "release on exit" but
release once the run's outcome no longer needs the tree — after the candidate is
committed for an approved run, and after a human answers for an escalated one.
A reaper for worktrees whose run is in a terminal state, plus a count of live
worktrees on the health endpoint, would make the growth visible before it is a
problem.

*Resolved* at both levels. Delivery and human-resolution handling release a
tree as soon as its outcome no longer needs it; rollback failures are released
by the graph. The cautious reaper independently finds terminal runs whose task
is not approved, paused, or awaiting a human. Unknown directories are reported
and never deleted, and `/health` exposes total, releasable, and unclaimed
counts.

## 35. Nothing bounds a run's wall-clock time, and the loop is now its owner — **resolved**

Concerns 8, 16 and 24, meeting in the place that can finally answer them. The
fix loop makes up to `max_attempts` turns, each of which starts a worker and runs
the whole verification profile, and each command gets its own
`WORKER_COMMAND_TIMEOUT_SECONDS` (900). A task with four commands and three
attempts can therefore occupy a worker slot for hours with every individual
command passing its own limit, and `TaskLimits.max_runtime_minutes` — which is
imported from the manifest and read by nothing — is exactly the number that was
supposed to stop it.

The loop is the natural owner: it is the only thing that knows when the run
started and how many turns are left. It does not enforce it.

*Fix:* stamp a deadline from `max_runtime_minutes` when the loop starts, check it
before each turn, and treat an overrun as `RETRY_EXHAUSTED` — a turn that cannot
finish inside the budget should not be started, and one already running should be
left to finish rather than killed mid-command. A whole-run worker ceiling
(concern 16) belongs in the same place.

*Resolved* as proposed. The deadline comes from the persisted `started_at`, is
stored in graph state, and is checked before every loop turn. It survives a
restart and does not reset across a pause. When spent, the run escalates as
`RETRY_EXHAUSTED`; work already in progress is allowed to settle safely.

## 36. The loop is one long transaction unless the caller commits — **resolved**

Every repository call in a turn writes through the caller's `Session` and
nothing in the loop commits. A run that dies in its third turn — the machine is
rebooted, the process is killed — loses all three turns' rows if the caller was
holding one transaction open, even though the artifacts are on disk and the
worktree still has the code. Section 8 wants the database to be the authority on
what happened; here the disk would know more than the database.

The loop also builds its escalation text from the `FixIteration` objects it holds
in memory, so a resumed run could not reconstruct the attempt history that
section 24 requires — the run events and verification rows have the facts, but
nothing reads them back.

*Fix:* phase K's checkpoint strategy, with a commit boundary at the end of each
turn (the natural unit: one attempt, one verification, at most one review), and
an escalation renderer that reads the history from `run_events` rather than from
the loop's own memory.

*Resolved* by an explicit end-of-turn checkpoint callback supplied by the graph.
Every attempt's events, model calls, verification rows, review and artifacts are
committed before another turn begins, while LangGraph checkpoints its own small
cursor independently. Recovery re-reads authoritative task/run state before
acting, so a graph cursor that trails a committed approval routes to delivery
instead of repeating the model call. The existing escalation artifact still
uses the in-memory iterations during an uninterrupted call; the durable rows
now contain the same turn history needed for a later renderer.

## 37. A rolled-back run loses the candidate a person might want to see

A `ROLLBACK`-class failure — a scope violation, a secret in the diff — resets the
worktree and ends the run with no escalation, which is correct as policy: the
candidate is unsafe and nobody should be invited to wave it through. But it is
also the candidate that would explain *why* the model went out of bounds, and
after the reset the only record is `candidate.patch` in the run directory.

That is probably enough, and it is deliberately not an escalation, because a
person asked to look at every scope violation would stop looking. Worth knowing
rather than worth changing: if these turn out to be common on a real project,
the interesting signal is the pattern across runs (section 32's lessons), not the
individual tree.

## 38. An unfixable task spends its whole budget before saying so — **resolved**

The loop is correct and expensive. A task the coder cannot fix costs three coding
attempts, three context builds, three workers and up to three reviews before it
escalates, and on a local model that is the better part of an hour for a task
that was under-specified from the start.

Nothing looks for the obvious case: three attempts whose diffs are nearly
identical, or three reviews raising the same finding with the same requirement
id, mean the coder is not converging and the fourth attempt will not either.
`unreraised_issues` already computes the identity that would detect it.

*Resolved.* `_reviews_are_stagnant` compares consecutive reviews' blocking
findings by the same fingerprint `unreraised_issues` uses for closing, and an
unchanged non-empty set escalates rather than spending the rest of the budget.
It is a setting -- `FIX_LOOP_STAGNANT_REVIEW_LIMIT`, default 2, zero disables
it -- because "the same finding twice" is also what a coder half-way through a
fix looks like. An empty set never counts: repeated approvals have their own
route and repeated human-only decisions have their own policy.

## 39. The coder is only told the last cycle's findings — **resolved**

`ReviewRouting.feedback` is built from the review that just happened, so a third
attempt is given cycle 2's findings and not cycle 1's. The open findings from
earlier cycles are in the *reviewer's* package (`render_unresolved_issues`) but
never in the coder's prompt.

So a coder can regress an earlier fix while addressing a later finding, and the
only thing that catches it is the next review re-raising the original — which
costs a cycle, and by concern 27's rule the original was already marked resolved,
so it comes back as a new finding rather than as a regression.

*Resolved with the budget the fix said it needed.* `_correction_feedback`
appends earlier distinct correction messages under "Earlier correction guidance
— do not regress these fixes", newest evidence first, bounded by
`FIX_LOOP_FEEDBACK_HISTORY_LIMIT` (default 3, zero disables it). It carries
earlier *guidance* rather than concatenating earlier findings, which keeps
`MAX_FEEDBACK_ISSUES` meaning what it means for each cycle's own findings.

## 40. Two unlocated findings in one category are treated as one finding

`issue_fingerprint` identifies a finding by requirement id, file and category,
because prose is not stable across cycles. A reviewer that raises two findings
with no requirement id and no file, both `correctness`, produces one fingerprint
for two findings — so a later review that re-raises one of them keeps both open,
and one that raises neither closes both.

The collision is in the closing direction by design: an issue closed a cycle
early is recoverable, since a real defect gets raised again and the new finding
is recorded either way. An issue wrongly kept open costs package budget and
invites a re-raise, which is the failure mode concern 27 was about.

*Fix:* nothing, until reviewers are observed producing unlocated findings often
enough to matter. The reviewer prompt asks for a file and a requirement id, and
the honest measurement is how often it declines to give them — which is a
question for section 35's model evaluation, not a guess to encode now.

## 41. Nothing is written for a run that is still open, so a crash leaves no account of it — **resolved**

`outcome.json` is written when a run *settles* -- accepted, failed or escalated.
A run that is in flight when the process dies, or one abandoned mid-flight
without a terminal transition, leaves no outcome file: the reviews and artifacts
under its run directory are all there, but nothing summarises them at the run
root the way a settled run does.

*Why it matters:* the failure this leaves unanswerable is the one you most want
answered -- what was the system doing when it stopped. `inspect_incomplete_runs`
can say a run is resumable; it cannot say what that run had concluded so far.

*Resolved as described.* The fix loop calls `record_outcome(..., outcome=
"in_progress")` at durable turn boundaries and a terminal call replaces the
file in place with `accepted`, `rejected` or `escalated`. One location, one
schema, one file -- so a reader handles a crashed run and a settled run the
same way, and post-crash inspection does not have to know which it is looking
at. The extra write is on a path already writing several artifacts a turn.

## 42. A lesson's usefulness is measured against a counter it also increments

`applied_per_retrieval` divides times-applied by times-retrieved, and both
counters are incremented by the system: retrieval by the context builder, and
application by delivery crediting the lessons that were in the prompt when the
run was accepted.

So the ratio answers "of the times we put a lesson in front of a coder, how
often did the run that had it get accepted" -- not "did the coder follow it".
Nothing here observes whether the guidance changed the diff, because nothing
downstream knows which lines a lesson influenced.

*Why it matters:* it is the only usefulness number section 32 rule 6 asks for,
and it is a proxy. Read as "is the guidance landing at all" it is useful; read
as a per-lesson quality score it will mislead, and the bias is systematic
rather than random: a lesson that rides along on runs that were succeeding
anyway scores well.

*Fix:* attribute applied lines to lessons, which means the edit applier would
have to record which lesson ids were in the prompt for the run and the reviewer
would have to judge whether the lesson's subject was actually addressed. That is
a real attribution problem, not a metric, and phase M is the first point at
which there are enough runs to tell whether it is worth building.

## 43. Training examples accumulate faster than anything consumes them — **resolved**

Every accepted run is copied into `data/training/` with its artifacts and
manifest. Nothing selects, prunes or expires them, so the directory grows with
every accepted run indefinitely, and `training_metrics` reports
`captured_but_not_selected` as a growing number with nothing acting on it.

*Why it matters:* disk, and a curation queue nobody curates. Section 34 is
explicit that not every accepted example should be trained on, and phase L
implements the capture half and the visibility of the backlog; the acting half
is phase M's.

*Resolved*, count-based and deliberately narrow. Capture enforces
`TRAINING_MAX_CAPTURED_PER_PROJECT` (default 500) by excluding the
lowest-ranked *uncurated* examples -- the same ranking concern 45's queue uses,
so what retention drops is the material a curator would have reached last.
Three properties make this safe enough to run automatically: a human decision
outranks the ceiling, because retention only ever looks at `CAPTURED`; the
exclusion is recorded with its reason rather than performed silently; and only
the duplicate copy under `data/training/` is deleted, never the run directory
that is the authoritative record. `curate_training_example` is the selection
step the entry asked for.

## 44. Lesson extraction is tuned on fixtures, not on observed reviewer output

The filters -- which severities and categories are worth teaching from, the
grouping by category and file, the conversion of a description into an
imperative, the keyword and tag overlap used for retrieval -- were written
against constructed findings. Whether a real reviewer writes `required_fix` as
an instruction or as a complaint is an empirical question, and the imperative
conversion in particular is a guess about prose.

*Why it matters:* the extraction is deterministic and never asks a model, which
is the right default for a system that will be judged on what it teaches. But a
rule tuned on fixtures can be tuned for the wrong thing, and the failure is
silent: a badly-phrased candidate is simply a bad lesson in the queue.

*Fix:* the measurable version is the approval rate. `lesson-metrics` reports
`proposed` beside `approved`, and a proposal rate near zero with a high approval
rate means the filters are too narrow; a high proposal rate with a high
rejection rate means the phrasing is wrong. That is answerable from section 35's
endpoints on a real project, and it should be looked at before the extraction
rules are changed.

## 45. A run's outcome is decided by the run's status, not by whether it was useful — **partly resolved**

`capture_training_example` accepts any `SUCCEEDED` run. A task that passed
because its requirement was trivial, or because the reviewer approved a diff
that happened to satisfy the tests, is captured on the same terms as a run that
solved something hard.

*Why it matters:* section 34 warns against training on every accepted example
partly for this reason, and the curation step is the intended answer -- but the
queue was ordered by recency, so the examples most likely to be selected first
were the most recent ones rather than the most instructive.

*The queue is ranked*, now that something selects from it.
`ranked_training_queue` orders uncurated examples by review cycles and attempts
beyond the first, so a run that took three attempts and two review cycles is
offered ahead of a trivial first-attempt pass, and an example already curated
is not in the queue at all. Diff size against the task's own limit is not in
the signal yet: it is recorded, but it is the one of the three that is as
easily read as "this task was bigger" as "this run was instructive".

*What remains open is the underlying question*, which ranking does not answer:
`capture_training_example` still accepts any `SUCCEEDED` run, so a task that
passed because its requirement was trivial is captured on the same terms as one
that solved something hard. Ranking changes what a curator sees first; it does
not make the capture decision selective, and section 34's warning is about the
capture decision. That stays an operator's judgement until real runs say what a
selective rule would have to look like.

## 46. Compose could report ready with no application schema — **resolved**

The Phase M live restart exercise found PostgreSQL reachable with only an
`alembic_version` table while `/health` returned `ok`. The running orchestrator
image was stale, did not run migrations at startup, and its Debian image no
longer installed the Docker CLI through the `docker.io` package when
recommendations were disabled. A service in that state accepts traffic and then
fails on its first real query or worker start.

*Resolved* at all three boundaries. The Compose image installs `docker-cli`,
runs `alembic upgrade head` before Uvicorn, and readiness compares the database
tables with SQLAlchemy's mapped tables instead of merely executing `SELECT 1`.
The empty inconsistent development database was repaired after verifying it had
no application tables, migrated to `f1a83c6d2e47`, and survived a PostgreSQL and
orchestrator restart with all 18 public tables intact. `/health` now reports the
database and Docker worker backend healthy.

## 47. The Phase M acceptance run measures orchestration, not model quality

The ten-task acceptance project uses scripted coder and reviewer responses. Its
Git repository, command execution, artifacts, workflow checkpoints, failure
routing, restarts, and cleanup are real, but the responses are deterministic so
the test can require exact failure and recovery paths.

*Why it matters:* passing this gate proves that the orchestrator handles a good,
bad, and unfixable response safely. It does not prove that a configured local
coder can solve ten tasks or that the configured reviewer catches realistic
defects. Those are empirical properties of the endpoint, context window, and
model combination.

*Next step:* repeat the same low-risk workload on a non-production repository
with the actual configured coder and reviewer, preserve the run metrics, and do
not cross section 54's production-safe boundary until those ten real-model runs
meet the same outcomes. This is an operator validation boundary, not an open
code blocker.

## 48. An approved run acquired a durable `rejected` outcome before delivery — **resolved**

**Found in the real execution path**, on the first task driven by a configured
local model (TS-101, `RUN-20260927-000001`). Unlike the entries above it, this
was not read out of the code: the run's own event log showed it.

`_settle` called `_record_outcome` for every terminal outcome, and
`_record_outcome` maps everything that is not an escalation to `"rejected"` --
including `APPROVED`. So an approved run's `outcome.json` and its
`OUTCOME_RECORDED` event both said `rejected`, and only the later delivery step
replaced them with `accepted`:

``` text
APPROVED -> record "rejected" -> deliver -> record "accepted"
```

Observed window: 196ms. A durable `OUTCOME_RECORDED / rejected` row remains in
that run's history permanently, because the event log is append-only and the
correction is a *new* event rather than an edit of the old one.

*Why it matters:* the window is the defect, not its width. **If delivery or the
process fails between the two writes, the durable history asserts the opposite
of what happened** -- an approved run recorded as rejected, with no escalation
and no other record contradicting it. That is a correctness and recovery defect,
and it lands precisely on what concern 41 exists to provide: an accurate account
of a run that stopped. It also misleads anything counting rejections, which is
every metric section 35 offers.

*Resolved* by not writing a terminal outcome for an approval at all. The branch
above it in `_settle` already says why -- an approved run deliberately stays
`RUNNING` because its candidate is uncommitted and delivery is the workflow's
next step -- so the honest record for it is the provisional `in_progress` from
the last turn boundary, which stands until delivery writes `accepted`. Two
guards, because one call site is one accident away from returning: the call is
skipped for `APPROVED`, and `_record_outcome` refuses an approved outcome
outright rather than mapping it to a value it cannot correctly represent. Its
docstring always said it was "for a run that did not get delivered"; the call
site simply did not honour that.

## 49. Every event in a turn shared one timestamp, up to 79 seconds early — **resolved**

Also found in the TS-101 run. `run_events.created_at` was written by its
`server_default` of `func.now()`, and PostgreSQL's `now()` returns the
*transaction* timestamp: it does not advance within a transaction. Concern 36
made each code/verify/review turn a single transaction -- correct for recovery,
and it is what makes this bite, because a longer transaction collapses more
events onto one instant.

Measured on that run: nine events shared `10:30:32.01` and four shared
`10:32:17.73`. `TESTS_PASSED` was stamped **79 seconds before the tests
actually ran**. Confirmed directly against the database: `now()` returned an
identical value either side of a 300ms sleep while `clock_timestamp()` advanced.

*Why it matters:* section 40's event stream is the record of what the system was
doing and when. Ordering was never affected -- `sequence` is assigned by the
repository and is independent of the clock -- so what was unusable was
elapsed-time analysis, which is exactly what a first real workload is measured
with. A stage that appears to take zero seconds and another that appears to take
79 is not a measurement, and the error is silent.

*Resolved* by stamping each event when it is appended, in
`RunEventRepository.append`, leaving any timestamp a caller supplied intact. The
clock is the application's rather than `clock_timestamp()` because that function
is PostgreSQL-only and the test suite runs on SQLite; the `server_default`
remains as a floor for any row written outside the repository. Note for anyone
reading timestamps from SQLite: it has no timezone-aware column type and drops
the offset on read, while PostgreSQL's `timestamptz` keeps it.

## 50. A reviewer that omitted `confidence` silently bypassed the confidence gate — **resolved**

The third defect from the TS-101 run, and the one with a safety consequence
rather than a bookkeeping one. The reviewer returned no `confidence` field. The
gate read:

``` python
if (policy.min_confidence is not None
        and result.confidence is not None
        and result.confidence < policy.min_confidence):
```

so an absent confidence skipped the comparison entirely. A missing value is not
a parse error either, so `_confidence` returned `None` without appending a
warning: the review recorded `warnings: []`, and nothing anywhere said the field
had been absent.

*Why it matters:* the reviewer prompt asks for `confidence` and explains that a
low-confidence approval is sent to a human, so its absence is a contract
violation, not an option being declined. An operator who sets
`REVIEW_MIN_CONFIDENCE=0.6` believing it protects them gets **no protection at
all** from a model that never emits the field, and no evidence that the gate
never fired. This is concern 31 in a sharper form: not merely that confidence is
uncalibrated, but that omitting it is indistinguishable from satisfying it.

*Resolved* by making the absence deterministic instead of silent: when a minimum
confidence is configured, a review with no confidence is a human-review reason
naming what was required and not stated. The reason travels the same route as
every other section 37 gate, so it appears in the routing record and in the
escalation rather than only in a log.

The fix is deliberately conditional on a requirement having been stated.
`min_confidence is None` -- which is what `REVIEW_MIN_CONFIDENCE=0` resolves to,
and what was configured during the TS-101 run -- states no requirement, and then
an absent confidence constrains nothing and must not escalate. A gate that fired
when the operator had asked for nothing would be the mirror-image defect, and
the sort that gets a guard switched off.

The same reasoning applies to the other reviewer-contract fields that came back
null on that run, `risk` and `reported_task_id`, but their consequences differ
and neither currently gates acceptance: nothing is decided from them, so an
omission costs evidence rather than policy. They are worth revisiting when
something starts deciding on them, and a test pins the direction this one was
settled in.

## 51. Task dependencies ordered work without sharing it — **resolved**

Found on the first multi-task autonomous run, and the clearest example so far of
a defect that only a real workload exposes: three tasks completed, every gate
passed, nothing warned, and the result was still wrong.

`depends_on` constrained *scheduling* and nothing else. `prepare_workspace`
resolved the project's imported branch as every run's starting point, so a
dependent task was correctly ordered after its dependency and then handed a tree
without the dependency's accepted work in it.

Evidence from the TraceStack run:

* TS-101 through TS-104 all recorded `starting_commit 06a0697`.
* TS-104 depends on TS-103, and its instructions said *reuse `find()` rather
  than duplicating the search*.
* TS-103 had added a **public** `find()`. TS-104 could not see it and wrote its
  own **private** `find()` with the same signature -- which, given its context,
  is the correct thing to have done. **This is not a model failure**, and reading
  it as one is how a real orchestration defect gets attributed to quantization.
* Test counts were non-cumulative: 10, 11, 10 against a baseline of 8, never
  accumulating.
* A throwaway merge probe: TS-101 merges, then TS-102, TS-103 and TS-104 all
  conflict. Four individually verified candidates, collectively un-integrable.

*Why it matters:* the failure is silent and it compounds. Every gate reports
success, because every gate is per-candidate and each candidate really is
correct in isolation. What nothing measured was whether the candidates compose,
so the pile of conflicting branches grows one task at a time and is cheapest to
address before it grows.

*Resolved* with one ref and one gate.

`domain.git.INTEGRATION_BRANCH` (`agent/integration`) holds the cumulative
accepted state, under the agent prefix because it is orchestrator-owned by the
same convention every task branch already is. `prepare_workspace` starts task
worktrees from it instead of from the imported branch -- the read side of the fix
is one function called where one line used to resolve `default_branch`, which is
why candidate diffs, rollback and recovery all followed without changes: they
were already relative to the run's recorded `starting_commit`.

Delivery then folds an accepted candidate in, after the commit and the tag and
before the worktree is released. Four properties make that safe:

* **The imported branch is never advanced.** It stays where the operator left
  it. `GitService` already protected it and `force_branch` refuses it too, so
  this path cannot move it by mistake. Merging the orchestrator's work into a
  project's own branch remains what `GitService` always said it was: a human's
  decision.
* **The ref moves last.** The integration worktree is always *detached*, so the
  merge and the cumulative verification both happen on a commit no branch points
  at, and the ref is moved only once both have passed. There is no window in
  which the baseline names a tree nobody verified, and nothing has to be rolled
  back when a gate fails -- the ref simply did not move.
* **Verification runs over the merged tree.** Two candidates can each pass alone
  and fail together, which is the whole reason a cumulative baseline needs a gate
  of its own. The commands are the project's own profile in an ordinary worker
  under the ordinary policy; nothing here relaxes the sandbox, and the logs are
  filed under the run whose acceptance triggered them.
* **A blocked integration is loud and keeps the last good baseline.** A conflict
  or a cumulative failure records `INTEGRATION_BLOCKED` with the conflicting
  paths or the failing commands, and the candidate's own commit and tag are
  untouched -- it is still the run's audit trail. The next task therefore starts
  from the last state known to work.

In practice the merge is a fast-forward, because a candidate built on the current
baseline already contains it. The conflict path exists for correctness rather
than for the common case, and it is tested rather than assumed.

One consequence worth knowing: the integration worktree persists under
`WORKTREE_ROOT/<project>/_integration` instead of being created and destroyed
per integration, because the dependency tree cumulative verification needs is
expensive to copy. It belongs to no run, so `reap` never considers it (that works
from runs) and the census now excludes it -- otherwise it would be reported
`unclaimed` for ever, and `unclaimed` is how the census asks for a human.

### What the first fix left open, and how it closed

The paragraph that used to stand here said a blocked integration does not fail
the task, that an operator has to resolve it, and that until they do *later tasks
build on the older baseline*. The first two sentences were the right decision.
The third was a defect with the same shape as the original one: a per-task gate
reporting success while the composition was wrong.

The hole was precise. A candidate could pass verification, pass review, be
delivered, be marked `COMPLETE` -- and then conflict, or fail cumulative
verification. `INTEGRATION_BLOCKED` recorded that, and nothing read it, because
scheduling reads *state* and an event is a record. `evaluate_readiness` asked
only "are the dependencies `COMPLETE`?", so a dependent task became eligible and
was handed a baseline without its dependency's work in it. Unattended, that is
exactly the TraceStack failure again, reached by a different route: the work is
ordered correctly and then not shared.

*Resolved* by making the invariant explicit and giving it somewhere to live.

**The invariant.** A dependency is satisfied only when it is `COMPLETE` *and* its
accepted output is in the current integration baseline. `COMPLETE` answers "was
this task done"; the new `tasks.unintegrated_commit` answers "is it in the tree",
and readiness needs both. `NULL` -- every existing row, and every task that
integrated -- means nothing is outstanding. A task that produced nothing to
integrate (`COMPLETED_BY_HAND`) is satisfied vacuously, which is not a loophole
but the honest reading: the invariant is about a tree containing a dependency's
work, and a dependency with no work of its own cannot be missing from it.

**No new task state, and no new escalation mechanism.** Both were considered and
both were unnecessary, which is the useful part of the answer. The task stays
`COMPLETE`: the candidate is real, a reviewer accepted it, and `COMPLETE` is
terminal in the state machine -- there is no transition out of a delivered task
and there should not be one. The *dependent* moves to `BLOCKED`, which already
means "a dependency cannot be satisfied without intervention" and already
recovers. And the operator-visible condition is an ordinary section 24
`HumanEscalation`, with reason `INTEGRATION_BLOCKED` and one machine-readable
intent, `RETRY_INTEGRATION`. Extending the existing table was the whole change:
`effect_of` gained one row, and `ResolutionEffect` one field.

**One deterministic resolution.** The single option offered is "resolve the
blockage by hand, then re-attempt the integration", and answering it re-runs the
same merge and the same cumulative verification. Three cases, checked in order:
the baseline already contains the candidate, so somebody merged it by hand and
only the record was wrong; the task branch has moved *on top of* the accepted
candidate, which is the ordinary resolution and is what gets integrated; or
neither, and the same commit is merged again. The coding model is never asked to
resolve a conflict, and a claimed resolution is verified with `merge-base
--is-ancestor` rather than believed. A branch that no longer contains the
reviewed commit is refused outright -- otherwise an escalation answer would be a
way to land unreviewed work, which is the one thing the delivery path exists to
prevent. A retry that fails again blocks again and opens a *new* escalation: the
answered one stays answered, because somebody did try, and a condition that is
still true is a new question.

**Blocking follows the edges.** It is not a project-wide halt. An independent
task whose chain is unaffected is selected exactly as before, and the readiness
report separates `unintegrated` from `blocked` so an operator can tell the two
apart -- a `FAILED` dependency is a task to re-run, and this is delivered work
whose *integration* needs a person. Down a chain it is transitive for free:
`BLOCKED` was already an unsatisfiable state.

**Three doors, not one.** Readiness is where the invariant is enforced, and it is
not the only way a task gets promoted. `resume_task` applies the same rule, so a
pause and a resume cannot walk a task past a blocked dependency; and
`prepare_workspace` refuses outright, because the invariant really belongs to the
*starting commit* and that is the function which resolves one. The third is
belt-and-braces by design: the scheduler will not select such a task, so it
should never fire, but it is the door a hand-run single task or a future
scheduler would otherwise come through.

Everything the first fix promised still holds: the candidate's commit, tag and
artifacts are untouched, the baseline does not advance on either failure, and the
imported branch is still the operator's. What is new is that the orchestrator now
*stops* instead of quietly building on a baseline it knows is incomplete.

The regression tests are in `tests/integration/test_integration_baseline.py`
under "a blocked integration, and its consequences", parametrised over both
failure paths, plus the restart, the pause/resume, both resolutions, the retry
that fails again and the refused branch; the pure eligibility rule is pinned in
`tests/unit/test_dependencies.py`.

One thing is deliberately *not* offered: a way to declare the divergence
acceptable and unblock the dependents with the work still outside the baseline.
It would be one field and it would undo the invariant, so an operator who wants
that dismisses the escalation -- the dependents stay blocked, which is the safe
direction -- and deals with the task by hand.

## 52. A resumed run restarts the run instead of continuing it — **resolved**

Found on the same first real run, in the gap left after concern 51's follow-up,
and the most expensive defect in the file: it does not fail, it spends money.

`run_fix_loop` held its iterations, its reviewer feedback, its attempt
numbering and the attempt number it was about to use in lists and locals that
existed only in the process running it, and committed a whole turn at once.
Nothing in it could answer "where was this run when it stopped", so a process
that came back for a run that was mid-correction began the run again from the
beginning — with the same prompt, minus the review.

Evidence from `RUN-20260927-000009` (TS-105), where the reviewer asked for
corrections and the correction call then timed out after 600 seconds:

* The resumed attempt's prompt (`attempt-1-cycle-2/prompt.txt`) is the original
  task prompt with the entire *Review feedback to address* section removed — the
  same prompt, byte for byte, minus the seventeen lines naming the two HIGH
  findings. The correction was therefore requested a second time from a coder
  that had been told nothing about the first request.
* Attempt numbering restarted. The correction was attempt 2; the resume
  committed as attempt 1.
* `fix-loop.json` reported `attempts_used: 1, cycles_used: 1`, and
  `outcome.json` reported `attempts: 1, review_cycles: 2`, for a run that had
  begun two coding attempts and completed two reviews.
* The escalation said `RETRY_EXHAUSTED`, which is a claim about a budget nobody
  had spent.

*Why it matters:* the resume path is not a recovery path at all — it is a second
attempt that believes it is a first. The reviewer asked for something specific,
the run spent another attempt without it, and the record reported that the
budget was healthy. Every gate after that point was measuring the wrong run.

*Resolved* by making the state of a run reconstructable instead of carried.

`agents/loop_recovery.py` reads a run's durable records — its reviews and their
unresolved issues, its model calls, its events, its own counters, and the
attempt directories on disk — and returns where the run had got to: the next
attempt number, the review cycle to resume in, the feedback text to put in the
prompt, the fingerprints of the last two reviews for the stagnation guard, the
attempt that was begun and never finished, and the turn history as a whole.
`run_fix_loop` reconstructs before it does anything else, and the run row is
read back from the database rather than trusted from the caller, so a run
recovered by a fresh process and a run recovered by the same one are the same
code path.

Four properties of the accounting are worth naming separately, because each is a
distinct way to get this wrong again:

* **A coding attempt is charged when its call starts, not when it finishes.** A
  run whose provider keeps timing out has to be able to exhaust its budget, and a
  timeout is precisely the case in which nothing finishes. The evidence is the
  `model_runs` row written by the call itself (concern 53), and the attempt
  number on it.
* **A review cycle is charged when a reviewer answers.** A process that died
  waiting for the answer spent nothing, and resuming inside the same cycle is
  the only honest reading. The alternative spends a cycle on a review that never
  happened, and a task with two cycles silently gets one.
* **The attempt number advances past the interrupted attempt.** Reusing it would
  overwrite the artifacts of the attempt that was lost, which is how the resumed
  prompt in the evidence above ended up filed as though it were a first attempt.
* **The attempt a review belongs to comes from evidence, not from counting.**
  Reviews and coding calls are separate tables with no key between them, so the
  reconstruction pairs each review with the attempt that produced its candidate
  using the coding calls' own attempt numbers. Guessing it by arithmetic is how an
  escalation ends up describing a turn that never happened.

Reporting is then read from those records rather than from `len(iterations)`,
which is empty on a resume. The same numbers now reach `FixLoopResult`,
`fix-loop.json`, `outcome.json` and the escalation summary, and the recovery
itself is reported, so a reader can see that a run was resumed instead of
inferring it from a missing iteration.

The regression tests are in `tests/integration/test_fix_loop_resume.py`. The
last of the ten is three real interpreters against a file-backed database: one
sets the run up, one loses the correction to a provider timeout and exits with
the loop's own commits on disk, and one picks it up. It asserts that the
resumed prompt carries the reviewer's own words, that the attempt numbering
neither reused a number nor a directory, that the failed call is on the record
after the rollback, and that the run finishes approved. Reverting the recovery
call fails five of the ten, which is the check that they describe something
rather than decorate it.

One thing deliberately not attempted: a serialized checkpoint, or a
`loop_state` column. A run's state is already fully derivable from records that
have to exist anyway, and a second copy of it is a second thing to be wrong.

## 53. A model call that failed left no record, and a slow one said zero — **resolved**

The table that sections 34 and 35 are arithmetic over held only the calls that
worked.

A failed call was recorded in the same transaction as the turn that raised, so
the rollback that unwound the turn unwound the record of the call that caused it.
`model_runs` therefore has no row for a provider that times out, no row for a
reviewer that cannot be reached, and no row for a response in a shape the parser
cannot use. A reliability question about a model is not missing from a report
here — the data is not in the database.

Two smaller untruths sat in the rows that did survive. `duration_ms` defaulted
to zero for a call that failed, because nothing measured the wait, so a
600-second timeout and a request that never left the endpoint were the same
number. And every successful call's `started_at` equalled its `completed_at`,
because the success path never passed a start time and the fallback was the
completion instant.

Evidence: `RUN-20260927-000009` has four `model_runs` rows — two `CODE`, two
`REVIEW` — and none of them is the 600-second correction that cost more than
every other call in that run put together. The four that do exist each claim a
duration between 38 and 190 seconds across a zero-width interval between
`started_at` and `completed_at`.

*Why it matters:* a table that only records the calls a model answered cannot
distinguish "this model is reliable" from "this model was never asked". The gap
is not a zero in a report; it is an absence that reads as a zero.

*Resolved* in four parts, and the last two are what make the first two mean
anything:

* **The failure path records, and measures.** `agents/timing.py` gives every
  agent one definition of a call's duration, taken from a `monotonic()` origin
  so that a wall clock stepping under NTP cannot produce a negative duration or
  an hour-long one. The recorded row carries the failure text through the log
  redactor — an exception's own `str()` can contain a URL with a key in it — and
  bounded in length.
* **The row is committed by the call.** Each model call is checkpointed before
  the exception is re-raised, which is the last moment at which writing it is
  possible. This is what separates "this call failed" from "everything after the
  last turn boundary was lost": the call happened either way. The graph's
  rollback still rolls back; it just no longer rolls back the fact that a call
  was made.
* **The row says where the call sat.** `attempt` and `review_cycle` are on every
  `model_runs` row, and they are what the loop's reconstruction reads to know an
  attempt was really begun (concern 52) and which attempt a review belongs to.
* **The two timestamps agree with the duration.** Callers bracket their calls
  and pass the start; where one does not, the start is derived from the measured
  duration rather than left equal to the completion time.

Three nullable columns, so every row written before the revision keeps the
meaning it already has: `error_detail`, `attempt`, `review_cycle` — migration
`b7c41d90e2a5`. `NULL` on all three is the ordinary case for a call that
succeeded, and for every row that predates the revision.

The tests are in `tests/integration/test_fix_loop_resume.py` (a timed-out call
is on the record after the graph's rollback, and a slow failure reports the time
it actually took — pinned with a sleep rather than a 600-second wait) and in
`tests/integration/test_model_runs.py` (the two timestamps agree with the
duration). The migration is pinned in `tests/integration/test_migrations.py`,
which now also covers the case a fresh test suite never takes: the upgrade
applied to a table that already has rows in it, keeping them.

## 54. The cumulative gate ran and wrote nothing down — **resolved**

Concern 51 added a gate that runs the project's own verification over the merged
tree before the integration ref is allowed to move. Its *decisions* were
recorded — an event, an artifact, the ref either moved or did not — and the
checks themselves were not.

`_verify_cumulative` executed the commands in an ordinary worker and discarded
the results, so `verification_runs` for an integrating run showed the candidate's
own checks and nothing about the tree that actually decided whether the baseline
advanced. In the TraceStack run that is eight green rows for the candidate and
no row at all for the cumulative pass.

*Why it matters:* the cumulative gate is the only gate in the loop whose verdict
is about the project rather than about a task, and it was the only gate with no
durable record of its own. A reader could not tell that it had run, which is the
question a `rejected` candidate's history invites first.

*Resolved* through the existing verification table rather than a new one. Each
cumulative command is recorded as a `VerificationRun` through the same
repository, under its own type: `INTEGRATION_BUILD`, `INTEGRATION_LINT`,
`INTEGRATION_TESTS`, `INTEGRATION_SECURITY`.

The type is the point. Same table, same model, same command text, and a history
that can answer "this passed on its own" from "this passed together with what
came before" without parsing the command to find out. The logs stay separate
from the candidate's own, filed under the run whose acceptance triggered them,
and the rows are flushed before the ref moves — otherwise a crash between the
verification and the flush would leave a moved ref with no record of why.

Worth recording as well: the four new enum values needed no migration, because
`verification_type` is stored as a plain `VARCHAR(32)` rather than a native
enum. That is the sort of thing the column-comparison test in
`tests/integration/test_migrations.py` would have caught for free if it had been
otherwise, which is the argument for having that test at all.

## 55. A file the task may replace became unreadable as the chain grew — **resolved**

Found by the clean cumulative TraceStack run: ten synthetic tasks, no injected
faults, the same Qwen at the same 32768, run until the orchestrator asked for a
human. TS-101 through TS-105 were accepted and integrated on first pass in about
twelve minutes. TS-106 was refused before a model was called at all.

The refusal itself was right. The edit contract asks for the *complete new
contents* of every file the coder changes, and the context builder had shown it
242 of the 276 lines of `src/test/navigation-stack.test.ts`. Asking a model to
reproduce a file it has seen five sixths of does not produce five sixths of a
file; it produces a whole file with the last sixth deleted, and that deletion
arrives as an ordinary-looking diff. The guard in `agents/coding_agent.py`
caught exactly that and escalated `HUMAN_DECISION_REQUIRED`.

What was wrong was the state it was catching. From `RUN-20260927-000016`:

* `source_bytes` 8110, `source_lines` 276, `shown_lines` 242, `truncated` true.
* `estimated_tokens` 2034 against `CONTEXT_MAX_ITEM_TOKENS=2000`.
* The whole package: 2041 tokens of a 14745-token budget.

The task was refused for want of 34 tokens with twelve thousand unspent.

*Why it matters:* the per-item cap was being asked a question it cannot answer.
A cap on supporting context is a sound idea — the head of a file you are only
reading is usually the useful part. A cap on a file the coder must hand back
whole is not a budget decision at all; it is the difference between a task that
works and a task that cannot be attempted. The two were the same number.

*Resolved* by making writability a property of a context item rather than
something only the agent downstream knows. `ContextItem.requires_complete` is
set in the builder's declared-file pass, from the task's own `files_to_modify`
allowance, before any budgeting happens. `domain.context.assemble` then settles
those items first: exempt from `max_item_tokens`, charged against `max_tokens`
in full, and taken off the top so that supporting context competes for what is
left. A file the task may only read is untouched by this and is still clipped,
which is what clipping is for.

The reservation is refused whole or not at all. If the required files cannot fit
alongside the task specification, the reservation is abandoned and the package
is built exactly as it was before — clipped, marked `truncated`, and refused by
the same guard, with the reason in the manifest and a warning naming the files.
A partly honoured reservation would be worse than either outcome, because it
looks like the file arrived whole.

Replayed against the real file at the integration tip `a2e40f2`: 2334 tokens,
truncated at 242 of 276 lines before, complete after, package 2347 of 14745.

The tests are in `tests/unit/test_context_budget.py` (seven, covering the 8110-
byte case, supporting context sacrificed first, several writable files ordered
deterministically, the impossible case still clipping, the total budget never
exceeded, large read-only files still clipped, and the manifest record) and in
`tests/integration/test_context_builder.py` and
`tests/integration/test_coding_agent.py`. Five of the seven unit tests fail
against the old assembly pass with the new field present, which is the check
that they describe the behaviour rather than decorate it.

`tests/integration/test_coding_agent.py` keeps both sides: the file that used to
be refused is now coded, and a file too large for the *total* budget is still
refused before any model call. The guard was not weakened. It is now the defence
in depth behind the budgeting rather than the thing that fires first.

## 56. The context budget had one cap for two different kinds of input — **resolved**

The general form of concern 55, and the reason the fix is not a larger number.

`CONTEXT_MAX_ITEM_TOKENS` was applied to every item alike: an interface the
coder glances at, a configuration file, a recent commit, and a file it is about
to rewrite from scratch. Only the last of those has a correctness requirement
attached to its completeness. Raising the cap would have moved the cliff without
removing it — the test file grows with every task that touches it, so any fixed
number is a task count, and the run would have stopped at TS-108 instead of
TS-106.

Measured over the clean run, on `src/test/navigation-stack.test.ts`:

```
06a0697  baseline   2985 bytes
6d5de16  TS-101     4044
613f311  TS-102     4621
02b3342  TS-103     5162
f6eaa23  TS-104     5628
a2e40f2  TS-105     8110   <- crosses the cap
```

*Why it matters:* this is a property of cumulative development, not of these ten
tasks. Each accepted task makes the next one harder, silently, until the ceiling
arrives and nothing works. Five runs in a row looked perfect. The sixth had no
useful failure mode available to it — the model was never asked, so there was
nothing to review, nothing to fix and nothing to learn from. A pipeline that
degrades with its own success cannot be judged by a short run, which is the
argument for running all ten rather than stopping at three or five.

*Resolved* by the same change: the budget now distinguishes required complete
writable inputs from supporting context, and only the second kind is clipped.
The total budget is unchanged and is never exceeded — `CONTEXT_MAX_TOKENS`,
`CONTEXT_WINDOW_SHARE`, the served 32768 window and the model are all as they
were. Requirement, not optimisation: nothing here may make a package larger than
the operator configured.

What the manifest now carries, so this is legible without re-reading a prompt:
`requires_complete` on every item, and a `required_complete` block giving the
paths, the tokens reserved, whether the reservation was honoured, and the reason
when it was not.

One gap was left open here because it is a different trigger and not what these
two concerns describe: a writable file larger than `CONTEXT_MAX_FILE_BYTES`
(262144) is not clipped but omitted entirely, with only a warning. It is then
absent rather than truncated, so the guard — which looked at truncated paths —
did not see it, and the coder could be asked to write a file it was never shown
at all. That is the same invariant with a worse failure mode. It is concern 58,
and it is now closed.

## 57. An abandoned HTTP client can park the whole orchestrator — **resolved**

Found while setting the clean run up, before it started. `GET /health` hung, and
had hung for twenty-three minutes with no log line after the last one.

The evidence, from `pg_stat_activity`:

```
33305 | idle in transaction | Client | ClientRead         | 00:27:37 | SELECT projects...
28269 | idle in transaction | Client | ClientRead         | 00:27:36 | SELECT task_runs...
33701 | idle in transaction | Client | ClientRead         | 00:26:50 | SELECT projects...
33702 | active              | Lock   | transactionid      | 00:26:49 | UPDATE task_runs SET external_run_id=$1 WHERE task_runs.id = $2
```

**Reproduced before anything was changed.** Two real connections to a real
PostgreSQL: one writes a `task_runs` row and then issues a `SELECT`, leaving the
transaction open; the second issues the same `UPDATE`. The second waited
indefinitely, and `pg_stat_activity` showed the identical signature, including
the misleading part — `idle in transaction / ClientRead` with a `SELECT` as the
holder's visible query.

**Which lock.** A PostgreSQL row-level write lock on one `task_runs` row.
Nothing application-level: there is no advisory lock, no `SELECT ... FOR UPDATE`
and no lock table anywhere in the codebase. `wait_event = transactionid` means
the waiter is queued behind another transaction's completion, not behind a named
lock object.

**Who held it, and the thing that made the diagnosis slow.** `pg_stat_activity`
reports a session's *last* statement, not the statement that took its locks. All
three holders showed a `SELECT`, so the table appeared to say that reads were
blocking a write, which is impossible. The lock came from an earlier `UPDATE` in
the same still-open transaction.

**Live, stale or contending.** Stale, on a *live* connection — the case that
neither of the other two covers. The driver process had died, but the
orchestrator's own backend connection was alive and idle inside a transaction.
Process death was never the failing case: PostgreSQL rolls back a backend whose
client is gone, which is pinned by
`test_a_holder_whose_process_dies_releases_the_row`. What leaked was an
orphaned `Session` inside a live uvicorn process, and those are not created by
`get_db` — the workflow runner makes its own sessions, and a request task
cancelled mid-`await` never reaches the code that would close them.

**Why the acquisition had no bound.** `lock_timeout` and
`idle_in_transaction_session_timeout` both default to `0` in PostgreSQL, meaning
wait forever, and `create_db_engine` set neither. There was no application-level
acquisition timeout either. So the bound on every lock wait in the orchestrator
was the lifetime of whatever held it.

**What controlled release.** The holding transaction's lifetime, which was the
lifetime of the Python `Session` object, which nothing bounded. In practice:
process exit. A restart cleared it, which is the wrong thing to have to
discover.

**Whether retry can recover.** Yes, and that is what makes a bounded failure
safe here rather than merely faster. A waiter that times out holds nothing and
has written nothing, so there is no second owner to reconcile; and concern 52's
recovery reconstructs a run's state from durable records, so a run that fails
this way is resumed rather than restarted.

*Why it mattered:* the orchestrator's claim is that a run's state is durable and
its progress visible. Here it was neither — no event, no log, no failed request,
and a health endpoint that could not answer because it was in the same queue.

*Resolved* with two per-connection settings, applied at connect time in
`db/session.py`, because a lock wait can happen on any write in a run and
enumerating them would leave every new write unbounded until someone remembered.

* `lock_timeout` (`DB_LOCK_TIMEOUT_SECONDS`, default 30s) bounds the **waiter**.
  A statement blocked on a row lock now fails with SQLSTATE `55P03` instead of
  waiting for a transaction that may never end.
* `idle_in_transaction_session_timeout`
  (`DB_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS`, default 300s) bounds the
  **holder**, which is the root cause rather than the symptom. An abandoned
  request leaves a live connection idle in a transaction; the server now ends
  it, and its locks go with it.

Neither weakens mutual exclusion and neither bypasses a lock. A lock that is
available is still taken, and still held for the whole transaction; what changed
is that waiting for one is no longer unbounded. `test_the_holder_keeps_its_
protection_while_it_is_active` is the test that says so: a transaction that
keeps working past both timeouts is not interrupted.

A bound that reports `OperationalError` would be a bound and not a diagnosis, so
SQLSTATE `55P03` is translated once, at the engine, into `LockWaitTimeout` —
matched on SQLSTATE rather than message text, which is localised. It maps to
HTTP 503 rather than 409: the request was valid and the contention is usually
transient. Its failure class is `RESOURCE_UNAVAILABLE`, whose existing policy is
`PAUSE` — a durable, visible stop rather than silence.

The tests are in `tests/integration/test_db_locking.py`, against a real server
in a scratch database that is created and dropped there, contending on a real
`task_runs` row with the incident's own statement. They cover the timeouts being
set on every connection, an uncontended write still succeeding, contention
failing in bounded time with a named and diagnosable error, the holder keeping
its protection while active, release letting the next writer through, the
abandoned transaction being ended by the server, a killed child process
releasing the row, and a retry after a timeout not producing a second owner.
They skip only when no PostgreSQL server is reachable; every other failure is
raised rather than swallowed, so a broken test cannot pass as a missing
environment.

Reverting the two settings fails
`test_a_contended_write_fails_in_bounded_time_and_says_why` on "the second
writer waited without a bound" — and then the pytest process itself cannot exit,
which is concern 57 in miniature and the most direct demonstration of it
available.

## 58. A writable file could go missing rather than arrive short — **resolved**

Noted while closing concerns 55 and 56 and left open there deliberately; this is
it on its own terms.

Concern 55 made a file the task may replace exempt from the per-item cap, and
the coding agent's guard stayed where it was, reading `truncated_paths`. That
pairing is sound only while "the coder did not see all of this file" and "there
is a clipped item in the package" mean the same thing. They do not.

`RepositoryReader` refuses a file larger than `CONTEXT_MAX_FILE_BYTES`, and it
refuses one with a NUL byte in it, and `_list_paths` never offers a path that
`is_excluded` or that is not textual. Each of those refusals produces *no item*:
no content, no truncation marker, no `DroppedItem`. A file the task declared as
writable and that exists in the checkout could therefore be absent from the
package entirely, and the guard would see an untruncated package and let the
attempt run. The coder would then be asked for the complete new contents of a
file it had never read -- the same failure concern 1 was written about, arrived
at from the opposite direction and with nothing at all to notice it by.

There is a second path to the same place, and it is the one that shows why the
fix could not be a longer list of exclusions. `_declared_paths` is built from
`reader.paths`, which has already dropped the excluded and the non-textual, so
such a file does not even reach `declared.existing`: it lands in `missing`,
which is the same classification as a file the task is going to *create*. "There
is nothing to supply" and "there is something and it never arrived" were the
same state.

*Why it matters:* the consequence is a silent deletion, not an error. A
whole-file replacement of a file the model never saw is a valid-looking diff
that removes everything that was there. Verification may well pass it if the
deleted part had no test, and the reviewer sees a diff, not an absence.

*Resolved* by recording completeness instead of inferring it.

* `domain.context.RequiredSource` -- one entry per *existing* file the task may
  replace, saying whether its complete original contents are in the package and,
  when they are not, why. Carried on `ContextPackage.required_sources` and
  written to `context-manifest.json`.
* The verdict is computed from the assembled package: a required path is
  complete when its item is present and not truncated, and incomplete
  otherwise -- clipped, dropped, or never a candidate. That phrasing is the
  point. A context builder acquires new exclusion paths over time, and "is its
  complete text in here?" keeps answering correctly when it does, where a list
  of known exclusions would have to be extended each time and would fail
  silently when it was not.
* Which paths are *required* is asked of the filesystem rather than of
  `reader.paths`, through `_required_source_paths`. A writable declaration that
  resolves to a real file is a required source even when selection would never
  have offered it, which is what separates it from a file the task will create.
* `RepositoryReader.exclusions` records why a path that exists could not be
  read, so the refusal can name the limit that caused it rather than say only
  that something is missing.
* The guard, now `_incomplete_writable_sources`, reads that record first and
  keeps the original `truncated_paths` check behind it. Two sources for one
  decision is deliberate: if the record is ever wrong or absent, a visibly
  clipped writable file still refuses.

The invariant the guard now enforces: for every existing file the coder is
permitted to replace, the complete original contents were supplied, whatever the
reason they might not have been.

`CONTEXT_MAX_FILE_BYTES` is unchanged at 262144, as are the model, the served
window, the budget shares and the worker policy. A file too large to read is
still too large to read; what changed is that the run now stops instead of
proceeding without it.

The refusal message changed with it. It names each file and the specific reason
-- the byte cap, the budget, the line counts -- and no longer recommends raising
`CONTEXT_MAX_ITEM_TOKENS`, which after concern 56 would have done nothing for a
writable file.

The tests are in `tests/integration/test_coding_agent.py` (an oversized writable
file refuses the attempt with no model call and no `model_runs` row; an
oversized file the task only *reads* is still simply omitted and the attempt
proceeds; an ordinary writable file still proceeds), in
`tests/integration/test_context_builder.py` (the byte cap and a binary file are
both recorded incomplete, and a file read whole is recorded complete, while a
file the task will create is not a required source at all) and in
`tests/unit/test_context_budget.py` (a required path that never became an item,
one dropped for the budget, one with no stated reason, and the manifest entry).
Restoring the truncation-only guard fails the oversized-writable test with the
coder having been asked for an answer, which is the check that it describes the
behaviour rather than decorating it.

## 59. A task could not start a second run once it had a branch — **resolved**

Found by resuming the synthetic TraceStack campaign through the supported
operator path. TS-106 had escalated before any model call (concerns 55, 56 and
58); its escalation was answered with `RETRY_TASK`, the task returned to
`READY`, a second run row was created — and `POST /projects/{id}/run` answered
`500`.

**Reproduction.** From the incident, and reproduced in a test with real Git:

```
apps/orchestrator/api/projects.py:110      run_project
apps/orchestrator/workflow/graph.py:265    _prepare_workspace
apps/orchestrator/services/workspace.py:134  prepare_workspace
apps/orchestrator/services/git_service.py:352  create_worktree
    raise BranchAlreadyExists: Branch agent/TS-106-remove-every-entry-belonging-to-one-file already exists
```

Deterministic: every later `POST /run` failed identically, and run 2 was reused
rather than multiplied, so the task was stuck with no way forward that did not
involve deleting a branch by hand.

**Root cause.** `worktree_dir_name` is run-scoped and says so in its own
docstring — *"includes the run number so a retried task never collides with the
leftovers of an earlier run"* — while `task_branch_name` was task-scoped. Run 1
of TS-106 created the branch; run 2 built a new run-specific worktree directory
and then asked Git for the same branch again. The collision was anticipated for
the directory and not for the branch.

**Which the branch represents: one run, not the task's lifetime.** This was
answered from the existing architecture rather than chosen. `branch_name` is a
column on `task_runs`, not on `tasks`. The generator had exactly one caller —
`prepare_workspace`, and only for a run with no branch yet — while every other
consumer reads the persisted `run.branch_name`: `attach_workspace`,
`integration.py`, `workflow/graph.py`, `workflow/recovery.py`,
`services/worktrees.py`. The persisted model already treated branch identity as
a property of a run. Only the name generator disagreed with it.

Section 10 does not contradict that. Rule 2 says the orchestrator chooses branch
names, and rule 7 asks that each task be *traceable* to a branch, which a name
carrying the task id verbatim still satisfies. `agent/TS-004-navigation-tree`
is an example, not a constraint.

*Resolved* by making the generator agree with the model: `run_branch_name(task,
title, run_number)` → `agent/TS-004-navigation-tree-run1`, derived from durable
identity with no randomness, alongside `worktree_dir_name`'s `ts-004-run1`.

**Why a suffix and not `agent/<task>/<run>`.** Git stores loose refs as files, so
`refs/heads/agent/TS-106-x` being a file makes `refs/heads/agent/TS-106-x/run-2`
impossible:

```
fatal: cannot lock ref 'refs/heads/agent/TS-106-slug/run-2':
       'refs/heads/agent/TS-106-slug' exists; cannot create ...
```

Checked in a scratch repository before choosing. A path segment would have made
every new branch fail in exactly the repositories that carry history from before
this change, and the only remedy would have been rewriting historical refs. A
suffix has no such conflict.

**Retry and resume stay distinct**, and were already distinguished correctly:
`graph.py` attaches when `run.branch_name` is set and prepares when it is not.
A retry is a new run with a new branch from the current integration baseline; a
resume re-opens the identity its run already holds. The retry does *not*
continue from the failed run's branch merely because that branch exists — pinned
by a test asserting the rejected commit is not an ancestor of the retry's branch
and that its file is absent from the retry's tree.

**Nothing is deleted.** The first run's branch stays where it is; it is that
run's audit trail under rule 7, and `release_workspace` keeps branches by
default. Cleanup is per run: releasing one run's workspace with
`delete_branch=True` leaves another run's branch and worktree untouched.

**The 500 was the second defect.** `services.git_errors` already distinguishes
an operator problem from a bug, and the API was not reading it, so every Git
failure — a dirty repository, a refused protected branch, a timeout — arrived as
an unclassified 500. `DirtyWorktree`, `WorktreeMissing`, `ProtectedBranch` and
`PushNotPermitted` now answer 409, and `GitCommandTimeout` 503.

Deliberately *not* translated: `BranchAlreadyExists`, `WorktreePathRejected`,
`GitCommandFailed`, `NotARepository`, `NothingToCommit`. Each now means an
invariant is broken, and a 500 with a traceback is the honest answer to a bug.
Mapping `GitError` wholesale would have turned a colliding run identity into a
tidy 409 that reads like an operator problem — which is how this concern would
have been hidden rather than found.

**Compatibility: no migration, no rewritten refs.** Branch identity is
persisted, not recomputed. The generator's single caller only runs for a run
that has no branch yet, so TS-101 through TS-106 keep the task-scoped names in
their records and every consumer keeps reading them. A test builds a run row the
way a historical one looks — a task-scoped branch and a worktree created outside
this code — and resumes it.

The tests are in `tests/integration/test_workspace.py` (a second run prepares a
distinct branch and worktree without deleting the first; the first run's branch
stays inspectable and still resolves to its commit; the retry starts from the
integration baseline and does not inherit the rejected candidate; a resume keeps
its own identity; four sequential runs stay collision-free; two tasks stay
isolated; names are recomputable from durable identity; releasing one run leaves
another intact; checkpoint tags remain attempt-scoped; a pre-change run still
attaches), in `tests/integration/test_projects_api.py` (the classification
table, driven through the real app's handlers, including the absence of a
mapping for the invariant breaches) and in `tests/unit/test_git_naming.py`.

Real repositories and real worktrees throughout: the defect was in Git's own ref
semantics, and a stub would have agreed with whatever the code did.

One test asserted the old name and was changed deliberately rather than to make
the suite pass: `test_prepare_creates_an_isolated_worktree_on_a_named_branch`
now states that the branch names the run. The other `agent/TS-001-first` strings
in `test_git_service.py` are arguments passed straight to `GitService` and never
assumed task-uniqueness.

Restoring task-scoped naming fails seven tests, the retry test among them, with
`BranchAlreadyExists: Branch agent/TS-001-fix-the-answer already exists` at
`git_service.py:352` — the same error, at the same line, as the TS-106 incident.

## 60. Run lifetime was charged as active runtime and deadline expiry was called retry exhaustion — **resolved**

Found while stopping the synthetic TraceStack campaign after TS-106 run 2.
The live record is intentionally unchanged: `RUN-20260927-000017`, escalation
`85411efc`, task state, timestamps, Git refs and integration state remain the
historical evidence, and TS-106 was not retried.

**Exact incident.** Run 2 was created at `16:24:01.855662`; its workspace was
successfully created at `17:29:50.033053`. With
`max_runtime_minutes = 30`, the implementation derived `16:54:01.855662` from
row creation. Concern 59's earlier workspace-preparation failure left the run
durable while its repair was made, but no coding work happened in that wait.
When the fix loop was finally entered, its deadline was already 35m48s in the
past. It returned before attempt 1 with `attempts_used = 0`, `cycles_used = 0`,
`iterations = []`, `recovery.next_attempt = 1` and `model_runs = 0`. The only
events were `TASK_SELECTED 16:24:01.860218`, `WORKSPACE_CREATED
17:29:50.033053`, `HUMAN_REVIEW_REQUIRED 17:29:50.090897` and
`OUTCOME_RECORDED 17:29:50.103286`; there was no `CODING_STARTED` or
`FIX_STARTED`. `agent/integration` remained `a2e40f2`.

**Root cause.** `task_runs.started_at` is row/audit creation time, but
`run_deadline(started_at, limits)` treated it as accumulated execution time.
The graph computed that deadline before workspace preparation and the fix loop
again intersected it with `started_at + WORKER_TIMEOUT_SECONDS`. This did
prevent a restart from granting a fresh budget, but only by charging every
kind of wall-clock waiting. The same deadline branch then settled with
`RETRY_EXHAUSTED`, even when zero of three attempts had started.

**Runtime semantics.** A task run now persists `active_runtime_ms` plus nullable
`active_started_at`. Row creation, scheduler queueing, workspace preparation
(including failure and later recovery), project/operator pause and escalation
wait are inactive. The workflow opens an active interval immediately before
the fix loop and commits that boundary before model work. Coding, provider
calls, verification, review/fix cycles and waits while that invocation owns its
worker are active. A clean success, escalation, failure or recoverable
exception closes the interval into the cumulative counter. Resume of the same
run computes its deadline from its remaining counter; `RETRY_TASK` creates a
new row with zero consumed runtime and therefore a fresh budget.

**Crash/restart semantics.** An open `active_started_at` survives transaction
rollback and process reconstruction. Recovery charges the abandoned invocation
from that boundary, capped at its per-invocation worker safety deadline, before
opening a new interval. Work done before a crash therefore cannot be reset by
restarting, repeated restart/resume monotonically spends the one run budget,
and an hour of inactivity after a dead process is not charged as an unbounded
hour. The cap is deliberately conservative: without a high-frequency durable
heartbeat, exact process-death time does not exist in the database. Charging
the invocation's worker lease preserves the anti-reset invariant without a
timer constantly writing rows.

The timeout scopes are now explicit. The task-run runtime budget is cumulative;
the worker timeout is recreated per invocation and bounds that invocation and
an abandoned crash interval; model/provider timeout remains `MODEL_TIMEOUT`;
verification command timeout remains local to its command; database lock and
statement timeouts remain concurrency controls. A worker deadline does not
masquerade as run-runtime or provider timeout.

**Classification and operator behavior.** Deadline expiry caused by the
cumulative run budget is `RUNTIME_EXHAUSTED`, with its own escalation policy.
The fix-loop artifact, event and escalation carry configured, consumed and
remaining runtime, actual coding attempts, actual completed review cycles, and
state explicitly that runtime—not retry budget—stopped the run. A legitimate
zero-attempt expiry says zero attempts without claiming “0 of 3 attempts were
made and none produced an accepted change.” Genuine attempt/review exhaustion
still uses `RETRY_EXHAUSTED`; provider call timeout still uses `MODEL_TIMEOUT`.
The runtime escalation does not presume the task should be reworded or split.
Its `RETRY_TASK` option says exactly that it creates a new run with a fresh
budget and leaves the exhausted run unchanged.

**Migration and compatibility.** Revision `e2d6b79a4f10` adds the two runtime
columns reversibly. Populated historical rows receive zero and NULL. That is a
conservative compatibility statement, not fabricated precision: old wall-clock
lifetime cannot be separated into active and inactive time after the fact.
Historical timestamps, results and rows—including the live TS-106 incident—are
not rewritten.

**Regression coverage.** `test_runtime_accounting.py` uses fixed datetimes to
cover a first execution delayed beyond the configured budget, workspace failure
and inactive pause, active accumulation, same-run remainder, crash recovery,
repeated restart exhaustion, fresh budget on a new retry run, and per-invocation
worker timeout. Fix-loop tests prove runtime-specific classification and
evidence (including zero attempts and no model call), worker-timeout separation,
genuine retry exhaustion, and provider `MODEL_TIMEOUT`. Migration tests cover
from-scratch upgrade, populated upgrade, full downgrade and mapped columns.
The existing Concern 52 crash/resume and Concern 59 retry/resume/workspace tests
remain the process/reconstruction and identity regressions. Reintroducing a
`started_at`-anchored deadline fails the delayed-first-execution test;
reintroducing `RETRY_EXHAUSTED` on deadline expiry fails the classification and
reporting test.

## 61. A rejected model edit could disappear silently and the output ceiling was
stale -- **resolved**

Found in `RUN-20260927-000018`, the second TS-106 run. Two related defects
proved themselves in the same coding attempt.

**Defect 1: parse-level silent edit loss.** A model response containing multiple
edits could have one edit rejected during `CodeChangeSet` parsing while other
edits survived. The rejected edit became only a warning string and disappeared
from `change_set.edits`. The coding attempt therefore proceeded through
verification and review with a partial candidate -- the valid edits were written,
the rejected one was silently omitted, and the completion report showed no sign
that the model's response had been incomplete.

The incident shape: the Orchestrator supplied the complete 8110-byte writable
test file to the coder (concern 56 had closed the input side). The coder
returned two edits, one valid and one whose content was past the per-file output
ceiling. The valid edit was applied; the oversized one became a warning and
vanished. The attempt finished, the diff was captured, the completion report was
written, and the candidate went to verification as though the response had been
complete.

**Defect 2: stale output-size derivation.** The whole-file replacement ceiling
was still derived from `CONTEXT_MAX_ITEM_TOKENS=2000`:

```
characters_for_tokens(2000) * 1.25 = 8750 bytes
```

Concern 56 intentionally stopped applying that per-item input ceiling to
required complete writable files, but the output side still derived its ceiling
from it. In TS-106 the Orchestrator supplied the complete 8110-byte writable
test file to the coder, then rejected the coder's 10604-byte replacement as
over the 8750-byte limit. The input and output ceilings were no longer describing
the same file at two moments; the output ceiling was describing a file the coder
had never been shown.

*Why it matters:* the first defect is a fail-open: a partial model response
proceeds as though it were complete, and the reviewer sees a diff without
knowing that the model asked for more. The second defect is the same shape as
concern 1 with the roles reversed: the input side was fixed to show the file
whole, but the output side was still bounded by the old input ceiling, so a
legitimate rewrite of a file the coder had actually seen was refused.

*Resolved* in two parts.

**Part A: fail closed on dropped requested edits.**
`domain.edits.RejectedParseEdit` is a new dataclass recording path, operation
and reason for every model-requested edit that could not be parsed.
`CodeChangeSet.rejected_parse_edits` carries them structurally, not as warning
strings. `CodeChangeSet.has_parse_rejections` is the load-bearing property: when
it is true, the coding agent fails the attempt with `INVALID_MODEL_RESPONSE` and
sends the rejected paths and reasons back to the coder as feedback. The attempt
does not proceed to `apply_change_set`, diff capture, scope evaluation or
completion report. No partial candidate is treated as successful.

Informational parser warnings -- content sent with a delete, a duplicate path --
remain warnings and do not trigger the fail-closed path. The distinction is
between *an edit the model asked for that could not be accepted* and *an edit
that was accepted with a note*.

**Part B: bounded per-path output allowance.**
For an existing writable file that was declared writable for the task, was
actually supplied to the coder, and was supplied complete, the output ceiling is
now derived from the source file's actual size:

```
allowance = max(MAX_EDIT_BYTES, int(source_bytes * EDIT_SIZE_HEADROOM))
```

capped at `CONTEXT_MAX_FILE_BYTES`. New files, files not supplied whole, and
paths without a trustworthy complete source record fall back to the default
`max_edit_bytes` derived from `CONTEXT_MAX_ITEM_TOKENS`. The invariant: a
bounded task may rewrite a complete writable file with reasonable growth
proportional to the file it was shown, but model output remains bounded.

`EDIT_SCHEMA_VERSION` is now `code-edits/2`. The `describe()` output carries
`rejected_parse_edits` alongside `warnings`.

**What is preserved.** Allowed-path enforcement, protected-file enforcement,
`max_files_changed`, diff policy, security policy, context completeness
protections, worker isolation, verification and review requirements are all
unchanged. The application-layer refusal semantics -- a scope violation still
fails with `SCOPE_VIOLATION`, an edit to a non-existent file with `update` is
still refused at application time -- are untouched. The parse layer and the
application layer remain distinct: a parse rejection is `INVALID_MODEL_RESPONSE`
with feedback to the coder; an application rejection is a scope or tree problem
recorded on the completion report.

**The tests are in** `tests/integration/test_concern61.py` (seventeen tests):
multi-edit payload with one valid and one oversized edit records the rejection
structurally; coding attempt with one valid and one rejected edit fails closed
with `INVALID_MODEL_RESPONSE` and feedback naming the rejected path; candidate
verification does not run after a parse rejection; the exact TS-106 shape -- an
existing writable file larger than the old 8750-byte ceiling, supplied
completely, with a proportionally larger replacement -- is accepted under the
new per-path allowance; new and small files remain bounded by the default
ceiling; grossly oversized output remains rejected even with a per-path
allowance; the outer ceiling caps the per-path allowance; informational warnings
(delete with content, duplicate path) are not treated as rejections;
application-layer scope refusals still fail with `SCOPE_VIOLATION`;
discrimination checks that restoring the silent drop or the fixed 8750-byte
ceiling fails the new tests.

Restoring the silent `if edit is None: continue` behavior fails the multi-edit
rejection test. Restoring the stale fixed 8750-byte output behavior fails the
TS-106 regression. The existing Concern 55/56/58 tests continue to pass, as do
the existing edit-contract, coding-agent, context-builder and Phase M acceptance
tests.

**Campaign facts preserved.** `RUN-20260927-000018` remains historical
FAILED / RETRY_EXHAUSTED. TS-106 remains HUMAN_REVIEW. `agent/integration`
remains `a2e40f2`. The existing escalation remains open. TS-106 was not retried
as part of this fix.

*(State as recorded at the time of this fix. TS-106 and the escalation have since
moved, through `RUN-20260927-000020` and Concerns 63/64; see section 64 for the
current values. The run and the ref above did not change.)*

## 62. Proportional-only growth left medium files with too little room -- **resolved**

Found in `RUN-20260927-000019`, the third TS-106 run. Concern 61 had closed the
input side (complete writable files were supplied whole) and the output side
(per-path allowance derived from source size). But the allowance was
proportional-only:

```
allowance = max(MAX_EDIT_BYTES, int(source_bytes * EDIT_SIZE_HEADROOM))
```

For the 8110-byte `src/test/navigation-stack.test.ts`, this gave:

```
max(8000, int(8110 * 1.25)) = max(8000, 10137) = 10137 bytes
```

The model returned three replacements: 10593, 10454, and 10586 bytes. All three
were ~29-31% growth, not ~5% as initially characterized. All three exceeded the
10137-byte allowance and failed closed with `INVALID_MODEL_RESPONSE` under
Concern 61. No verification, review, candidate, or integration occurred.
Concern 61 therefore worked correctly -- the problem was that proportional-only
growth left medium files with too little room for legitimate additions.

*Why it matters:* a task that adds a few functions to a medium file is a normal
coding task, not a policy violation. The proportional-only allowance was
derived from the same headroom factor that bounds the input side, but that
factor describes *rewriting the same file with minor additions*, not *adding
significant new content*. A medium file that grows by 2500 bytes of legitimate
new code was refused, wasting three attempts.

*Resolved* in three parts.

**Part A: absolute growth allowance.** The per-path allowance now includes an
absolute growth term on top of the proportional headroom:

```
allowance = min(
    outer_ceiling,
    max(
        MAX_EDIT_BYTES,
        source_bytes + absolute_growth_allowance,
        int(source_bytes * EDIT_SIZE_HEADROOM)
    )
)
```

The default `absolute_growth_allowance` is 2500 bytes, configurable via
`CONTEXT_ABSOLUTE_GROWTH_ALLOWANCE_BYTES`. For the 8110-byte TS-106 source:

```
max(8000, 8110 + 2500, int(8110 * 1.25)) = max(8000, 10610, 10137) = 10610 bytes
```

This is enough room for the model's ~10500-byte replacements.

**Part B: upfront communication of effective limits.** Before the first coder
attempt, the coding instructions now include the effective per-path byte
allowance for each complete writable file:

```
- The following files have byte limits on their complete replacement contents:
  - `src/test/navigation-stack.test.ts`: 10610 bytes
  Your returned complete contents for each file must not exceed its limit.
```

The prompt makes clear that edits use complete replacement contents and that
the returned complete contents must remain within the stated byte limit. The
prompt uses the actual effective allowance calculated by the Orchestrator, not
a duplicate of the allowance arithmetic.

**Part C: configuration.** The absolute growth allowance is configurable via
`CONTEXT_ABSOLUTE_GROWTH_ALLOWANCE_BYTES` (default 2500), documented in
`.env.example`. No database migration is required.

**What is preserved.** Concern 61's fail-closed behavior is unchanged: if the
model nevertheless returns an edit above its effective allowance, the rejected
parse edit is represented structurally, the coding attempt fails closed with
`INVALID_MODEL_RESPONSE`, no partial candidate is applied, and path/actual-size/limit
feedback is provided. The `MAX_EDIT_BYTES` floor, `EDIT_SIZE_HEADROOM`
proportional protection, and `CONTEXT_MAX_FILE_BYTES` ultimate ceiling are all
preserved. Required-source completeness protections are unchanged. New files
and files without a trustworthy complete supplied source do NOT receive the
complete-source growth allowance merely because the path is writable.

**Attempt accounting.** No free retries or separate retry budget are introduced.
The existing attempt accounting is unchanged. Communicating the deterministic
constraint upfront is the preferred first solution; retry policy can be
revisited only if real runs continue wasting attempts despite being told the
limits.

**The tests are in** `tests/integration/test_concern62.py` (fifteen tests plus
discrimination checks): tiny file floor dominates; medium file absolute-growth
component can dominate; large file proportional component can dominate;
allowance is monotonic with source size; allowance never exceeds outer ceiling;
new file does not receive complete-source growth allowance; incomplete/untrusted
source does not receive complete-source growth allowance; exact TS-106 shape
(8110-byte source) receives at least 10610-byte allowance; reasonable TS-106-sized
replacement is accepted; output exceeding the new allowance still fails closed;
prompt contains the effective per-path byte allowance before the first model
call; prompt value exactly matches the enforcement value; changing the configured
absolute-growth value changes both enforcement and communicated allowance
consistently; `CONTEXT_MAX_FILE_BYTES` remains an effective outer ceiling;
existing Concern 55/56/58/61 regressions remain green. Discrimination checks
verify that removing the absolute-growth term causes the medium/TS-106
regression to fail, removing the outer ceiling causes an outer-bound regression
to fail, removing upfront prompt communication causes the prompt regression to
fail, and restoring Concern 61's silent-drop behavior causes the existing
Concern 61 discrimination test to fail.

**Campaign facts preserved.** `RUN-20260927-000019` remains historical and
unchanged. TS-106 remains HUMAN_REVIEW. Escalation
`deb00534-4521-416b-ad0f-75e6d1266e3c` remains OPEN. `agent/integration`
remains `a2e40f226146feb723658b5eae0c7dac3635cf7e`. TS-106 was not retried,
TS-107 was not run, no candidate was accepted, and no model or context
configuration was changed.

*(State as recorded at the time of this fix. Section 63 established that
`RUN-20260927-000020` -- the run that was meant to validate Concern 62 -- ran
against a stale image, and section 64 records the campaign state after that run.
TS-106 is now FAILED and escalation `deb00534` is RESOLVED; the run and the ref
above did not change.)*

## 63. Stale container image defeated Concern 62 validation -- **resolved**

Found after `RUN-20260927-000020`, the fourth TS-106 run. The investigation
established that the run executed against stale container image `168a74d323e2`,
built before Concern 62 commit `d50752d` by approximately 49 minutes. The host
source and the Concern 62 tests were correct. The deployed image contained
Concern 61-era code.

Both the missing prompt limit and the old 10137 enforcement had that one cause.
`RUN-20260927-000020` therefore was not a valid Concern 62 experiment, and the
second half of the problem is the reason it went unnoticed: the *code under
test* and *the code running* were different code, and nothing in the system said
so. A green host suite and an invalid experiment looked identical.

*Why it matters:* the same class of defect can silently invalidate any future
experiment, and the only detection mechanism was a person comparing image
digests. Worse, the naive fix -- asking the container for its own revision at
runtime -- reproduces the lie: the container has no `.git`, and a bind-mounted
host value would report the host's HEAD, which is not what is running either.

*Resolved* with a build-time identity the image carries, and a check that refuses
to let an unverified deployment be believed.

**Build-time metadata injection.** The Dockerfile accepts `SOURCE_REVISION`,
`SOURCE_DIRTY` and `BUILD_TIME` as build arguments and writes them to
`apps/orchestrator/_build_meta.py` during the image build. The values describe
the source the image was built from, baked in at build time, with no `.git` and
no runtime inspection involved. `SOURCE_DIRTY` is mapped onto three real Python
values -- `True`, `False`, `None` -- by shell `case` in the Dockerfile, so a
build that could not determine it writes `None` rather than a string that would
read as a boolean.

**Three states, kept distinct.** `apps/orchestrator/services/deployment.py`
derives one line of identity from those values, and it is deliberately not
two-valued:

| build | `source_state` | meaning |
| --- | --- | --- |
| `SOURCE_REVISION=<sha> SOURCE_DIRTY=false` | `clean@<sha>` | built from a known commit with no local changes |
| `SOURCE_REVISION=<sha> SOURCE_DIRTY=true` | `dirty@<sha>` | the SHA names a commit that is *not* what is in the image |
| defaults, or no SHA | `unknown/dev` | the build cannot be verified, and says so |

`dirty@<sha>` exists because collapsing it into `clean@<sha>` is exactly how a
modified artifact acquires a passing freshness check. The default is
`unknown/dev` rather than a placeholder SHA, because a placeholder would pass a
comparison and the comparison is the whole mechanism.

**Health endpoint exposure.** `/health` now returns `source_revision`,
`source_dirty`, `source_state` and `build_time` alongside the existing `status`,
`version` and `components`. The identity is read per report (through
`current_source()`), not captured at import, so a test can replace the values and
a production build is not affected by import order. The response shape is:

```json
{
  "status": "ok",
  "version": "0.1.0",
  "source_revision": "d50752dfc52d02aa2bcc29a25928fff1c1bcfaa8",
  "source_dirty": false,
  "source_state": "clean@d50752dfc52d02aa2bcc29a25928fff1c1bcfaa8",
  "build_time": "2026-09-27T21:24:36Z",
  "components": [...]
}
```

The new fields are additive: `status` still answers for the same three
components, so existing monitoring does not start failing.

**The check that goes red.** `assert_deployment_fresh()` raises
`StaleDeploymentError` -- it does not return a bool, because the interesting case
is the one a caller is tempted to log and move past -- and refuses four
situations, each naming the expected source, the actual source and the difference:
a revision that does not match, an image built from uncommitted changes, an image
that reports `unknown/dev`, and an expected value that is not a commit (so
"checked against nothing" cannot pass forever). A short SHA is not accepted as a
match: comparing prefixes would call a stale image fresh the first time two
commits shared seven characters. A dirty image can be accepted with an explicit
`allow_dirty=True`, because refusing everything is not verification, and a check
that cannot be overridden is a check people turn off.

**Reaching the running deployment.** `scripts/check_deployment_freshness.py`
asks a deployment what it is and compares that to the commit that was meant to
be running:

```bash
scripts/check_deployment_freshness.py --expected HEAD
SOURCE_REVISION=$(git rev-parse HEAD) SOURCE_DIRTY=false \
  BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ) docker compose build orchestrator
```

It exits 0 when the deployment is the intended source, 1 when it is not, and 2
when the check could not be performed. The third code exists because "I could
not find out" is not "the image is wrong", and reporting the second when the
truth is the first is how a real incident gets filed against the wrong thing. A
`/health` response missing the fields is read as unknown and therefore refused:
something that is not this orchestrator must not pass by omission. Git is used
on the host to resolve `HEAD`, and only there -- the machine deciding what
*should* be running is a different machine from the one that has to know what
*is* running.

**Regression coverage.** `tests/integration/test_concern63.py` (32 tests): the
developer build's defaults are the truth and the fallback imports; clean, dirty
and unknown stay three distinguishable states, including a `dirty=None` build and
an empty revision; the health report and the endpoint carry the whole identity;
the identity is read per report; the existing health contract is untouched; a
matching clean build passes; a stale image is refused *even though it reports
healthy*, which is the exact incident; the refusal message names both sides; a
dirty build is refused, and can be accepted deliberately; an unidentifiable
deployment is refused; an expected value that is not a commit is refused; a
short SHA is not a match; the script returns 0 / 1 / 2 as designed through a
real socket; a response without the fields cannot pass by omission; `HEAD`
resolves to a full commit; the script runs as a standalone process and exits
non-zero against a stale server; and the Dockerfile and compose plumbing pass all
three values through.

**Discrimination evidence.** Mutation: `assert_deployment_fresh` made to return
instead of raise, in `deployment.py` and not in the test. Result: **RED** --
`test_a_stale_image_is_refused_even_though_it_reports_healthy` and the rest of the
refusal tests fail. A check that cannot fail is a comment, so this is the test
that matters most in this concern, and it was run rather than assumed.

**Validation.** `tests/integration/test_concern63.py` 32 passed;
`tests/integration/test_concern64.py` 56 passed; full suite 1272 passed in the
default order and under two randomized orders; Phase M acceptance gate passed;
`ruff check .` clean.

**Campaign facts preserved.** `RUN-20260927-000020` remains historical and
invalid (it ran under the stale image). TS-106 remains FAILED. Escalation
`deb00534-4521-416b-ad0f-75e6d1266e3c` remains RESOLVED. `agent/integration`
remains `a2e40f226146feb723658b5eae0c7dac3635cf7e`. TS-106 was not retried,
TS-107 was not run, no candidate was accepted, and no model or context
configuration was changed. The Concern 62 policy implementation was not changed.

## 64. Supported operator abandonment of an active durable run -- **resolved**

Found in `RUN-20260927-000020`, the fourth TS-106 run. The run was still
RUNNING/recoverable but was known to be an invalid experiment because it began
under the stale supervisor image described in Concern 63, and the operator had
no supported way to stop it.

The domain model already had `RunStatus.ABANDONED` and
`TaskRunRepository.finish()` was already capable of terminalizing a run, but
abandonment was not exposed as a first-class operation. The only way to stop an
invalid run was manual database mutation, which bypasses the audit trail, breaks
recovery's invariants, and risks leaving the task state machine inconsistent.

*Why it matters:* a run that keeps going wastes resources and produces evidence
that looks real; a run deleted from the database loses the forensic record of
what happened. And the interesting part is not the endpoint -- it is that a
workflow already in flight, holding an open transaction across a model call, must
not be able to undo the operator's decision by finishing afterwards.

*Resolved* with an operator operation, and with fencing at every layer that could
otherwise have overruled it.

**Operator API.** `POST /runs/{run_id}/abandon` takes a required `reason` and an
optional `requested_by`, and is an operator action only -- it is not in the model
tool surface. A blank or whitespace-only reason is refused with 422 by a Pydantic
`field_validator` rather than being stored as an empty string and reported as
"the operator gave no reason", which is the failure this endpoint most invites.

**Terminal semantics.** The run becomes `RunStatus.ABANDONED` and is terminal: it
cannot be resumed, recovered or restarted. Recovery no longer has an ABANDONED
branch to classify -- an abandoned run is simply not an incomplete run, so it
cannot be picked up by `list_incomplete()`, `inspect_incomplete_runs()` or
recovery. The `RecoveryDisposition.ABANDONED` member that would have existed is
removed rather than left returning a disposition nothing can act on.

**Audit evidence.** One `RUN_ABANDONED` event per abandonment, carrying the
reason, the operator and the previous status. The log is append-only, and a
repeated request appends nothing.

**Task state semantics.** The owning task moves to `TaskStatus.FAILED` in the
same transaction as the run. This required widening the state machine: `PENDING ->
FAILED` and `READY -> FAILED` did not exist, and an abandoned run whose task was
never picked up could not otherwise be recorded. Every state except `COMPLETE` is
now failable, because the work is genuinely over in all of them and a task stuck
in `VERIFYING` with no run left to finish it is a lie about the repository's
state. From `FAILED` the state machine still permits an explicit `READY` for a
future retry. The task does not become COMPLETE, downstream tasks stay blocked,
and no replacement run starts by itself.

**Fencing, at each layer that could have overruled the operator.**

1. `TaskRunRepository.finish()` compare-and-swaps on the in-flight status, so a
   late `SUCCEEDED` (delivery) or `FAILED` (fix loop) or `RUNNING`
   (`_prepare_workspace`, which used `update_fields`) cannot overwrite
   `ABANDONED`. The guarded `update_fields` path matters as much as `finish`: an
   unguarded one resurrects a run with no exception and no event.
2. `TaskRunRepository.require_in_flight()` -- `SELECT ... FOR UPDATE` with the
   in-flight predicate, called by `durable_checkpoint()` before **every** turn
   and model-call commit. This is the guard the incident actually needed. A loop
   turn holds one transaction open across a model call, so the graph's own status
   read (in an earlier node) is stale by the time the turn commits, and the
   operator's transaction can commit in the middle of it. The lock makes the
   ordering explicit: whichever transaction gets the row lock first is the one
   whose decision stands, and the other is refused. A plain read would take no
   lock, so the operator could commit immediately afterwards and the check would
   be true and wrong.
3. `deliver_candidate()` re-checks the run immediately before the first
   irreversible step, the commit -- and **before** the task-status check, on
   purpose. An abandonment also moves the task to FAILED, so checking the task
   first reports "expected APPROVED, found FAILED": a description of the symptom
   that reads as though the caller were the problem. The run being ABANDONED is
   the cause.
4. `TaskRepository.transition()` is a compare-and-swap. The run's guard is only
   half the answer: a workflow also moves the task, from a copy that is exactly as
   stale as its copy of the run. Without it, an abandoned run's FAILED task is
   quietly rewritten to APPROVED by a session that read it earlier, and APPROVED
   is the state that makes a candidate deliverable. On a CAS miss the current
   status is re-read with a statement rather than through the identity map,
   because the identity map is the stale copy the guard is about and quoting it
   would produce a confident and false message.

**What the race actually looks like.** Not what it was first assumed to look
like. A turn holds the run-row lock through its model call, so an operator
request arriving mid-call *blocks*, and the ordering is: turn checkpoints and
commits; operator's `abandon` commits `ABANDONED` and moves the task to FAILED;
the workflow resumes, is refused by the barrier, and rolls back. The
`test_a_late_worker_cannot_resurrect_an_abandoned_run` test drives this against
real PostgreSQL with two sessions and asserts the operator request is still
blocked while the turn is in flight, so the test proves the ordering rather than
assuming it.

The in-flight coder call survives as a model-run record. The guard stops the
outcome from changing; it does not make the attempt not have happened, and a
table holding only the calls that mattered would not be an audit trail. What the
barrier rolls back is everything the turn claimed afterwards -- including its own
`CODING_COMPLETED` event, because the attempt was not finished.

**Idempotency and conflicts.** Repeating the request on an already-abandoned run
returns the existing state with no second event. A terminal run
(SUCCEEDED/FAILED) returns 409, and the repository's `abandon()` returns `None`
rather than raising, because "the run got there first" is a result the caller has
to report. An unknown run is a 404.

**Failure is atomic.** A task transition that cannot be made takes the run with
it: the whole abandonment is one savepoint, and the run is still RUNNING
afterwards. This is asserted, not assumed -- the failure is provoked by making
the task update raise, and the run's status is then read back.

**What is preserved.** The integration ref does not move. No candidate commit is
created or accepted. `candidate_commit` stays null. The task does not become
COMPLETE. Downstream tasks stay blocked. Artifacts, model-call history,
verification history and checkpoints remain inspectable; the workspace is
released by the project's existing terminal policy rather than by a new one.

**Regression coverage.** `tests/integration/test_concern64.py` (56 tests), in
seven groups: the service boundary (abandon a running run; status; task to FAILED;
candidate stays null; integration ref unmoved and at the expected SHA; downstream
blocked; no replacement run; idempotency; conflict on a terminal run; the event's
reason and operator; atomic failure); the state machine (every state except
COMPLETE fails, and the ones that should not, do not); durability across a commit
and a whole-process reconstruction; the persistence boundary (`finish` refused
for each terminal status a real workflow writes; `update_fields` refused; the
guard is one status wide, so every other transition still works; `abandon` loses
to a run that already finished; the turn barrier permits an in-flight run and
refuses an abandoned one; it distinguishes an abandoned run from one that
finished on its own; a stale session cannot overwrite a newer task status); the
HTTP boundary (the endpoint abandons a running run; 422 for a blank reason; the
persisted event carries the reason; idempotent over HTTP; 409 on a terminal run;
404 unknown; an abandoned run reads back as abandoned; the route is part of the
documented contract; a lost race is a 409); a real PostgreSQL race; and the
pollution guarantee.

**The pollution guarantee, and its own discrimination check.** The shared
`session` fixture commits nothing: a test's `session.commit()` releases a
savepoint inside an outer transaction that is rolled back at teardown, so nothing
a test writes is durable. The cheap form of the check reads every table through a
connection that had no part in the work and compares before and after. The form
that depends on nothing else in the repository runs this whole module in a
subprocess against a database file it shares with nobody, and counts the
survivors with a standard-library `sqlite3` connection -- no fixture teardown, no
test ordering, no other file. That is the assertion the old `session.commit()`
could not have survived, and the one "the full suite passes" could never have
supplied, because a leaked row in a single process is only visible to whichever
test happens to run next.

**Discrimination evidence.** Five mutations, each in the source and not in the
test, each restored afterwards, each observed to go red:

| mutation | result |
| --- | --- |
| A. `require_in_flight` no longer raises (the lock and the predicate stay) | RED -- `test_a_turn_may_only_be_made_durable_while_the_run_is_in_flight` |
| B. the delivery fence removed | RED -- `test_abandonment_before_delivery_prevents_the_candidate` |
| C. the abandon route not registered | RED -- `test_the_endpoint_abandons_a_running_run` |
| D. the fixture stops rolling back and commits at teardown | RED -- `test_projects_api.py::test_a_project_is_created_and_listed`, the original failure, with 47 projects listed instead of 1 |
| F. one `session.commit()` added inside the module | RED -- `test_this_module_leaves_no_rows_in_a_shared_database` |

Mutation F is the one that checks the checker. Mutation A is the shape of a guard
that survives review -- it still locks the row and still evaluates the
predicate -- and it is the guard the incident needed. A first attempt at the
mutation driver reported every mutation as RED without running anything, because
it invoked `python` on the test path instead of `python -m pytest`; the driver
now treats a pytest usage error as invalid rather than as a failure, since that
is the same "a check that cannot fail" mistake one level up.

**Validation.** `tests/integration/test_concern64.py` 56 passed, including three
consecutive runs of the race tests with no flakiness; Concern 63 32 passed; the
full suite 1272 passed in the default order and under two randomized orders;
Phase M acceptance gate passed; `ruff check .` clean.

**Campaign facts preserved.** `RUN-20260927-000020` remains historical and
invalid, and remains as it was found: `ABANDONED`, attempt 3, run 5,
`candidate_commit` null, no `requested_by`, reason `"x"`. TS-106 remains FAILED
and was not retried, TS-107 was not run, no candidate was accepted, and no model
or context configuration was changed. Escalation
`deb00534-4521-416b-ad0f-75e6d1266e3c` remains RESOLVED. `agent/integration`
remains `a2e40f226146feb723658b5eae0c7dac3635cf7e`. The new abandonment
capability was **not** used against `RUN-20260927-000020`; validating a
capability by exercising it on the campaign's historical run would have changed
the record it is supposed to explain.

## 65. A failed task had a decision behind it and no way to record it -- **resolved**

Concern 64 made it possible to stop an invalid run, and it left the owning task
in `FAILED` because the work was genuinely over. Then the next TS-106
experiment ran into what that state actually is: `create_run` accepts only
`READY` and `CHANGES_REQUESTED`; the scheduler does not manage `FAILED`; task
resume accepts only `PAUSED`; and escalation resolution needs an `OPEN`
escalation, which a consumed `RETRY_TASK` answer had already closed. A person
was holding a decision -- this attempt is invalid, run it again against the
current image -- with no supported way to record it.

The workarounds were both refused, correctly, and neither was an operation. A
direct database mutation bypasses the audit trail, the state machine and the
locks, and is the failure mode Concern 64 existed to remove. A manifest
re-import cannot move a task out of `FAILED` either, and would have refreshed
declarative fields of a task mid-campaign to get nowhere. The honest summary is
that the repository had a supported way to *stop* work and no supported way to
*start* it again.

*Why it matters:* `FAILED` is the state the orchestrator deliberately parks in
when it cannot finish. Treating that as terminal makes a failed campaign
unrecoverable without a DBA, and makes the one state a person most wants to
act on the one state they are least able to. The alternative is not "add a
flag"; it is a manual database write, which is exactly the kind of repair that
leaves a repository unable to explain itself later.

**Resolved** with an operator operation, and with the refusals that make it
trustworthy moved into the single statement that performs the write.

**Operator API.** `POST /tasks/{task_id}/retry` takes a required `reason` and an
optional `requested_by`, and is an operator action only -- it is not in the
model tool surface, and the orchestrator never retries its own failures from
here. A blank or whitespace-only reason is refused by the same Pydantic
validator Concern 64 introduced (`OperatorReason`), before a task is even read.
README section "Retrying a failed task" documents the call, the refusals and
what it does not do, and a test reads the README rather than trusting it.

**Authorization, not execution.** The operation moves the task `FAILED ->
READY` and appends one `TASK_RETRY_AUTHORIZED` event. The run is created by the
next project execution, by the same scheduler that creates every other run.
That was a decision with a rejected alternative rather than a default: having
this endpoint create the run itself would be more direct, and it would
duplicate the pause check, the project-runnable check, the one-task-at-a-time
check and the dependency check in a second place that does not own them, and it
would introduce a `PENDING` run whose task is still `READY` for the scheduler to
learn to respect. The authorization is the part a person decides; the run is
the part the orchestrator already knows how to create from the accepted
integration baseline.

**Two refusals the database has to make, in one statement.** The task must
still be `FAILED`, and no run of it may be in flight. Both are predicates on the
same guarded `UPDATE` in `TaskRepository.transition_guarded` -- `WHERE status =
expected AND NOT EXISTS (SELECT ... status IN ('PENDING','RUNNING'))` -- which
is evaluated while the row lock taken by `TaskRepository.lock()` is held. A
read-then-write would answer "there was no run in flight a moment ago" and be
wrong the moment after. The same statement works unchanged on SQLite and
PostgreSQL, so the guarantee is not a dialect's. When the guarded move matches
nothing, `_explain_refusal` re-reads both facts with statements rather than
through the identity map, because the identity map is the stale copy the guard
exists about, and reports which predicate lost: the task moved, or a run is
live.

**What is preserved, and it is the point.** The abandoned run is left exactly as
found: `ABANDONED`, terminal, unresumable, `candidate_commit` null, its event
log unchanged, and still refused by `require_in_flight()`. Nothing is resumed,
rewritten or re-pointed. Resolved escalations stay resolved and none is
reopened. The integration ref does not move. No candidate is created. The
downstream task stays blocked. The new run is a new run -- new durable and
external id, `run_number` incremented, new branch and worktree, and the
integration baseline **as it stands when the run is created**, not as it stood
when the retry was authorized; a dependency that regressed in between blocks
the run instead of being silently assumed. `HUMAN_REVIEW` is deliberately *not*
retryable: that state is a task waiting for an answer to an open escalation, and
answering it is `apply_escalation_answer`'s job. `COMPLETE` is terminal and has
no move out of it, so it cannot be made retryable.

**The dependency rule is one rule, not two.** `unsatisfied_dependencies()` is
shared with the scheduler and with `pauses.resume_task`, so a dependency that is
complete but unintegrated leaves a task un-runnable the same way in both paths.
Resume defers such a task to `PENDING`; a retry refuses it outright. What must
never happen is either path producing a `READY` task the scheduler would create
a run for. A dependency on a task that does not exist is refused rather than
treated as satisfied, which is the mistake a lenient "not in the unsatisfied
list" would make.

**Audit evidence.** One `TASK_RETRY_AUTHORIZED` event per authorization, with
`task_run_id = NULL`: this is a decision about a task made before any run
exists, and filing it against the abandoned run would put a new decision on the
record of an execution that had nothing to do with it. The payload carries the
external task id, reason, operator, the previous and authorized statuses, and
`historical_runs` -- a fact rather than a prediction, so a later reader can tell
a new run from a reused one without counting rows.

**Idempotency and conflicts.** Repeating the request finds a task that is no
longer `FAILED` and is refused with a truthful 409. There is no "already
retried, here is the old answer" branch, because the answer would be a claim
that the caller did not ask for and that the current state may not support.
An unknown task is a 404. `422` for a missing or blank reason. `409` for a
non-`FAILED` task, an unrunnable project, a pause in force, an unsatisfied
dependency, and a run already in flight -- each with a message that names the
rule it violated.

**Regression coverage.** `tests/integration/test_concern65.py` (54 tests), in
eight groups: the authorization itself (FAILED becomes READY; only FAILED is
retryable, parametrized over all seven other states plus a COMPLETE task that is
also not made retriable; an unknown task; a repeat request authorizing once; the
authorization creates no run of its own; the run event never reaches a run's
log; the route in both the OpenAPI schema and the README); the refusals (a
dependency not complete; a dependency that does not exist; complete but
unintegrated; a task under a task pause; a project pause; a released pause that
does not block; an unrunnable project; one rule shared with resume); what the
authorization must not disturb (the abandoned run's status, attempt, run number
and null candidate; its event log; `AbandonedRunError` from
`require_in_flight`; a resolved escalation staying resolved, none reopened); the
run that follows (new id, run number and external id; new branch and worktree;
attempt 1, review cycle 0, zero active runtime, and no inherited candidate,
failure reason, context hash or worker image; a new run executes and
completes through the real LangGraph graph with scripted models and a real
commit; the scheduler creates the run and the next pass continues that same
run; the baseline at the time it runs, not the time it was authorized; a
dependency that regressed afterwards blocks the run; a pause that arrives
between authorization and execution stops the run); the HTTP boundary (a failed
task is retried; 409 for a non-FAILED task, a pause and unmet dependencies; 404
unknown; 422 for a missing and a blank reason; a repeat conflicts and creates
nothing; the documented contract); durability across a real commit and a
rebuilt process read by a child interpreter with only the standard library; the
races against real PostgreSQL; and the pollution guarantee.

**The races, on a real database.** SQLite serializes writers, so "one
transaction reads while another writes" is not a thing it can express, and a
race test there would pass without testing anything. Three tests use a scratch
PostgreSQL database, two sessions and two threads: two operator requests
through the real HTTP handler cannot both authorize; a retry racing a run
creation cannot produce two runs; and a committed in-flight run closes the
door, which is a real scheduler state rather than a synthetic one. The HTTP
concurrency test goes through `get_db` and `session_scope` with a real
`DATABASE_URL` instead of a dependency override, because FastAPI runs a
handler on a worker thread and an override that creates a session per request
in the portal thread leaves sessions nobody closes.

**One bug this file found, in itself.** The first version of the race test
asserted through a session that its own `with` block had already closed, which
borrows a fresh connection from the pool and leaves that connection's
transaction open. Nothing failed: the test passed, and the scratch database's
`drop_all` then blocked for 30 seconds on the lock its own teardown could not
get, which surfaced as a `LockWaitTimeout` during teardown rather than as a
test failure -- 31 seconds for a test whose assertions take milliseconds, and a
`LockWaitTimeout` raised from `drop_all`, a line that is not part of the claim
under test at all. The assertion now happens inside the block and the module
runs in 4.9 seconds. A check that reports the wrong layer is still a wrong
check, and the way to find that out was to read which test the teardown error
was attached to rather than to look at the passing count.

**Discrimination evidence.** Four mutations, each in the source and not in the
test, each restored afterwards, each observed to go red:

| mutation | result |
| --- | --- |
| A. the FAILED-only eligibility rule removed | RED -- 13 tests, including every non-FAILED state, the repeat request, and the in-flight race |
| B. `require_no_in_flight_run=False` on the guarded transition | RED -- `test_a_task_with_a_run_in_flight_is_refused`, `test_a_task_with_a_run_in_flight_is_a_conflict_over_http` |
| C. the dependency rule removed | RED -- 5 tests, including the shared-rule comparison with resume |
| D. the authorization resurrecting the most recent historical run instead of leaving history alone | RED -- 13 failures and 9 errors, across identity, history, event-log and race assertions |

Mutation B is the shape of a guard that survives review -- the row is still
locked, the CAS on status is still there, and the only thing removed is the
predicate about runs -- and it is the predicate the incident needed. A and C
needed a second attempt: the driver's first edits did not match the source as
written, it raised, and the shell loop went on to run the **unmutated** module,
which reported 54 passed under a heading that said A. A mutation check that
measures nothing is the same "check that cannot fail" as Concern 64's driver
bug, one layer down. Both were re-applied and re-run, and the four rows above
are what those runs reported.

**Validation.** `tests/integration/test_concern65.py` 54 passed, three
consecutive runs under three different randomized orders (`pytest-randomly` is
installed and active) with no flakiness, including the three PostgreSQL races;
the full suite 1326 passed; `ruff check` clean on the changed files;
`ruff format --check` clean on the changed files (the repository-wide
`--check` still reports its pre-existing 135-file baseline, none of them
touched here). The Phase M acceptance gate is unaffected by this change and was
not re-run as a gate for it.

**Deployed, with its identity.** The image was rebuilt with `SOURCE_REVISION`
and `BUILD_TIME` set from the commit, `scripts/check_deployment_freshness.py
--expected HEAD` reports `fresh`, and `/health` answers
`clean@<commit>`. The first rebuild of the session did not set them, so
`/health` reported `unknown/dev` and the freshness check refused it --
`STALE DEPLOYMENT: the running deployment reports its source as unknown/dev` --
which is the check working, not a flake. The route answers on the live service
and `POST /tasks/{task_id}/retry` appears in the deployed OpenAPI schema. It was
not called: the live `run_events` table still holds zero
`TASK_RETRY_AUTHORIZED` rows, because the only place it would be used is the
task it was written for.

**Campaign facts preserved.** `RUN-20260927-000020` remains as it was found:
`ABANDONED`, attempt 3, run 5, `candidate_commit` null. TS-106 remains `FAILED`
and was **not** retried -- the new capability was deliberately not used on the
task it was written for, for the same reason Concern 64 did not abandon a run to
validate abandonment. TS-107 was not run. No candidate was accepted, no
manifest was re-imported, no model or context configuration was changed, and no
direct database mutation was performed. Escalation
`deb00534-4521-416b-ad0f-75e6d1266e3c` remains RESOLVED. `agent/integration`
remains `a2e40f226146feb723658b5eae0c7dac3635cf7e`.

## 66. A slow provider call outlived its database transaction -- **resolved**

**Observed evidence.** In project `d98cb1e7-75e4-401a-8b21-d0965b0b3115`,
TS-109 run `RUN-20260928-000005`
(`9760dfeb-3112-4b4f-b67f-5ae4daa7b2b1`) failed deterministic verification on
attempt 1 with `BUILD_FAILED` and correctly routed `SEND_TO_CODER`. Attempt 2
entered its model request at `2026-09-28T11:07:34.541897Z`. The provider's
600-second timeout exceeded PostgreSQL's 300-second
`idle_in_transaction_session_timeout`; PostgreSQL terminated the idle
transaction, the model later raised `ModelTimeout`, and rollback raised
`psycopg.errors.IdleInTransactionSessionTimeout`. The durable run remained
RUNNING, TS-109 remained VERIFYING, integration stayed at
`fc6abc579cee88f821c5f72f00162872b2dc8326`, and TS-110 never started.

**Root cause and invariant.** A fix-loop turn used one transaction across model
inference. The database was therefore both a lock holder and an idle connection
for an operation whose timeout was deliberately longer than the database's
safety timeout. Cleanup then reused that dead Session and allowed rollback to
replace the provider exception. The invariant is now structural: persist and
commit everything needed for the request; perform every coder/reviewer call
with no transaction open; begin a new transaction after it returns; lock and
revalidate that the run is still in flight; only then persist the result or
failure. Timeout alignment remains defense in depth, never the mechanism.

**Implementation.** Both recorded provider boundaries checkpoint before
awaiting the external call. The post-call transaction executes
`TaskRunRepository.require_in_flight`, whose `SELECT ... FOR UPDATE` predicate
refuses abandoned, superseded, or terminal runs before recording a response.
The same sequence applies to failure records, so `ModelTimeout` remains the
existing `MODEL_TIMEOUT -> RETRY` policy and is made durable without inventing
a new outcome. The graph releases active-runtime accounting through a new
Session after failure rather than relying on the Session that crossed the
external wait. `session_scope` uses SQLAlchemy invalidation when rollback says
the DBAPI connection is invalid; it preserves the original exception only for
that classified condition and still propagates unrelated rollback errors.

Concern 64's fencing is preserved but its concurrency is intentionally
improved: an operator abandonment no longer waits behind inference. It commits
while the provider is in flight, and the late answer loses at the post-call
locking fence. Durable attempt accounting and reconstruction still derive from
the pre-call prompt/checkpoint and the recorded failed call.

**Regression coverage.** `tests/integration/test_fix_loop_resume.py` now
asserts that neither coder nor reviewer sees an open transaction; simulates a
provider wait longer than the configured idle limit without a long sleep;
persists `ModelTimeout` with `MODEL_TIMEOUT -> RETRY`; invalidates the pre-call
Session and proves post-call persistence gets a valid checkout; preserves
attempt accounting and reconstruction; and reproduces `BUILD_FAILED ->
SEND_TO_CODER -> slow attempt 2 -> ModelTimeout` with a durable resumable
state. The real PostgreSQL race in `test_concern64.py` now requires operator
abandonment to commit while the provider is blocked and requires the late
answer to be fenced. `tests/unit/test_db_session_cleanup.py` proves an
invalidated rollback cannot mask the original model failure and that an
unrelated rollback error is not blindly suppressed. Existing restart/process
tests continue to prove reconstruction from committed rows and artifacts.

**Mutation evidence.** Three controlled source mutations were confirmed to
apply, run, fail for the intended reason, and were reversed before validation:

| mutation | result |
| --- | --- |
| remove the coder's pre-provider checkpoint, leaving its transaction open | RED -- `test_model_wait_has_no_open_database_transaction` observed `session.in_transaction() is True` inside the provider |
| remove the successful-call `require_in_flight` validation | RED -- `test_a_late_model_answer_is_refused_before_it_is_recorded` found a committed SUCCEEDED model row after abandonment |
| omit SQLAlchemy `Session.invalidate()` after an invalidated rollback | RED -- `test_invalidated_rollback_does_not_mask_the_model_failure` observed that the dead Session was not discarded |

The restored-source discrimination run passed all five directly affected
tests. No mutation result was counted from an edit that failed to apply.

**Validation and deployment.** Recorded after the complete acceptance run and
fresh deployment in this repair.
