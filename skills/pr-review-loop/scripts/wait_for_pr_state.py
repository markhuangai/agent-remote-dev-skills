#!/usr/bin/env python3
"""Wait for a fenced, non-empty, terminal PR check set.

The gate deliberately distinguishes terminal checks from successful checks. It
fetches review-activity metadata plus top-level bodies for bounded CodeRabbit
terminal markers, then emits one JSON receipt after the fingerprints remain
stable or current-cycle no-findings approvals complete the review fence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reviewer_state import canonical_reviewer, node_login, reviewers_in_progress, summarize_reviewers


TERMINAL_BUCKETS = {"pass", "fail", "skipping", "cancel"}
NONBLOCKING_BUCKETS = {"pass", "skipping"}
CHECK_EXIT_CODES = {0, 1, 8}
_deadline: float | None = None
_last_observation: dict[str, Any] = {}
STATUS_CONTEXT_CREATORS_QUERY = """
query($owner: String!, $repo: String!, $oid: GitObjectID!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    object(oid: $oid) {
      ... on Commit {
        statusCheckRollup {
          contexts(first: 100, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              __typename
              ... on StatusContext {
                context state description targetUrl createdAt
                creator { __typename login }
              }
            }
          }
        }
      }
    }
  }
}
"""


class GateError(RuntimeError):
    pass


class RetryableGateError(GateError):
    pass


class WaitDeadlineExceeded(GateError):
    pass


def run_gh(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    remaining = None if _deadline is None else _deadline - time.monotonic()
    if remaining is not None and remaining <= 0:
        raise WaitDeadlineExceeded("The observation deadline expired during GitHub collection.")
    try:
        return subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=False,
            timeout=60 if remaining is None else min(60, remaining), **kwargs,
        )
    except subprocess.TimeoutExpired as error:
        if _deadline is not None and time.monotonic() >= _deadline:
            raise WaitDeadlineExceeded("The observation deadline expired during GitHub collection.") from error
        raise RetryableGateError("GitHub collection exceeded its 60-second request bound.") from error


def gh(args: list[str], accepted: set[int] | tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
    result = run_gh(args)
    if result.returncode not in accepted:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RetryableGateError(f"`gh {' '.join(args)}` failed: {detail}")
    return result


def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    result = run_gh(
        ["api", "graphql", "--input", "-"],
        input=json.dumps({"query": query, "variables": variables}),
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RetryableGateError(f"`gh api graphql` failed: {detail}")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as error:
        raise RetryableGateError(f"Could not parse GitHub GraphQL JSON: {error}") from error
    if payload.get("errors"):
        raise RetryableGateError(
            f"GitHub GraphQL returned errors: {json.dumps(payload['errors'])}"
        )
    return payload


def resolve_repo(explicit: str | None) -> str:
    if explicit:
        return explicit
    result = gh(
        ["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]
    )
    if repo := result.stdout.strip():
        return repo
    raise GateError("Could not resolve the GitHub repository from this checkout.")


def split_repo(repo: str) -> tuple[str, str]:
    parts = repo.strip().split("/", 1)
    if len(parts) != 2 or not all(parts):
        raise GateError(f"Repository must be OWNER/REPO, got {repo!r}.")
    return parts[0], parts[1]


def read_pr(repo: str, selector: str | None = None) -> dict[str, Any]:
    command = ["pr", "view"]
    if selector:
        command.append(str(selector))
    command += [
        "--repo",
        repo,
        "--json",
        "number,url,headRefOid,headRefName,state,isDraft",
    ]
    return json.loads(gh(command).stdout)


def read_commit_statuses(repo: str, head: str) -> list[dict[str, Any]]:
    result = gh(
        [
            "api",
            "--paginate",
            "--slurp",
            f"repos/{repo}/commits/{head}/statuses?per_page=100",
        ]
    )
    try:
        pages = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as error:
        raise RetryableGateError(
            f"Could not parse GitHub commit-status JSON: {error}"
        ) from error
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise RetryableGateError("GitHub returned an unexpected commit-status shape.")
    return [status for page in pages for status in page if isinstance(status, dict)]


def commit_status_identity(status: dict[str, Any]) -> tuple[Any, ...] | None:
    created_at = parse_github_timestamp(status.get("created_at") or status.get("createdAt"))
    if created_at is None:
        return None
    return (
        str(status.get("context") or ""),
        str(status.get("state") or "").lower(),
        str(status.get("description") or ""),
        str(status.get("target_url") or status.get("targetUrl") or ""),
        created_at,
    )


def read_commit_status_context_creators(
    repo: str, head: str
) -> dict[tuple[Any, ...], tuple[str, str]]:
    owner, repository = split_repo(repo)
    cursor: str | None = None
    candidates: dict[tuple[Any, ...], set[tuple[str, str]]] = {}
    while True:
        payload = graphql(
            STATUS_CONTEXT_CREATORS_QUERY,
            {"owner": owner, "repo": repository, "oid": head, "cursor": cursor},
        )
        repository_data = (payload.get("data") or {}).get("repository") or {}
        commit = repository_data.get("object") or {}
        rollup = commit.get("statusCheckRollup") or {}
        connection = rollup.get("contexts")
        if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
            raise RetryableGateError("GitHub omitted commit status contexts from its GraphQL response.")
        for status in connection["nodes"]:
            if status.get("__typename") != "StatusContext":
                continue
            creator = status.get("creator") or {}
            login = str(creator.get("login") or "")
            actor_type = str(creator.get("__typename") or "")
            identity = commit_status_identity(status)
            if identity is not None and login and actor_type:
                candidates.setdefault(identity, set()).add((login, actor_type))

        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        next_cursor = page_info.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
            raise RetryableGateError("GitHub returned an invalid commit status context cursor.")
        cursor = next_cursor
    return {identity: next(iter(creators)) for identity, creators in candidates.items() if len(creators) == 1}


def hydrate_missing_commit_status_creators(
    repo: str, head: str, statuses: list[dict[str, Any]]
) -> None:
    missing = [
        status for status in statuses
        if not isinstance(status.get("creator"), dict) or not status["creator"].get("login")
    ]
    if not missing:
        return
    creators = read_commit_status_context_creators(repo, head)
    for status in missing:
        identity = commit_status_identity(status)
        creator = creators.get(identity) if identity is not None else None
        if creator is not None:
            login, actor_type = creator
            status["creator"] = {"login": login, "__typename": actor_type}


def status_id(status: dict[str, Any]) -> int:
    try:
        return int(status.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def enrich_status_check_timestamps(
    checks: list[dict[str, Any]], statuses: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_context: dict[str, list[dict[str, Any]]] = {}
    for status in statuses:
        context = str(status.get("context") or "")
        if context:
            by_context.setdefault(context, []).append(status)

    enriched: list[dict[str, Any]] = []
    for original in checks:
        check = dict(original)
        if parse_github_timestamp(check.get("startedAt")) is not None and (
            parse_github_timestamp(check.get("completedAt")) is not None
            or (check.get("bucket") or "").lower() not in TERMINAL_BUCKETS
        ):
            enriched.append(check)
            continue

        history = sorted(by_context.get(str(check.get("name") or ""), []), key=status_id)
        if not history:
            enriched.append(check)
            continue
        latest = history[-1]
        latest_state = str(latest.get("state") or "").lower()
        check_state = str(check.get("state") or "").lower()
        if check_state and latest_state != check_state:
            enriched.append(check)
            continue

        attempt_start = latest
        if latest_state != "pending":
            for prior in reversed(history[:-1]):
                prior_state = str(prior.get("state") or "").lower()
                if prior_state == "pending":
                    attempt_start = prior
                    break
                if prior_state in {"success", "failure", "error"}:
                    break

        if parse_github_timestamp(check.get("startedAt")) is None:
            started_at = attempt_start.get("created_at") or attempt_start.get("updated_at")
            if parse_github_timestamp(started_at) is not None:
                check["startedAt"] = started_at
        if (
            (check.get("bucket") or "").lower() in TERMINAL_BUCKETS
            and parse_github_timestamp(check.get("completedAt")) is None
        ):
            completed_at = latest.get("updated_at") or latest.get("created_at")
            if parse_github_timestamp(completed_at) is not None:
                check["completedAt"] = completed_at
        enriched.append(check)
    return enriched


def read_checks(repo: str, number: int, head: str) -> list[dict[str, Any]]:
    result = gh(
        [
            "pr",
            "checks",
            str(number),
            "--repo",
            repo,
            "--json",
            "name,state,bucket,link,startedAt,completedAt,workflow,event",
        ],
        CHECK_EXIT_CODES,
    )
    if output := result.stdout.strip():
        payload = json.loads(output)
        if isinstance(payload, list):
            needs_status_timestamps = any(
                parse_github_timestamp(check.get("startedAt")) is None
                or (
                    (check.get("bucket") or "").lower() in TERMINAL_BUCKETS
                    and parse_github_timestamp(check.get("completedAt")) is None
                )
                for check in payload
            )
            if needs_status_timestamps:
                return enrich_status_check_timestamps(
                    payload, read_commit_statuses(repo, head)
                )
            return payload
        raise RetryableGateError("GitHub returned an unexpected PR check shape.")
    if "no checks reported" in result.stderr.lower():
        return []
    raise RetryableGateError(
        f"Could not read PR checks: {result.stderr.strip() or result.returncode}"
    )


def is_deferred_status(
    pr: dict[str, Any], check: dict[str, Any], status: dict[str, Any],
    run: dict[str, Any], names: list[str], workflow: str, description: str,
) -> bool:
    repository_url = str(pr.get("url") or "").rsplit("/pull/", 1)[0]
    target = str(status.get("target_url") or "")
    match = re.fullmatch(re.escape(repository_url) + r"/actions/runs/([1-9][0-9]*)", target)
    return bool(
        match and check.get("name") in names
        and check.get("bucket") == "pending" and check.get("state") == "PENDING"
        and status.get("context") == check.get("name") and status.get("state") == "pending"
        and (
            (status.get("creator") or {}).get("login") == "github-actions[bot]"
            or (
                (status.get("creator") or {}).get("login") == "github-actions"
                and (status.get("creator") or {}).get("__typename") == "Bot"
            )
        )
        and status.get("description") == description and check.get("link") == target
        and run.get("id") == int(match.group(1))
        and run.get("event") == "pull_request_target"
        and run.get("head_sha") == pr.get("headRefOid")
        and run.get("path") == f".github/workflows/{workflow}"
        and run.get("status") == "completed" and run.get("conclusion") == "success"
    )


def phase_checks(
    repo: str, pr: dict[str, Any], checks: list[dict[str, Any]],
    phase: str, policy: dict[str, Any],
) -> list[dict[str, Any]]:
    if phase != "review":
        return checks
    names = policy.get("statuses") or []
    candidates = [check for check in checks if check.get("name") in names and check.get("bucket") == "pending"]
    if not candidates:
        return checks
    statuses = read_commit_statuses(repo, pr["headRefOid"])
    hydrate_missing_commit_status_creators(repo, pr["headRefOid"], statuses)
    latest: dict[str, dict[str, Any]] = {}
    for status in sorted(statuses, key=status_id):
        latest[str(status.get("context") or "")] = status
    runs: dict[int, dict[str, Any]] = {}
    result = []
    repository_url = str(pr["url"]).rsplit("/pull/", 1)[0]
    for original in checks:
        check = dict(original)
        status = latest.get(str(check.get("name") or ""), {})
        match = re.fullmatch(re.escape(repository_url) + r"/actions/runs/([1-9][0-9]*)", str(status.get("target_url") or ""))
        if check in candidates and match and status.get("description") == policy.get("description"):
            run_id = int(match.group(1))
            if run_id not in runs:
                runs[run_id] = json.loads(gh(["api", f"repos/{repo}/actions/runs/{run_id}"]).stdout)
            run = runs[run_id]
            if is_deferred_status(pr, check, status, run, names, policy["workflow"], policy["description"]):
                check.update(deferred=True, status_id=status_id(status), controller_attempt=run.get("run_attempt"))
        result.append(check)
    return result


def actions_json(path: str, collection: str | None = None) -> Any:
    args = ["api"]
    if collection:
        args += ["--paginate", "--slurp"]
    try:
        value = json.loads(gh([*args, path]).stdout)
    except json.JSONDecodeError as error:
        raise RetryableGateError("GitHub returned malformed native Actions JSON.") from error
    if collection:
        if not isinstance(value, list) or any(not isinstance(page, dict) or not isinstance(page.get(collection), list) for page in value):
            raise RetryableGateError("GitHub returned an incomplete native Actions collection.")
        items = [item for page in value for item in page[collection]]
        if any(not isinstance(item, dict) for item in items):
            raise RetryableGateError("GitHub returned a malformed native Actions collection member.")
        return items
    if not isinstance(value, dict):
        raise RetryableGateError("GitHub returned an unexpected native Actions object.")
    return value


def read_native_actions(repo: str, head: str, checks: list[dict[str, Any]]) -> dict[str, Any]:
    observation = _last_observation
    native_runs, blockers, failures = [], [], []
    observation.update(repo=repo, head_sha=head, checks=checks, native_actions=native_runs, blockers=blockers, native_collection_complete=False)
    linked = set()
    for check in checks:
        match = re.search(r"/" + re.escape(repo) + r"/actions/runs/([1-9][0-9]*)(?:/|$)", str(check.get("link") or ""))
        if match:
            linked.add(int(match.group(1)))
    observation["referenced_run_ids"] = sorted(linked)
    runs = actions_json(f"repos/{repo}/actions/runs?head_sha={head}&per_page=100", "workflow_runs")
    latest: dict[tuple[Any, Any], int] = {}
    for run in runs:
        if run.get("head_sha") != head:
            continue
        key = (run.get("workflow_id") or run.get("path"), run.get("event"))
        latest[key] = max(latest.get(key, 0), int(run["id"]))
        if run.get("status") != "completed":
            linked.add(int(run["id"]))
    linked.update(latest.values())

    def problem(code: str, run: dict[str, Any], job: dict[str, Any] | None = None, **details: Any) -> None:
        entry = {
            "code": code, "run_id": run.get("id"), "run_attempt": run.get("run_attempt"),
            "run_status": run.get("status"), "run_conclusion": run.get("conclusion"),
            "run_url": run.get("html_url"), **({"job_id": job.get("id"), "job_name": job.get("name"),
                "job_attempt": job.get("run_attempt"), "job_status": job.get("status"),
                "job_conclusion": job.get("conclusion"), "check_run_url": job.get("check_run_url"),
                "last_step_completed_at": max((step.get("completed_at") or "" for step in job.get("steps", [])), default="")} if job else {}),
            **details,
            "next_action": "Inspect this native run, attempt, job, and check; wait for GitHub or escalate the inconsistent state. Do not infer success or rerun active work.",
        }
        blockers.append(entry)
        if code not in {"native_run_pending", "native_job_pending"}:
            print(json.dumps({"diagnostic": "native_actions_blocked", "head_sha": head,
                "observed_at": datetime.now(timezone.utc).isoformat(), "blockers": [entry]}, sort_keys=True), file=sys.stderr, flush=True)

    for run_id in sorted(linked):
        run_snapshot = {"id": run_id, "jobs": [], "collection_complete": False}
        native_runs.append(run_snapshot)
        run = actions_json(f"repos/{repo}/actions/runs/{run_id}")
        run_snapshot.update({key: run.get(key) for key in ("id", "head_sha", "run_attempt", "status", "conclusion", "updated_at", "html_url")})
        attempt = run.get("run_attempt")
        if run.get("id") != run_id or run.get("head_sha") != head or not isinstance(attempt, int) or attempt < 1:
            problem("native_identity_mismatch", run)
            continue
        jobs = actions_json(f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100", "jobs")
        jobs.sort(key=lambda item: item.get("id", 0))
        snapshots = [{key: job.get(key) for key in ("id", "name", "run_attempt", "status", "conclusion", "started_at", "completed_at", "check_run_url")} for job in jobs]
        run_snapshot["jobs"] = snapshots
        attempt_jobs = actions_json(f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100", "jobs")
        if any(job.get("run_id") != run_id or job.get("run_attempt") != attempt or job.get("head_sha") != head for job in attempt_jobs):
            problem("native_identity_mismatch", run)
        current_ids = {job.get("id") for job in attempt_jobs}
        if not current_ids or not current_ids.issubset({job.get("id") for job in jobs}):
            problem("native_attempt_registration_pending", run)
        if run.get("status") != "completed":
            problem("native_run_pending", run)
        elif not run.get("conclusion"):
            problem("native_state_inconsistent", run)
        elif run["conclusion"] not in {"success", "skipped", "neutral"}:
            failures.append({"name": run.get("name") or f"Actions run {run_id}", "bucket": "fail", "state": run["conclusion"], "link": run.get("html_url"), "run_id": run_id, "run_attempt": attempt})
        names = set()
        for job, snapshot in zip(jobs, snapshots):
            job_attempt = job.get("run_attempt")
            check_match = re.search(r"/repos/" + re.escape(repo) + r"/check-runs/([1-9][0-9]*)$", str(job.get("check_run_url") or ""))
            if (job.get("run_id") != run_id or job.get("head_sha") != head or not isinstance(job_attempt, int)
                    or not 1 <= job_attempt <= attempt or not check_match or job.get("name") in names):
                problem("native_identity_mismatch", run, job)
                continue
            names.add(job.get("name"))
            check_id = int(check_match.group(1))
            snapshot["check_run_id"] = check_id
            check = actions_json(f"repos/{repo}/check-runs/{check_id}")
            snapshot.update(check_status=check.get("status"), check_conclusion=check.get("conclusion"), check_completed_at=check.get("completed_at"))
            if check.get("id") != check_id or check.get("head_sha") != head:
                problem("native_identity_mismatch", run, job, check_run_id=check_id)
            elif (check.get("status"), check.get("conclusion")) != (job.get("status"), job.get("conclusion")):
                problem("native_state_inconsistent", run, job, check_run_id=check_id, check_status=check.get("status"), check_conclusion=check.get("conclusion"))
            if job.get("status") != "completed" or not job.get("conclusion"):
                problem("native_state_inconsistent" if run.get("status") == "completed" else "native_job_pending", run, job, check_run_id=check_id)
            elif job_attempt < attempt and job["conclusion"] not in {"success", "skipped", "neutral"}:
                problem("native_attempt_registration_pending", run, job, check_run_id=check_id)
            elif job["conclusion"] not in {"success", "skipped", "neutral"}:
                failures.append({"name": job.get("name"), "bucket": "fail", "state": job["conclusion"], "link": job.get("html_url"), "job_id": job.get("id"), "run_id": run_id, "run_attempt": job_attempt})
        after = actions_json(f"repos/{repo}/actions/runs/{run_id}")
        if any(after.get(key) != run.get(key) for key in ("id", "head_sha", "run_attempt", "status", "conclusion", "updated_at")):
            problem("native_run_changed_during_collection", after)
        run_snapshot["collection_complete"] = True
    observation["native_collection_complete"] = True
    return {"runs": native_runs, "blockers": blockers, "failures": failures, "fingerprint": stable_hash(native_runs)}


def required_check_problems(checks: list[dict[str, Any]], required: list[str]) -> list[dict[str, Any]]:
    problems = []
    for name in required:
        matches = [check for check in checks if check.get("name") == name]
        if not matches:
            problems.append({"name": name, "reason": "missing", "bucket": "pending"})
        elif any(check.get("bucket") != "pass" for check in matches):
            problems.append({"name": name, "reason": "not_successful", "bucket": "fail"})
    return problems


def required_reviewer_problems(
    checks: list[dict[str, Any]],
    reviewers: dict[str, dict[str, Any]],
    required: list[str],
) -> list[dict[str, str]]:
    starts = [timestamp for check in checks
              if (timestamp := parse_github_timestamp(check.get("startedAt"))) is not None]
    problems = []
    for name in dict.fromkeys(canonical_reviewer(value) for value in required):
        state = reviewers.get(name) or {}
        if state.get("in_progress"):
            reason = "in_progress"
        elif state.get("state") in {"unavailable", "deferred"}:
            reason = "unavailable"
        elif (state.get("reviewed_current_head")
              and state.get("latest_review_state") in {"APPROVED", "COMMENTED", "CHANGES_REQUESTED"}):
            continue
        else:
            signal = {"codex": "thumbs_up", "coderabbit": "approval_comment"}.get(name)
            terminal = parse_github_timestamp(state.get("latest_terminal_at"))
            if (signal and state.get("state") == "approved"
                    and state.get("terminal_signal") == signal
                    and starts and terminal is not None and terminal >= min(starts)):
                continue
            reason = "missing_current_head_review"
        problems.append({"name": name, "reason": reason,
                         "bucket": "fail" if reason == "unavailable" else "pending"})
    return problems


COMMENTS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      comments(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { id body createdAt updatedAt author { login } }
      }
    }
  }
}
"""

