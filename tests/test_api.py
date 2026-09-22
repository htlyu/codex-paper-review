"""HTTP/queue integration tests; no Codex credentials or Docker are required."""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from paper_review_service.app import create_app
from paper_review_service.config import Settings
from paper_review_service.runner import ReviewCancelled, ReviewCleanupFailed
from paper_review_service.store import Store, utc_now


PDF = b"%PDF-1.7\nsmall fake PDF for the injected runner\n%%EOF"
TOKEN = "test-api-token-never-returned"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeRunner:
    def __init__(self, *, block: bool = False, fail: bool = False):
        self.entered = threading.Event()
        self.release = threading.Event()
        if not block:
            self.release.set()
        self.fail = fail
        self.calls: list[str] = []

    def __call__(self, settings, job, update, should_cancel):
        self.calls.append(job["id"])
        job_dir = settings.data_dir / "jobs" / job["id"]
        if (job_dir / "input.pdf").read_bytes()[:5] != b"%PDF-":
            raise AssertionError("Worker was given a job without a complete input")
        update("reviewing")
        self.entered.set()
        while not self.release.wait(0.01):
            if should_cancel():
                raise ReviewCancelled()
        if should_cancel():
            raise ReviewCancelled()
        (job_dir / "run.json").write_text('{"exit_code": 0}', encoding="utf-8")
        # Deliberately leave partial semantic output on failure: it must be hidden.
        (job_dir / "report.md").write_text("# Review\nA fake review.\n", encoding="utf-8")
        if self.fail:
            raise RuntimeError("SENSITIVE-AUTH-AND-STDOUT")
        update("validating")
        (job_dir / "result.json").write_text('{"summary": "A fake review"}', encoding="utf-8")
        return {"internal_only": "SENSITIVE-AUTH-AND-STDOUT"}


class APITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="paper-review-api-test-")
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def settings(self, **overrides):
        values = {
            "data_dir": self.root / "data",
            "api_token": TOKEN,
            "codex_home": self.root / "codex",
        }
        values.update(overrides)
        return Settings(**values)

    def post(self, client, *, content=PDF, filename="paper.pdf", **form):
        return client.post("/jobs", headers=AUTH, files={"file": (filename, content, "application/pdf")}, data=form)

    def wait_for(self, client, job_id, expected="succeeded"):
        expected_states = {expected} if isinstance(expected, str) else set(expected)
        deadline = time.monotonic() + 4
        last = None
        while time.monotonic() < deadline:
            response = client.get(f"/jobs/{job_id}", headers=AUTH)
            self.assertEqual(response.status_code, 200)
            last = response.json()
            if last["status"] in expected_states:
                return last
            time.sleep(0.01)
        self.fail(f"Job did not reach {expected_states}; last state was {last}")

    def test_authentication_health_and_no_sensitive_response_fields(self):
        runner = FakeRunner()
        with TestClient(create_app(self.settings(), runner)) as client:
            health = client.get("/health")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json(), {"status": "ok", "worker": "running"})
            for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"}):
                self.assertEqual(client.get("/jobs", headers=headers).status_code, 401)
            response = self.post(client)
            self.assertEqual(response.status_code, 202)
            job = self.wait_for(client, response.json()["id"])
            self.assertNotIn("metadata", job)
            self.assertNotIn("internal_only", json.dumps(job))
            listing = client.get("/jobs", headers=AUTH)
            self.assertNotIn(TOKEN, listing.text)
            self.assertNotIn("SENSITIVE", listing.text)
            self.assertEqual(len(listing.json()["jobs"]), 1)
            self.assertEqual(client.get(f"/jobs/{job['id']}/artifacts/report.md").status_code, 401)

    def test_success_lifecycle_and_artifacts(self):
        runner = FakeRunner(block=True)
        with TestClient(create_app(self.settings(), runner)) as client:
            response = self.post(client, focus="Check the ablations", cutoff_date="2026-09-22")
            self.assertEqual(response.status_code, 202)
            accepted = response.json()
            self.assertEqual(accepted["status"], "queued")
            self.assertEqual(response.headers["location"], f"/jobs/{accepted['id']}")
            self.assertTrue(runner.entered.wait(2))
            running = client.get(f"/jobs/{accepted['id']}", headers=AUTH).json()
            self.assertEqual(running["status"], "reviewing")
            self.assertEqual(client.get(f"/jobs/{accepted['id']}/artifacts/report.md", headers=AUTH).status_code, 409)
            runner.release.set()
            finished = self.wait_for(client, accepted["id"])
            self.assertEqual(finished["cutoff_date"], "2026-09-22")
            self.assertEqual(finished["focus"], "Check the ablations")
            self.assertEqual(set(finished["artifacts"]), {"report.md", "result.json", "run.json"})
            for name, url in finished["artifacts"].items():
                artifact = client.get(url, headers=AUTH)
                self.assertEqual(artifact.status_code, 200, name)
                self.assertEqual(artifact.headers["x-content-type-options"], "nosniff")

    def test_pdf_signature_size_and_metadata_validation(self):
        runner = FakeRunner()
        with TestClient(create_app(self.settings(max_upload_bytes=64), runner)) as client:
            self.assertEqual(self.post(client, content=b"not a PDF").status_code, 415)
            self.assertEqual(self.post(client, content=b"").status_code, 415)
            self.assertEqual(self.post(client, content=b"%PDF-" + b"x" * 60).status_code, 413)
            self.assertEqual(self.post(client, focus="x" * 2001).status_code, 422)
            for invalid_date in ("tomorrow", "2026-02-30", "20260922", "2026-9-22"):
                self.assertEqual(self.post(client, cutoff_date=invalid_date).status_code, 422)
            accepted = self.post(client, content=b"%PDF-" + b"x" * 59)
            self.assertEqual(accepted.status_code, 202)
            self.assertEqual(accepted.json()["cutoff_date"], date.today().isoformat())
            self.wait_for(client, accepted.json()["id"])
            self.assertEqual(len(client.get("/jobs", headers=AUTH).json()["jobs"]), 1)
            self.assertEqual(list((self.root / "data" / ".uploads").iterdir()), [])
            self.assertEqual(len(list((self.root / "data" / "jobs").iterdir())), 1)

    def test_streaming_request_limit_without_content_length_and_preparse_auth(self):
        app = create_app(self.settings(max_upload_bytes=64), FakeRunner())

        async def request(*, authenticated, huge):
            messages = []
            consumed = False

            async def receive():
                nonlocal consumed
                consumed = True
                return {"type": "http.request", "body": b"x" * (70 * 1024 if huge else 0), "more_body": False}

            async def send(message):
                messages.append(message)

            headers = [(b"content-type", b"multipart/form-data; boundary=test")]
            if authenticated:
                headers.append((b"authorization", f"Bearer {TOKEN}".encode()))
            scope = {
                "type": "http", "method": "POST", "path": "/jobs", "raw_path": b"/jobs",
                "root_path": "", "query_string": b"", "headers": headers,
                "http_version": "1.1", "scheme": "http", "server": ("testserver", 80),
                "client": ("testclient", 123),
            }
            await app(scope, receive, send)
            return next(item["status"] for item in messages if item["type"] == "http.response.start"), consumed

        with TestClient(app):
            status, consumed = asyncio.run(request(authenticated=False, huge=True))
            self.assertEqual(status, 401)
            self.assertFalse(consumed)
            status, consumed = asyncio.run(request(authenticated=True, huge=True))
            self.assertEqual(status, 413)
            self.assertTrue(consumed)

    def test_queue_limit_and_queued_cancellation(self):
        runner = FakeRunner(block=True)
        with TestClient(create_app(self.settings(max_pending_jobs=2), runner)) as client:
            first = self.post(client).json()
            self.assertTrue(runner.entered.wait(2))
            second = self.post(client).json()
            self.assertEqual(self.post(client).status_code, 429)
            cancelled = client.post(f"/jobs/{second['id']}/cancel", headers=AUTH)
            self.assertEqual(cancelled.status_code, 200)
            self.assertEqual(cancelled.json()["status"], "cancelled")
            self.assertTrue(cancelled.json()["cancel_requested"])
            third = self.post(client)
            self.assertEqual(third.status_code, 202)
            runner.release.set()
            self.wait_for(client, first["id"])
            self.wait_for(client, third.json()["id"])
            self.assertNotIn(second["id"], runner.calls)

    def test_running_cancellation_and_terminal_cancellation_idempotence(self):
        runner = FakeRunner(block=True)
        with TestClient(create_app(self.settings(), runner)) as client:
            job_id = self.post(client).json()["id"]
            self.assertTrue(runner.entered.wait(2))
            response = client.post(f"/jobs/{job_id}/cancel", headers=AUTH)
            self.assertTrue(response.json()["cancel_requested"])
            cancelled = self.wait_for(client, job_id, "cancelled")
            self.assertEqual(cancelled["artifacts"], {})
            self.assertEqual(client.post(f"/jobs/{job_id}/cancel", headers=AUTH).json()["status"], "cancelled")
            self.assertEqual(client.get(f"/jobs/{job_id}/artifacts/report.md", headers=AUTH).status_code, 409)

    def test_failure_hides_partial_report_and_exception_details(self):
        runner = FakeRunner(fail=True)
        with TestClient(create_app(self.settings(), runner)) as client:
            with self.assertLogs("paper_review_service.app", level="ERROR"):
                job_id = self.post(client).json()["id"]
                failed = self.wait_for(client, job_id, "failed")
            self.assertEqual(failed["error"], "review_failed")
            self.assertNotIn("SENSITIVE", json.dumps(failed))
            self.assertEqual(client.get(f"/jobs/{job_id}/artifacts/report.md", headers=AUTH).status_code, 409)
            self.assertEqual(client.get(f"/jobs/{job_id}/artifacts/run.json", headers=AUTH).status_code, 200)

    def test_untrusted_names_and_artifact_path_boundaries(self):
        runner = FakeRunner()
        settings = self.settings()
        with TestClient(create_app(settings, runner)) as client:
            accepted = self.post(client, filename="../../auth.json").json()
            job_id = accepted["id"]
            self.wait_for(client, job_id)
            self.assertRegex(job_id, r"^[0-9a-f]{32}$")
            self.assertEqual(accepted["filename"], "auth.json")
            self.assertEqual((settings.data_dir / "jobs" / job_id / "input.pdf").read_bytes(), PDF)
            for path in (
                "/jobs/not-a-uuid", f"/jobs/{job_id.upper()}", f"/jobs/{job_id}/artifacts/input.pdf",
                f"/jobs/{job_id}/artifacts/auth.json", f"/jobs/{job_id}/artifacts/%2e%2e%2fauth.json",
            ):
                self.assertEqual(client.get(path, headers=AUTH).status_code, 404, path)
            outside = self.root / "private.json"
            outside.write_text("SECRET", encoding="utf-8")
            artifact = settings.data_dir / "jobs" / job_id / "result.json"
            artifact.unlink()
            artifact.symlink_to(outside)
            self.assertEqual(client.get(f"/jobs/{job_id}/artifacts/result.json", headers=AUTH).status_code, 404)
            self.assertEqual(client.get("/jobs?limit=101", headers=AUTH).status_code, 422)
            self.assertEqual(client.get("/jobs?offset=-1", headers=AUTH).status_code, 422)

    def test_restart_interrupts_running_job_and_resumes_queued_job(self):
        settings = self.settings()
        store = Store(settings.data_dir / "jobs.sqlite3")
        store.initialize()
        ids = []
        for _ in range(2):
            job_id = uuid.uuid4().hex
            ids.append(job_id)
            folder = settings.data_dir / "jobs" / job_id
            folder.mkdir(parents=True)
            (folder / "input.pdf").write_bytes(PDF)
            store.enqueue({"id": job_id, "filename": "paper.pdf", "cutoff_date": "2026-09-22", "focus": "", "created_at": utc_now()}, 20)
        self.assertEqual(store.claim_next()["id"], ids[0])
        store.update_status(ids[0], "validating")
        runner = FakeRunner()
        stopped = []

        def stop_known_container(active_settings, name):
            self.assertEqual(store.get(ids[0])["status"], "validating")
            self.assertEqual(runner.calls, [])
            self.assertEqual(active_settings.data_dir, settings.data_dir)
            stopped.append(name)

        with patch("paper_review_service.runner.stop_container", side_effect=stop_known_container):
            with TestClient(create_app(settings, runner)) as client:
                interrupted = self.wait_for(client, ids[0], "interrupted")
                self.assertEqual(interrupted["error"], "service_interrupted")
                self.wait_for(client, ids[1])
                self.assertEqual(runner.calls, [ids[1]])
        self.assertEqual(stopped, ["paper-review-" + ids[0]])

    def test_cleanup_failure_stops_queue_and_is_not_overridden_by_cancellation(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def runner(settings, job, update, should_cancel):
            calls.append(job["id"])
            entered.set()
            self.assertTrue(release.wait(3))
            self.assertTrue(should_cancel())
            raise ReviewCleanupFailed("cannot reach daemon")

        settings = self.settings()
        with TestClient(create_app(settings, runner)) as client:
            first = self.post(client).json()["id"]
            self.assertTrue(entered.wait(2))
            second = self.post(client).json()["id"]
            client.post(f"/jobs/{first}/cancel", headers=AUTH)
            with self.assertLogs("paper_review_service.app", level="ERROR"):
                release.set()
                failed = self.wait_for(client, first, "cleanup_failed")
            self.assertEqual(failed["error"], "container_cleanup_failed")
            self.assertEqual(failed["artifacts"], {})
            self.assertTrue(failed["cancel_requested"])
            self.assertEqual(client.get("/health").status_code, 503)
            self.assertEqual(self.post(client).status_code, 503)
            self.assertEqual(client.get(f"/jobs/{second}", headers=AUTH).json()["status"], "queued")
            self.assertEqual(calls, [first])

    def test_restart_refuses_to_run_queue_until_orphan_cleanup_is_confirmed(self):
        settings = self.settings()
        store = Store(settings.data_dir / "jobs.sqlite3")
        store.initialize()
        ids = [uuid.uuid4().hex for _ in range(2)]
        for job_id in ids:
            folder = settings.data_dir / "jobs" / job_id
            folder.mkdir(parents=True)
            (folder / "input.pdf").write_bytes(PDF)
            store.enqueue({"id": job_id, "filename": "paper.pdf", "cutoff_date": "2026-09-22",
                           "focus": "", "created_at": utc_now()}, 20)
        store.claim_next()
        store.request_cancel(ids[0])
        runner = FakeRunner()
        with patch("paper_review_service.runner.stop_container", side_effect=ReviewCleanupFailed("unavailable")) as stop:
            with self.assertRaises(ReviewCleanupFailed):
                with TestClient(create_app(settings, runner)):
                    self.fail("Cannot start with an unconfirmed running container")
        stop.assert_called_once_with(settings, "paper-review-" + ids[0])
        self.assertEqual(store.get(ids[0])["status"], "cleanup_failed")
        self.assertEqual(store.get(ids[1])["status"], "queued")
        self.assertEqual(runner.calls, [])
        # Retry after Docker recovers: cleanup_failed is included in recovery, and
        # the failed startup must have released the data-directory lock.
        with patch("paper_review_service.runner.stop_container") as stop:
            with TestClient(create_app(settings, runner)) as client:
                self.wait_for(client, ids[0], "interrupted")
                self.wait_for(client, ids[1])
        stop.assert_called_once_with(settings, "paper-review-" + ids[0])
        self.assertEqual(runner.calls, [ids[1]])

    def test_shutdown_marks_running_job_interrupted(self):
        settings = self.settings()
        runner = FakeRunner(block=True)
        with TestClient(create_app(settings, runner)) as client:
            job_id = self.post(client).json()["id"]
            self.assertTrue(runner.entered.wait(2))
        job = Store(settings.data_dir / "jobs.sqlite3").get(job_id)
        self.assertEqual(job["status"], "interrupted")
        replacement = FakeRunner()
        with TestClient(create_app(settings, replacement)) as client:
            self.assertEqual(client.get(f"/jobs/{job_id}", headers=AUTH).json()["status"], "interrupted")
            self.assertEqual(replacement.calls, [])

    def test_same_directory_rejects_second_worker_and_blank_token_rejected(self):
        settings = self.settings()
        with TestClient(create_app(settings, FakeRunner())):
            with self.assertRaisesRegex(RuntimeError, "Another worker"):
                with TestClient(create_app(settings, FakeRunner())):
                    self.fail("Second worker should not start")
        with self.assertRaises(ValueError):
            with TestClient(create_app(self.settings(api_token=" "), FakeRunner())):
                self.fail("Blank API token should not start")


if __name__ == "__main__":
    unittest.main()
