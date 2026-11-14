#!/usr/bin/env python3
"""Reply to and resolve existing GitHub pull-request review threads only."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from typing import Any


DISPOSITION_PREFIXES = (
    "Fixed in ",
    "Valid but non-blocking:",
    "False positive:",
    "Not actionable for this PR:",
    "Not validated:",
)
FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


class ActionError(RuntimeError):
    pass


THREAD_QUERY = """
query($threadId: ID!) {
  node(id: $threadId) {
    __typename
    ... on PullRequestReviewThread {
      id
      isResolved
      isOutdated
      comments(first: 100) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          databaseId
          body
          viewerDidAuthor
          author { login }
          pullRequestReview { id databaseId state commit { oid } }
        }
      }
    }
  }
}
"""

THREAD_COMMENTS_QUERY = """
query($threadId: ID!, $cursor: String) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      comments(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          databaseId
          body
          viewerDidAuthor
          author { login }
          pullRequestReview { id databaseId state commit { oid } }
        }
      }
    }
  }
}
"""

REPLY_MUTATION = """
mutation($threadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(
    input: {pullRequestReviewThreadId: $threadId, body: $body}
  ) {
    comment {
      id
      databaseId
      body
      viewerDidAuthor
      author { login }
      pullRequestReview { id databaseId state commit { oid } }
    }
  }
}
"""

RESOLVE_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
"""


def gh(
    args: list[str], accepted: set[int] | tuple[int, ...] = (0,)
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=False
    )
    if result.returncode not in accepted:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ActionError(f"`gh {' '.join(args)}` failed: {detail}")
    return result


