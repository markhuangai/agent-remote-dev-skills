#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).with_name("review_thread_action.py")
SPEC = importlib.util.spec_from_file_location("review_thread_action", SCRIPT_PATH)
review_thread_action = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(review_thread_action)


class ReviewThreadActionTest(unittest.TestCase):
    head = "a" * 40
    other_head = "b" * 40

    def pr(self, head: str | None = None) -> dict[str, object]:
        return {
            "number": 7,
            "state": "OPEN",
            "isDraft": False,
            "headRefOid": head or self.head,
        }

    def thread(
        self,
        *,
        reply: str | None = None,
        resolved: bool = False,
        reply_id: str = "reply",
    ) -> dict[str, object]:
        comments: list[dict[str, object]] = [
            {
                "id": "root",
                "databaseId": 10,
                "body": "review finding",
                "viewerDidAuthor": False,
                "author": {"login": "chatgpt-codex-connector"},
            }
        ]
        if reply is not None:
            comments.append(
                {
                    "id": reply_id,
                    "databaseId": 11,
                    "body": reply,
                    "viewerDidAuthor": True,
                    "author": {"login": "Z-M-Huang"},
                    "pullRequestReview": {
                        "id": "reply-review",
                        "databaseId": 20,
                        "state": "COMMENTED",
                    },
                }
            )
        return {
            "__typename": "PullRequestReviewThread",
            "id": "thread",
            "isResolved": resolved,
            "isOutdated": False,
            "pullRequest": {"number": 7, "repository": {"nameWithOwner": "owner/repo"}},
            "comments": comments,
        }

    def mutation_comment(self, body: str) -> dict[str, object]:
        return {
            "id": "reply",
            "databaseId": 11,
            "body": body,
            "viewerDidAuthor": True,
            "author": {"login": "Z-M-Huang"},
            "pullRequestReview": {
                "id": "reply-review",
                "databaseId": 20,
                "state": "COMMENTED",
                "commit": {"oid": self.head},
            },
        }

    def test_mutations_reject_a_thread_owned_by_another_pr_or_repository(self):
        for owner in [{"number": 8, "repository": {"nameWithOwner": "owner/repo"}},
                      {"number": 7, "repository": {"nameWithOwner": "other/repo"}}, {}]:
            for action in ["reply", "resolve"]:
                with self.subTest(owner=owner, action=action):
                    thread = self.thread(reply="False positive: evidence")
                    thread["pullRequest"] = owner
                    with mock.patch.object(review_thread_action, "read_pr", return_value=self.pr()), \
                         mock.patch.object(review_thread_action, "read_thread", return_value=thread), \
                         mock.patch.object(review_thread_action, "graphql") as graphql:
                        with self.assertRaisesRegex(review_thread_action.ActionError, "different pull request"):
                            if action == "reply":
                                review_thread_action.reply_thread("owner/repo", "7", self.head, "thread", "False positive: evidence")
                            else:
                                review_thread_action.resolve_thread("owner/repo", "7", self.head, "thread")
                        graphql.assert_not_called()

    def test_reply_uses_only_thread_reply_mutation(self):
        body = "False positive: current code proves the claim is unreachable"
        with (
            mock.patch.object(
                review_thread_action, "read_pr", side_effect=[self.pr(), self.pr()]
            ),
            mock.patch.object(
                review_thread_action,
                "read_thread",
                side_effect=[self.thread(), self.thread(reply=body, reply_id="reply")],
            ),
            mock.patch.object(
                review_thread_action,
                "graphql",
                return_value={
                    "data": {
                        "addPullRequestReviewThreadReply": {
                            "comment": self.mutation_comment(body)
                        }
                    }
                },
            ) as graphql,
        ):
            receipt = review_thread_action.reply_thread(
                "owner/repo", "7", self.head, "thread", body
            )

        query = graphql.call_args.args[0]
        self.assertIn("addPullRequestReviewThreadReply", query)
        self.assertNotIn("addPullRequestReview(", query)
        self.assertNotIn("submitPullRequestReview", query)
        self.assertEqual("thread", graphql.call_args.args[1]["threadId"])
        self.assertEqual("replied", receipt["action"])
        self.assertTrue(receipt["verified"])
        self.assertEqual("COMMENTED", receipt["review_state"])

    def test_reply_is_idempotent_for_an_existing_exact_disposition(self):
        body = "Fixed in abc1234: the regression test passes"
        with (
            mock.patch.object(review_thread_action, "read_pr", return_value=self.pr()),
            mock.patch.object(
                review_thread_action,
                "read_thread",
                return_value=self.thread(reply=body),
            ),
            mock.patch.object(review_thread_action, "graphql") as graphql,
        ):
            receipt = review_thread_action.reply_thread(
                "owner/repo", "7", self.head, "thread", body
            )

        graphql.assert_not_called()
        self.assertEqual("already_present", receipt["action"])
        self.assertTrue(receipt["verified"])

    def test_valid_nonblocking_disposition_is_idempotent(self):
        body = "Valid but non-blocking: the consequence is limited to test diagnostics"
        with (
            mock.patch.object(review_thread_action, "read_pr", return_value=self.pr()),
            mock.patch.object(
                review_thread_action,
                "read_thread",
                return_value=self.thread(reply=body),
            ),
            mock.patch.object(review_thread_action, "graphql") as graphql,
        ):
            receipt = review_thread_action.reply_thread(
                "owner/repo", "7", self.head, "thread", body
            )

        graphql.assert_not_called()
        self.assertEqual("already_present", receipt["action"])
        self.assertTrue(receipt["verified"])
        self.assertEqual("valid_nonblocking", review_thread_action.disposition_kind(body))

    def test_reply_rejects_conflicting_existing_disposition(self):
        with (
            mock.patch.object(review_thread_action, "read_pr", return_value=self.pr()),
            mock.patch.object(
                review_thread_action,
                "read_thread",
                return_value=self.thread(
                    reply="False positive: this is already covered"
                ),
            ),
            mock.patch.object(review_thread_action, "graphql") as graphql,
        ):
            with self.assertRaisesRegex(review_thread_action.ActionError, "different"):
                review_thread_action.reply_thread(
                    "owner/repo",
                    "7",
                    self.head,
                    "thread",
                    "Fixed in abc1234: tested",
                )

        graphql.assert_not_called()

    def test_valid_nonblocking_conflicts_with_an_existing_disposition(self):
        with (
            mock.patch.object(review_thread_action, "read_pr", return_value=self.pr()),
            mock.patch.object(
                review_thread_action,
                "read_thread",
                return_value=self.thread(reply="False positive: current code disproves it"),
            ),
            mock.patch.object(review_thread_action, "graphql") as graphql,
        ):
            with self.assertRaisesRegex(review_thread_action.ActionError, "different"):
                review_thread_action.reply_thread(
                    "owner/repo",
                    "7",
                    self.head,
                    "thread",
                    "Valid but non-blocking: the consequence is limited to test diagnostics",
                )

        graphql.assert_not_called()

    def test_reply_rejects_head_drift_before_mutation(self):
        with (
            mock.patch.object(
                review_thread_action, "read_pr", return_value=self.pr(self.other_head)
            ),
            mock.patch.object(review_thread_action, "read_thread") as read_thread,
            mock.patch.object(review_thread_action, "graphql") as graphql,
        ):
            with self.assertRaisesRegex(review_thread_action.ActionError, "head changed"):
                review_thread_action.reply_thread(
                    "owner/repo",
                    "7",
                    self.head,
                    "thread",
                    "False positive: tested",
                )

        read_thread.assert_not_called()
        graphql.assert_not_called()

    def test_reply_verification_failure_does_not_retry(self):
        body = "Not validated: the required reproduction is unavailable"
        with (
            mock.patch.object(
                review_thread_action, "read_pr", side_effect=[self.pr(), self.pr()]
            ),
            mock.patch.object(
                review_thread_action,
                "read_thread",
                side_effect=[self.thread(), self.thread()],
            ) as read_thread,
            mock.patch.object(
                review_thread_action,
                "graphql",
                return_value={
                    "data": {
                        "addPullRequestReviewThreadReply": {
                            "comment": self.mutation_comment(body)
                        }
                    }
                },
            ) as graphql,
        ):
            with self.assertRaisesRegex(review_thread_action.ActionError, "do not retry"):
                review_thread_action.reply_thread(
                    "owner/repo", "7", self.head, "thread", body
                )

        self.assertEqual(1, graphql.call_count)
        self.assertEqual(2, read_thread.call_count)

    def test_resolve_requires_a_disposition_reply(self):
        with (
            mock.patch.object(review_thread_action, "read_pr", return_value=self.pr()),
            mock.patch.object(
                review_thread_action,
                "read_thread",
                return_value=self.thread(),
            ),
            mock.patch.object(review_thread_action, "graphql") as graphql,
        ):
            with self.assertRaisesRegex(review_thread_action.ActionError, "without"):
                review_thread_action.resolve_thread(
                    "owner/repo", "7", self.head, "thread"
                )

        graphql.assert_not_called()

    def test_resolve_uses_thread_resolution_mutation_and_refetches(self):
        body = "Not actionable for this PR: outside the accepted contract"
        resolved_thread = self.thread(reply=body, resolved=True)
        with (
            mock.patch.object(
                review_thread_action, "read_pr", side_effect=[self.pr(), self.pr()]
            ),
            mock.patch.object(
                review_thread_action,
                "read_thread",
                side_effect=[self.thread(reply=body), resolved_thread],
            ),
            mock.patch.object(
                review_thread_action,
                "graphql",
                return_value={
                    "data": {
                        "resolveReviewThread": {
                            "thread": {"id": "thread", "isResolved": True}
                        }
                    }
                },
            ) as graphql,
        ):
            receipt = review_thread_action.resolve_thread(
                "owner/repo", "7", self.head, "thread"
            )

        query = graphql.call_args.args[0]
        self.assertIn("resolveReviewThread", query)
        self.assertNotIn("addPullRequestReview", query)
        self.assertEqual("resolved", receipt["action"])
        self.assertTrue(receipt["verified"])

    def test_read_thread_paginates_comments(self):
        first = {
            "data": {
                "node": {
                    "__typename": "PullRequestReviewThread",
                    "id": "thread",
                    "isResolved": False,
                    "isOutdated": False,
                    "comments": {
                        "nodes": [{"id": "root"}],
                        "pageInfo": {"hasNextPage": True, "endCursor": "next"},
                    },
                }
            }
        }
        second = {
            "data": {
                "node": {
                    "comments": {
                        "nodes": [{"id": "reply"}],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        }
        with mock.patch.object(
            review_thread_action, "graphql", side_effect=[first, second]
        ) as graphql:
            thread = review_thread_action.read_thread("thread")

        self.assertEqual(["root", "reply"], [item["id"] for item in thread["comments"]])
        self.assertEqual("next", graphql.call_args_list[1].args[1]["cursor"])


if __name__ == "__main__":
    unittest.main()
