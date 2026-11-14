#!/usr/bin/env python3

from __future__ import annotations

import unittest

import reviewer_state


def reaction(login: str, content: str, created_at: str) -> dict:
    return {
        "id": f"{login}:{content}:{created_at}",
        "content": content,
        "createdAt": created_at,
        "user": {"login": login},
    }


def review(login: str, submitted_at: str, head: str, state: str = "COMMENTED") -> dict:
    return {
        "id": f"{login}:{submitted_at}",
        "state": state,
        "submittedAt": submitted_at,
        "updatedAt": submitted_at,
        "commit": {"oid": head},
        "author": {"login": login},
    }


class ReviewerStateTest(unittest.TestCase):
    def summarize(self, reactions=(), reviews=(), comments=(), head="head"):
        return reviewer_state.summarize_reviewers(
            reactions=reactions,
            reviews=reviews,
            comments=comments,
            review_comments=(),
            current_head=head,
        )

    def test_new_eye_after_old_approval_is_in_progress(self):
        summary = self.summarize(
            reactions=[
                reaction(
                    "chatgpt-codex-connector[bot]",
                    "THUMBS_UP",
                    "2026-08-10T10:00:00Z",
                ),
                reaction(
                    "chatgpt-codex-connector[bot]",
                    "EYES",
                    "2026-08-10T11:00:00Z",
                ),
            ]
        )

        self.assertEqual("in_progress", summary["codex"]["state"])
        self.assertEqual(["codex"], reviewer_state.reviewers_in_progress(summary))

    def test_thumbs_up_after_eye_completes_codex_review(self):
        summary = self.summarize(
            reactions=[
                reaction(
                    "chatgpt-codex-connector[bot]",
                    "EYES",
                    "2026-08-10T10:00:00Z",
                ),
                reaction(
                    "chatgpt-codex-connector[bot]",
                    "THUMBS_UP",
                    "2026-08-10T11:00:00Z",
                ),
            ]
        )

        self.assertEqual("approved", summary["codex"]["state"])
        self.assertFalse(summary["codex"]["in_progress"])

    def test_submitted_review_after_eye_completes_coderabbit_review(self):
        summary = self.summarize(
            reactions=[
                reaction("coderabbitai[bot]", "EYES", "2026-08-10T10:00:00Z")
            ],
            reviews=[
                review("coderabbitai[bot]", "2026-08-10T11:00:00Z", "head")
            ],
        )

        self.assertEqual("reviewed", summary["coderabbit"]["state"])
        self.assertTrue(summary["coderabbit"]["reviewed_current_head"])
        self.assertFalse(summary["coderabbit"]["in_progress"])

    def test_old_review_does_not_claim_current_head(self):
        summary = self.summarize(
            reviews=[
                review("coderabbitai[bot]", "2026-08-10T11:00:00Z", "old-head")
            ],
            head="new-head",
        )

        self.assertEqual("reviewed", summary["coderabbit"]["state"])
        self.assertFalse(summary["coderabbit"]["reviewed_current_head"])

    def test_absent_coderabbit_is_nonblocking(self):
        summary = self.summarize()

        self.assertEqual("not_observed", summary["coderabbit"]["state"])
        self.assertEqual([], reviewer_state.reviewers_in_progress(summary))

    def test_coderabbit_no_actionable_comment_is_approval(self):
        summary = self.summarize(
            comments=[
                {
                    "id": "summary",
                    "body": "No actionable comments were generated in the recent review. 🎉",
                    "createdAt": "2026-08-10T11:00:00Z",
                    "updatedAt": "2026-08-10T11:00:00Z",
                    "author": {"login": "coderabbitai[bot]"},
                }
            ]
        )

        self.assertEqual("approved", summary["coderabbit"]["state"])
        self.assertEqual(
            "approval_comment", summary["coderabbit"]["terminal_signal"]
        )

    def test_coderabbit_rate_limit_comments_are_deferred(self):
        bodies = [
            (
                "Review limit reached, so we couldn't start this review. "
                "Next review available in: 6 minutes"
            ),
            (
                "<!-- This is an auto-generated comment: rate limited by coderabbit.ai -->\n\n"
                "> ## Review limit reached\n>\n"
                "> **Next included review available in 13 minutes.**"
            ),
            "Review limit reached, so we couldn't start this review.",
            "<!-- This is an auto-generated comment: rate limited by coderabbit.ai -->",
            (
                "<!-- This is an auto-generated comment: rate limit exceeded by coderabbit.ai -->\n\n"
                "## Rate limit exceeded"
            ),
        ]
        for login in ("coderabbitai[bot]", "coderabbitai"):
            for body in bodies:
                with self.subTest(login=login, body=body):
                    summary = self.summarize(
                        reactions=[reaction(login, "EYES", "2026-08-10T10:00:00Z")],
                        comments=[
                            {
                                "id": "summary",
                                "body": body,
                                "createdAt": "2026-08-10T11:00:00Z",
                                "updatedAt": "2026-08-10T11:00:00Z",
                                "author": {"login": login},
                            }
                        ],
                    )

                    self.assertEqual("deferred", summary["coderabbit"]["state"])
                    self.assertEqual(
                        "deferred_comment", summary["coderabbit"]["terminal_signal"]
                    )
                    self.assertEqual([], reviewer_state.reviewers_in_progress(summary))

    def test_new_eye_after_coderabbit_rate_limit_is_in_progress(self):
        summary = self.summarize(
            reactions=[reaction("coderabbitai[bot]", "EYES", "2026-08-10T12:00:00Z")],
            comments=[
                {
                    "id": "summary",
                    "body": "Review limit reached",
                    "createdAt": "2026-08-10T11:00:00Z",
                    "updatedAt": "2026-08-10T11:00:00Z",
                    "author": {"login": "coderabbitai[bot]"},
                }
            ]
        )

        self.assertEqual("in_progress", summary["coderabbit"]["state"])
        self.assertEqual(["coderabbit"], reviewer_state.reviewers_in_progress(summary))

    def test_non_coderabbit_rate_limit_comment_does_not_defer_reviewers(self):
        for login in ("contributor", "chatgpt-codex-connector[bot]"):
            with self.subTest(login=login):
                summary = self.summarize(
                    comments=[
                        {
                            "id": "summary",
                            "body": "Review limit reached",
                            "createdAt": "2026-08-10T11:00:00Z",
                            "author": {"login": login},
                        }
                    ]
                )

                self.assertEqual("not_observed", summary["coderabbit"]["state"])
                for state in summary.values():
                    self.assertIsNone(state["terminal_signal"])

    def test_coderabbit_unrelated_unavailability_remains_unavailable(self):
        for body in ("Review skipped", "We couldn't start this review", "Too many files"):
            with self.subTest(body=body):
                summary = self.summarize(
                    comments=[
                        {
                            "id": "summary",
                            "body": body,
                            "createdAt": "2026-08-10T11:00:00Z",
                            "author": {"login": "coderabbitai[bot]"},
                        }
                    ]
                )

                self.assertEqual("unavailable", summary["coderabbit"]["state"])
                self.assertEqual(
                    "unavailable_comment", summary["coderabbit"]["terminal_signal"]
                )

    def test_discussing_upstream_rate_limits_is_not_a_reviewer_deferral(self):
        summary = self.summarize(
            comments=[
                {
                    "id": "summary",
                    "body": "Handle upstream 'rate limit exceeded' errors when requests are rate limited.",
                    "createdAt": "2026-08-10T11:00:00Z",
                    "updatedAt": "2026-08-10T11:00:00Z",
                    "author": {"login": "coderabbitai[bot]"},
                }
            ]
        )

        self.assertEqual("observed", summary["coderabbit"]["state"])
        self.assertIsNone(summary["coderabbit"]["terminal_signal"])

    def test_unknown_eye_timestamp_fails_closed(self):
        summary = self.summarize(
            reactions=[reaction("coderabbitai[bot]", "EYES", "not-a-time")]
        )

        self.assertEqual("in_progress", summary["coderabbit"]["state"])


if __name__ == "__main__":
    unittest.main()
