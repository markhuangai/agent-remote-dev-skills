---
name: plan-audit
description: Independently compare repository implementation with an exact supplied plan and base commit, returning evidence-backed conformance findings without changing code or running tests. Use after implementation exists, directly or from a delivery skill. Requires an available independent auditor session, without a fixed model or vendor.
license: Apache-2.0
---

# Plan Audit

Compare the complete implementation with the supplied execution contract in an
independent, read-only session. A conformance audit does not redesign the plan
or perform a general style, architecture, or security review.

## Resolve the contract

Require the repository root, plan verbatim, exact locally resolvable base SHA,
branch and HEAD, complete committed/staged/unstaged/relevant-untracked state,
planned validation, and pre-existing baseline. Accept inline text, a readable
plan file, or the conversation plan explicitly identified by the caller. The
user's direct execution request can establish the plan; no planning-mode entry
or new approval cycle is required.

Inspect applicable instructions and use read-only Git operations to establish
the audit boundary. Return `blocked` if the plan, base, implementation boundary,
or authority to inspect is ambiguous. Ask only for an indispensable missing
contract input. Never fetch, edit, stage, commit, switch branches, run tests,
clean, or mutate external state.

## Use an independent auditor

Use the host's supported separate subagent or independent CLI session with an
available model. Record the actual session and model when exposed by the host;
do not invent an identity or require a particular provider, model, or reasoning
setting. If no independent session can be launched within existing permissions,
return `blocked`. A same-session self-review cannot satisfy this gate.

Give the auditor only the raw contract: execution root, exact base SHA, current
branch and HEAD, plan verbatim, validation assignments, baseline, and a request
to inspect every plan item against the complete current implementation. Require
read-only operation without tests. Do not supply an implementation summary,
suspected omission, intended answer, or suggested fix before its first verdict.

## Apply the conformance rubric

Permit only these categories:

- `missing_behavior`: a required plan item has no complete implementation path.
- `contradiction`: implementation conflicts with the supplied plan.
- `unapproved_scope`: material behavior exceeds the plan and its necessary support.
- `planned_test_gap`: required tests are missing or do not exercise the real criterion.

Use concrete repository evidence and exact plan criteria for each finding.
Illustrative snippets do not prohibit equivalent implementations. Exclude
pre-existing behavior and unrelated work. Inspect test logic without executing
it; test results are collected separately by the delivery workflow.

## Return a receipt

```text
verdict: conformant | nonconformant | blocked
auditor:
  session: <actual independent session identifier>
  model: <actual model, or not exposed by host>
base_sha: <exact SHA>
audited_state: <branch, HEAD, and full worktree summary>
findings:
  - plan_item: <exact requirement>
    category: missing_behavior | contradiction | unapproved_scope | planned_test_gap
    evidence: <file:line or other concrete evidence>
    gap: <observed mismatch>
    required_outcome: <minimum conformance result>
    guidance: <focused correction within scope>
summary: <evidence-backed verdict>
```

Return `conformant` only after checking every criterion with no unresolved gap.
Return `nonconformant` for evidenced, actionable gaps and `blocked` when the
contract, independent capability, or evidence is insufficient.

The caller owns fixes. Reuse the independent session for a whole-plan rescan
after changes when available. Record rounds, audited states, findings, and
dispositions. Permit one evidence-backed reconsideration of a disputed finding;
only the auditor can withdraw it. An unresolved dispute blocks delivery rather
than allowing the implementer to self-certify. A material contract change needs
the user's decision and a fresh independent audit of the resulting contract.
