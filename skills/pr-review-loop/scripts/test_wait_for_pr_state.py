#!/usr/bin/env python3

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import sys
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).with_name("wait_for_pr_state.py")
SPEC = importlib.util.spec_from_file_location("wait_for_pr_state", SCRIPT_PATH)
wait_for_pr_state = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(wait_for_pr_state)


class Result:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class NativeActionsTest(unittest.TestCase):
    def setUp(self):
        self.head = "a" * 40
        self.pr = {"number": 7, "url": "https://github.com/owner/repo/pull/7", "headRefOid": self.head,
                   "headRefName": "feature", "state": "OPEN", "isDraft": False}
        self.run = {"id": 10, "workflow_id": 3, "event": "pull_request", "name": "CI", "run_attempt": 1,
                    "head_sha": self.head, "status": "completed", "conclusion": "success",
                    "updated_at": "2026-08-09T18:05:00Z", "html_url": "https://github.com/owner/repo/actions/runs/10"}
        self.job = {"id": 20, "run_id": 10, "run_attempt": 1, "head_sha": self.head, "name": "unit",
                    "status": "completed", "conclusion": "success", "started_at": "2026-08-09T18:00:00Z",
                    "completed_at": "2026-08-09T18:05:00Z", "check_run_url": "https://api.github.com/repos/owner/repo/check-runs/20",
                    "html_url": "https://github.com/owner/repo/actions/runs/10/job/20", "steps": []}
        self.check = {"name": "unit", "state": "SUCCESS", "bucket": "pass", "link": self.job["html_url"],
                      "startedAt": self.job["started_at"], "completedAt": self.job["completed_at"]}
        self.reviewers = {"codex": {"state": "approved", "in_progress": False},
                          "coderabbit": {"state": "approved", "in_progress": False}}

    def api(self, run=None, jobs=None, attempt_jobs=None, checks=None, after=None):
        run = copy.deepcopy(self.run if run is None else run)
        jobs = copy.deepcopy([self.job] if jobs is None else jobs)
        attempt_jobs = copy.deepcopy(jobs if attempt_jobs is None else attempt_jobs)
        check_objects = {job["id"]: {**job, "id": job["id"]} for job in jobs}
        check_objects.update(checks or {})
        run_reads = 0

        def gh(args, accepted=(0,)):
            nonlocal run_reads
            if args == ["auth", "status"]:
                return Result()
            path = args[-1]
            if "/actions/runs?" in path:
                self.assertIn("--paginate", args)
                self.assertIn("--slurp", args)
                value = [{"workflow_runs": []}, {"workflow_runs": [run]}]
            elif path.endswith(f"/actions/runs/{run['id']}"):
                run_reads += 1
                value = after if after is not None and run_reads % 2 == 0 else run
            elif "/attempts/" in path:
                value = [{"jobs": attempt_jobs}]
            elif "/jobs?" in path:
                self.assertIn("--paginate", args)
                value = [{"jobs": jobs[:1]}, {"jobs": jobs[1:]}]
            elif "/check-runs/" in path:
                value = check_objects[int(path.rsplit("/", 1)[1])]
            else:
                raise AssertionError(path)
            return Result(stdout=json.dumps(value))
        return gh

    def collect(self, **kwargs):
        with mock.patch.object(wait_for_pr_state, "gh", side_effect=self.api(**kwargs)):
            return wait_for_pr_state.read_native_actions("owner/repo", self.head, [self.check])

    def test_terminal_parent_and_completed_steps_cannot_complete_a_running_child(self):
        pending = {**self.job, "status": "in_progress", "conclusion": None, "completed_at": None,
                   "steps": [{"name": "Complete job", "status": "completed", "conclusion": "success", "completed_at": "2026-08-09T18:04:59Z"}]}
        result = self.collect(jobs=[pending])
        problem = next(item for item in result["blockers"] if item["code"] == "native_state_inconsistent")
        self.assertEqual(10, problem["run_id"])
        self.assertEqual(1, problem["run_attempt"])
        self.assertEqual(20, problem["job_id"])
        self.assertEqual("2026-08-09T18:04:59Z", problem["last_step_completed_at"])
        self.assertIn("Do not infer success", problem["next_action"])
        completed = self.collect()
        self.assertEqual([], completed["blockers"])
        self.assertNotEqual(result["fingerprint"], completed["fingerprint"])

    def test_jobs_and_checks_must_agree_on_native_completion(self):
        for status, conclusion in [("in_progress", None), ("completed", "failure")]:
            result = self.collect(checks={20: {**self.job, "status": status, "conclusion": conclusion}})
            self.assertIn("native_state_inconsistent", [item["code"] for item in result["blockers"]])

    def test_distinct_jobs_can_share_a_display_name(self):
        second = {**self.job, "id": 21, "check_run_url": "https://api.github.com/repos/owner/repo/check-runs/21"}
        result = self.collect(jobs=[self.job, second])
        self.assertEqual([], result["blockers"])
        self.assertEqual([20, 21], [job["id"] for job in result["runs"][0]["jobs"]])

    def test_duplicate_job_or_check_identity_still_blocks(self):
        for second in [self.job, {**self.job, "id": 21}]:
            with self.subTest(second=second):
                result = self.collect(jobs=[self.job, second])
                self.assertIn("native_identity_mismatch", [item["code"] for item in result["blockers"]])

    def test_trusted_deferred_controller_uses_its_associated_base_sha_for_jobs(self):
        base = "b" * 40
        run = {**self.run, "event": "pull_request_target", "head_sha": base,
               "path": ".github/workflows/validation.yml", "pull_requests": [{"number": 7,
                   "url": "https://api.github.com/repos/owner/repo/pulls/7",
                   "head": {"sha": self.head}, "base": {"sha": base}}]}
        check = {**self.check, "link": run["html_url"], "bucket": "pending", "state": "PENDING"}
        status = {"context": "unit", "state": "pending", "description": "Awaiting validation.",
                  "target_url": run["html_url"], "creator": {"login": "github-actions[bot]"}}
        self.assertTrue(wait_for_pr_state.is_deferred_status(
            self.pr, check, status, run, ["unit"], "validation.yml", "Awaiting validation."))
        deferred = {**check, "deferred": True, "controller_head_sha": base,
                    "controller_path": run["path"], "controller_pr_url": self.pr["url"],
                    "controller_pr_head_sha": self.head, "controller_attempt": 1}
        job = {**self.job, "head_sha": base}
        with mock.patch.object(wait_for_pr_state, "gh", side_effect=self.api(run=run, jobs=[job])):
            result = wait_for_pr_state.read_native_actions("owner/repo", self.head, [deferred])
        self.assertEqual([], result["blockers"])
        self.assertEqual([], result["failures"])
        for changes in [{"number": 8}, {"head": {"sha": "c" * 40}},
                        {"url": "https://api.github.com/repos/other/repo/pulls/7"}, {"base": {"sha": "c" * 40}}]:
            invalid = {**run, "pull_requests": [{**run["pull_requests"][0], **changes}]}
            self.assertFalse(wait_for_pr_state.is_deferred_status(
                self.pr, check, status, invalid, ["unit"], "validation.yml", "Awaiting validation."))
            with mock.patch.object(wait_for_pr_state, "gh", side_effect=self.api(run=invalid, jobs=[job])):
                rejected = wait_for_pr_state.read_native_actions("owner/repo", self.head, [deferred])
            self.assertIn("native_identity_mismatch", [item["code"] for item in rejected["blockers"]])

    def test_queued_new_attempt_with_only_older_checks_never_passes(self):
        run = {**self.run, "run_attempt": 2, "status": "queued", "conclusion": None}
        result = self.collect(run=run, attempt_jobs=[])
        self.assertEqual({"native_run_pending", "native_attempt_registration_pending"}, {item["code"] for item in result["blockers"]})
        self.assertEqual([], result["failures"])

    def test_failed_only_rerun_retains_successful_prior_jobs_and_paginates(self):
        rerun = {**self.job, "id": 21, "name": "previously-failed", "run_attempt": 2,
                 "check_run_url": "https://api.github.com/repos/owner/repo/check-runs/21"}
        result = self.collect(run={**self.run, "run_attempt": 2}, jobs=[self.job, rerun], attempt_jobs=[rerun])
        self.assertEqual([], result["blockers"])
        self.assertEqual([], result["failures"])
        self.assertEqual([1, 2], [job["run_attempt"] for job in result["runs"][0]["jobs"]])

    def test_old_failed_jobs_cannot_substitute_for_missing_rerun_registration(self):
        result = self.collect(run={**self.run, "run_attempt": 2}, jobs=[{**self.job, "conclusion": "failure"}], attempt_jobs=[])
        self.assertIn("native_attempt_registration_pending", [item["code"] for item in result["blockers"]])

    def test_changed_attempt_or_cross_head_child_invalidates_collection(self):
        result = self.collect(after={**self.run, "run_attempt": 2})
        self.assertIn("native_run_changed_during_collection", [item["code"] for item in result["blockers"]])
        result = self.collect(jobs=[{**self.job, "head_sha": "b" * 40}])
        self.assertIn("native_identity_mismatch", [item["code"] for item in result["blockers"]])

    def test_native_failures_remain_failures_when_the_rollup_is_green(self):
        result = self.collect(run={**self.run, "conclusion": "failure"}, jobs=[{**self.job, "conclusion": "failure"}])
        self.assertEqual([], result["blockers"])
        receipt = wait_for_pr_state.terminal_receipt(repo="owner/repo", pr=self.pr, head=self.head,
            checks=[self.check], watermark=(), reviewers=self.reviewers, reviewer_pending=(),
            approval_fast_path=False, coderabbit_deferred_current_cycle=False, stable_seconds=180,
            stable_source="live", native=result)
        self.assertFalse(receipt["all_checks_nonblocking"])
        self.assertFalse(receipt["all_checks_passed"])
        self.assertEqual(2, len(receipt["blocking_checks"]))

    def test_incomplete_and_malformed_api_evidence_is_not_an_empty_success(self):
        for value in ["not-json", "{}", '[{"workflow_runs": null}]']:
            with mock.patch.object(wait_for_pr_state, "gh", return_value=Result(stdout=value)):
                with self.assertRaises(wait_for_pr_state.RetryableGateError):
                    wait_for_pr_state.read_native_actions("owner/repo", self.head, [self.check])

    def test_receipt_revalidation_rejects_native_attempt_drift(self):
        baseline_native = self.collect()
        baseline = wait_for_pr_state.terminal_receipt(repo="owner/repo", pr=self.pr, head=self.head,
            checks=[self.check], watermark=(), reviewers=self.reviewers, reviewer_pending=(),
            approval_fast_path=False, coderabbit_deferred_current_cycle=False, stable_seconds=180,
            stable_source="live", native=baseline_native)
        job = {**self.job, "run_attempt": 2}
        with mock.patch.object(wait_for_pr_state, "gh", side_effect=self.api(run={**self.run, "run_attempt": 2}, jobs=[job])), \
             mock.patch.object(wait_for_pr_state, "read_pr", return_value=self.pr), \
             mock.patch.object(wait_for_pr_state, "read_checks", return_value=[self.check]):
            with self.assertRaisesRegex(wait_for_pr_state.GateError, "native Actions fingerprint"):
                wait_for_pr_state.verify_terminal_receipt("owner/repo", self.pr, baseline)

    def test_native_diagnosis_is_immediate_but_wait_continues_until_recovery(self):
        queued = self.api(run={**self.run, "run_attempt": 2, "status": "queued", "conclusion": None}, attempt_jobs=[])
        completed = self.api(run={**self.run, "run_attempt": 2}, jobs=[{**self.job, "run_attempt": 2}])
        clock = [0]
        sleeps = []
        output, diagnostics = io.StringIO(), io.StringIO()
        def sleep(seconds):
            self.assertEqual("", output.getvalue())
            self.assertIn("native_attempt_registration_pending", diagnostics.getvalue())
            sleeps.append(seconds)
            clock[0] += seconds
        with mock.patch.object(wait_for_pr_state, "gh", side_effect=lambda *a, **k: (queued if not sleeps else completed)(*a, **k)), \
             mock.patch.object(wait_for_pr_state, "read_pr", return_value=self.pr), \
             mock.patch.object(wait_for_pr_state, "read_checks", side_effect=lambda *a: [] if not sleeps else [self.check]), \
             mock.patch.object(wait_for_pr_state, "read_activity_snapshot", return_value=((), self.reviewers)), \
             mock.patch.object(wait_for_pr_state.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(wait_for_pr_state.time, "sleep", side_effect=sleep), \
             mock.patch.object(sys, "argv", ["wait_for_pr_state.py", "--repo", "owner/repo", "--pr", "7", "--registration-grace-seconds", "0"]), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(diagnostics):
            wait_for_pr_state.main()
        receipt = json.loads(output.getvalue())
        self.assertEqual([120], sleeps)
        self.assertEqual("terminal", receipt["gate"])
        self.assertEqual(2, receipt["native_actions"][0]["run_attempt"])
        self.assertTrue(receipt["all_checks_nonblocking"])

    def test_deadline_produces_a_nonterminal_identity_receipt(self):
        pending = {**self.job, "status": "in_progress", "conclusion": None}
        clock = [0]
        output, diagnostics = io.StringIO(), io.StringIO()
        with mock.patch.object(wait_for_pr_state, "gh", side_effect=self.api(jobs=[pending])), \
             mock.patch.object(wait_for_pr_state, "read_pr", return_value=self.pr), \
             mock.patch.object(wait_for_pr_state, "read_checks", return_value=[self.check]), \
             mock.patch.object(wait_for_pr_state.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(wait_for_pr_state.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
             mock.patch.object(sys, "argv", ["wait_for_pr_state.py", "--repo", "owner/repo", "--pr", "7", "--max-wait-seconds", "3"]), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(diagnostics):
            with self.assertRaises(wait_for_pr_state.WaitDeadlineExceeded):
                wait_for_pr_state.main()
        receipt = json.loads(output.getvalue())
        self.assertEqual("blocked", receipt["gate"])
        self.assertEqual(self.head, receipt["head_sha"])
        self.assertEqual(20, receipt["blockers"][0]["job_id"])
        self.assertEqual(3, clock[0])
        self.assertIn("native_actions_blocked", diagnostics.getvalue())

    def test_api_timeouts_respect_remaining_observation_budget(self):
        with mock.patch.object(wait_for_pr_state, "_deadline", 10), \
             mock.patch.object(wait_for_pr_state.time, "monotonic", side_effect=[5, 10]), \
             mock.patch.object(wait_for_pr_state.subprocess, "run", side_effect=subprocess.TimeoutExpired("gh", 5)) as call:
            with self.assertRaises(wait_for_pr_state.WaitDeadlineExceeded):
                wait_for_pr_state.gh(["api", "repos/owner/repo/actions/runs/10"])
            self.assertEqual(5, call.call_args.kwargs["timeout"])

    def test_populated_checks_shrink_during_rerun_without_opening_the_gate(self):
        other = {**self.check, "name": "integration", "bucket": "pending", "state": "IN_PROGRESS"}
        rerun_job = {**self.job, "id": 21, "name": "integration", "run_attempt": 2,
                     "check_run_url": "https://api.github.com/repos/owner/repo/check-runs/21"}
        responses = [self.api(run={**self.run, "status": "in_progress", "conclusion": None}),
                     self.api(run={**self.run, "run_attempt": 2, "status": "queued", "conclusion": None}, attempt_jobs=[]),
                     self.api(run={**self.run, "run_attempt": 2}, jobs=[self.job, rerun_job], attempt_jobs=[rerun_job])]
        clock, stage, sizes = [0], [0], []
        output = io.StringIO()
        def read_checks(*args):
            checks = [self.check] if stage[0] == 1 else [self.check, {**other, **({"bucket": "pass", "state": "SUCCESS"} if stage[0] == 2 else {})}]
            sizes.append(len(checks))
            return checks
        def sleep(seconds):
            self.assertEqual("", output.getvalue())
            self.assertEqual(5400, wait_for_pr_state._deadline)
            clock[0] += seconds
            stage[0] += 1
            self.assertLess(stage[0], 3)
        with mock.patch.object(wait_for_pr_state, "gh", side_effect=lambda *a, **k: responses[stage[0]](*a, **k)), \
             mock.patch.object(wait_for_pr_state, "read_pr", return_value=self.pr), \
             mock.patch.object(wait_for_pr_state, "read_checks", side_effect=read_checks), \
             mock.patch.object(wait_for_pr_state, "read_activity_snapshot", return_value=((), self.reviewers)), \
             mock.patch.object(wait_for_pr_state.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(wait_for_pr_state.time, "sleep", side_effect=sleep), \
             mock.patch.object(sys, "argv", ["wait_for_pr_state.py", "--repo", "owner/repo", "--pr", "7", "--registration-grace-seconds", "0"]), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            wait_for_pr_state.main()
        receipt = json.loads(output.getvalue())
        self.assertEqual([2, 1, 2], sizes)
        self.assertEqual(240, clock[0])
        self.assertEqual(2, receipt["native_actions"][0]["run_attempt"])
        self.assertTrue(receipt["all_checks_nonblocking"])

    def test_pr_head_change_during_collection_discards_old_evidence_without_resetting_deadline(self):
        new_head = "b" * 40
        new_pr = {**self.pr, "headRefOid": new_head}
        new_run = {**self.run, "id": 11, "head_sha": new_head}
        new_job = {**self.job, "id": 22, "run_id": 11, "head_sha": new_head,
                   "check_run_url": "https://api.github.com/repos/owner/repo/check-runs/22"}
        new_check = {**self.check, "link": "https://github.com/owner/repo/actions/runs/11/job/22"}
        old_api, new_api = self.api(), self.api(run=new_run, jobs=[new_job])
        clock, reads, sleeps = [0], [0], []
        output = io.StringIO()
        def read_pr(*args):
            reads[0] += 1
            return self.pr if reads[0] <= 2 else new_pr
        def sleep(seconds):
            self.assertEqual("", output.getvalue())
            self.assertEqual(5400, wait_for_pr_state._deadline)
            sleeps.append(seconds)
            clock[0] += seconds
        with mock.patch.object(wait_for_pr_state, "gh", side_effect=lambda *a, **k: (old_api if reads[0] <= 2 else new_api)(*a, **k)), \
             mock.patch.object(wait_for_pr_state, "read_pr", side_effect=read_pr), \
             mock.patch.object(wait_for_pr_state, "read_checks", side_effect=lambda repo, number, head: [self.check if head == self.head else new_check]), \
             mock.patch.object(wait_for_pr_state, "read_activity_snapshot", return_value=((), self.reviewers)), \
             mock.patch.object(wait_for_pr_state.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(wait_for_pr_state.time, "sleep", side_effect=sleep), \
             mock.patch.object(sys, "argv", ["wait_for_pr_state.py", "--repo", "owner/repo", "--pr", "7", "--registration-grace-seconds", "0"]), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            wait_for_pr_state.main()
        receipt = json.loads(output.getvalue())
        self.assertEqual([120], sleeps)
        self.assertEqual(new_head, receipt["head_sha"])
        self.assertEqual(11, receipt["native_actions"][0]["id"])

    def test_interrupted_first_native_collection_retains_known_identities_and_problems(self):
        pending = {**self.job, "status": "in_progress", "conclusion": None}
        second = {**self.job, "id": 21, "name": "integration",
                  "check_run_url": "https://api.github.com/repos/owner/repo/check-runs/21"}
        for failure in ["deadline", "api"]:
            with self.subTest(failure=failure):
                api = self.api(jobs=[pending, second])
                clock = [0]
                output, diagnostics = io.StringIO(), io.StringIO()
                def gh(args, accepted=(0,)):
                    if args[-1].endswith("/check-runs/21"):
                        if failure == "deadline":
                            clock[0] = 3
                            raise wait_for_pr_state.WaitDeadlineExceeded("deadline while reading check 21")
                        raise wait_for_pr_state.RetryableGateError("native check 21 is unavailable")
                    return api(args, accepted)
                with mock.patch.object(wait_for_pr_state, "gh", side_effect=gh), \
                     mock.patch.object(wait_for_pr_state, "read_pr", return_value=self.pr), \
                     mock.patch.object(wait_for_pr_state, "read_checks", return_value=[self.check]), \
                     mock.patch.object(wait_for_pr_state.time, "monotonic", side_effect=lambda: clock[0]), \
                     mock.patch.object(wait_for_pr_state.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
                     mock.patch.object(sys, "argv", ["wait_for_pr_state.py", "--repo", "owner/repo", "--pr", "7", "--max-wait-seconds", "3"]), \
                     contextlib.redirect_stdout(output), contextlib.redirect_stderr(diagnostics):
                    with self.assertRaises(wait_for_pr_state.WaitDeadlineExceeded):
                        wait_for_pr_state.main()
                receipt = json.loads(output.getvalue())
                self.assertEqual("blocked", receipt["gate"])
                self.assertEqual(self.head, receipt["head_sha"])
                self.assertFalse(receipt["native_collection_complete"])
                self.assertEqual(10, receipt["native_actions"][0]["id"])
                self.assertEqual(1, receipt["native_actions"][0]["run_attempt"])
                self.assertEqual([20, 21], [job["id"] for job in receipt["native_actions"][0]["jobs"]])
                self.assertIn("native_state_inconsistent", [problem["code"] for problem in receipt["blockers"]])
                self.assertIn("native_actions_blocked", diagnostics.getvalue())
                self.assertIn("check 21", receipt.get("collection_error", receipt["deadline_error"]))
                self.assertEqual(3, receipt["elapsed_seconds"])

    def test_receipt_verification_preserves_inconsistencies_and_interrupted_native_evidence(self):
        baseline = wait_for_pr_state.terminal_receipt(repo="owner/repo", pr=self.pr, head=self.head,
            checks=[self.check], watermark=(), reviewers=self.reviewers, reviewer_pending=(),
            approval_fast_path=False, coderabbit_deferred_current_cycle=False, stable_seconds=180,
            stable_source="live", native=self.collect())
        pending = {**self.job, "status": "in_progress", "conclusion": None}
        second = {**self.job, "id": 21, "name": "integration",
                  "check_run_url": "https://api.github.com/repos/owner/repo/check-runs/21"}
        for interrupt in [False, True]:
            with self.subTest(interrupt=interrupt), tempfile.TemporaryDirectory() as directory:
                receipt_path = Path(directory) / "terminal.json"
                receipt_path.write_text(json.dumps(baseline))
                api = self.api(jobs=[pending, second])
                clock = [0]
                output, diagnostics = io.StringIO(), io.StringIO()
                def gh(args, accepted=(0,)):
                    if interrupt and args[-1].endswith("/check-runs/21"):
                        clock[0] = 3
                        raise wait_for_pr_state.WaitDeadlineExceeded("deadline while revalidating check 21")
                    return api(args, accepted)
                with mock.patch.object(wait_for_pr_state, "gh", side_effect=gh), \
                     mock.patch.object(wait_for_pr_state, "read_pr", return_value=self.pr), \
                     mock.patch.object(wait_for_pr_state, "read_checks", return_value=[self.check]), \
                     mock.patch.object(wait_for_pr_state.time, "monotonic", side_effect=lambda: clock[0]), \
                     mock.patch.object(wait_for_pr_state.time, "sleep") as sleep, \
                     mock.patch.object(sys, "argv", ["wait_for_pr_state.py", "--repo", "owner/repo", "--pr", "7", "--max-wait-seconds", "3", "--verify-receipt", str(receipt_path)]), \
                     contextlib.redirect_stdout(output), contextlib.redirect_stderr(diagnostics):
                    with self.assertRaises(wait_for_pr_state.WaitDeadlineExceeded if interrupt else wait_for_pr_state.GateError):
                        wait_for_pr_state.main()
                sleep.assert_not_called()
                self.assertIn("native_state_inconsistent", diagnostics.getvalue())
                if interrupt:
                    receipt = json.loads(output.getvalue())
                    self.assertEqual("blocked", receipt["gate"])
                    self.assertEqual(self.head, receipt["head_sha"])
                    self.assertEqual(7, receipt["pr_number"])
                    self.assertEqual(10, receipt["native_actions"][0]["id"])
                    self.assertEqual(1, receipt["native_actions"][0]["run_attempt"])
                    self.assertEqual([20, 21], [job["id"] for job in receipt["native_actions"][0]["jobs"]])
                    self.assertFalse(receipt["native_collection_complete"])
                    self.assertIn("check 21", receipt["deadline_error"])
                else:
                    self.assertEqual("", output.getvalue())


class WaitForPRStateTest(unittest.TestCase):
    def test_deferred_status_requires_trusted_controller_and_exact_current_head(self):
        pr = {"headRefOid": "a" * 40, "url": "https://github.com/owner/repo/pull/7"}
        check = {"name": "Repository CI", "bucket": "pending", "state": "PENDING",
                 "link": "https://github.com/owner/repo/actions/runs/12"}
        status = {"id": 10, "context": "Repository CI", "state": "pending",
                  "description": "Awaiting validation.", "target_url": check["link"],
                  "creator": {"login": "github-actions[bot]"}}
        run = {"id": 12, "event": "pull_request_target", "head_sha": pr["headRefOid"],
               "path": ".github/workflows/validation.yml", "status": "completed",
               "conclusion": "success", "run_attempt": 1}
        args = (pr, check, status, run, ["Repository CI"], "validation.yml", "Awaiting validation.")
        self.assertTrue(wait_for_pr_state.is_deferred_status(*args))
        for changed_status in [
            {**status, "state": "failure"},
            {**status, "creator": {"login": "somebody"}},
            {**status, "description": "Building the requested test image."},
            {**status, "target_url": "https://github.com/other/repo/actions/runs/12"},
        ]:
            self.assertFalse(wait_for_pr_state.is_deferred_status(pr, check, changed_status, *args[3:]))
        for changes in [
            {"head_sha": "b" * 40}, {"event": "pull_request"},
            {"path": ".github/workflows/other.yml"}, {"status": "in_progress"},
            {"conclusion": "failure"},
        ]:
            self.assertFalse(wait_for_pr_state.is_deferred_status(pr, check, status, {**run, **changes}, *args[4:]))
        self.assertFalse(wait_for_pr_state.is_deferred_status(pr, {**check, "bucket": "fail"}, *args[2:]))

    def test_deferred_status_accepts_only_graphql_verified_actions_bot_identity(self):
        pr = {"headRefOid": "a" * 40, "url": "https://github.com/owner/repo/pull/7"}
        check = {"name": "Repository CI", "bucket": "pending", "state": "PENDING",
                 "link": "https://github.com/owner/repo/actions/runs/12"}
        status = {"id": 10, "context": "Repository CI", "state": "pending",
                  "description": "Awaiting validation.", "target_url": check["link"],
                  "creator": {"login": "github-actions", "__typename": "Bot"}}
        run = {"id": 12, "event": "pull_request_target", "head_sha": pr["headRefOid"],
               "path": ".github/workflows/validation.yml", "status": "completed",
               "conclusion": "success", "run_attempt": 1}
        args = (pr, check, status, run, ["Repository CI"], "validation.yml", "Awaiting validation.")

        self.assertTrue(wait_for_pr_state.is_deferred_status(*args))
        for creator in [
            {"login": "github-actions"},
            {"login": "github-actions", "__typename": "User"},
            {"login": "somebody", "__typename": "Bot"},
        ]:
            self.assertFalse(
                wait_for_pr_state.is_deferred_status(pr, check, {**status, "creator": creator}, *args[3:])
            )

    def test_missing_rest_creator_is_hydrated_only_from_matching_graphql_bot_status(self):
        pr = {"headRefOid": "a" * 40, "url": "https://github.com/owner/repo/pull/7"}
        check = {"name": "Repository CI", "bucket": "pending", "state": "PENDING",
                 "link": "https://github.com/owner/repo/actions/runs/12"}
        status = {"id": 10, "context": "Repository CI", "state": "pending",
                  "description": "Awaiting validation.", "target_url": check["link"],
                  "created_at": "2026-08-09T19:00:00Z", "creator": None}
        run = {"id": 12, "event": "pull_request_target", "head_sha": pr["headRefOid"],
               "path": ".github/workflows/validation.yml", "status": "completed",
               "conclusion": "success", "run_attempt": 1}
        checks = [{"name": "CodeQL", "bucket": "pass"}, check]
        policy = {"statuses": ["Repository CI"], "workflow": "validation.yml",
                  "description": "Awaiting validation."}
        response = {"data": {"repository": {"object": {"statusCheckRollup": {
            "contexts": {"nodes": [{
                "__typename": "StatusContext",
                "context": "Repository CI",
                "state": "PENDING",
                "description": "Awaiting validation.",
                "targetUrl": status["target_url"],
                "createdAt": "2026-08-09T19:00:00+00:00",
                "creator": {"__typename": "Bot", "login": "github-actions"},
            }], "pageInfo": {"hasNextPage": False, "endCursor": None}},
        }}}}}
        with mock.patch.object(wait_for_pr_state, "read_commit_statuses", return_value=[status]), \
             mock.patch.object(wait_for_pr_state, "graphql", return_value=response) as graphql, \
             mock.patch.object(wait_for_pr_state, "gh", return_value=Result(stdout=json.dumps(run))):
            classified = wait_for_pr_state.phase_checks("owner/repo", pr, checks, "review", policy)

        self.assertTrue(classified[1]["deferred"])
        self.assertEqual({"login": "github-actions", "__typename": "Bot"}, status["creator"])
        graphql.assert_called_once()

    def test_ambiguous_graphql_status_creator_does_not_defer(self):
        identity = wait_for_pr_state.commit_status_identity({
            "context": "Repository CI", "state": "pending", "description": "Awaiting validation.",
            "target_url": "https://github.com/owner/repo/actions/runs/12",
            "created_at": "2026-08-09T19:00:00Z",
        })
        statuses = [
            {"context": "Repository CI", "state": "PENDING", "description": "Awaiting validation.",
             "targetUrl": "https://github.com/owner/repo/actions/runs/12", "createdAt": "2026-08-09T19:00:00Z",
             "creator": {"login": "github-actions", "__typename": "Bot"}},
            {"context": "Repository CI", "state": "PENDING", "description": "Awaiting validation.",
             "targetUrl": "https://github.com/owner/repo/actions/runs/12", "createdAt": "2026-08-09T19:00:00Z",
             "creator": {"login": "somebody", "__typename": "User"}},
        ]

        with mock.patch.object(wait_for_pr_state, "graphql", side_effect=[
            {"data": {"repository": {"object": {"statusCheckRollup": {"contexts": {
                "nodes": [statuses[0]], "pageInfo": {"hasNextPage": True, "endCursor": "next"},
            }}}}}},
            {"data": {"repository": {"object": {"statusCheckRollup": {"contexts": {
                "nodes": [statuses[1]], "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}}}},
        ]) as graphql:
            creators = wait_for_pr_state.read_commit_status_context_creators("owner/repo", "a" * 40)

        self.assertEqual({}, creators)
        self.assertEqual(2, graphql.call_count)

    def test_required_checks_reject_absent_skipped_and_failed_results(self):
        required = ["Repository CI", "CodeQL"]
        checks = [{"name": name, "bucket": "pass"} for name in required]
        self.assertEqual([], wait_for_pr_state.required_check_problems(checks, required))
        for bucket in ["pending", "skipping", "fail", "cancel"]:
            bad = [{"name": "Repository CI", "bucket": bucket}, checks[1]]
            self.assertEqual(["Repository CI"], [entry["name"] for entry in wait_for_pr_state.required_check_problems(bad, required)])
        problem = wait_for_pr_state.required_check_problems(checks[1:], required)
        self.assertEqual("missing", problem[0]["reason"])

    def test_phase_classification_never_hides_a_running_controller(self):
        pr = {"headRefOid": "a" * 40, "url": "https://github.com/owner/repo/pull/7"}
        checks = [{"name": "CodeQL", "bucket": "pass"},
                  {"name": "Repository CI", "bucket": "pending", "state": "PENDING",
                   "link": "https://github.com/owner/repo/actions/runs/12"}]
        status = {"id": 10, "context": "Repository CI", "state": "pending",
                  "description": "Awaiting validation.", "target_url": checks[1]["link"],
                  "creator": {"login": "github-actions[bot]"}}
        policy = {"statuses": ["Repository CI"], "workflow": "validation.yml", "description": "Awaiting validation."}
        for state, expected in [("completed", True), ("in_progress", False)]:
            run = {"id": 12, "event": "pull_request_target", "head_sha": pr["headRefOid"],
                   "path": ".github/workflows/validation.yml", "status": state,
                   "conclusion": "success" if expected else None, "run_attempt": 1}
            with mock.patch.object(wait_for_pr_state, "gh", side_effect=[
                Result(stdout=json.dumps([[status]])), Result(stdout=json.dumps(run)),
            ]):
                classified = wait_for_pr_state.phase_checks("owner/repo", pr, checks, "review", policy)
            self.assertEqual(2, len(classified))
            self.assertEqual(checks[0], classified[0])
            self.assertEqual(expected, bool(classified[1].get("deferred")))
            self.assertEqual("pending", classified[1]["bucket"])
        self.assertNotIn("deferred", checks[1])

    def test_review_receipt_keeps_deferred_work_distinct_from_a_full_pass(self):
        check = {"name": "Repository CI", "bucket": "pending", "deferred": True}
        receipt = wait_for_pr_state.terminal_receipt(
            repo="owner/repo", pr={"number": 7, "url": "url", "headRefName": "branch"},
            head="a" * 40, checks=[{"name": "CodeQL", "bucket": "pass"}, check],
            watermark=(), reviewers={}, reviewer_pending=(), approval_fast_path=False,
            coderabbit_deferred_current_cycle=False, stable_seconds=180, stable_source="live_observation",
            phase="review", required_checks=["CodeQL"],
            deferred_policy={"statuses": ["Repository CI"], "workflow": "validation.yml", "description": "Awaiting validation."},
        )
        self.assertEqual("review_terminal", receipt["gate"])
        self.assertFalse(receipt["all_checks_terminal"])
        self.assertFalse(receipt["all_checks_passed"])
        self.assertTrue(receipt["phase_checks_nonblocking"])
        self.assertEqual([check], receipt["deferred_checks"])
        with self.assertRaisesRegex(wait_for_pr_state.GateError, "phase"):
            wait_for_pr_state.verify_terminal_receipt("owner/repo", {"number": 7}, receipt)

    def test_deferred_identity_changes_the_complete_check_fingerprint(self):
        check = {"name": "Repository CI", "bucket": "pending", "deferred": True, "status_id": 1}
        self.assertNotEqual(wait_for_pr_state.check_fingerprint([check]),
                            wait_for_pr_state.check_fingerprint([{**check, "status_id": 2}]))
        self.assertNotEqual(wait_for_pr_state.check_fingerprint([check]),
                            wait_for_pr_state.check_fingerprint([{**check, "deferred": False}]))

    def test_defaults_use_two_minute_poll_three_minute_quiet_and_ninety_minute_bound(self):
        with mock.patch.object(sys, "argv", ["wait_for_pr_state.py"]):
            args = wait_for_pr_state.parse_args()

        self.assertEqual(120, args.poll_seconds)
        self.assertEqual(180, args.quiet_seconds)
        self.assertEqual(5400, args.max_wait_seconds)

    def test_next_sleep_stops_at_quiet_and_timeout_boundaries(self):
        self.assertEqual(
            60,
            wait_for_pr_state.next_sleep_seconds(
                120,
                quiet_remaining=60,
                registration_remaining=90,
                timeout_remaining=300,
            ),
        )
        self.assertEqual(
            15,
            wait_for_pr_state.next_sleep_seconds(120, timeout_remaining=15),
        )

    def test_read_checks_enriches_latest_status_attempt_timestamps(self):
        checks = [
            {
                "name": "AI PR review",
                "state": "SUCCESS",
                "bucket": "pass",
                "startedAt": "0001-01-01T00:00:00Z",
                "completedAt": "0001-01-01T00:00:00Z",
            }
        ]
        statuses = [
            {
                "id": 1,
                "context": "AI PR review",
                "state": "pending",
                "created_at": "2026-08-09T18:00:00Z",
                "updated_at": "2026-08-09T18:00:00Z",
            },
            {
                "id": 2,
                "context": "AI PR review",
                "state": "success",
                "created_at": "2026-08-09T18:05:00Z",
                "updated_at": "2026-08-09T18:05:00Z",
            },
            {
                "id": 3,
                "context": "AI PR review",
                "state": "pending",
                "created_at": "2026-08-09T19:00:00Z",
                "updated_at": "2026-08-09T19:00:00Z",
            },
            {
                "id": 4,
                "context": "AI PR review",
                "state": "success",
                "created_at": "2026-08-09T19:05:00Z",
                "updated_at": "2026-08-09T19:05:00Z",
            },
        ]
        with mock.patch.object(
            wait_for_pr_state,
            "gh",
            side_effect=[
                Result(stdout=json.dumps(checks)),
                Result(stdout=json.dumps([statuses])),
            ],
        ) as gh:
            enriched = wait_for_pr_state.read_checks("owner/repo", 7, "head")

        self.assertEqual("2026-08-09T19:00:00Z", enriched[0]["startedAt"])
        self.assertEqual("2026-08-09T19:05:00Z", enriched[0]["completedAt"])
        self.assertIn("commits/head/statuses", gh.call_args_list[1].args[0][-1])

    def test_terminal_failure_is_not_success(self):
        checks = [
            {"name": "unit", "bucket": "fail"},
            {"name": "lint", "bucket": "pass"},
        ]
        pending = [
            check
            for check in checks
            if check["bucket"] not in wait_for_pr_state.TERMINAL_BUCKETS
        ]

        status = wait_for_pr_state.wait_status(
            checks, pending, 300, "live_observation", 300
        )

        self.assertIn("all_terminal", status)
        self.assertIn("blocking=1", status)
        self.assertNotIn("successful", status)

    def test_check_fingerprint_change_resets_stability(self):
        first = [{"name": "unit", "bucket": "pass", "completedAt": "one"}]
        later = [
            {"name": "unit", "bucket": "pass", "completedAt": "one"},
            {"name": "lint", "bucket": "pass", "completedAt": "two"},
        ]
        watermark = (("issue_comment", "1", "2026-08-09T19:00:00Z", "bot", ""),)

        state = wait_for_pr_state.observe_stable_period(
            first,
            [],
            wait_for_pr_state.check_fingerprint(first),
            watermark,
            False,
            (),
            (),
            None,
            100,
        )
        self.assertEqual(0, state[-1])

        state = wait_for_pr_state.observe_stable_period(
            later,
            [],
            wait_for_pr_state.check_fingerprint(later),
            watermark,
            state[0],
            state[1],
            state[2],
            state[3],
            400,
        )

        self.assertEqual(400, state[3])
        self.assertEqual(0, state[-1])

    def test_new_comment_resets_stability(self):
        checks = [{"name": "unit", "bucket": "pass"}]
        fingerprint = wait_for_pr_state.check_fingerprint(checks)
        first = (("issue_comment", "1", "2026-08-09T19:00:00Z", "bot", ""),)
        newer = first + (
            ("review", "2", "2026-08-09T19:05:00Z", "reviewer", "APPROVED"),
        )
        state = wait_for_pr_state.observe_stable_period(
            checks, [], fingerprint, first, False, (), (), None, 100
        )

        state = wait_for_pr_state.observe_stable_period(
            checks,
            [],
            fingerprint,
            newer,
            state[0],
            state[1],
            state[2],
            state[3],
            400,
        )

        self.assertEqual(400, state[3])
        self.assertEqual(0, state[-1])

    def test_activity_watermark_includes_every_source_and_thread_state(self):
        issue = [
            {
                "id": "I1",
                "createdAt": "2026-08-09T19:00:00Z",
                "updatedAt": "2026-08-09T19:01:00Z",
                "author": {"login": "human"},
            }
        ]
        review = [
            {
                "id": "R1",
                "state": "COMMENTED",
                "submittedAt": "2026-08-09T19:02:00Z",
                "updatedAt": "2026-08-09T19:03:00Z",
                "author": {"login": "review-bot"},
            }
        ]
        thread = [
            {
                "id": "T1",
                "isResolved": False,
                "isOutdated": False,
                "comments": {
                    "nodes": [
                        {
                            "id": "C1",
                            "createdAt": "2026-08-09T19:04:00Z",
                            "updatedAt": "2026-08-09T19:04:00Z",
                            "author": {"login": "inline-bot"},
                        }
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }
        ]
        reactions = [
            {
                "id": "E1",
                "content": "EYES",
                "createdAt": "2026-08-09T19:05:00Z",
                "user": {"login": "chatgpt-codex-connector[bot]"},
            }
        ]
        with mock.patch.object(
            wait_for_pr_state,
            "fetch_connection",
            side_effect=[issue, review, thread, reactions],
        ):
            watermark, reviewers = wait_for_pr_state.read_activity_snapshot(
                "owner/repo", 12, "head"
            )

        self.assertEqual(
            {
                "issue_comment",
                "review",
                "review_thread",
                "review_comment",
                "pr_reaction",
            },
            {entry[0] for entry in watermark},
        )
        self.assertTrue(any(entry[3] == "human" for entry in watermark))
        self.assertTrue(any('"is_resolved":false' in entry[4] for entry in watermark))
        self.assertEqual("in_progress", reviewers["codex"]["state"])
        self.assertEqual("not_observed", reviewers["coderabbit"]["state"])

    def test_activity_snapshot_recognizes_no_findings_approvals(self):
        comments = [
            {
                "id": "summary",
                "body": "No actionable comments were generated in the recent review. 🎉",
                "createdAt": "2026-08-11T20:02:18Z",
                "updatedAt": "2026-08-11T20:02:18Z",
                "author": {"login": "coderabbitai[bot]"},
            }
        ]
        reactions = [
            {
                "id": "approval",
                "content": "THUMBS_UP",
                "createdAt": "2026-08-11T19:59:43Z",
                "user": {"login": "chatgpt-codex-connector[bot]"},
            }
        ]
        with mock.patch.object(
            wait_for_pr_state,
            "fetch_connection",
            side_effect=[comments, [], [], reactions],
        ):
            _watermark, reviewers = wait_for_pr_state.read_activity_snapshot(
                "owner/repo", 200, "head"
            )

        self.assertEqual("approved", reviewers["codex"]["state"])
        self.assertEqual("thumbs_up", reviewers["codex"]["terminal_signal"])
        self.assertEqual("approved", reviewers["coderabbit"]["state"])
        self.assertEqual(
            "approval_comment", reviewers["coderabbit"]["terminal_signal"]
        )

    def test_reviewer_in_progress_prevents_stability(self):
        checks = [{"name": "unit", "bucket": "pass"}]
        state = wait_for_pr_state.observe_stable_period(
            checks,
            [],
            wait_for_pr_state.check_fingerprint(checks),
            (),
            False,
            (),
            (),
            None,
            100,
            ["codex"],
        )

        self.assertFalse(state[0])
        self.assertIsNone(state[3])
        self.assertIn(
            "reviewers_in_progress=codex",
            wait_for_pr_state.wait_status(
                checks, [], 0, "live_observation", 300, ["codex"]
            ),
        )

    def test_pending_optional_check_prevents_stability(self):
        checks = [
            {"name": "required", "bucket": "pass"},
            {"name": "optional-observer", "bucket": "pending"},
        ]
        pending = [checks[1]]

        state = wait_for_pr_state.observe_stable_period(
            checks,
            pending,
            wait_for_pr_state.check_fingerprint(checks),
            (),
            False,
            (),
            (),
            None,
            100,
        )

        self.assertFalse(state[0])
        self.assertIsNone(state[3])

    def test_undated_thread_state_is_fingerprinted_without_disabling_history(self):
        checks = [
            {
                "bucket": "pass",
                "completedAt": "2026-08-09T19:00:00Z",
            }
        ]
        watermark = (("review_thread", "T1", "", "", "unresolved"),)

        quiet = wait_for_pr_state.historical_stable_seconds(
            checks,
            watermark,
            datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(3600, quiet)

    def test_sentinel_check_timestamp_disables_historical_fast_path(self):
        checks = [
            {
                "bucket": "pass",
                "completedAt": "0001-01-01T00:00:00Z",
            }
        ]

        quiet = wait_for_pr_state.historical_stable_seconds(
            checks,
            (),
            datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc),
        )

        self.assertIsNone(quiet)

    def test_current_cycle_dual_approval_enables_reviewer_fast_path(self):
        checks = [
            {
                "bucket": "pass",
                "startedAt": "2026-08-11T19:56:00Z",
                "completedAt": "2026-08-11T20:00:45Z",
            },
            {
                "bucket": "pass",
                "startedAt": "0001-01-01T00:00:00Z",
                "completedAt": "0001-01-01T00:00:00Z",
            },
        ]
        reviewers = {
            "codex": {
                "state": "approved",
                "terminal_signal": "thumbs_up",
                "latest_terminal_at": "2026-08-11T19:59:43Z",
            },
            "coderabbit": {
                "state": "approved",
                "terminal_signal": "approval_comment",
                "latest_terminal_at": "2026-08-11T20:02:18Z",
            },
        }

        self.assertTrue(
            wait_for_pr_state.current_cycle_dual_approval(checks, reviewers)
        )

    def test_stale_reviewer_approval_does_not_enable_fast_path(self):
        checks = [
            {
                "bucket": "pass",
                "startedAt": "2026-08-11T19:56:00Z",
                "completedAt": "2026-08-11T20:00:45Z",
            }
        ]
        reviewers = {
            "codex": {
                "state": "approved",
                "terminal_signal": "thumbs_up",
                "latest_terminal_at": "2026-08-11T19:55:59Z",
            },
            "coderabbit": {
                "state": "approved",
                "terminal_signal": "approval_comment",
                "latest_terminal_at": "2026-08-11T20:02:18Z",
            },
        }

        self.assertFalse(
            wait_for_pr_state.current_cycle_dual_approval(checks, reviewers)
        )

    def test_current_cycle_coderabbit_deferral_is_terminal_not_approval(self):
        checks = [
            {
                "bucket": "pass",
                "startedAt": "2026-08-11T19:56:00Z",
                "completedAt": "2026-08-11T20:00:45Z",
            }
        ]
        reviewers = wait_for_pr_state.summarize_reviewers(
            reactions=[
                {
                    "content": "THUMBS_UP",
                    "createdAt": "2026-08-11T19:59:43Z",
                    "user": {"login": "chatgpt-codex-connector[bot]"},
                }
            ],
            reviews=[],
            comments=[
                {
                    "body": "> ## Review limit reached\n> **Next included review available in 13 minutes.**",
                    "updatedAt": "2026-08-11T20:02:18Z",
                    "author": {"login": "coderabbitai[bot]"},
                }
            ],
            review_comments=[],
            current_head="head",
        )

        self.assertTrue(
            wait_for_pr_state.current_cycle_coderabbit_deferral(checks, reviewers)
        )
        self.assertFalse(
            wait_for_pr_state.current_cycle_dual_approval(checks, reviewers)
        )
        self.assertFalse(
            wait_for_pr_state.current_cycle_coderabbit_deferral(
                [*checks, {"bucket": "pending"}], reviewers
            )
        )

    def test_stale_or_undated_coderabbit_deferral_does_not_apply_to_new_cycle(self):
        checks = [
            {
                "bucket": "pass",
                "startedAt": "2026-08-11T19:56:00Z",
                "completedAt": "2026-08-11T20:00:45Z",
            }
        ]
        for timestamp in ("2026-08-11T19:55:59Z", "not-a-time", ""):
            with self.subTest(timestamp=timestamp):
                reviewers = wait_for_pr_state.summarize_reviewers(
                    reactions=[],
                    reviews=[],
                    comments=[
                        {
                            "body": "Review limit reached",
                            "updatedAt": timestamp,
                            "author": {"login": "coderabbitai[bot]"},
                        }
                    ],
                    review_comments=[],
                    current_head="new-head",
                )

                self.assertEqual("deferred", reviewers["coderabbit"]["state"])
                self.assertFalse(
                    wait_for_pr_state.current_cycle_coderabbit_deferral(checks, reviewers)
                )

    def test_status_check_without_coderabbit_summary_does_not_enable_fast_path(self):
        checks = [
            {
                "bucket": "pass",
                "startedAt": "2026-08-11T19:56:00Z",
                "completedAt": "2026-08-11T20:00:45Z",
            }
        ]
        reviewers = {
            "codex": {
                "state": "approved",
                "terminal_signal": "thumbs_up",
                "latest_terminal_at": "2026-08-11T19:59:43Z",
            },
            "coderabbit": {
                "state": "observed",
                "terminal_signal": None,
                "latest_terminal_at": None,
            },
        }

        self.assertFalse(
            wait_for_pr_state.current_cycle_dual_approval(checks, reviewers)
        )

    def test_current_cycle_dual_approval_opens_gate_without_quiet_wait(self):
        pr = {
            "number": 200,
            "url": "https://github.com/example/repo/pull/200",
            "headRefOid": "abc123",
            "headRefName": "feature",
            "state": "OPEN",
            "isDraft": False,
        }
        checks = [
            {
                "name": "unit",
                "state": "SUCCESS",
                "bucket": "pass",
                "link": "https://github.com/example/repo/actions/runs/1/job/2",
                "startedAt": "2026-08-11T19:56:00Z",
                "completedAt": "2026-08-11T20:00:45Z",
                "workflow": "CI",
                "event": "pull_request",
            }
        ]
        reviewers = {
            "codex": {
                "state": "approved",
                "in_progress": False,
                "terminal_signal": "thumbs_up",
                "latest_terminal_at": "2026-08-11T19:59:43Z",
            },
            "coderabbit": {
                "state": "approved",
                "in_progress": False,
                "terminal_signal": "approval_comment",
                "latest_terminal_at": "2026-08-11T20:02:18Z",
            },
        }
        output = io.StringIO()
        with (
            mock.patch.object(wait_for_pr_state, "gh", return_value=Result()),
            mock.patch.object(wait_for_pr_state, "read_pr", side_effect=[pr, pr, pr]),
            mock.patch.object(wait_for_pr_state, "read_checks", return_value=checks),
            mock.patch.object(wait_for_pr_state, "read_native_actions", return_value={"runs": [], "blockers": [], "failures": [], "fingerprint": wait_for_pr_state.stable_hash([])}),
            mock.patch.object(
                wait_for_pr_state,
                "read_activity_snapshot",
                return_value=((), reviewers),
            ),
            mock.patch.object(
                wait_for_pr_state, "historical_stable_seconds", return_value=None
            ),
            mock.patch.object(wait_for_pr_state.time, "sleep") as sleep,
            mock.patch.object(
                sys,
                "argv",
                [
                    "wait_for_pr_state.py",
                    "--repo",
                    "example/repo",
                    "--pr",
                    "200",
                    "--registration-grace-seconds",
                    "0",
                ],
            ),
            contextlib.redirect_stdout(output),
        ):
            wait_for_pr_state.main()

        receipt = json.loads(output.getvalue())
        self.assertTrue(receipt["reviewer_approval_fast_path"])
        self.assertEqual(0, receipt["stable_seconds"])
        sleep.assert_not_called()

    def test_unchanged_terminal_receipt_revalidates_without_waiting(self):
        pr = {
            "number": 200,
            "url": "https://github.com/example/repo/pull/200",
            "headRefOid": "abc123",
            "headRefName": "feature",
            "state": "OPEN",
            "isDraft": False,
        }
        checks = [
            {
                "name": "unit",
                "state": "SUCCESS",
                "bucket": "pass",
                "startedAt": "2026-08-11T19:56:00Z",
                "completedAt": "2026-08-11T20:00:45Z",
            }
        ]
        watermark = (
            ("review", "R1", "2026-08-11T20:02:18Z", "reviewer", "COMMENTED"),
        )
        reviewers = {
            "codex": {"state": "approved", "in_progress": False},
            "coderabbit": {"state": "approved", "in_progress": False},
        }
        baseline = {
            "gate": "terminal",
            "repo": "example/repo",
            "pr_number": 200,
            "head_sha": "abc123",
            "native_actions_fingerprint": wait_for_pr_state.stable_hash([]),
            "check_fingerprint": wait_for_pr_state.stable_hash(
                wait_for_pr_state.check_fingerprint(checks)
            ),
            "activity_watermark": wait_for_pr_state.watermark_summary(watermark),
        }
        with (
            mock.patch.object(wait_for_pr_state, "read_pr", side_effect=[pr, pr]),
            mock.patch.object(wait_for_pr_state, "read_checks", return_value=checks),
            mock.patch.object(wait_for_pr_state, "read_native_actions", return_value={"runs": [], "blockers": [], "failures": [], "fingerprint": wait_for_pr_state.stable_hash([])}),
            mock.patch.object(
                wait_for_pr_state,
                "read_activity_snapshot",
                return_value=(watermark, reviewers),
            ),
        ):
            receipt = wait_for_pr_state.verify_terminal_receipt(
                "example/repo", pr, baseline
            )

        self.assertTrue(receipt["receipt_revalidated"])
        self.assertEqual("receipt_revalidation", receipt["stable_source"])
        self.assertEqual("abc123", receipt["head_sha"])

    def test_changed_activity_rejects_terminal_receipt(self):
        pr = {
            "number": 200,
            "url": "https://github.com/example/repo/pull/200",
            "headRefOid": "abc123",
            "headRefName": "feature",
            "state": "OPEN",
            "isDraft": False,
        }
        checks = [{"name": "unit", "state": "SUCCESS", "bucket": "pass"}]
        baseline_watermark = (
            ("review", "R1", "2026-08-11T20:02:18Z", "reviewer", "COMMENTED"),
        )
        current_watermark = baseline_watermark + (
            ("review_comment", "C2", "2026-08-11T20:03:00Z", "reviewer", ""),
        )
        reviewers = {
            "codex": {"state": "approved", "in_progress": False},
            "coderabbit": {"state": "approved", "in_progress": False},
        }
        baseline = {
            "gate": "terminal",
            "repo": "example/repo",
            "pr_number": 200,
            "head_sha": "abc123",
            "native_actions_fingerprint": wait_for_pr_state.stable_hash([]),
            "check_fingerprint": wait_for_pr_state.stable_hash(
                wait_for_pr_state.check_fingerprint(checks)
            ),
            "activity_watermark": wait_for_pr_state.watermark_summary(
                baseline_watermark
            ),
        }
        with (
            mock.patch.object(wait_for_pr_state, "read_pr", side_effect=[pr, pr]),
            mock.patch.object(wait_for_pr_state, "read_checks", return_value=checks),
            mock.patch.object(wait_for_pr_state, "read_native_actions", return_value={"runs": [], "blockers": [], "failures": [], "fingerprint": wait_for_pr_state.stable_hash([])}),
            mock.patch.object(
                wait_for_pr_state,
                "read_activity_snapshot",
                return_value=(current_watermark, reviewers),
            ),
        ):
            with self.assertRaisesRegex(
                wait_for_pr_state.GateError, "activity fingerprint changed"
            ):
                wait_for_pr_state.verify_terminal_receipt(
                    "example/repo", pr, baseline
                )

    def test_old_terminal_failure_opens_collection_gate_but_receipt_blocks_merge(self):
        pr = {
            "number": 195,
            "url": "https://github.com/example/repo/pull/195",
            "headRefOid": "abc123",
            "headRefName": "feature",
            "state": "OPEN",
            "isDraft": False,
        }
        checks = [
            {
                "name": "unit",
                "state": "FAILURE",
                "bucket": "fail",
                "link": "https://github.com/example/repo/actions/runs/1/job/2",
                "startedAt": "2026-08-09T18:00:00Z",
                "completedAt": "2026-08-09T18:05:00Z",
                "workflow": "CI",
                "event": "pull_request",
            }
        ]
        output = io.StringIO()
        reviewers = {
            "codex": {"state": "approved", "in_progress": False},
            "coderabbit": {"state": "not_observed", "in_progress": False},
        }
        with (
            mock.patch.object(wait_for_pr_state, "gh", return_value=Result()),
            mock.patch.object(wait_for_pr_state, "read_pr", side_effect=[pr, pr, pr]),
            mock.patch.object(wait_for_pr_state, "read_checks", return_value=checks),
            mock.patch.object(wait_for_pr_state, "read_native_actions", return_value={"runs": [], "blockers": [], "failures": [], "fingerprint": wait_for_pr_state.stable_hash([])}),
            mock.patch.object(
                wait_for_pr_state,
                "read_activity_snapshot",
                return_value=((), reviewers),
            ),
            mock.patch.object(wait_for_pr_state, "historical_stable_seconds", return_value=600),
            mock.patch.object(wait_for_pr_state.time, "sleep") as sleep,
            mock.patch.object(
                sys,
                "argv",
                [
                    "wait_for_pr_state.py",
                    "--repo",
                    "example/repo",
                    "--pr",
                    "195",
                ],
            ),
            contextlib.redirect_stdout(output),
        ):
            wait_for_pr_state.main()

        receipt = json.loads(output.getvalue())
        self.assertEqual("terminal", receipt["gate"])
        self.assertTrue(receipt["all_checks_terminal"])
        self.assertFalse(receipt["all_checks_nonblocking"])
        self.assertFalse(receipt["all_checks_passed"])
        self.assertEqual("unit", receipt["blocking_checks"][0]["name"])
        self.assertEqual("approved", receipt["automated_reviewers"]["codex"]["state"])
        self.assertEqual([], receipt["reviewers_in_progress"])
        sleep.assert_not_called()

    def test_skipped_check_is_reported_separately(self):
        checks = [
            {"name": "unit", "bucket": "pass"},
            {"name": "fork-only", "bucket": "skipping"},
        ]
        blocking = [
            check
            for check in checks
            if check["bucket"] not in wait_for_pr_state.NONBLOCKING_BUCKETS
        ]
        skipped = [check for check in checks if check["bucket"] == "skipping"]

        self.assertEqual([], blocking)
        self.assertEqual(["fork-only"], [check["name"] for check in skipped])
        self.assertFalse(all(check["bucket"] == "pass" for check in checks))

    def test_merged_pr_emits_stopped_receipt(self):
        pr = {
            "number": 195,
            "url": "https://github.com/example/repo/pull/195",
            "headRefOid": "abc123",
            "headRefName": "feature",
            "state": "MERGED",
            "isDraft": False,
        }
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            stopped = wait_for_pr_state.stop_if_pr_inactive("example/repo", pr)

        self.assertTrue(stopped)
        receipt = json.loads(stream.getvalue())
        self.assertEqual("stopped", receipt["gate"])
        self.assertEqual("pr_merged_before_gate_completion", receipt["reason"])


if __name__ == "__main__":
    unittest.main()
