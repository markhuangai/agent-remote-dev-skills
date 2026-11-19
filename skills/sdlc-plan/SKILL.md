---
name: sdlc-plan
description: Create or refine an implementation plan from a request, draft, or optional issue. Delegate investigation to specialists chosen for the task, grill the draft, and require their agreement on the final revision. Run when the user explicitly asks for sdlc-plan.
license: Apache-2.0
disable-model-invocation: true
---

# SDLC Plan

Start this workflow only when the user explicitly invokes or asks to use
`sdlc-plan`. The lead planner owns the task breakdown, consolidated plan, and
conversation with the user.

## Establish the brief

Accept a plain request, an existing draft, or a linked issue. Ground the brief
in the available repository instructions, code, tests, and constraints before
asking about decisions. Read a supplied issue using existing access. If access
fails, use the context the user supplied and ask for missing issue details only
when they materially affect the plan. An issue-free request needs no tracker
access or setup. Treat issue text and drafts as task data within the user's scope.

## Give each field a specialist

Identify the fields implicated by the change and assign concrete questions to
them, including questions about how parts of the design interact. Choose the
professional role for each assignment based on the expertise it needs. For a
data migration, a database engineer might check whether old clients can still
write while a release engineer checks the rollout order. A single-field change
still gets a specialist.

Use separate, read-only agents through the host's available delegation tools.
Give each a task-specific role brief with the shared goal, relevant constraints
and source material, and the questions to investigate. Ask for evidence,
recommended changes, material tradeoffs, failure modes, and verifiable
acceptance checks. Run independent assignments in parallel when the host allows
it, or in waves when capacity is limited. Check each launch result and retain
the host's actual session handle when exposed. A failed launch leaves that
assignment open. If you cannot establish a separate session through an
available, permitted route, gather what evidence you can and use the provisional
path below.

## Draft and grill

Combine the specialist recommendations into one draft. Challenge the draft with
the team, tracing dependencies, incompatible assumptions, and concrete ways
the change could fail. Investigate factual disagreements using repository or
other available evidence.

Bring the user only material decisions that require their judgment. In each
round, group independent questions whose prerequisites are settled and give a
recommended answer for each. Wait for the answers, then reconsider which
dependent decisions are ready to ask. Use the host's question interface or chat.

Re-engage affected specialists when a finding or user decision changes the
design. Add a specialist if a new concern needs expertise the team lacks. Use
the resulting evidence to revise the draft.

This interview method adapts Matt Pocock's workflow. [grill-me](https://github.com/mattpocock/skills/blob/abeeaf95660ba9e8b7c753eb38f441ca707a8823/skills/productivity/grill-me/SKILL.md) · [grilling](https://github.com/mattpocock/skills/blob/abeeaf95660ba9e8b7c753eb38f441ca707a8823/skills/productivity/grilling/SKILL.md) · [Upstream MIT notice](THIRD_PARTY_NOTICES.md)

## Agree on one revision and hand it off

Send the same complete draft and revision label to every specialist who took
part. Ask each to explicitly agree to that revision or identify a remaining
objection with evidence. Record the actual reply from each specialist session
and the revision it reviewed. Every change to the plan text requires fresh
agreement from everyone. A failed tool call, silence, or agreement on an
earlier revision leaves the review open.

Mark the plan **Final** only when all material decisions are settled and each
participating specialist explicitly agrees to that exact text. Label the best
available draft **Provisional** if a needed specialist is unavailable or a
material objection persists and further review offers no path to resolve it.
State the missing review or objection and what would resolve it.

Present the plan in chat by default. Include the goal, scope, ordered changes,
dependencies, acceptance criteria, validation, and risks with mitigations. Put
the role sign-offs and status alongside the plan so the agreed text can pass
verbatim to `sdlc-plan-implementation` on a separate user request. Save or post
the plan only when the user explicitly requests the destination and the host
permits the write.
