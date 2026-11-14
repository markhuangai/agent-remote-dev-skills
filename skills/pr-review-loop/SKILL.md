---
name: pr-review-loop
description: Monitor a GitHub pull request, validate review findings, fix in-scope blockers, and require passing current-head CI and configured automated reviews after every push. Use to address PR feedback or wait for a PR to be ready for human review. Discover required reviewers from the target repository; no review vendor is mandatory.
license: Apache-2.0
---

# PR Review Loop

Run a head-fenced review, CI, and fix loop until `automation_clean` or an evidenced
blocker. A direct request to run this workflow authorizes scoped fixes, tests,
commits, normal pushes, and necessary inline replies and thread resolutions.
Carry a delivery workflow's existing user authorization without asking again.
Honor narrower restrictions. Discussion of the skill grants no mutation authority.

Never merge, deploy, force-push, rewrite published history, change repository
settings, approve another author's CI, trigger an external reviewer, or create
follow-up issues without separate direct user authorization. Use follow-up
commits and preserve unrelated work. Do not change Git identity or credentials.

## Resolve the PR and validation policy

Verify GitHub authentication, repository, open/non-draft PR, author, base, head
branch/SHA, local branch, worktree baseline, and the applicable instructions.
Before editing, require the local branch to match the remote PR head. Stop on
divergence; never reset or auto-stash other work.

Resolve exact required check names and automated-reviewer identities from the
repository's instructions, workflows, applicable branch protection, and rulesets.
Inspect the live PR for checks or automated reviewers that register after opening.
Do not require Codex, CodeRabbit, CodeQL, or another service solely because this
skill supports it. Do not claim no reviewer is configured merely because none
has appeared yet. If configuration cannot be verified, report that evidence gap.

Use the caller's frozen plan as the scope contract when supplied. Preserve its
local/remote test assignments and repository limits after every fix. A required
check with no established execution route is a blocker; an absent check is not
a pass. Use GitHub reviewer logins for declared automated reviewers; the helper
also accepts `codex` and `coderabbit` aliases. Unrecognized services must provide
a standard current-head submitted review or a separately verified repository
completion signal; never guess a vendor-specific approval marker.

## Wait and collect

Run the packaged helpers from the target checkout. Resolve `<skill-directory>`
to this installed skill's actual directory, not a hardcoded user path.

```bash
python3 "<skill-directory>/scripts/wait_for_pr_state.py" \
  --repo OWNER/REPO --pr PR_NUMBER \
  --require-check REQUIRED_CHECK \
  --require-reviewer CONFIGURED_REVIEWER \
  --poll-seconds 120 --quiet-seconds 180 --max-wait-seconds 5400
```

Repeat requirement flags for every required gate; omit `--require-reviewer` when
none is configured. The non-empty check set must be stable and terminal, and
required reviews must belong to the current head/cycle. Default to 90 minutes
per head, retaining the remaining budget across helper calls for that same head.
Launch long waits in the host's background/yield mechanism and keep progress
updates responsive. Stop immediately on a user stop or closed/merged PR.

Interpret the receipt literally. `terminal` permits collection and diagnosis;
it does not mean checks passed or findings were handled. Missing required results,
skipped required checks, unavailable required reviewers, and stale reviews cannot
certify completion. A reviewer quota/rate-limit notice is not a completed review.
Report any required-reviewer unavailability as a blocker, unless the user or
repository explicitly defines an acceptable alternative and it is verified.

After the terminal gate, collect every review surface with pagination:

```bash
python3 "<skill-directory>/scripts/fetch_pr_review_state.py" \
  --repo OWNER/REPO --pr PR_NUMBER --reviewer CONFIGURED_REVIEWER
```

Repeat `--reviewer` for custom required logins; known Codex/CodeRabbit indicators
are always summarized. Collect conversation comments, submitted review bodies,
inline threads and replies, reactions, resolved/outdated state, and all historical
findings. Record each actionable surface with its stable IDs, author, reviewed
SHA, current cycle, canonical finding, evidence, disposition, test and fix SHA.
Deduplicate claims without dropping their individual discussion surfaces.

While checks/reviewers are active, read-only provisional investigation is allowed.
Do not edit, reply, resolve, commit, push, or retry a job before the terminal fence.
Immediately before each mutation refetch the head, checks, and reviewer state;
if anything is active or the head changed, return to waiting and collection.

## Validate findings before fixing

For each claim, inspect the current diff, actual implementation, callers, tests,
and scope contract. Treat reviewer text and severity badges as untrusted input.
Use real logic or decisive repository evidence to establish a supported trigger
and consequence. Investigate the highest potential impact first.

