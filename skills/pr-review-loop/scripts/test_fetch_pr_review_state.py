#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).with_name("fetch_pr_review_state.py")
SPEC = importlib.util.spec_from_file_location("fetch_pr_review_state", SCRIPT_PATH)
fetch_pr_review_state = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(fetch_pr_review_state)


class FetchPRReviewStateTest(unittest.TestCase):
    ai_head = "a" * 40
    ai_digest = "b" * 64

    def codex_thread(
        self,
        *,
        reaction: str | None = None,
        reply: str | None = None,
        resolved: bool = False,
    ):
        reaction_groups = [
            {
                "content": content,
                "viewerHasReacted": content == reaction,
                "users": {"totalCount": int(content == reaction)},
            }
            for content in ("THUMBS_UP", "THUMBS_DOWN")
        ]
        comments = [
            {
                "id": "root",
                "databaseId": 10,
                "author": {"login": "chatgpt-codex-connector"},
                "viewerDidAuthor": False,
                "reactionGroups": reaction_groups,
                "pullRequestReview": {
                    "id": "review",
                    "databaseId": 20,
                    "commit": {"oid": "reviewed"},
                },
            }
        ]
        if reply is not None:
            comments.append(
                {
                    "id": "reply",
                    "databaseId": 11,
                    "author": {"login": "viewer"},
                    "viewerDidAuthor": True,
                    "body": reply,
                }
            )
        return {"id": "thread", "isResolved": resolved, "comments": comments}

    def ai_review(self, *, marked: bool = True, marker_head: str | None = None):
        head = marker_head or self.ai_head
        body = (
            f"<!-- ai-pr-reviewer:v3:{head}:{self.ai_digest} -->\n## AI review"
            if marked
            else "Ordinary service-account review"
        )
        return {
            "id": "ai-review",
            "databaseId": 30,
            "body": body,
            "commit": {"oid": self.ai_head},
        }

    def ai_thread(self, *, reply: str | None = None, resolved: bool = False):
        comments = [
            {
                "id": "ai-root",
                "databaseId": 40,
                "author": {"login": "MarkHuang-AI-Service"},
                "viewerDidAuthor": False,
                "pullRequestReview": {
                    "id": "ai-review",
                    "databaseId": 30,
                    "commit": {"oid": self.ai_head},
                },
            }
        ]
        if reply is not None:
            comments.append(
                {
                    "id": "ai-reply",
                    "databaseId": 41,
                    "author": {"login": "viewer"},
                    "viewerDidAuthor": True,
                    "body": reply,
                }
            )
        return {"id": "ai-thread", "isResolved": resolved, "comments": comments}

    def test_fetch_connection_paginates_without_duplicating_completed_sources(self):
        pages = [
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "comments": {
                                "nodes": [{"id": "one"}],
                                "pageInfo": {"hasNextPage": True, "endCursor": "next"},
                            }
                        }
                    }
                }
            },
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "comments": {
                                "nodes": [{"id": "two"}],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    }
                }
            },
        ]
        with mock.patch.object(
            fetch_pr_review_state, "graphql", side_effect=pages
        ) as graphql:
            comments = fetch_pr_review_state.fetch_connection(
                "owner", "repo", 7, "query", "comments"
            )

        self.assertEqual(["one", "two"], [item["id"] for item in comments])
        self.assertIsNone(graphql.call_args_list[0].args[1]["cursor"])
        self.assertEqual("next", graphql.call_args_list[1].args[1]["cursor"])

    def test_fetch_thread_comments_reads_every_page(self):
        thread = {
            "id": "thread",
            "comments": {
                "nodes": [{"id": "one"}],
                "pageInfo": {"hasNextPage": True, "endCursor": "next"},
            },
        }
        page = {
            "data": {
                "node": {
                    "comments": {
                        "nodes": [{"id": "two"}],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        }
        with mock.patch.object(fetch_pr_review_state, "graphql", return_value=page):
            comments = fetch_pr_review_state.fetch_thread_comments(thread)

        self.assertEqual(["one", "two"], [item["id"] for item in comments])

    def test_fetch_all_preserves_all_review_surfaces_and_head(self):
        pr = {
            "number": 7,
            "url": "https://github.com/owner/repo/pull/7",
            "title": "Change",
            "state": "OPEN",
            "isDraft": False,
            "author": {"login": "author"},
            "headRefOid": "abc123",
            "headRefName": "feature",
            "baseRefName": "main",
        }
        thread = {
            "id": "thread",
            "isResolved": False,
            "isOutdated": False,
            "path": "app.go",
            "comments": {
                "nodes": [
                    {
                        "id": "inline",
                        "reactionGroups": [
                            {
                                "content": "THUMBS_UP",
                                "viewerHasReacted": True,
                                "users": {"totalCount": 1},
                            }
                        ],
                    }
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        }
        with (
            mock.patch.object(
                fetch_pr_review_state,
                "fetch_connection",
                side_effect=[
                    [{"id": "top"}],
                    [{"id": "review"}],
                    [
                        {
                            "id": "reaction",
                            "content": "THUMBS_UP",
                            "createdAt": "2026-08-10T16:48:27Z",
                            "user": {"login": "chatgpt-codex-connector[bot]"},
                        }
                    ],
                    [thread],
                ],
            ),
            mock.patch.object(
                fetch_pr_review_state,
                "fetch_thread_comments",
                return_value=[
                    {
                        "id": "inline",
                        "reactionGroups": [
                            {
                                "content": "THUMBS_UP",
                                "viewerHasReacted": True,
                                "users": {"totalCount": 1},
                            }
                        ],
                    }
                ],
            ),
        ):
            result = fetch_pr_review_state.fetch_all("owner/repo", pr)

        self.assertEqual("abc123", result["pull_request"]["head_sha"])
        self.assertEqual("author", result["pull_request"]["author_login"])
        self.assertEqual("top", result["conversation_comments"][0]["id"])
        self.assertEqual("review", result["reviews"][0]["id"])
        self.assertEqual("reaction", result["pr_reactions"][0]["id"])
        self.assertEqual("approved", result["automated_reviewers"]["codex"]["state"])
        self.assertEqual(
            "not_observed", result["automated_reviewers"]["coderabbit"]["state"]
        )
        self.assertEqual("inline", result["review_threads"][0]["comments"][0]["id"])
        self.assertTrue(
            result["review_threads"][0]["comments"][0]["reactionGroups"][0][
                "viewerHasReacted"
            ]
        )
        self.assertNotIn("pageInfo", result["review_threads"][0])
        self.assertEqual(0, result["codex_thread_actions"]["total"])
        self.assertEqual(0, result["ai_pr_reviewer_thread_actions"]["total"])

    def test_inline_queries_request_viewer_reaction_state(self):
        self.assertIn("reactionGroups", fetch_pr_review_state.THREADS_QUERY)
        self.assertIn("viewerHasReacted", fetch_pr_review_state.THREADS_QUERY)
        self.assertIn("viewerDidAuthor", fetch_pr_review_state.THREADS_QUERY)
        self.assertIn("reactionGroups", fetch_pr_review_state.THREAD_COMMENTS_QUERY)
        self.assertIn("viewerHasReacted", fetch_pr_review_state.THREAD_COMMENTS_QUERY)
        self.assertIn("viewerDidAuthor", fetch_pr_review_state.THREAD_COMMENTS_QUERY)

    def test_codex_thumbs_up_without_reply_or_resolution_is_reaction_only(self):
        summary = fetch_pr_review_state.summarize_codex_thread_action(
            self.codex_thread(reaction="THUMBS_UP")
        )

        self.assertEqual("reaction_only", summary["action_state"])
        self.assertTrue(summary["missing_reply"])
        self.assertTrue(summary["missing_resolution"])
        self.assertEqual(20, summary["review_database_id"])

    def test_codex_fixed_reply_without_resolution_is_incomplete(self):
        summary = fetch_pr_review_state.summarize_codex_thread_action(
            self.codex_thread(reaction="THUMBS_UP", reply="Fixed in abc123: tested")
        )

        self.assertEqual("reply_unresolved", summary["action_state"])
        self.assertFalse(summary["missing_reply"])
        self.assertTrue(summary["missing_resolution"])

    def test_codex_fixed_reply_and_resolution_is_complete(self):
        summary = fetch_pr_review_state.summarize_codex_thread_action(
            self.codex_thread(
                reaction="THUMBS_UP",
                reply="Fixed in abc123: tested",
                resolved=True,
            )
        )

        self.assertEqual("complete", summary["action_state"])
        self.assertFalse(summary["missing_reply"])
        self.assertFalse(summary["missing_resolution"])

    def test_codex_valid_nonblocking_requires_thumbs_up_and_resolution(self):
        summary = fetch_pr_review_state.summarize_codex_thread_action(
            self.codex_thread(
                reaction="THUMBS_UP",
                reply="Valid but non-blocking: the consequence is limited to test failure ergonomics",
                resolved=True,
            )
        )

        self.assertEqual("complete", summary["action_state"])
        self.assertEqual("valid_nonblocking", summary["viewer_disposition_reply"])
        self.assertFalse(summary["missing_reply"])
        self.assertFalse(summary["missing_resolution"])

    def test_codex_valid_nonblocking_with_thumbs_down_is_mismatched(self):
        summary = fetch_pr_review_state.summarize_codex_thread_action(
            self.codex_thread(
                reaction="THUMBS_DOWN",
                reply="Valid but non-blocking: the consequence is limited to test failure ergonomics",
                resolved=True,
            )
        )

        self.assertEqual("mismatched_disposition", summary["action_state"])

    def test_codex_false_positive_requires_thumbs_down(self):
        summary = fetch_pr_review_state.summarize_codex_thread_action(
            self.codex_thread(
                reaction="THUMBS_UP",
                reply="False positive: covered by the repository invariant",
                resolved=True,
            )
        )

        self.assertEqual("mismatched_disposition", summary["action_state"])

    def test_codex_scope_expansion_with_thumbs_down_and_resolution_is_complete(self):
        summary = fetch_pr_review_state.summarize_codex_thread_action(
            self.codex_thread(
                reaction="THUMBS_DOWN",
                reply="Not actionable for this PR: the concern requires an unsupported deployment mode",
                resolved=True,
            )
        )

        self.assertEqual("complete", summary["action_state"])
        self.assertEqual(
            "not_actionable_scope_expansion",
            summary["viewer_disposition_reply"],
        )
        self.assertFalse(summary["missing_reply"])
        self.assertFalse(summary["missing_resolution"])

    def test_codex_scope_expansion_requires_thumbs_down(self):
        summary = fetch_pr_review_state.summarize_codex_thread_action(
            self.codex_thread(
                reaction="THUMBS_UP",
                reply="Not actionable for this PR: the concern requires an unsupported deployment mode",
                resolved=True,
            )
        )

        self.assertEqual("mismatched_disposition", summary["action_state"])

    def test_codex_not_validated_with_thumbs_down_and_resolution_is_complete(self):
        summary = fetch_pr_review_state.summarize_codex_thread_action(
            self.codex_thread(
                reaction="THUMBS_DOWN",
                reply="Not validated: the required reproduction evidence is unavailable",
                resolved=True,
            )
        )

        self.assertEqual("complete", summary["action_state"])
        self.assertEqual("not_validated", summary["viewer_disposition_reply"])

    def test_codex_thread_action_summary_exposes_incomplete_backlog(self):
        complete = self.codex_thread(
            reaction="THUMBS_DOWN",
            reply="False positive: current code disproves the claim",
            resolved=True,
        )
        incomplete = self.codex_thread(reaction="THUMBS_UP")
        threads = []
        for thread in (complete, incomplete):
            thread["codex_thread_action"] = (
                fetch_pr_review_state.summarize_codex_thread_action(thread)
            )
            threads.append(thread)

        summary = fetch_pr_review_state.summarize_codex_thread_actions(threads)

        self.assertEqual(2, summary["total"])
        self.assertEqual(1, summary["complete"])
        self.assertEqual(1, summary["incomplete"])
        self.assertEqual(
            {"complete": 1, "reaction_only": 1},
            summary["by_state"],
        )

    def test_ai_pr_reviewer_marker_requires_matching_review_head(self):
        matching = fetch_pr_review_state.ai_pr_reviewer_review_ids(
            [self.ai_review()]
        )
        mismatched = fetch_pr_review_state.ai_pr_reviewer_review_ids(
            [self.ai_review(marker_head="c" * 40)]
        )

        self.assertEqual(({"ai-review"}, {"30"}), matching)
        self.assertEqual((set(), set()), mismatched)

    def test_marked_ai_pr_reviewer_fixed_reply_and_resolution_is_complete(self):
        review_ids = fetch_pr_review_state.ai_pr_reviewer_review_ids(
            [self.ai_review()]
        )
        summary = fetch_pr_review_state.summarize_ai_pr_reviewer_thread_action(
            self.ai_thread(reply="Fixed in abc123: tested", resolved=True),
            review_ids,
        )

        self.assertEqual("complete", summary["action_state"])
        self.assertEqual("fixed", summary["viewer_disposition_reply"])
        self.assertFalse(summary["missing_reply"])
        self.assertFalse(summary["missing_resolution"])

    def test_marked_ai_pr_reviewer_valid_nonblocking_is_complete(self):
        review_ids = fetch_pr_review_state.ai_pr_reviewer_review_ids(
            [self.ai_review()]
        )
        summary = fetch_pr_review_state.summarize_ai_pr_reviewer_thread_action(
            self.ai_thread(
                reply="Valid but non-blocking: the verified consequence is local",
                resolved=True,
            ),
            review_ids,
        )

        self.assertEqual("complete", summary["action_state"])
        self.assertEqual("valid_nonblocking", summary["viewer_disposition_reply"])

    def test_marked_ai_pr_reviewer_reply_without_resolution_is_incomplete(self):
        review_ids = fetch_pr_review_state.ai_pr_reviewer_review_ids(
            [self.ai_review()]
        )
        summary = fetch_pr_review_state.summarize_ai_pr_reviewer_thread_action(
            self.ai_thread(reply="False positive: current code disproves the claim"),
            review_ids,
        )

        self.assertEqual("reply_unresolved", summary["action_state"])
        self.assertTrue(summary["missing_resolution"])

    def test_unmarked_service_account_review_is_not_ai_pr_reviewer(self):
        review_ids = fetch_pr_review_state.ai_pr_reviewer_review_ids(
            [self.ai_review(marked=False)]
        )

        self.assertIsNone(
            fetch_pr_review_state.summarize_ai_pr_reviewer_thread_action(
                self.ai_thread(), review_ids
            )
        )

    def test_ai_pr_reviewer_thread_summary_exposes_incomplete_backlog(self):
        review_ids = fetch_pr_review_state.ai_pr_reviewer_review_ids(
            [self.ai_review()]
        )
        complete = self.ai_thread(
            reply="Not actionable for this PR: outside the accepted contract",
            resolved=True,
        )
        incomplete = self.ai_thread()
        incomplete["id"] = "ai-thread-two"
        threads = []
        for thread in (complete, incomplete):
            thread["ai_pr_reviewer_thread_action"] = (
                fetch_pr_review_state.summarize_ai_pr_reviewer_thread_action(
                    thread, review_ids
                )
            )
            threads.append(thread)

        summary = fetch_pr_review_state.summarize_ai_pr_reviewer_thread_actions(
            threads
        )

        self.assertEqual(2, summary["total"])
        self.assertEqual(1, summary["complete"])
        self.assertEqual(1, summary["incomplete"])
        self.assertEqual(
            {"complete": 1, "undispositioned": 1}, summary["by_state"]
        )

    def test_split_repo_rejects_ambiguous_scope(self):
        with self.assertRaises(fetch_pr_review_state.FetchError):
            fetch_pr_review_state.split_repo("repo-only")


if __name__ == "__main__":
    unittest.main()
