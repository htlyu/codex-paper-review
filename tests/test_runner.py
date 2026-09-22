from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid

from paper_review_service.config import Settings
from paper_review_service.container_job import codex_command
from paper_review_service.models import Report, validate_report
from paper_review_service.runner import (
    ReviewCancelled, ReviewCleanupFailed, ReviewFailed, docker_command, run_job, stop_container,
)


class EvidenceTests(unittest.TestCase):
    def report(self):
        return Report.model_validate({
            "summary": "A narrow test", "strengths": [], "issues": [{
                "id": "R1", "title": "A question", "category": "method", "severity": "major",
                "status": "unresolved", "confidence": "medium", "page": 1,
                "evidence_kind": "text", "evidence": "A claim under assumptions.",
                "problem": "A question", "impact": "A consequence", "countercheck": "Looked at the definition",
                "next_action": "Clarify assumption", "source_urls": []}],
            "coverage": [{"area": "method", "status": "partial", "details": "First page"}],
            "reviewed_pages": [1], "visual_pages": [], "unreviewed_pages": [2], "sources": [], "limitations": []})

    def test_valid_quote_and_explicit_partial_coverage(self):
        self.assertEqual(validate_report(self.report(), [{"text": "A claim\nunder assumptions."}, {"text": "Next page"}]), [])

    def test_rejects_invented_quote_invalid_page_and_unsupported_source(self):
        report = self.report()
        report.issues[0].evidence = "Invented words"
        report.issues[0].source_urls = ["https://example.org/not-read"]
        report.reviewed_pages = [1, 3]
        errors = validate_report(report, [{"text": "A claim under assumptions."}, {"text": ""}])
        self.assertTrue(any("quotation" in error for error in errors))
        self.assertTrue(any("external evidence" in error for error in errors))
        self.assertTrue(any("partition" in error for error in errors))

    def test_visual_evidence_requires_declared_visual_inspection(self):
        report = self.report()
        report.issues[0].evidence_kind = "visual"
        self.assertTrue(validate_report(report, [{"text": ""}, {"text": ""}]))
        report.visual_pages = [1]
        self.assertEqual(validate_report(report, [{"text": ""}, {"text": ""}]), [])

    def test_model_and_subagents_are_explicit(self):
        command = codex_command(Path("/work"), {"model": "gpt-6-astra", "reasoning": "xhigh", "max_subagents": 6})
        self.assertIn('agents.default_subagent_model="gpt-6-astra"', command)
        self.assertIn('agents.default_subagent_reasoning_effort="xhigh"', command)
        self.assertIn("agents.max_concurrent_threads_per_session=6", command)
        disabled = codex_command(Path("/work"), {"model": "gpt-6-astra", "reasoning": "xhigh", "max_subagents": 0})
        self.assertIn("agents.enabled=false", disabled)
        self.assertFalse(any("max_concurrent_threads" in arg for arg in disabled))

    def test_stage_command_uses_its_schema_result_and_search_policy(self):
        output = Path("/work/stages/technical_answers")
        command = codex_command(Path("/work"),
                                {"model": "gpt-6-astra", "reasoning": "xhigh", "max_subagents": 0},
                                output=output, web_search=False)
        self.assertEqual(command[command.index("--output-schema") + 1], str(output / "schema.json"))
        self.assertEqual(command[command.index("--output-last-message") + 1], str(output / "result.json"))
        self.assertIn('web_search="disabled"', command)
        self.assertIn("agents.enabled=false", command)


class DockerRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.auth = self.root / "auth.json"
        self.auth.write_text('{"test_credential": "first"}')
        self.fake = self.root / "docker"
        self.fake.write_text('''#!/usr/bin/env python3
import sys, json, time
from pathlib import Path
args=sys.argv[1:]
root=Path(__file__).parent
mode=(root/'mode').read_text() if (root/'mode').exists() else ''
marker=root/'container-running'
if args[0]=='rm':
    if mode=='rm_fail':
        raise SystemExit(1)
    marker.unlink(missing_ok=True)
    raise SystemExit(0)
if args[:2]==['container','ls']:
    if marker.exists():
        print(marker.read_text())
    raise SystemExit(0)
mounts=[args[i+1] for i,arg in enumerate(args) if arg=='--mount']
work=Path(next(m.split('source=',1)[1].split(',target=',1)[0] for m in mounts if 'target=/work' in m))
auth=Path(next(m.split('source=',1)[1].split(',target=',1)[0] for m in mounts if 'target=/codex-home' in m))/'auth.json'
token=json.loads(auth.read_text())['test_credential']
if mode in {'refresh', 'refresh_block'}:
    auth.write_text(json.dumps({'test_credential':'refreshed'}))
if mode in {'block','rm_fail','refresh_block'}:
    marker.write_text(args[args.index('--name')+1])
    time.sleep(60)
(work/'run.json').write_text(json.dumps({'status':'succeeded','test_value':token}))
(work/'result.json').write_text('{}')
(work/'report.md').write_text('Test report')
''')
        self.fake.chmod(0o700)
        self.settings = Settings(data_dir=self.root / "data", api_token="a" * 32,
                                 auth_file=self.auth, docker_bin=str(self.fake))

    def job(self):
        identifier = uuid.uuid4().hex
        directory = self.settings.data_dir / "jobs" / identifier
        directory.mkdir(parents=True)
        (directory / "input.pdf").write_bytes(b"%PDF-test")
        return {"id": identifier, "filename": "paper.pdf", "focus": "", "cutoff_date": "2026-09-22"}

    def test_reads_latest_auth_each_job_without_publishing_it(self):
        first = self.job()
        self.assertEqual(run_job(self.settings, first, lambda _: None, lambda: False)["test_value"], "first")
        # Atomic replacement, as a host login refresh may do.
        replacement = self.auth.with_suffix(".new")
        replacement.write_text('{"test_credential": "second"}')
        replacement.replace(self.auth)
        second = self.job()
        self.assertEqual(run_job(self.settings, second, lambda _: None, lambda: False)["test_value"], "second")
        self.assertFalse(list((self.settings.data_dir / ".auth").iterdir()))
        self.assertFalse((self.settings.data_dir / "jobs" / first["id"] / "auth.json").exists())
        self.assertEqual(json.loads(self.auth.read_text())["test_credential"], "second")

    def test_cancelled_before_start_does_not_read_auth_or_launch(self):
        with patch("paper_review_service.runner.subprocess.Popen") as popen:
            with self.assertRaises(ReviewCancelled):
                run_job(self.settings, self.job(), lambda _: None, lambda: True)
            popen.assert_not_called()

    def test_container_token_refresh_survives_success_and_cancellation(self):
        for mode in ("refresh", "refresh_block"):
            with self.subTest(mode=mode):
                self.auth.write_text('{"test_credential": "first"}')
                (self.root / "mode").write_text(mode)
                if mode == "refresh":
                    result = run_job(self.settings, self.job(), lambda _: None, lambda: False)
                    self.assertEqual(result["auth_sync"], "refreshed")
                else:
                    with self.assertRaises(ReviewCancelled):
                        run_job(self.settings, self.job(), lambda _: None, (self.root / "container-running").exists)
                self.assertEqual(json.loads(self.auth.read_text())["test_credential"], "refreshed")
                self.assertFalse(list((self.settings.data_dir / ".auth").iterdir()))

    def test_auth_failure_does_not_disclose_content(self):
        self.auth.write_text("THIS_IS_NOT_A_VALID_SECRET_DOCUMENT")
        with self.assertRaises(ReviewFailed) as error:
            run_job(self.settings, self.job(), lambda _: None, lambda: False)
        self.assertNotIn("THIS_IS_NOT", str(error.exception))

    def test_running_cancellation_removes_container_after_client_exits(self):
        (self.root / "mode").write_text("block")
        marker = self.root / "container-running"
        with self.assertRaises(ReviewCancelled):
            run_job(self.settings, self.job(), lambda _: None, marker.exists)
        # The fake client leaves daemon state behind when terminated; only rm removes it.
        self.assertFalse(marker.exists())
        self.assertFalse(list((self.settings.data_dir / ".auth").iterdir()))

    def test_running_timeout_removes_container(self):
        (self.root / "mode").write_text("block")
        settings = replace(self.settings, job_timeout_seconds=1)
        with self.assertRaisesRegex(ReviewFailed, "time limit"):
            run_job(settings, self.job(), lambda _: None, lambda: False)
        self.assertFalse((self.root / "container-running").exists())

    def test_failed_rm_cannot_report_successful_cancellation(self):
        (self.root / "mode").write_text("rm_fail")
        marker = self.root / "container-running"
        with self.assertRaises(ReviewCleanupFailed):
            run_job(self.settings, self.job(), lambda _: None, marker.exists)
        self.assertTrue(marker.exists())

    def test_rm_timeout_and_unavailable_daemon_is_cleanup_failure(self):
        with patch("paper_review_service.runner.subprocess.run", side_effect=[
            subprocess.TimeoutExpired("docker rm", 15), OSError("daemon unavailable"),
        ]):
            with self.assertRaises(ReviewCleanupFailed):
                stop_container(self.settings, "test-container")

    def test_failed_rm_accepts_confirmed_absent_container(self):
        (self.root / "mode").write_text("rm_fail")
        stop_container(self.settings, "already-auto-removed")

    def test_proxy_uses_environment_keys_without_putting_url_in_argv(self):
        settings = replace(self.settings, proxy_url="http://private:password@host.docker.internal:7897")
        command = docker_command(settings, self.root / "work", self.auth, "test-container")
        self.assertIn("host.docker.internal:host-gateway", command)
        self.assertIn("HTTPS_PROXY", command)
        self.assertNotIn(settings.proxy_url, " ".join(command))

    def test_no_socket_whole_home_or_api_token_in_container_command(self):
        command = docker_command(self.settings, self.root / "work", self.root / "private/auth.json", "test")
        self.assertNotIn(self.settings.api_token, " ".join(command))
        self.assertFalse(any("docker.sock" in item for item in command))
        self.assertIn("--read-only", command)
        self.assertEqual(command.count("--mount"), 2)


if __name__ == "__main__":
    unittest.main()
