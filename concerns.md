# Known issues and concerns

Written while building phases A–M. Each entry says what is wrong, why it
matters, and the smallest change that would fix it. Ordered by how likely it is
to bite during the first real project (build.md phase M); concerns 18–24 came
out of phase H, 25–33 out of phase I, 34–40 out of phase J, and 41–45 out of
phase L.

Resolved entries are kept in place and marked, rather than deleted or
renumbered: the reasoning is referenced from code comments and tests, and the
numbers are how they are referenced. **Resolved: 1–12, 16, 18–20, 22–29, 32–36,
38–39, 41, 43, 46.** **Partly resolved: 30, 45** -- each says which half.
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
