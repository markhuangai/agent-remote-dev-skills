from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

import yaml


NODE_RUNNER = """
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const { script, jobs } = JSON.parse(readFileSync(0, 'utf8'));
const failures = [];
const run = new (Object.getPrototypeOf(async function () {}).constructor)(
  'github', 'context', 'core', 'process', script);
await run({
  rest: { actions: { listJobsForWorkflowRun: 'jobs' } },
  paginate: async (endpoint, input) => {
    assert.equal(endpoint, 'jobs');
    assert.deepEqual(input, {
      owner: 'markhuangai', repo: 'agent-remote-dev-skills',
      run_id: 10, filter: 'all', per_page: 100,
    });
    return jobs;
  },
}, { repo: { owner: 'markhuangai', repo: 'agent-remote-dev-skills' }, runId: 10 }, {
  setFailed: message => failures.push(message),
}, { env: { GITHUB_RUN_ATTEMPT: '2' } });
console.log(JSON.stringify(failures));
"""


class CodeQLWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / ".github/workflows/codeql.yml"
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        cls.script = workflow["jobs"]["result"]["steps"][0]["with"]["script"]

    def successful_jobs(self):
        return [{"name": f"Analyze ({language})", "status": "completed",
                 "conclusion": "success", "run_attempt": 2}
                for language in ("actions", "python")]

    def assert_gate(self, jobs, failing_languages=()):
        result = subprocess.run(
            ["node", "--input-type=module", "-e", NODE_RUNNER],
            input=json.dumps({"script": self.script, "jobs": jobs}),
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [
            f"CodeQL {language} has no successful latest execution."
            for language in failing_languages
        ])

    def test_both_languages_succeed_while_aggregate_is_running(self):
        self.assert_gate(self.successful_jobs() + [{
            "name": "CodeQL analysis", "status": "in_progress",
            "conclusion": None, "run_attempt": 2,
        }])

    def test_partial_retry_retains_other_languages_success(self):
        for index in range(2):
            jobs = self.successful_jobs()
            jobs[index]["run_attempt"] = 1
            with self.subTest(language=jobs[index]["name"]):
                self.assert_gate(jobs)

    def test_successful_retry_replaces_older_failure(self):
        for job in self.successful_jobs():
            with self.subTest(language=job["name"]):
                self.assert_gate([{**job, "run_attempt": 1, "conclusion": "failure"}]
                                 + self.successful_jobs())

    def test_latest_failure_cannot_use_older_success(self):
        for index, language in enumerate(("actions", "python")):
            jobs = self.successful_jobs()
            older = {**jobs[index], "run_attempt": 1}
            jobs[index]["conclusion"] = "failure"
            for executions in ([older] + jobs, jobs + [older]):
                with self.subTest(language=language, jobs=executions):
                    self.assert_gate(executions, (language,))

    def test_missing_and_duplicate_languages_fail(self):
        self.assert_gate([], ("actions", "python"))
        for index, language in enumerate(("actions", "python")):
            jobs = self.successful_jobs()
            with self.subTest(language=language, case="missing"):
                self.assert_gate(jobs[:index] + jobs[index + 1:], (language,))
            with self.subTest(language=language, case="duplicate"):
                self.assert_gate(jobs + [jobs[index]], (language,))

    def test_future_attempt_cannot_replace_current_results(self):
        for index, language in enumerate(("actions", "python")):
            jobs = self.successful_jobs()
            future = {**jobs[index], "run_attempt": 3}
            with self.subTest(language=language, case="future_only"):
                self.assert_gate(jobs[:index] + jobs[index + 1:] + [future], (language,))
            jobs[index]["conclusion"] = "failure"
            with self.subTest(language=language, case="current_failure"):
                self.assert_gate(jobs + [future], (language,))
            with self.subTest(language=language, case="current_success"):
                self.assert_gate(self.successful_jobs() + [{**future, "conclusion": "failure"}])

    def test_unsuccessful_and_incomplete_languages_fail(self):
        outcomes = [("completed", conclusion) for conclusion in
                    ("failure", "skipped", "cancelled", "timed_out", "neutral", None)]
        outcomes += [("queued", None), ("in_progress", None)]
        for index, language in enumerate(("actions", "python")):
            for status, conclusion in outcomes:
                jobs = self.successful_jobs()
                jobs[index].update(status=status, conclusion=conclusion)
                with self.subTest(language=language, status=status, conclusion=conclusion):
                    self.assert_gate(jobs, (language,))


if __name__ == "__main__":
    unittest.main()
