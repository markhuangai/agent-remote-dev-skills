# Reviewer signals and thread actions

These adapters interpret observed services; they do not make those services
mandatory. Required identities come from the target repository's policy.

## Completion signals

Codex identities are `chatgpt-codex-connector[bot]` and its login without `[bot]`.
CodeRabbit identities are `coderabbitai[bot]` and its login without `[bot]`.

An `EYES` reaction newer than the same reviewer's terminal event means work is
in progress. Codex's PR-summary `THUMBS_UP` is a no-findings terminal signal;
CodeRabbit's `No actionable comments were generated in the recent review` is a
no-findings summary. A submitted review for the exact head also completes a
review. Timestamp-only signals must belong to the current head's check cycle;
old approvals and unchanged comment counts do not certify a new push.

CodeRabbit rate-limit notices (`Review limit reached`, `rate limited by
coderabbit.ai`, or `rate limit exceeded by coderabbit.ai`) are reported as deferred,
not as completed reviews. Other skipped/unavailable notices are unavailable.
Do not wait for a quota reset or trigger the reviewer. If it is required, report
the blocker; an alternative requires explicit repository or user policy.

For another configured GitHub login, require a submitted `APPROVED`, `COMMENTED`,
or `CHANGES_REQUESTED` review attached to the current head. A completed review
can contain blockers; findings still need validation and disposition. Do not
interpret arbitrary approval text or reactions from an unknown vendor.

## Guarded inline actions

Use existing thread IDs and the expected PR head:

```bash
python3 "<skill-directory>/scripts/review_thread_action.py" reply \
  --repo OWNER/REPO --pr PR_NUMBER --expected-head HEAD_SHA \
  --thread-id THREAD_NODE_ID --body "False positive: <repository evidence>"
python3 "<skill-directory>/scripts/review_thread_action.py" resolve \
  --repo OWNER/REPO --pr PR_NUMBER --expected-head HEAD_SHA \
  --thread-id THREAD_NODE_ID
```

The helper uses `addPullRequestReviewThreadReply`, refetches the same thread, and
guards against duplicate/conflicting dispositions. Do not use a top-level review
or conversation comment as an inline disposition. Refetch ambiguous responses
before retrying. GitHub may create an empty `COMMENTED` record for an inline
reply; thread membership, not that record alone, establishes what was posted.

Use `Fixed in <SHA>:`, `Valid but non-blocking:`, `False positive:`,
`Not actionable for this PR:`, or `Not validated:` with concrete evidence.

For Codex-rooted threads, use exactly one acting-account usefulness reaction:
👍 for valid/fixed claims and 👎 for disproved or unsupported claims. Correct only
the acting account's opposite reaction; never remove another person's reaction.
A reaction alone is incomplete. After a pushed/tested fix, reply with its SHA
and result, resolve, refetch, and require `codex_thread_action.action_state` to
be `complete`. Resolve non-valid and documented non-blocking claims with their
matching replies. No `codex_thread_actions.incomplete` items may remain.

For CodeRabbit required-fix threads, push the verified fix and leave the thread
open for incremental review; do not post a fixed reply or manually resolve it.
Its open state can be recorded as fixed after verification, while the required
review completion gate still applies. Reply to and resolve non-valid or deferred
non-blocking claims without usefulness reactions.

The collector recognizes reviews beginning with
`<!-- ai-pr-reviewer:v3:<40 lowercase hex SHA>:<64 lowercase hex digest> -->`
only when the marker SHA matches the review commit. Use that association,
not the author's login. Reply and resolve fixed threads only after the tested
fix is remotely reachable and its new-head gate passed. Require
`ai_pr_reviewer_thread_actions.incomplete` to be zero. No usefulness reaction is
required. For other reviewers, use ordinary evidence-backed thread replies and
resolutions subject to repository policy.
