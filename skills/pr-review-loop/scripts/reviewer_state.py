#!/usr/bin/env python3
"""Summarize configured pull-request reviewers and known service indicators."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable


REVIEWER_ALIASES = {
    "codex": {
        "chatgpt-codex-connector",
        "chatgpt-codex-connector[bot]",
    },
    "coderabbit": {
        "coderabbitai",
        "coderabbitai[bot]",
    },
}

CODERABBIT_APPROVAL_MARKERS = (
    "no actionable comments were generated in the recent review",
)
CODERABBIT_DEFERRED_MARKERS = (
    "review limit reached",
    "rate limited by coderabbit.ai",
    "rate limit exceeded by coderabbit.ai",
)
CODERABBIT_UNAVAILABLE_MARKERS = (
    "review skipped",
    "couldn't start this review",
    "too many files",
)


def reviewer_name(login: str) -> str | None:
    normalized = login.strip().lower()
    for name, aliases in REVIEWER_ALIASES.items():
        if normalized in aliases:
            return name
    return None


def canonical_reviewer(login: str) -> str:
    normalized = login.strip().lower()
    return reviewer_name(normalized) or normalized


def node_login(node: dict[str, Any]) -> str:
    actor = node.get("author") or node.get("user") or {}
    return str(actor.get("login") or "").strip()


def parse_timestamp(value: Any) -> datetime | None:
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


def node_timestamp(node: dict[str, Any], *fields: str) -> str:
    for field in fields:
        value = node.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def reaction_content(node: dict[str, Any]) -> str:
    return str(node.get("content") or "").strip().upper()


def review_commit(node: dict[str, Any]) -> str:
    commit = node.get("commit") or {}
    return str(commit.get("oid") or "").strip()


def comment_terminal_signal(reviewer: str, node: dict[str, Any]) -> str | None:
    if reviewer != "coderabbit":
        return None
    body = str(node.get("body") or "").lower()
    if any(marker in body for marker in CODERABBIT_DEFERRED_MARKERS):
        return "deferred_comment"
    if any(marker in body for marker in CODERABBIT_UNAVAILABLE_MARKERS):
        return "unavailable_comment"
    if any(marker in body for marker in CODERABBIT_APPROVAL_MARKERS):
        return "approval_comment"
    return None


def _reviewer_nodes(
    nodes: Iterable[dict[str, Any]], reviewer: str
) -> list[dict[str, Any]]:
    if reviewer in REVIEWER_ALIASES:
        return [node for node in nodes if reviewer_name(node_login(node)) == reviewer]
    return [node for node in nodes if node_login(node).strip().lower() == reviewer]


def _latest_event(
    events: Iterable[tuple[str, str, dict[str, Any]]]
) -> tuple[datetime | None, str, str, dict[str, Any]] | None:
    prepared = [
        (parse_timestamp(timestamp), timestamp, signal, node)
        for timestamp, signal, node in events
    ]
    if not prepared:
        return None
    valid = [event for event in prepared if event[0] is not None]
    if valid:
        return max(valid, key=lambda event: event[0] or datetime.min.replace(tzinfo=timezone.utc))
    return prepared[-1]


def summarize_reviewers(
    *,
    reactions: Iterable[dict[str, Any]],
    reviews: Iterable[dict[str, Any]],
    comments: Iterable[dict[str, Any]],
    review_comments: Iterable[dict[str, Any]],
    current_head: str,
    required_reviewers: Iterable[str] = (),
) -> dict[str, dict[str, Any]]:
    reaction_nodes = list(reactions)
    review_nodes = list(reviews)
    comment_nodes = list(comments)
    inline_nodes = list(review_comments)
    summary: dict[str, dict[str, Any]] = {}

    identities = dict.fromkeys([*REVIEWER_ALIASES, *(canonical_reviewer(name) for name in required_reviewers)])
    for reviewer in identities:
        reviewer_reactions = _reviewer_nodes(reaction_nodes, reviewer)
        reviewer_reviews = _reviewer_nodes(review_nodes, reviewer)
        reviewer_comments = _reviewer_nodes(comment_nodes, reviewer)
        reviewer_inline = _reviewer_nodes(inline_nodes, reviewer)

        eyes = _latest_event(
            (
                node_timestamp(node, "createdAt", "created_at"),
                "eyes",
                node,
            )
            for node in reviewer_reactions
            if reviewer in REVIEWER_ALIASES and reaction_content(node) == "EYES"
        )
        approvals = [
            (
                node_timestamp(node, "createdAt", "created_at"),
                "thumbs_up",
                node,
            )
            for node in reviewer_reactions
            if reviewer in REVIEWER_ALIASES and reaction_content(node) in {"THUMBS_UP", "+1"}
        ]
        submitted_reviews = [
            (
                node_timestamp(node, "updatedAt", "submittedAt", "submitted_at"),
                "review",
                node,
            )
            for node in reviewer_reviews
        ]
        comment_terminals = [
            (
                node_timestamp(node, "updatedAt", "createdAt", "updated_at", "created_at"),
                signal,
                node,
            )
            for node in reviewer_comments
            if (signal := comment_terminal_signal(reviewer, node)) is not None
        ]
        terminal = _latest_event(
            [*approvals, *submitted_reviews, *comment_terminals]
        )

        in_progress = False
        if eyes is not None:
            eye_time = eyes[0]
            terminal_time = terminal[0] if terminal is not None else None
            in_progress = (
                eye_time is None
                or terminal_time is None
                or terminal_time < eye_time
            )

        latest_review = _latest_event(submitted_reviews)
        latest_activity = _latest_event(
            [
                *[
                    (
                        node_timestamp(node, "createdAt", "created_at"),
                        f"reaction:{reaction_content(node).lower()}",
                        node,
                    )
                    for node in reviewer_reactions
                ],
                *submitted_reviews,
                *[
                    (
                        node_timestamp(node, "updatedAt", "createdAt", "updated_at", "created_at"),
                        "comment",
                        node,
                    )
                    for node in reviewer_comments
                ],
                *[
                    (
                        node_timestamp(node, "updatedAt", "createdAt", "updated_at", "created_at"),
                        "review_comment",
                        node,
                    )
                    for node in reviewer_inline
                ],
            ]
        )

        latest_review_node = latest_review[3] if latest_review is not None else {}
        latest_review_head = review_commit(latest_review_node)
        if in_progress:
            state = "in_progress"
        elif terminal is not None and terminal[2] in {
            "thumbs_up",
            "approval_comment",
        }:
            state = "approved"
        elif terminal is not None and terminal[2] == "deferred_comment":
            state = "deferred"
        elif terminal is not None and terminal[2] == "unavailable_comment":
            state = "unavailable"
        elif latest_review is not None:
            state = "reviewed"
        elif latest_activity is not None:
            state = "observed"
        else:
            state = "not_observed"

        summary[reviewer] = {
            "state": state,
            "in_progress": in_progress,
            "latest_eye_at": eyes[1] if eyes is not None else None,
            "latest_terminal_at": terminal[1] if terminal is not None else None,
            "terminal_signal": terminal[2] if terminal is not None else None,
            "latest_activity_at": latest_activity[1] if latest_activity is not None else None,
            "latest_review_state": str(latest_review_node.get("state") or "") or None,
            "latest_review_head_sha": latest_review_head or None,
            "reviewed_current_head": bool(
                latest_review_head and current_head and latest_review_head == current_head
            ),
            "review_count": len(reviewer_reviews),
            "conversation_comment_count": len(reviewer_comments),
            "inline_comment_count": len(reviewer_inline),
        }

    return summary


def reviewers_in_progress(summary: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(
        reviewer for reviewer, state in summary.items() if state.get("in_progress")
    )