REVIEWS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviews(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { id state submittedAt updatedAt commit { oid } author { login } }
      }
    }
  }
}
"""

THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          isOutdated
          comments(first: 100) {
            pageInfo { hasNextPage endCursor }
            nodes { id createdAt updatedAt author { login } }
          }
        }
      }
    }
  }
}
"""

THREAD_COMMENTS_QUERY = """
query($thread: ID!, $cursor: String) {
  node(id: $thread) {
    ... on PullRequestReviewThread {
      comments(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { id createdAt updatedAt author { login } }
      }
    }
  }
}
"""

REACTIONS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reactions(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { id content createdAt user { login } }
      }
    }
  }
}
"""


def author_login(node: dict[str, Any]) -> str:
    return node_login(node)


def record(source: str, node: dict[str, Any], timestamp: str, state: str = "") -> tuple[str, str, str, str, str]:
    return (
        source,
        str(node.get("id") or ""),
        timestamp or "",
        author_login(node),
        state,
    )


def fetch_connection(
    owner: str,
    repo_name: str,
    number: int,
    query: str,
    connection_name: str,
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        variables: dict[str, Any] = {
            "owner": owner,
            "repo": repo_name,
            "number": number,
            "cursor": cursor,
        }
        payload = graphql(query, variables)
        pr = payload["data"]["repository"]["pullRequest"]
        if pr is None:
            raise GateError(f"PR #{number} was not found in {owner}/{repo_name}.")
        connection = pr[connection_name]
        nodes.extend(connection.get("nodes") or [])
        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            return nodes
        cursor = page_info["endCursor"]


def fetch_remaining_thread_comments(thread: dict[str, Any]) -> list[dict[str, Any]]:
    connection = thread["comments"]
    comments = list(connection.get("nodes") or [])
    page_info = connection["pageInfo"]
    cursor = page_info["endCursor"] if page_info["hasNextPage"] else None
    while cursor:
        payload = graphql(
            THREAD_COMMENTS_QUERY,
            {"thread": thread["id"], "cursor": cursor},
        )
        connection = payload["data"]["node"]["comments"]
        comments.extend(connection.get("nodes") or [])
        page_info = connection["pageInfo"]
        cursor = page_info["endCursor"] if page_info["hasNextPage"] else None
    return comments


def read_activity_snapshot(
    repo: str, number: int, current_head: str, required_reviewers: list[str] | None = None
) -> tuple[
    tuple[tuple[str, str, str, str, str], ...],
    dict[str, dict[str, Any]],
]:
    owner, repo_name = split_repo(repo)
    records: list[tuple[str, str, str, str, str]] = []
    review_comments: list[dict[str, Any]] = []

    comments = fetch_connection(owner, repo_name, number, COMMENTS_QUERY, "comments")
    for node in comments:
        records.append(
            record("issue_comment", node, node.get("updatedAt") or node.get("createdAt") or "")
        )

    reviews = fetch_connection(owner, repo_name, number, REVIEWS_QUERY, "reviews")
    for node in reviews:
        records.append(
            record(
                "review",
                node,
                node.get("updatedAt") or node.get("submittedAt") or "",
                node.get("state") or "",
            )
        )

    threads = fetch_connection(owner, repo_name, number, THREADS_QUERY, "reviewThreads")
    for thread in threads:
        thread_state = json.dumps(
            {
                "is_outdated": bool(thread.get("isOutdated")),
                "is_resolved": bool(thread.get("isResolved")),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        records.append(("review_thread", str(thread.get("id") or ""), "", "", thread_state))
        for node in fetch_remaining_thread_comments(thread):
            review_comments.append(node)
            records.append(
                record(
                    "review_comment",
                    node,
                    node.get("updatedAt") or node.get("createdAt") or "",
                )
            )

    reactions = fetch_connection(owner, repo_name, number, REACTIONS_QUERY, "reactions")
    for node in reactions:
        records.append(
            record(
                "pr_reaction",
                node,
                node.get("createdAt") or "",
                str(node.get("content") or ""),
            )
        )

    reviewers = summarize_reviewers(
        reactions=reactions,
        reviews=reviews,
        comments=comments,
        review_comments=review_comments,
        current_head=current_head,
        required_reviewers=required_reviewers or [],
    )
    return tuple(sorted(records)), reviewers


def read_activity_watermark(
    repo: str, number: int
) -> tuple[tuple[str, str, str, str, str], ...]:
    watermark, _reviewers = read_activity_snapshot(repo, number, "")
    return watermark


def parse_github_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.year == 1:
        return None
    return parsed.astimezone(timezone.utc)


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def check_fingerprint(checks: list[dict[str, Any]]) -> tuple[tuple[str, ...], ...]:
    keys = (
        "name",
        "state",
        "bucket",
        "link",
        "startedAt",
        "completedAt",
        "workflow",
        "event",
        "deferred",
        "status_id",
        "controller_attempt",
    )
    return tuple(
        sorted(tuple(str(check.get(key) or "") for key in keys) for check in checks)
    )


def latest_activity_timestamp(
    watermark: tuple[tuple[str, str, str, str, str], ...]
) -> datetime | None:
    timestamps: list[datetime] = []
    for _source, _identifier, timestamp, _author, _state in watermark:
        parsed = parse_github_timestamp(timestamp)
        if parsed is None:
            return None
        timestamps.append(parsed)
    return max(timestamps) if timestamps else None


def historical_stable_seconds(
    checks: list[dict[str, Any]],
    watermark: tuple[tuple[str, str, str, str, str], ...],
    observed_at: datetime,
) -> float | None:
    if not checks or observed_at.tzinfo is None:
        return None
    completions: list[datetime] = []
    for check in checks:
        if (check.get("bucket") or "").lower() not in TERMINAL_BUCKETS:
            return None
        parsed = parse_github_timestamp(check.get("completedAt"))
        if parsed is None:
            return None
        completions.append(parsed)
    latest = max(completions)
    if watermark:
        timestamped_activity = tuple(
            entry
            for entry in watermark
            if not (entry[0] == "review_thread" and not entry[2])
        )
        if timestamped_activity:
            activity = latest_activity_timestamp(timestamped_activity)
            if activity is None:
                return None
            latest = max(latest, activity)
    quiet_for = (observed_at.astimezone(timezone.utc) - latest).total_seconds()
    return quiet_for if quiet_for >= 0 else None


def current_cycle_dual_approval(
    checks: list[dict[str, Any]],
    reviewers: dict[str, dict[str, Any]],
) -> bool:
    if not checks or any(
        (check.get("bucket") or "").lower() not in TERMINAL_BUCKETS
        for check in checks
    ):
        return False
    starts = [
        parsed
        for check in checks
        if (parsed := parse_github_timestamp(check.get("startedAt"))) is not None
    ]
    if not starts:
        return False
    cycle_start = min(starts)
    required = {
        "codex": "thumbs_up",
        "coderabbit": "approval_comment",
    }
    for reviewer, signal in required.items():
        state = reviewers.get(reviewer) or {}
        terminal_at = parse_github_timestamp(state.get("latest_terminal_at"))
        if (
            state.get("state") != "approved"
            or state.get("terminal_signal") != signal
            or terminal_at is None
            or terminal_at < cycle_start
        ):
            return False
    return True


def current_cycle_coderabbit_deferral(
    checks: list[dict[str, Any]],
    reviewers: dict[str, dict[str, Any]],
) -> bool:
    if not checks or any(
        (check.get("bucket") or "").lower() not in TERMINAL_BUCKETS
        for check in checks
    ):
        return False
    starts = [
        parsed
        for check in checks
        if (parsed := parse_github_timestamp(check.get("startedAt"))) is not None
    ]
    if not starts:
        return False
    state = reviewers.get("coderabbit") or {}
    terminal_at = parse_github_timestamp(state.get("latest_terminal_at"))
    return bool(
        state.get("state") == "deferred"
        and state.get("terminal_signal") == "deferred_comment"
        and terminal_at is not None
        and terminal_at >= min(starts)
    )


def observe_stable_period(
    checks: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    current_checks: tuple[tuple[str, ...], ...],
    current_watermark: tuple[tuple[str, str, str, str, str], ...],
    seen: bool,
    prior_checks: tuple[tuple[str, ...], ...],
    prior_watermark: tuple[tuple[str, str, str, str, str], ...],
    stable_since: float | None,
    observed_at: float,
    reviewer_pending: tuple[str, ...] | list[str] = (),
) -> tuple[
    bool,
    tuple[tuple[str, ...], ...],
    tuple[tuple[str, str, str, str, str], ...],
    float | None,
    float,
]:
    if not checks or pending or reviewer_pending:
        return False, (), (), None, 0
    if (
        not seen
        or current_checks != prior_checks
        or current_watermark != prior_watermark
        or stable_since is None
    ):
        return True, current_checks, current_watermark, observed_at, 0
    return True, prior_checks, prior_watermark, stable_since, observed_at - stable_since


def effective_stable_seconds(live: float, historical: float | None) -> tuple[float, str]:
    if historical is not None and historical > live:
        return historical, "github_timestamps"
    return live, "live_observation"


def watermark_summary(
    watermark: tuple[tuple[str, str, str, str, str], ...]
) -> dict[str, Any]:
    latest: dict[str, str] | None = None
    timestamped = [entry for entry in watermark if parse_github_timestamp(entry[2])]
    if timestamped:
        source, identifier, timestamp, author, _state = max(
            timestamped, key=lambda entry: parse_github_timestamp(entry[2]) or datetime.min.replace(tzinfo=timezone.utc)
        )
        latest = {
            "source": source,
            "id": identifier,
            "timestamp": timestamp,
            "author": author,
        }
    return {
        "count": len(watermark),
        "fingerprint": stable_hash(watermark),
        "latest": latest,
    }


def load_terminal_receipt(path: str) -> dict[str, Any]:
    try:
        receipt = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as error:
        raise GateError(f"Could not read terminal receipt {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise GateError(f"Could not parse terminal receipt {path}: {error}") from error
    if not isinstance(receipt, dict) or receipt.get("gate") not in {"terminal", "review_terminal"}:
        raise GateError("The verification receipt must contain a terminal gate.")
    activity = receipt.get("activity_watermark")
    if not isinstance(activity, dict):
        raise GateError("The verification receipt has no activity watermark.")
    for field in ("repo", "pr_number", "head_sha", "check_fingerprint"):
        if receipt.get(field) in (None, ""):
            raise GateError(f"The verification receipt has no {field}.")
    if not activity.get("fingerprint"):
        raise GateError("The verification receipt has no activity fingerprint.")
    return receipt


def next_sleep_seconds(
    poll_seconds: float,
    *,
    quiet_remaining: float | None = None,
    registration_remaining: float | None = None,
    timeout_remaining: float | None = None,
) -> float:
    candidates = [poll_seconds]
    for remaining in (quiet_remaining, registration_remaining):
        if remaining is not None and remaining > 0:
            candidates.append(remaining)
    if timeout_remaining is not None:
        candidates.append(max(timeout_remaining, 0))
    return min(candidates)


def wait_remaining(max_wait_seconds: float, started: float) -> float | None:
    if not max_wait_seconds:
        return None
    return max(max_wait_seconds - (time.monotonic() - started), 0)


def terminal_receipt(
    *,
    repo: str,
    pr: dict[str, Any],
    head: str,
    checks: list[dict[str, Any]],
    watermark: tuple[tuple[str, str, str, str, str], ...],
    reviewers: dict[str, dict[str, Any]],
    reviewer_pending: tuple[str, ...] | list[str],
    approval_fast_path: bool,
    coderabbit_deferred_current_cycle: bool,
    stable_seconds: float,
    stable_source: str,
    receipt_revalidated: bool = False,
    phase: str = "full",
    required_checks: list[str] | None = None,
    deferred_policy: dict[str, Any] | None = None,
    native: dict[str, Any] | None = None,
    required_reviewers: list[str] | None = None,
) -> dict[str, Any]:
    native = native or {"runs": [], "blockers": [], "failures": [], "fingerprint": stable_hash([])}
    evaluated = [check for check in checks if not check.get("deferred")]
    required_problems = required_check_problems(checks, required_checks or [])
    reviewer_problems = required_reviewer_problems(evaluated, reviewers, required_reviewers or [])
    blocking = [
        check
        for check in evaluated
        if (check.get("bucket") or "").lower() not in NONBLOCKING_BUCKETS
    ]
    blocking += [problem for problem in required_problems
                 if problem["name"] not in {check.get("name") for check in blocking}]
    blocking += native["failures"]
    skipped = [
        check
        for check in checks
        if (check.get("bucket") or "").lower() == "skipping"
    ]
    receipt = {
        "gate": "review_terminal" if phase == "review" else "terminal",
        "phase": phase,
        "required_checks": required_checks or [],
        "required_reviewers": required_reviewers or [],
        "required_reviewer_problems": reviewer_problems,
        "required_reviewers_satisfied": not reviewer_problems,
        "deferred_policy": deferred_policy or {},
        "deferred_checks": [check for check in checks if check.get("deferred")],
        "required_check_problems": required_problems,
        "phase_checks_nonblocking": bool(checks) and not blocking and not required_problems and not native["blockers"],
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "repo": repo,
        "pr_number": pr["number"],
        "pr_url": pr["url"],
        "head_branch": pr["headRefName"],
        "head_sha": head,
        "observed_check_count": len(checks),
        "all_checks_terminal": bool(checks) and all(check.get("bucket") in TERMINAL_BUCKETS for check in checks) and not native["blockers"],
        "all_checks_passed": bool(checks) and all(
            (check.get("bucket") or "").lower() == "pass" for check in checks
        ) and not native["blockers"] and not native["failures"],
        "all_checks_nonblocking": bool(checks) and all(check.get("bucket") in NONBLOCKING_BUCKETS for check in checks) and not required_problems and not native["blockers"] and not native["failures"],
        "blocking_checks": blocking,
        "skipped_checks": skipped,
        "check_fingerprint": stable_hash(check_fingerprint(checks)),
        "native_actions": native["runs"],
        "native_actions_fingerprint": native["fingerprint"],
        "activity_watermark": watermark_summary(watermark),
        "automated_reviewers": reviewers,
        "reviewers_in_progress": list(reviewer_pending),
        "reviewer_approval_fast_path": approval_fast_path,
        "coderabbit_deferred_current_cycle": coderabbit_deferred_current_cycle,
        "stable_seconds": stable_seconds,
        "stable_source": stable_source,
        "checks": checks,
    }
    if receipt_revalidated:
        receipt["receipt_revalidated"] = True
    return receipt


def verify_terminal_receipt(
    repo: str,
    pr: dict[str, Any],
    baseline: dict[str, Any],
    phase: str = "full",
    required_checks: list[str] | None = None,
    deferred_policy: dict[str, Any] | None = None,
    required_reviewers: list[str] | None = None,
) -> dict[str, Any] | None:
    if baseline.get("phase", "full") != phase:
        raise GateError("The receipt belongs to a different validation phase.")
    if baseline.get("required_checks", []) != (required_checks or []) or baseline.get("deferred_policy", {}) != (deferred_policy or {}):
        raise GateError("The validation policy changed after the receipt.")
    if baseline.get("required_reviewers", []) != (required_reviewers or []):
        raise GateError("The reviewer policy changed after the receipt.")
    if baseline["repo"] != repo or int(baseline["pr_number"]) != int(pr["number"]):
        raise GateError("The verification receipt belongs to a different pull request.")
    expected_head = str(baseline["head_sha"])
    before = read_pr(repo, str(pr["number"]))
    if stop_if_pr_inactive(repo, before):
        return None
    if before["headRefOid"] != expected_head:
        raise GateError(
            f"PR #{pr['number']} moved from receipt head {expected_head} "
            f"to {before['headRefOid']}."
        )
    checks = read_checks(repo, pr["number"], expected_head)
    checks = phase_checks(repo, before, checks, phase, deferred_policy or {})
    native = read_native_actions(repo, expected_head, checks)
    if native["blockers"]:
        raise GateError("Native Actions state became nonterminal or inconsistent during receipt verification.")
    if baseline.get("native_actions_fingerprint") != native["fingerprint"]:
        raise GateError("The native Actions fingerprint changed or is missing from the terminal receipt.")
    if not checks:
        raise GateError("The current head has no observed checks during receipt verification.")
    evaluated = [check for check in checks if not check.get("deferred")]
    pending = [
        check
        for check in evaluated
        if (check.get("bucket") or "").lower() not in TERMINAL_BUCKETS
    ]
    pending += [problem for problem in required_check_problems(checks, required_checks or []) if problem["reason"] == "missing"]
    if pending:
        names = ", ".join(check.get("name") or "unnamed" for check in pending[:3])
        raise GateError(f"Current-head checks became nonterminal: {names}.")
    watermark, reviewers = read_activity_snapshot(repo, pr["number"], expected_head, required_reviewers)
    reviewer_pending = reviewers_in_progress(reviewers)
    reviewer_pending += [problem["name"] for problem in required_reviewer_problems(
        evaluated, reviewers, required_reviewers or []) if problem["bucket"] == "pending"
        and problem["name"] not in reviewer_pending]
    if reviewer_pending:
        raise GateError(
            "Supported reviewers became active: " + ", ".join(reviewer_pending) + "."
        )
    after = read_pr(repo, str(pr["number"]))
    if stop_if_pr_inactive(repo, after):
        return None
    if after["headRefOid"] != expected_head:
        raise GateError(
            f"PR #{pr['number']} moved to {after['headRefOid']} during receipt verification."
        )

    current_check_fingerprint = stable_hash(check_fingerprint(checks))
    current_activity_fingerprint = watermark_summary(watermark)["fingerprint"]
    if current_check_fingerprint != baseline["check_fingerprint"]:
        raise GateError("The current-head check fingerprint changed after the terminal receipt.")
    if current_activity_fingerprint != baseline["activity_watermark"]["fingerprint"]:
        raise GateError("The pull-request activity fingerprint changed after the terminal receipt.")

    approval_fast_path = current_cycle_dual_approval(evaluated, reviewers)
    coderabbit_deferred_current_cycle = current_cycle_coderabbit_deferral(
        evaluated, reviewers
    )
    return terminal_receipt(
        repo=repo,
        pr=after,
        head=expected_head,
        checks=checks,
        watermark=watermark,
        reviewers=reviewers,
        reviewer_pending=reviewer_pending,
        approval_fast_path=approval_fast_path,
        coderabbit_deferred_current_cycle=coderabbit_deferred_current_cycle,
        stable_seconds=0,
        stable_source="receipt_revalidation",
        receipt_revalidated=True,
        phase=phase,
        required_checks=required_checks,
        deferred_policy=deferred_policy,
        native=native,
        required_reviewers=required_reviewers,
    )


def stop_if_pr_inactive(repo: str, pr: dict[str, Any]) -> bool:
    state = (pr.get("state") or "").upper()
    if state in {"MERGED", "CLOSED"}:
        print(
            json.dumps(
                {
                    "gate": "stopped",
                    "reason": f"pr_{state.lower()}_before_gate_completion",
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "repo": repo,
                    "pr_number": pr["number"],
                    "pr_url": pr["url"],
                    "pr_state": state,
                    "head_branch": pr["headRefName"],
                    "head_sha": pr["headRefOid"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return True
    if state != "OPEN":
        raise GateError(f"PR #{pr['number']} has unexpected state {state or 'unknown'}.")
    if pr.get("isDraft"):
        raise GateError(f"PR #{pr['number']} is a draft.")
    return False


def wait_status(
    checks: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    stable_for: float,
    stable_source: str,
    quiet_seconds: float,
    reviewer_pending: tuple[str, ...] | list[str] = (),
    approval_fast_path: bool = False,
) -> str:
    if not checks:
        return "checks=0; waiting for workflow registration"
    if pending:
        names = ", ".join(check.get("name") or "unnamed" for check in pending[:3])
        return f"checks={len(checks)} pending={len(pending)} ({names})"
    if reviewer_pending:
        return (
            f"checks={len(checks)} all_terminal reviewers_in_progress="
            f"{','.join(reviewer_pending)}"
        )
    blocking = [
        check
        for check in checks
        if (check.get("bucket") or "").lower() not in NONBLOCKING_BUCKETS
    ]
    remaining = max(quiet_seconds - stable_for, 0)
    approval = "reviewer_approval_fast_path=true " if approval_fast_path else ""
    return (
        f"checks={len(checks)} all_terminal blocking={len(blocking)} "
        f"{approval}"
        f"stable_for={stable_for:g}s stable_source={stable_source} "
        f"minimum_remaining={remaining:g}s"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Wait for a fenced, non-empty, terminal PR check set and PR "
            "review-activity metadata, including Codex and CodeRabbit "
            "terminal indicators."
        )
    )
    parser.add_argument("--repo", help="OWNER/REPO; defaults to this checkout.")
    parser.add_argument("--pr", help="PR number, URL, or branch.")
    parser.add_argument("--phase", choices=("review", "full"), default="full")
    parser.add_argument("--require-check", action="append", default=[], help="Exact check name that must exist and pass.")
    parser.add_argument("--require-reviewer", action="append", default=[], help="Required GitHub reviewer login, or codex/coderabbit alias.")
    parser.add_argument("--defer-status", action="append", default=[], help="Exact pending commit-status name allowed only before validation starts.")
    parser.add_argument("--deferred-workflow", help="Trusted pull_request_target controller filename.")
    parser.add_argument("--deferred-description", help="Exact pending description for work not yet requested.")
    parser.add_argument("--poll-seconds", type=float, default=120)
    parser.add_argument("--quiet-seconds", type=float, default=180)
    parser.add_argument("--registration-grace-seconds", type=float, default=60)
    parser.add_argument("--max-wait-seconds", type=float, default=5400)
    parser.add_argument(
        "--verify-receipt",
        metavar="PATH",
        help="Refetch once and require an unchanged prior terminal receipt.",
    )
    args = parser.parse_args()
    if any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]*(?:\[bot\])?", name) for name in args.require_reviewer):
        parser.error("--require-reviewer must be a GitHub login or supported alias")
    args.require_reviewer = list(dict.fromkeys(canonical_reviewer(name) for name in args.require_reviewer))
    if args.phase == "review" and not (args.defer_status and args.deferred_workflow and args.deferred_description):
        parser.error("review phase requires an explicit deferred status, workflow, and description policy")
    if args.phase == "full" and (args.defer_status or args.deferred_workflow or args.deferred_description):
        parser.error("full validation cannot defer checks")
    if set(args.require_check) & set(args.defer_status):
        parser.error("a required successful check cannot be deferred")
    if args.poll_seconds <= 0:
        parser.error("--poll-seconds must be greater than zero")
    if args.quiet_seconds < 0:
        parser.error("--quiet-seconds cannot be negative")
    if args.registration_grace_seconds < 0:
        parser.error("--registration-grace-seconds cannot be negative")
    if args.max_wait_seconds < 0:
        parser.error("--max-wait-seconds cannot be negative")
    return args


def wait_main() -> None:
    global _deadline, _last_observation
    args = parse_args()
    started = time.monotonic()
    _deadline = started + args.max_wait_seconds if args.max_wait_seconds else None
    _last_observation = {"repo": args.repo, "pr_number": args.pr, "phase": args.phase, "max_wait_seconds": args.max_wait_seconds}
    gh(["auth", "status"])
    repo = resolve_repo(args.repo)
    deferred_policy = ({"statuses": args.defer_status, "workflow": args.deferred_workflow,
                        "description": args.deferred_description} if args.phase == "review" else {})
    baseline = load_terminal_receipt(args.verify_receipt) if args.verify_receipt else None

    while True:
        try:
            pr = read_pr(repo, args.pr)
            if stop_if_pr_inactive(repo, pr):
                return
            break
        except RetryableGateError as error:
            if args.max_wait_seconds and time.monotonic() - started >= args.max_wait_seconds:
                raise WaitDeadlineExceeded(f"Timed out resolving {repo} PR {args.pr or ''}.") from error
            delay = next_sleep_seconds(
                args.poll_seconds,
                timeout_remaining=wait_remaining(args.max_wait_seconds, started),
            )
            print(f"[retry] {error}; retrying in {delay:g}s", file=sys.stderr, flush=True)
            time.sleep(delay)

    _last_observation.update(repo=repo, pr_number=pr["number"], pr_url=pr["url"], head_sha=pr["headRefOid"])
    if baseline is not None:
        receipt = verify_terminal_receipt(repo, pr, baseline, args.phase, args.require_check, deferred_policy, args.require_reviewer)
        if receipt is not None:
            print(json.dumps(receipt, indent=2, sort_keys=True))
        return

    head = pr["headRefOid"]
    head_since = started
    seen = False
    prior_checks: tuple[tuple[str, ...], ...] = ()
    prior_watermark: tuple[tuple[str, str, str, str, str], ...] = ()
    stable_since: float | None = None

    while True:
        now = time.monotonic()
        if args.max_wait_seconds and now - started >= args.max_wait_seconds:
            raise WaitDeadlineExceeded(
                f"Timed out waiting for checks or reviewer indicators on "
                f"{repo} PR #{pr['number']}."
            )
        try:
            before = read_pr(repo, str(pr["number"]))
            if stop_if_pr_inactive(repo, before):
                return
            _last_observation = {"repo": repo, "pr_number": pr["number"], "phase": args.phase,
                "head_sha": before["headRefOid"], "checked_at": datetime.now(timezone.utc).isoformat(),
                "max_wait_seconds": args.max_wait_seconds, "elapsed_seconds": time.monotonic() - started}
            checks = read_checks(repo, pr["number"], before["headRefOid"])
            checks = phase_checks(repo, before, checks, args.phase, deferred_policy)
            _last_observation["checks"] = checks
            native = read_native_actions(repo, before["headRefOid"], checks)
            evaluated = [check for check in checks if not check.get("deferred")]
            pending = [
                check
                for check in evaluated
                if (check.get("bucket") or "").lower() not in TERMINAL_BUCKETS
            ]
            pending += [problem for problem in required_check_problems(checks, args.require_check) if problem["reason"] == "missing"]
            pending += [{**problem, "name": problem.get("job_name") or f"Actions run {problem.get('run_id')}", "bucket": "pending"} for problem in native["blockers"]]
            if checks and not pending:
                current_watermark, current_reviewers = read_activity_snapshot(
                    repo, pr["number"], before["headRefOid"], args.require_reviewer
                )
            else:
                current_watermark = ()
                current_reviewers = summarize_reviewers(
                    reactions=(),
                    reviews=(),
                    comments=(),
                    review_comments=(),
                    current_head=before["headRefOid"],
                    required_reviewers=args.require_reviewer,
                )
            reviewer_pending = reviewers_in_progress(current_reviewers)
            reviewer_problems = required_reviewer_problems(evaluated, current_reviewers, args.require_reviewer)
            reviewer_pending += [problem["name"] for problem in reviewer_problems
                                 if problem["bucket"] == "pending" and problem["name"] not in reviewer_pending]
            approval_fast_path = current_cycle_dual_approval(
                evaluated, current_reviewers
            )
            coderabbit_deferred_current_cycle = current_cycle_coderabbit_deferral(
                evaluated, current_reviewers
            )
            after = read_pr(repo, str(pr["number"]))
            if stop_if_pr_inactive(repo, after):
                return
        except RetryableGateError as error:
            _last_observation.update(collection_error=str(error), blocker_code="native_evidence_unavailable")
            delay = next_sleep_seconds(
                args.poll_seconds,
                timeout_remaining=wait_remaining(args.max_wait_seconds, started),
            )
            print(f"[retry] {error}; retrying in {delay:g}s", file=sys.stderr, flush=True)
            time.sleep(delay)
            continue

        _last_observation.update(pending_checks=pending, reviewers_in_progress=reviewer_pending,
                                 required_reviewer_problems=reviewer_problems)

        if before["headRefOid"] != head:
            head = before["headRefOid"]
            started = now
            _deadline = started + args.max_wait_seconds if args.max_wait_seconds else None
            head_since = now
            seen, prior_checks, prior_watermark, stable_since = False, (), (), None
            print(f"[reset] head changed to {head[:12]}; restarting gate", file=sys.stderr, flush=True)

        if after["headRefOid"] != head:
            head = after["headRefOid"]
            started = time.monotonic()
            _deadline = started + args.max_wait_seconds if args.max_wait_seconds else None
            head_since = time.monotonic()
            seen, prior_checks, prior_watermark, stable_since = False, (), (), None
            print(f"[reset] head changed to {head[:12]} during check read", file=sys.stderr, flush=True)
            time.sleep(
                next_sleep_seconds(
                    args.poll_seconds,
                    timeout_remaining=wait_remaining(args.max_wait_seconds, started),
                )
            )
            continue

        current_checks = (*check_fingerprint(checks), ("native_actions", native["fingerprint"]))
        observed_at = time.monotonic()
        seen, prior_checks, prior_watermark, stable_since, live_stable_for = observe_stable_period(
            checks,
            pending,
            current_checks,
            current_watermark,
            seen,
            prior_checks,
            prior_watermark,
            stable_since,
            observed_at,
            reviewer_pending,
        )
        historical = (
            historical_stable_seconds(evaluated, current_watermark, datetime.now(timezone.utc))
            if checks and not pending and len(evaluated) == len(checks)
            else None
        )
        if historical is not None and native["runs"]:
            timestamps = [parse_github_timestamp(run.get("updated_at")) for run in native["runs"]]
            if any(timestamp is None for timestamp in timestamps):
                historical = None
            else:
                age = (datetime.now(timezone.utc) - max(timestamps)).total_seconds()
                historical = min(historical, age) if age >= 0 else None
        stable_for, stable_source = effective_stable_seconds(live_stable_for, historical)
        print(
            f"[wait] head={head[:12]} "
            f"{wait_status(checks, pending, stable_for, stable_source, args.quiet_seconds, reviewer_pending, approval_fast_path)}",
            file=sys.stderr,
            flush=True,
        )

        registration_ready = (
            time.monotonic() - head_since >= args.registration_grace_seconds
            or historical is not None
        )
        gate_open = (
            bool(checks)
            and not pending
            and not reviewer_pending
            and registration_ready
            and seen
            and stable_since is not None
            and (stable_for >= args.quiet_seconds or approval_fast_path)
        )
        if gate_open:
            receipt = terminal_receipt(
                repo=repo,
                pr=after,
                head=head,
                checks=checks,
                watermark=current_watermark,
                reviewers=current_reviewers,
                reviewer_pending=reviewer_pending,
                approval_fast_path=approval_fast_path,
                coderabbit_deferred_current_cycle=coderabbit_deferred_current_cycle,
                stable_seconds=stable_for,
                stable_source=stable_source,
                phase=args.phase,
                required_checks=args.require_check,
                deferred_policy=deferred_policy,
                native=native,
                required_reviewers=args.require_reviewer,
            )
            print(json.dumps(receipt, indent=2, sort_keys=True))
            return

        quiet_remaining = None
        if checks and not pending and not reviewer_pending and not approval_fast_path:
            quiet_remaining = max(args.quiet_seconds - stable_for, 0)
        registration_remaining = max(
            args.registration_grace_seconds - (time.monotonic() - head_since), 0
        )
        time.sleep(
            next_sleep_seconds(
                args.poll_seconds,
                quiet_remaining=quiet_remaining,
                registration_remaining=registration_remaining,
                timeout_remaining=wait_remaining(args.max_wait_seconds, started),
            )
        )


def main() -> None:
    global _deadline
    try:
        wait_main()
    except WaitDeadlineExceeded as error:
        print(json.dumps({**_last_observation, "gate": "blocked", "reason": "observation_deadline_exceeded",
            "blockers": [*_last_observation.get("blockers", []), {"code": "observation_deadline_exceeded", "message": str(error)}],
            "deadline_error": str(error), "emitted_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": max(0, _last_observation.get("max_wait_seconds", 0) + time.monotonic() - _deadline) if _deadline is not None else _last_observation.get("elapsed_seconds", 0),
            "next_action": "Inspect the recorded native identities and collection errors; obtain terminal GitHub results before resuming the review gate."}, indent=2, sort_keys=True))
        raise
    finally:
        _deadline = None


if __name__ == "__main__":
    try:
        main()
    except GateError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        raise SystemExit(130)