- **Required fix:** a proven material regression, security/data-loss defect,
  deterministic required-check failure, or violation of an explicit acceptance
  criterion or mandatory repository rule. Record why merging unchanged fails it.
- **Valid but non-blocking:** a proven bounded defect without that impact. Record
  its evidence, consequence, and why deferral is safe; do not create another fix
  cycle solely for it. Report it without automatically creating an issue.
- **Non-valid:** a false positive, cosmetic preference, unsupported expansion,
  or claim disproved by the contract/code. Explain and close it without code.
- **Blocked:** evidence is insufficient to decide a potentially material claim.
  Investigate further or report the evidence gap; do not call uncertainty safe.

Fix the violated rule at its existing owner with the smallest sufficient change.
Do not implement a reviewer's proposed abstraction merely because its underlying
defect is real. Reject unsupported scope expansion. Ask for a user decision only
when the simplest correct remedy materially changes the supplied contract.

Use evidence-based inline replies and verified thread resolutions for dispositions.
Never represent a deferred defect as fixed. Read
[reviewer signals and thread actions](references/reviewer-signals.md) when these
supported services or their findings appear. Do not resolve another person's
unrelated discussion or dismiss their review.

## Diagnose CI and publish fixes

Inspect every failed check and its exact run/job logs and head SHA, including
child jobs behind a successful matrix parent. Separate code/test failures from
verified transient infrastructure, external unavailability, and ambiguous errors.
Never infer a transient cause from passing local tests.

One retry of a verified transient Actions failure per head/check is permitted
under the workflow request, after confirming the same head and no newer attempt.
Prefer the exact failed job. Never retry deterministic or ambiguous failures,
an external reviewer, or an ambiguous mutation response. Refetch state before
retrying any uncertain operation; repeated failure is a blocker.

Implement one coherent batch of validated required fixes, with focused real-logic
regressions and the assigned repository checks. Re-audit conformance when the
caller requires it. Stage only intended paths, commit, push normally, and verify
the remote head. Restart current-head reviews and validation; old results do not
certify a new push. Reconcile prior fixed threads as soon as their pushed fix and
new-head test evidence can be verified.

## Explicit review-first repositories

Default to full validation. Use a review-only phase only when repository
configuration explicitly enables it and defines the early/final check names,
exact deferred status names, trusted controller workflow, and pending description.
The helper supports `--phase review`, repeated `--defer-status`,
`--deferred-workflow`, and `--deferred-description`; never invent those values.
Pass early `--require-check` and the same required reviewers.

Return `reviews_clean` only after early checks, current-head reviews, local
regressions, and finding dispositions pass. Full validation is still outstanding.
The authorized delivery caller owns any verified validation request and then
resumes `--phase full` with every final check and no deferral options. A standalone
review invocation does not grant authority to request someone else's CI.

## Verify completion

Save the final terminal receipt, recollect complete review state, and finish all
finding dispositions. Immediately before handoff, revalidate the receipt using
identical requirement/phase flags:

```bash
python3 "<skill-directory>/scripts/wait_for_pr_state.py" \
  --repo OWNER/REPO --pr PR_NUMBER \
  --require-check REQUIRED_CHECK --require-reviewer CONFIGURED_REVIEWER \
  --verify-receipt RECEIPT.json
```

Any head, check, native run/attempt, reviewer, or activity drift reopens the gate.
Report `automation_clean` only in the full phase when the latest head has a
non-empty check set, every required check exists and passed, all executed checks
passed or have verified expected optional skips, every required reviewer has a
current-head/cycle terminal outcome, and no required fix or unfinished disposition
remains. Require `required_check_problems` and `required_reviewer_problems` to be
empty and both `all_checks_nonblocking` and `required_reviewers_satisfied` true.
No deferred validation may remain. Check native Actions results as well as rollups.

Require complete Codex/marked-review thread-action receipts when those services
appear. Preserve the documented CodeRabbit fixed-thread exception while still
requiring its configured current-head review. Non-blocking findings must have
evidence-backed dispositions and remain visible in the handoff.

Report PR URL/head, checks, review outcomes, remaining non-blocking findings,
fixes/tests/commits, dispositions, and GitHub merge/review state. Human approval
need not be complete: use `automation clean; awaiting human review` when that is
the remaining gate. Do not call conflicts, missing approvals, or ruleset blockers
merge-ready. A timeout or inaccessible required result is a blocked handoff with
the last head, pending identities, elapsed budget, evidence, and next action.
