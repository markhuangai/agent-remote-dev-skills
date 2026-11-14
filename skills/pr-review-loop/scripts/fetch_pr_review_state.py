#!/usr/bin/env python3
"""Fetch complete pull-request conversation, review, and thread state."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any

from reviewer_state import summarize_reviewers


CODEX_LOGINS = {"chatgpt-codex-connector", "chatgpt-codex-connector[bot]"}
AI_PR_REVIEW_MARKER = re.compile(
    r"^<!-- ai-pr-reviewer:v3:(?P<head>[0-9a-f]{40}):[0-9a-f]{64} -->"
)


class FetchError(RuntimeError):
    pass


def gh(args: list[str], accepted: set[int] | tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=False
    )
    if result.returncode not in accepted:
        detail = result.stderr.strip() or result.stdout.strip()
        raise FetchError(f"`gh {' '.join(args)}` failed: {detail}")
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
        raise FetchError(f"`gh api graphql` failed: {detail}")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as error:
        raise FetchError(f"Could not parse GitHub GraphQL JSON: {error}") from error
    if payload.get("errors"):
        raise FetchError(f"GitHub GraphQL returned errors: {json.dumps(payload['errors'])}")
    return payload


def resolve_repo(explicit: str | None) -> str:
    if explicit:
        return explicit
    result = gh(
        ["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]
    )
    if repo := result.stdout.strip():
        return repo
    raise FetchError("Could not resolve the GitHub repository from this checkout.")


def split_repo(repo: str) -> tuple[str, str]:
    parts = repo.strip().split("/", 1)
    if len(parts) != 2 or not all(parts):
        raise FetchError(f"Repository must be OWNER/REPO, got {repo!r}.")
    return parts[0], parts[1]


def read_pr(repo: str, selector: str | None) -> dict[str, Any]:
    command = ["pr", "view"]
    if selector:
        command.append(str(selector))
    command += [
        "--repo",
        repo,
        "--json",
        "number,url,title,state,isDraft,author,headRefOid,headRefName,baseRefName",
    ]
    return json.loads(gh(command).stdout)


COMMENTS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      comments(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id databaseId url body createdAt updatedAt
          author { login }
        }
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
        nodes {
          id databaseId url body state commit { oid } submittedAt updatedAt
          author { login }
        }
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
          id isResolved isOutdated path line diffSide startLine startDiffSide
          originalLine originalStartLine resolvedBy { login }
          comments(first: 100) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id databaseId url body createdAt updatedAt
              author { login }
              viewerDidAuthor
              pullRequestReview { id databaseId state commit { oid } }
              reactionGroups { content viewerHasReacted users { totalCount } }
            }
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
        nodes {
          id databaseId url body createdAt updatedAt
          author { login }
          viewerDidAuthor
          pullRequestReview { id databaseId state commit { oid } }
          reactionGroups { content viewerHasReacted users { totalCount } }
        }
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
        payload = graphql(
            query,
            {
                "owner": owner,
                "repo": repo_name,
                "number": number,
                "cursor": cursor,
            },
        )
        pr = payload["data"]["repository"]["pullRequest"]
        if pr is None:
            raise FetchError(f"PR #{number} was not found in {owner}/{repo_name}.")
        connection = pr[connection_name]
        nodes.extend(connection.get("nodes") or [])
        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            return nodes
        cursor = page_info["endCursor"]


def fetch_thread_comments(thread: dict[str, Any]) -> list[dict[str, Any]]:
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


def disposition_reply_kind(body: Any) -> str | None:
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


def viewer_disposition_replies(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": reply.get("id"),
            "databaseId": reply.get("databaseId"),
            "kind": kind,
        }
        for reply in comments[1:]
        if reply.get("viewerDidAuthor")
        if (kind := disposition_reply_kind(reply.get("body"))) is not None
    ]


def ai_pr_reviewer_review_ids(
    reviews: list[dict[str, Any]],
) -> tuple[set[str], set[str]]:
    node_ids: set[str] = set()
    database_ids: set[str] = set()
    for review in reviews:
        body = review.get("body")
        if not isinstance(body, str):
            continue
        marker = AI_PR_REVIEW_MARKER.match(body.lstrip())
        reviewed_head = str((review.get("commit") or {}).get("oid") or "")
        if marker is None or marker.group("head") != reviewed_head:
            continue
        if node_id := str(review.get("id") or ""):
            node_ids.add(node_id)
        if database_id := str(review.get("databaseId") or ""):
            database_ids.add(database_id)
    return node_ids, database_ids


def thread_belongs_to_ai_pr_reviewer(
    thread: dict[str, Any], review_ids: tuple[set[str], set[str]]
) -> bool:
    comments = thread.get("comments") or []
    if not comments:
        return False
    review = comments[0].get("pullRequestReview") or {}
    node_ids, database_ids = review_ids
    return bool(
        str(review.get("id") or "") in node_ids
        or str(review.get("databaseId") or "") in database_ids
    )


def viewer_reaction_kind(comment: dict[str, Any]) -> str:
    reactions = {
        str(group.get("content") or "")
        for group in comment.get("reactionGroups") or []
        if group.get("viewerHasReacted")
        and group.get("content") in {"THUMBS_UP", "THUMBS_DOWN"}
    }
    if len(reactions) > 1:
        return "conflicting"
    if "THUMBS_UP" in reactions:
        return "thumbs_up"
    if "THUMBS_DOWN" in reactions:
        return "thumbs_down"
    return "none"


def summarize_codex_thread_action(thread: dict[str, Any]) -> dict[str, Any] | None:
    comments = thread.get("comments") or []
    if not comments:
        return None
    root = comments[0]
    author = str((root.get("author") or {}).get("login") or "").lower()
    if author not in CODEX_LOGINS:
        return None

    reaction = viewer_reaction_kind(root)
    disposition_replies = viewer_disposition_replies(comments)
    reply_kinds = {reply["kind"] for reply in disposition_replies}
    reply_kind = next(iter(reply_kinds)) if len(reply_kinds) == 1 else "none"
    resolved = bool(thread.get("isResolved"))

    if reaction == "conflicting" or len(reply_kinds) > 1:
        action_state = "conflicting_disposition"
    elif reaction == "none" and not disposition_replies:
        action_state = "resolved_without_disposition" if resolved else "undispositioned"
    elif reaction != "none" and not disposition_replies:
        action_state = "reaction_only"
    elif reaction == "none":
        action_state = "reply_without_reaction"
    elif (reaction, reply_kind) not in {
        ("thumbs_up", "fixed"),
        ("thumbs_up", "valid_nonblocking"),
        ("thumbs_down", "false_positive"),
        ("thumbs_down", "not_actionable_scope_expansion"),
        ("thumbs_down", "not_validated"),
    }:
        action_state = "mismatched_disposition"
    elif not resolved:
        action_state = "reply_unresolved"
    else:
        action_state = "complete"

    review = root.get("pullRequestReview") or {}
    return {
        "action_state": action_state,
        "root_comment_id": root.get("id"),
        "root_comment_database_id": root.get("databaseId"),
        "review_id": review.get("id"),
        "review_database_id": review.get("databaseId"),
        "reviewed_sha": (review.get("commit") or {}).get("oid"),
        "viewer_reaction": reaction,
        "viewer_disposition_reply": reply_kind,
        "viewer_disposition_replies": disposition_replies,
        "missing_reply": not disposition_replies,
        "missing_resolution": not resolved,
    }


def summarize_ai_pr_reviewer_thread_action(
    thread: dict[str, Any], review_ids: tuple[set[str], set[str]]
) -> dict[str, Any] | None:
    comments = thread.get("comments") or []
    if not comments or not thread_belongs_to_ai_pr_reviewer(thread, review_ids):
        return None
    root = comments[0]
    disposition_replies = viewer_disposition_replies(comments)
    reply_kinds = {reply["kind"] for reply in disposition_replies}
    reply_kind = next(iter(reply_kinds)) if len(reply_kinds) == 1 else "none"
    resolved = bool(thread.get("isResolved"))

    if len(reply_kinds) > 1:
        action_state = "conflicting_disposition"
    elif not disposition_replies:
        action_state = "resolved_without_disposition" if resolved else "undispositioned"
    elif not resolved:
        action_state = "reply_unresolved"
    else:
        action_state = "complete"

    review = root.get("pullRequestReview") or {}
    return {
        "action_state": action_state,
        "root_comment_id": root.get("id"),
        "root_comment_database_id": root.get("databaseId"),
        "review_id": review.get("id"),
        "review_database_id": review.get("databaseId"),
        "reviewed_sha": (review.get("commit") or {}).get("oid"),
        "viewer_disposition_reply": reply_kind,
        "viewer_disposition_replies": disposition_replies,
        "missing_reply": not disposition_replies,
        "missing_resolution": not resolved,
    }


def summarize_thread_actions(
    threads: list[dict[str, Any]], action_key: str
) -> dict[str, Any]:
    states: dict[str, int] = {}
    for thread in threads:
        action = thread.get(action_key)
        if not isinstance(action, dict):
            continue
        state = str(action.get("action_state") or "undispositioned")
        states[state] = states.get(state, 0) + 1
    complete = states.get("complete", 0)
    total = sum(states.values())
    return {
        "total": total,
        "complete": complete,
        "incomplete": total - complete,
        "by_state": dict(sorted(states.items())),
    }


def summarize_codex_thread_actions(threads: list[dict[str, Any]]) -> dict[str, Any]:
    return summarize_thread_actions(threads, "codex_thread_action")


def summarize_ai_pr_reviewer_thread_actions(
    threads: list[dict[str, Any]],
) -> dict[str, Any]:
    return summarize_thread_actions(threads, "ai_pr_reviewer_thread_action")


def fetch_all(repo: str, pr: dict[str, Any], required_reviewers: list[str] | None = None) -> dict[str, Any]:
    owner, repo_name = split_repo(repo)
    number = int(pr["number"])
    comments = fetch_connection(owner, repo_name, number, COMMENTS_QUERY, "comments")
    reviews = fetch_connection(owner, repo_name, number, REVIEWS_QUERY, "reviews")
    ai_pr_review_ids = ai_pr_reviewer_review_ids(reviews)
    reactions = fetch_connection(owner, repo_name, number, REACTIONS_QUERY, "reactions")
    raw_threads = fetch_connection(owner, repo_name, number, THREADS_QUERY, "reviewThreads")
    threads: list[dict[str, Any]] = []
    review_comments: list[dict[str, Any]] = []
    for raw in raw_threads:
        thread = {key: value for key, value in raw.items() if key != "comments"}
        thread["comments"] = fetch_thread_comments(raw)
        if action := summarize_ai_pr_reviewer_thread_action(thread, ai_pr_review_ids):
            thread["ai_pr_reviewer_thread_action"] = action
        elif action := summarize_codex_thread_action(thread):
            thread["codex_thread_action"] = action
        review_comments.extend(thread["comments"])
        threads.append(thread)
    reviewers = summarize_reviewers(
        reactions=reactions,
        reviews=reviews,
        comments=comments,
        review_comments=review_comments,
        current_head=pr["headRefOid"],
        required_reviewers=required_reviewers or [],
    )
    return {
        "pull_request": {
            "repo": repo,
            "number": number,
            "url": pr["url"],
            "title": pr["title"],
            "state": pr["state"],
            "is_draft": pr["isDraft"],
            "author_login": (pr.get("author") or {}).get("login"),
            "head_sha": pr["headRefOid"],
            "head_branch": pr["headRefName"],
            "base_branch": pr["baseRefName"],
        },
        "pr_reactions": reactions,
        "automated_reviewers": reviewers,
        "conversation_comments": comments,
        "reviews": reviews,
        "review_threads": threads,
        "codex_thread_actions": summarize_codex_thread_actions(threads),
        "ai_pr_reviewer_thread_actions": summarize_ai_pr_reviewer_thread_actions(
            threads
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch every PR conversation comment, review, and inline review thread."
    )
    parser.add_argument("--repo", help="OWNER/REPO; defaults to this checkout.")
    parser.add_argument("--pr", help="PR number, URL, or branch.")
    parser.add_argument("--reviewer", action="append", default=[], help="Additional configured reviewer login to summarize.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    gh(["auth", "status"])
    repo = resolve_repo(args.repo)
    pr = read_pr(repo, args.pr)
    print(json.dumps(fetch_all(repo, pr, args.reviewer), indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except FetchError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
