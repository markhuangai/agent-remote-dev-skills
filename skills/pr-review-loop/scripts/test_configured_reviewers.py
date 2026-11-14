from __future__ import annotations

import unittest

from reviewer_state import summarize_reviewers
from wait_for_pr_state import required_reviewer_problems, terminal_receipt, verify_terminal_receipt, GateError


class ConfiguredReviewerTest(unittest.TestCase):
    def setUp(self):
        self.head = "a" * 40
        self.checks = [{"name": "unit", "bucket": "pass", "startedAt": "2026-11-14T10:00:00Z"}]

    def summarize(self, *, reviews=(), reactions=(), comments=(), required=("quality-bot[bot]",)):
        return summarize_reviewers(reactions=reactions, reviews=reviews, comments=comments,
                                   review_comments=(), current_head=self.head, required_reviewers=required)

    def review(self, *, head=None, state="COMMENTED"):
        return {"author": {"login": "quality-bot[bot]"}, "state": state,
                "submittedAt": "2026-11-14T10:05:00Z", "commit": {"oid": head or self.head}}

    def receipt(self, reviewers, required, checks=None):
        return terminal_receipt(repo="owner/repo", pr={"number": 1, "url": "https://github.com/owner/repo/pull/1",
            "headRefName": "task"}, head=self.head, checks=self.checks if checks is None else checks,
            watermark=(), reviewers=reviewers, reviewer_pending=(), approval_fast_path=False,
            coderabbit_deferred_current_cycle=False, stable_seconds=180, stable_source="live",
            required_reviewers=required)

    def test_repository_without_required_reviewers_has_no_vendor_requirement(self):
        result = self.receipt(self.summarize(required=()), [])
        self.assertTrue(result["required_reviewers_satisfied"])
        self.assertTrue(result["all_checks_nonblocking"])

    def test_missing_required_reviewer_cannot_certify_completion(self):
        result = self.receipt(self.summarize(), ["quality-bot[bot]"])
        self.assertFalse(result["required_reviewers_satisfied"])
        self.assertEqual("missing_current_head_review", result["required_reviewer_problems"][0]["reason"])

    def test_generic_current_head_submitted_review_completes_requirement(self):
        summary = self.summarize(reviews=[self.review()])
        self.assertEqual([], required_reviewer_problems(self.checks, summary, ["QUALITY-BOT[bot]"]))

    def test_stale_and_dismissed_reviews_do_not_complete_requirement(self):
        for review in [self.review(head="b" * 40), self.review(state="DISMISSED"), self.review(state="PENDING")]:
            with self.subTest(review=review):
                result = self.receipt(self.summarize(reviews=[review]), ["quality-bot[bot]"])
                self.assertFalse(result["required_reviewers_satisfied"])

    def test_generic_reaction_is_not_a_vendor_completion_signal(self):
        summary = self.summarize(reactions=[{"user": {"login": "quality-bot[bot]"}, "content": "THUMBS_UP",
                                               "createdAt": "2026-11-14T10:05:00Z"}])
        self.assertFalse(self.receipt(summary, ["quality-bot[bot]"])["required_reviewers_satisfied"])

    def test_human_with_similar_name_cannot_satisfy_bot_requirement(self):
        review = self.review()
        review["author"]["login"] = "quality-bot"
        summary = self.summarize(reviews=[review])
        self.assertFalse(self.receipt(summary, ["quality-bot[bot]"])["required_reviewers_satisfied"])

    def test_human_named_after_a_vendor_cannot_satisfy_known_bot_requirement(self):
        for vendor in ["codex", "coderabbit"]:
            with self.subTest(vendor=vendor):
                review = self.review()
                review["author"]["login"] = vendor
                summary = self.summarize(reviews=[review], required=(vendor,))
                self.assertFalse(self.receipt(summary, [vendor])["required_reviewers_satisfied"])

    def test_stale_codex_approval_does_not_complete_new_cycle(self):
        summary = self.summarize(required=("codex",), reactions=[{
            "user": {"login": "chatgpt-codex-connector[bot]"}, "content": "THUMBS_UP",
            "createdAt": "2026-11-14T09:55:00Z"}])
        self.assertFalse(self.receipt(summary, ["codex"])["required_reviewers_satisfied"])

    def test_current_codex_approval_completes_requirement(self):
        summary = self.summarize(required=("codex",), reactions=[{
            "user": {"login": "chatgpt-codex-connector[bot]"}, "content": "THUMBS_UP",
            "createdAt": "2026-11-14T10:05:00Z"}])
        self.assertTrue(self.receipt(summary, ["codex"])["required_reviewers_satisfied"])

    def test_unavailable_or_rate_limited_required_reviewer_is_not_a_pass(self):
        for body in ["Review skipped", "Review limit reached"]:
            with self.subTest(body=body):
                summary = self.summarize(required=("coderabbit",), comments=[{
                    "author": {"login": "coderabbitai[bot]"}, "body": body,
                    "createdAt": "2026-11-14T10:05:00Z"}])
                result = self.receipt(summary, ["coderabbit"])
                self.assertFalse(result["required_reviewers_satisfied"])
                self.assertEqual("unavailable", result["required_reviewer_problems"][0]["reason"])

    def test_required_check_failure_and_empty_check_set_cannot_pass(self):
        for checks in [[], [{"name": "unit", "bucket": "fail"}]]:
            with self.subTest(checks=checks):
                self.assertFalse(self.receipt(self.summarize(required=()), [], checks)["all_checks_nonblocking"])

    def test_new_reviewer_policy_invalidates_old_receipt_before_collection(self):
        baseline = self.receipt(self.summarize(required=()), [])
        with self.assertRaisesRegex(GateError, "reviewer policy changed"):
            verify_terminal_receipt("owner/repo", {"number": 1}, baseline, required_reviewers=["quality-bot"])


if __name__ == "__main__":
    unittest.main()
