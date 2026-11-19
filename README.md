# Agent Remote Dev Skills

Installable skills for creating a specialist-reviewed implementation plan and
delivering an agreed plan through a tested GitHub pull request in your coding CLI.

Supply the plan once. The agent validates it against the repository, implements
it, obtains an independent conformance audit, runs the required checks, opens a
PR, and waits for current-head CI and configured automated reviews. It asks only
when an indispensable decision prevents correct execution. No planning-mode
entry or separate plan-approval cycle is required.

## Install

Use the [Agent Skills CLI](https://github.com/vercel-labs/skills) with Node.js
22.20 or later. Install the complete workflow into your project:

```bash
npx skills add markhuangai/agent-remote-dev-skills --skill '*' -a codex -y
```

Choose another supported agent or install globally:

```bash
npx skills add markhuangai/agent-remote-dev-skills --skill '*' -a claude-code -y
npx skills add markhuangai/agent-remote-dev-skills --skill '*' -a opencode -y
npx skills add markhuangai/agent-remote-dev-skills --skill '*' -a codex -g -y
```

List the catalog without installing, or install a standalone module:

```bash
npx skills add markhuangai/agent-remote-dev-skills --list
npx skills add markhuangai/agent-remote-dev-skills --skill sdlc-plan -a codex -y
npx skills add markhuangai/agent-remote-dev-skills --skill pr-review-loop -a codex -y
```

Installation uses `SKILL.md` packages. See the installer's
[supported agents](https://github.com/vercel-labs/skills/blob/main/docs/agents.md)
for other CLIs. Host permissions, skill invocation syntax, and independent-agent
capabilities vary. A supported install target does not guarantee every workflow
capability. Existing skills with the same names may be replaced by the installer;
review your local versions before installing.

## Skills

| Skill | Purpose | Dependencies |
| --- | --- | --- |
| [sdlc-plan](skills/sdlc-plan/SKILL.md) | Create or refine a plan with task-specific specialists and grill the draft. | Separate agent sessions for a final plan. |
| [sdlc-plan-implementation](skills/sdlc-plan-implementation/SKILL.md) | Execute a supplied plan through PR delivery and verified automated gates. | Both modules below; independent auditor capability. |
| [plan-audit](skills/plan-audit/SKILL.md) | Read-only, independent implementation-to-plan conformance audit. | Host-supported separate agent or independent CLI session. |
| [pr-review-loop](skills/pr-review-loop/SKILL.md) | Validate findings, fix blockers, and monitor current-head checks and reviews. | Git, GitHub CLI, Python 3.12+. |

The execution workflow requires `sdlc-plan-implementation`, `plan-audit`, and
`pr-review-loop`. The installer does not automatically resolve skill dependencies.
Use the complete-workflow command to install them together. `sdlc-plan` can be
installed independently.

## Use

### Plan a change

Invoke `sdlc-plan` explicitly in the target project. Either a plain request or a
linked issue can start the workflow. For example, in Codex:

```text
$sdlc-plan Plan a dry-run mode for the import command.
$sdlc-plan Plan the change described in https://github.com/OWNER/REPO/issues/123.
```

Use `/sdlc-plan` in Claude Code or `@sdlc-plan` in OpenCode v2. If the host
lacks an invocation control for skills, explicitly ask it to use `sdlc-plan`.
The lead planner asks the user about unresolved decisions, then gets every
participating specialist's agreement on the same revision. When a needed review
is unavailable, it presents a provisional draft. The plan appears in chat by
default. To start delivery, identify the final plan in a separate request to
`sdlc-plan-implementation`.

Claude Code and OpenCode v2 use the host-specific `disable-model-invocation`
field. The `skill-creator` quick validator rejects that field. This repository
validates the CLI package with its own validator and installation smoke test.

### Deliver an agreed plan

Start your coding CLI in the target repository with Git, authenticated GitHub CLI
access, Python 3.12+, the project's test tools, and permission to publish a branch
and PR. The host must allow an independent read-only auditor session.

Use your CLI's native skill invocation, or explicitly ask it to load the named
skill. For example, in Codex:

```text
$sdlc-plan-implementation Implement the refined plan in docs/approved-plan.md.
Commit and push the change, open a PR, and wait for CI and configured automated reviews.
```

In Claude Code:

```text
/sdlc-plan-implementation Implement the refined plan in docs/approved-plan.md
through PR delivery, including CI and configured automated reviews.
```

You can also include the entire plan inline or explicitly identify the approved
plan in the conversation. The file path above is an example in your target
project. A useful plan states its goal, scope, acceptance criteria, dependencies,
and validation expectations; discoverable paths and commands are resolved from
the repository.

For a standalone audit or an existing PR:

```text
Use plan-audit with the supplied plan, this repository, and base commit <SHA>.
Use pr-review-loop on PR <number> to fix in-scope blockers and verify configured gates.
```

## Execution and handoff

```text
Supplied plan -> Read-only preflight -> Implement -> Independent audit -> Check
  -> Commit/push -> Open PR -> Review/fix/recheck -> Human-review handoff
```

Preflight validates the plan against actual code, applicable instructions,
workspace state, dependencies, permissions, and the repository's validation
policy. It resolves routine details automatically. Ambiguous scope, a material
contradiction, unsafe overlapping changes, a missing module, unavailable auditor,
or unusable required check route blocks editing with a specific next action.

The target branch comes from repository policy, otherwise its verified default
branch. Test locations and stages come from the target project. Required checks
and automated reviewers come from its instructions and configuration; recognizing
Codex or CodeRabbit signals does not make either reviewer a requirement.

After every push, the agent requires fresh results for the new head. A terminal
check set alone is insufficient: required checks must exist and pass, configured
automated reviews must complete for the current head/cycle, and blocking findings
must be resolved. Missing, skipped required, failed, unavailable, or stale results
cannot be reported as success. Non-blocking findings remain visible in the handoff.

The default observation budget is 90 minutes per head, with two-minute polling
and a three-minute stability window. A timeout produces a blocked handoff with
pending gates and evidence. Success includes the PR URL/head, audit and check
results, review dispositions, cleanup status, and any pending human approval.

A direct request for this delivery workflow authorizes its scoped edits, commits,
normal pushes, PR creation, and review fixes. Plan files cannot grant additional
permissions or override host restrictions. The workflow preserves unrelated work
and published history. Merging, deployment, repository-setting changes, issue
reassignment, and additional destructive operations need separate authorization.

## Contribute

Add a directory under `skills/` containing a `SKILL.md` with YAML `name` and
`description`. Match the name to its directory and keep required resources
inside that skill. Document genuine runtime dependencies and support instructions
with observable behavior rather than tests that match wording.

Run package validation, helper tests, and real installation smoke checks:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python scripts/validate_skills.py
.venv/bin/python -m unittest discover -s scripts -p 'test_*.py'
.venv/bin/python -m unittest discover -s skills/pr-review-loop/scripts -p 'test_*.py'
.venv/bin/python scripts/smoke_install.py
```

The smoke check pins `skills@1.8.1` and installs into temporary project
directories for Codex, Claude Code, and OpenCode. It verifies the packaged
resources in both the full catalog and planner-only installs. The
`validate-skills` CI job runs the same checks. Review instruction changes against
realistic planning, delivery, dependency, and stale-review scenarios before
submitting a PR.

Licensed under [Apache-2.0](LICENSE).