def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    result = subprocess.run(
        ["gh", "api", "graphql", "--input", "-"],
        input=json.dumps({"query": query, "variables": variables}),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ActionError(f"`gh api graphql` failed: {detail}")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as error:
        raise ActionError(f"Could not parse GitHub GraphQL JSON: {error}") from error
    if payload.get("errors"):
        raise ActionError(
            f"GitHub GraphQL returned errors: {json.dumps(payload['errors'])}"
        )
    return payload


def read_pr(repo: str, pr: str) -> dict[str, Any]:
    result = gh(
        [
            "pr",
            "view",
            pr,
            "--repo",
            repo,
            "--json",
            "number,state,isDraft,headRefOid",
        ]
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ActionError(f"Could not parse pull-request JSON: {error}") from error


def assert_expected_head(pr: dict[str, Any], expected_head: str) -> str:
    if not FULL_SHA.fullmatch(expected_head):
        raise ActionError("--expected-head must be the full 40-character commit SHA.")
    if pr.get("state") != "OPEN":
        raise ActionError("The pull request is not open; no thread mutation was made.")
    if pr.get("isDraft"):
        raise ActionError("The pull request is a draft; no thread mutation was made.")
    actual_head = str(pr.get("headRefOid") or "")
    if actual_head.lower() != expected_head.lower():
        raise ActionError(
            f"PR head changed from {expected_head} to {actual_head}; "
            "refetch before retrying."
        )
    return actual_head


def fetch_thread_comments(thread_id: str, connection: dict[str, Any]) -> list[dict[str, Any]]:
    comments = list(connection.get("nodes") or [])
    page_info = connection.get("pageInfo") or {}
    cursor = page_info.get("endCursor") if page_info.get("hasNextPage") else None
    while cursor:
        payload = graphql(THREAD_COMMENTS_QUERY, {"threadId": thread_id, "cursor": cursor})
        node = (payload.get("data") or {}).get("node") or {}
        connection = node.get("comments") or {}
        comments.extend(connection.get("nodes") or [])
        page_info = connection.get("pageInfo") or {}
        cursor = page_info.get("endCursor") if page_info.get("hasNextPage") else None
    return comments


def read_thread(thread_id: str) -> dict[str, Any]:
    if not thread_id.strip():
        raise ActionError("--thread-id is required.")
    payload = graphql(THREAD_QUERY, {"threadId": thread_id})
    node = (payload.get("data") or {}).get("node")
    if not node:
        raise ActionError(f"Review thread {thread_id} was not found.")
    if node.get("__typename") != "PullRequestReviewThread":
        raise ActionError(f"Node {thread_id} is not a pull-request review thread.")
    connection = node.get("comments") or {}
    return {
        **{key: value for key, value in node.items() if key != "comments"},
        "comments": fetch_thread_comments(thread_id, connection),
    }


def disposition_kind(body: Any) -> str | None:
    if not isinstance(body, str):
        return None
    normalized = body.lstrip()
    if normalized.startswith("Fixed in "):
        return "fixed"
    if normalized.startswith("Valid but non-blocking:"):
        return "valid_nonblocking"
    if normalized.startswith("False positive:"):
        return "false_positive"
    if normalized.startswith("Not actionable for this PR:"):
        return "not_actionable_scope_expansion"
    if normalized.startswith("Not validated:"):
        return "not_validated"
    return None


def viewer_dispositions(thread: dict[str, Any]) -> list[dict[str, Any]]:
    comments = thread.get("comments") or []
    return [
        {"comment": comment, "kind": kind}
        for comment in comments[1:]
        if comment.get("viewerDidAuthor")
        if (kind := disposition_kind(comment.get("body"))) is not None
    ]


def validate_body(body: str) -> str:
    if not body.strip():
        raise ActionError("A disposition reply body is required.")
    if disposition_kind(body) is None:
        allowed = ", ".join(repr(prefix) for prefix in DISPOSITION_PREFIXES)
        raise ActionError(f"Disposition reply must start with one of: {allowed}.")
    return body


def body_digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def base_receipt(
    repo: str, pr: str, expected_head: str, thread_id: str, thread: dict[str, Any]
) -> dict[str, Any]:
    return {
        "repo": repo,
        "pr": pr,
        "expected_head": expected_head,
        "thread_id": thread_id,
        "thread_resolved": bool(thread.get("isResolved")),
        "thread_outdated": bool(thread.get("isOutdated")),
    }


def reply_thread(
    repo: str, pr: str, expected_head: str, thread_id: str, body: str
) -> dict[str, Any]:
    body = validate_body(body)
    before_pr = read_pr(repo, pr)
    expected = assert_expected_head(before_pr, expected_head)
    before_thread = read_thread(thread_id)
    if not before_thread.get("comments"):
        raise ActionError("The review thread has no root comment.")

    existing = viewer_dispositions(before_thread)
    for disposition in existing:
        comment = disposition["comment"]
        if comment.get("body") == body:
            receipt = base_receipt(repo, pr, expected, thread_id, before_thread)
            receipt.update(
                {
                    "action": "already_present",
                    "verified": True,
                    "reply_comment_id": comment.get("id"),
                    "reply_comment_database_id": comment.get("databaseId"),
                    "body_sha256": body_digest(body),
                }
            )
            return receipt
    if existing:
        raise ActionError(
            "A different viewer disposition already exists on this thread; "
            "refusing to create a conflicting reply."
        )
    if before_thread.get("isResolved"):
        raise ActionError(
            "The thread is already resolved without a viewer disposition; "
            "refetch and reconcile it before replying."
        )

    payload = graphql(REPLY_MUTATION, {"threadId": thread_id, "body": body})
    mutation = (payload.get("data") or {}).get("addPullRequestReviewThreadReply") or {}
    comment = mutation.get("comment")
    if not comment or not comment.get("id"):
        raise ActionError(
            "GitHub returned no reply comment; do not retry without refetching the thread."
        )

    after_pr = read_pr(repo, pr)
    after_head = assert_expected_head(after_pr, expected)
    after_thread = read_thread(thread_id)
    matches = [
        candidate
        for candidate in viewer_dispositions(after_thread)
        if candidate["comment"].get("id") == comment.get("id")
        or candidate["comment"].get("body") == body
    ]
    if len(matches) != 1:
        raise ActionError(
            "Reply mutation completed but the reply was not uniquely visible in "
            "the target thread; do not retry before refetching."
        )

    receipt = base_receipt(repo, pr, after_head, thread_id, after_thread)
    review = comment.get("pullRequestReview") or {}
    receipt.update(
        {
            "action": "replied",
            "verified": True,
            "reply_comment_id": comment.get("id"),
            "reply_comment_database_id": comment.get("databaseId"),
            "review_id": review.get("id"),
            "review_database_id": review.get("databaseId"),
            "review_state": review.get("state"),
            "body_sha256": body_digest(body),
        }
    )
    return receipt


def resolve_thread(
    repo: str, pr: str, expected_head: str, thread_id: str
) -> dict[str, Any]:
    before_pr = read_pr(repo, pr)
    expected = assert_expected_head(before_pr, expected_head)
    before_thread = read_thread(thread_id)
    dispositions = viewer_dispositions(before_thread)
    if not dispositions:
        raise ActionError(
            "Cannot resolve a thread without a viewer disposition reply."
        )
    if len({disposition["kind"] for disposition in dispositions}) > 1:
        raise ActionError("Conflicting viewer dispositions prevent resolution.")
    if before_thread.get("isResolved"):
        receipt = base_receipt(repo, pr, expected, thread_id, before_thread)
        receipt.update({"action": "already_resolved", "verified": True})
        return receipt

    payload = graphql(RESOLVE_MUTATION, {"threadId": thread_id})
    mutation = (payload.get("data") or {}).get("resolveReviewThread") or {}
    returned_thread = mutation.get("thread") or {}
    if returned_thread.get("id") != thread_id:
        raise ActionError(
            "GitHub returned no matching resolved thread; refetch before retrying."
        )

    after_pr = read_pr(repo, pr)
    after_head = assert_expected_head(after_pr, expected)
    after_thread = read_thread(thread_id)
    if not after_thread.get("isResolved") or not viewer_dispositions(after_thread):
        raise ActionError(
            "Resolve mutation completed but the thread was not verified as complete; "
            "do not retry before refetching."
        )

    receipt = base_receipt(repo, pr, after_head, thread_id, after_thread)
    receipt.update({"action": "resolved", "verified": True})
    return receipt


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True, help="OWNER/REPO")
    parser.add_argument("--pr", required=True, help="Pull-request number or URL")
    parser.add_argument(
        "--expected-head", required=True, help="Full 40-character cycle head SHA"
    )
    parser.add_argument("--thread-id", required=True, help="PullRequestReviewThread node ID")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reply to or resolve an existing pull-request review thread."
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    reply_parser = subparsers.add_parser("reply", help="Reply to an existing thread")
    add_common_arguments(reply_parser)
    reply_parser.add_argument("--body", required=True, help="Disposition reply body")

    resolve_parser = subparsers.add_parser(
        "resolve", help="Resolve a thread that already has a disposition reply"
    )
    add_common_arguments(resolve_parser)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    gh(["auth", "status"])
    if args.action == "reply":
        receipt = reply_thread(
            args.repo, args.pr, args.expected_head, args.thread_id, args.body
        )
    else:
        receipt = resolve_thread(
            args.repo, args.pr, args.expected_head, args.thread_id
        )
    print(json.dumps(receipt, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except ActionError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
