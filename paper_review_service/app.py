"""Authenticated, single-worker HTTP service for persistent PDF review jobs."""

from __future__ import annotations

import fcntl
import hmac
import logging
import os
import re
import shutil
import tempfile
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from .config import Settings
from .store import QueueFull, Store, TERMINAL_STATES, utc_now


logger = logging.getLogger(__name__)
JOB_ID = re.compile(r"^[0-9a-f]{32}$")
ARTIFACTS = {"result.json": "application/json", "report.md": "text/markdown", "run.json": "application/json"}
PUBLIC_JOB_FIELDS = (
    "id", "filename", "cutoff_date", "focus", "created_at", "updated_at",
    "status", "cancel_requested", "error",
)
Runner = Callable[[Settings, dict[str, Any], Callable[[str], None], Callable[[], bool]], dict[str, Any]]


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    result = {field: job[field] for field in PUBLIC_JOB_FIELDS}
    result["artifacts"] = {
        name: f"/jobs/{job['id']}/artifacts/{name}" for name in ARTIFACTS
    } if job["status"] == "succeeded" else {}
    return result


class RequestGuards:
    """Authenticate before parsing uploads and bound streamed multipart bodies."""

    def __init__(self, app: Any, *, service: FastAPI):
        self.app = app
        self.service = service

    async def __call__(self, scope: dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] == "/health" and scope["method"] == "GET":
            await self.app(scope, receive, send)
            return
        settings = getattr(self.service.state, "settings", None)
        if settings is None:
            await JSONResponse({"detail": "Service is not ready"}, status_code=503)(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope["headers"]}
        authorization = headers.get(b"authorization", b"")
        scheme, _, supplied = authorization.partition(b" ")
        if scheme.lower() != b"bearer" or not hmac.compare_digest(supplied, settings.api_token.encode("utf-8")):
            await JSONResponse(
                {"detail": "Invalid or missing bearer token"}, status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )(scope, receive, send)
            return
        maximum = settings.max_upload_bytes + 64 * 1024
        if b"content-length" in headers:
            try:
                length = int(headers[b"content-length"])
                if length < 0:
                    raise ValueError()
            except ValueError:
                await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(scope, receive, send)
                return
            if length > maximum:
                await JSONResponse({"detail": "Upload exceeds size limit"}, status_code=413)(scope, receive, send)
                return
        seen = 0
        response_started = False

        async def bounded_receive() -> dict[str, Any]:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > maximum:
                    raise HTTPException(413, "Upload exceeds size limit")
            return message

        async def tracked_send(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, bounded_receive, tracked_send)
        except HTTPException as exc:
            if response_started:
                raise
            await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(scope, receive, send)


class Worker:
    def __init__(self, settings: Settings, store: Store, runner: Runner, lock_file: Any):
        self.settings = settings
        self.store = store
        self.runner = runner
        self.lock_file = lock_file
        self.stopping = threading.Event()
        self.wakeup = threading.Event()
        self.thread = threading.Thread(target=self._work, name="paper-review-worker", daemon=True)

    @property
    def available(self) -> bool:
        return self.thread.is_alive() and not self.stopping.is_set()

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stopping.set()
        self.wakeup.set()
        self.thread.join(timeout=15)
        if self.thread.is_alive():
            # The worker retains the instance lock until its runner has stopped.
            logger.error("Review worker is still stopping; retaining the data-directory lock")

    def _run_one(self, job: dict[str, Any]) -> None:
        from .runner import ReviewCancelled, ReviewCleanupFailed
        from .auth_state import AuthSyncFailed

        job_id = job["id"]

        def should_cancel() -> bool:
            return self.stopping.is_set() or self.store.cancellation_requested(job_id)

        try:
            if should_cancel():
                raise ReviewCancelled()
            metadata = self.runner(
                self.settings, job,
                lambda status: self.store.update_status(job_id, status),
                should_cancel,
            )
            status = "interrupted" if self.stopping.is_set() else "succeeded"
            self.store.finish(
                job_id, status, metadata=metadata,
                error="service_interrupted" if status == "interrupted" else None,
            )
        except ReviewCleanupFailed:
            # Do not start a second job while the previous container may still run.
            self.stopping.set()
            self.store.finish(job_id, "cleanup_failed", error="container_cleanup_failed")
            logger.exception("Could not stop review container for job %s; queue stopped", job_id)
        except AuthSyncFailed:
            self.stopping.set()
            self.store.finish(job_id, "cleanup_failed", error="auth_sync_failed")
            logger.error("Could not persist refreshed login for job %s; private workspace retained and queue stopped", job_id)
        except ReviewCancelled:
            status = "interrupted" if self.stopping.is_set() else "cancelled"
            self.store.finish(job_id, status, error="service_interrupted" if status == "interrupted" else None)
        except Exception:
            logger.exception("Review job %s failed", job_id)
            status = "interrupted" if self.stopping.is_set() else "failed"
            self.store.finish(job_id, status, error="service_interrupted" if status == "interrupted" else "review_failed")

    def _work(self) -> None:
        try:
            while not self.stopping.is_set():
                self.wakeup.clear()
                job = self.store.claim_next()
                if job is None:
                    self.wakeup.wait(timeout=0.5)
                else:
                    self._run_one(job)
        except Exception:
            logger.exception("Review worker stopped unexpectedly")
        finally:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
            self.lock_file.close()


def create_app(settings: Settings | None = None, runner: Runner | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(service: FastAPI):
        active_settings = settings if settings is not None else Settings.from_env()
        if not active_settings.api_token or not active_settings.api_token.strip():
            raise ValueError("A nonempty API token is required")
        if active_settings.max_upload_bytes < 5 or active_settings.max_pending_jobs < 1:
            raise ValueError("Upload and queue limits must be positive")
        active_settings.data_dir.mkdir(parents=True, exist_ok=True)
        (active_settings.data_dir / "jobs").mkdir(exist_ok=True)
        (active_settings.data_dir / ".uploads").mkdir(exist_ok=True)
        lock_file = (active_settings.data_dir / "worker.lock").open("a+")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock_file.close()
            raise RuntimeError("Another worker already owns this data directory") from exc
        try:
            store = Store(active_settings.data_dir / "jobs.sqlite3")
            store.initialize()
            from .runner import ReviewCleanupFailed, container_name, stop_container
            from .auth_state import recover_auth_workspaces
            for job_id in store.recovery_job_ids():
                try:
                    stop_container(active_settings, container_name(job_id))
                except ReviewCleanupFailed:
                    store.finish(job_id, "cleanup_failed", error="container_cleanup_failed")
                    raise
                recover_auth_workspaces(active_settings.data_dir / ".auth", job_id, active_settings.auth_file)
                store.recover_interrupted(job_id)
            if runner is None:
                from .runner import run_job
                active_runner = run_job
            else:
                active_runner = runner
            worker = Worker(active_settings, store, active_runner, lock_file)
            service.state.settings = active_settings
            service.state.store = store
            service.state.worker = worker
            worker.start()
        except BaseException:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
            raise
        try:
            yield
        finally:
            worker.stop()

    service = FastAPI(title="Personal Paper Review", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    service.add_middleware(RequestGuards, service=service)

    def find_job(job_id: str) -> dict[str, Any]:
        if JOB_ID.fullmatch(job_id) is None:
            raise HTTPException(404, "Job not found")
        job = service.state.store.get(job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        return job

    @service.get("/health")
    def health() -> JSONResponse:
        worker = getattr(service.state, "worker", None)
        available = worker is not None and worker.available
        return JSONResponse(
            {"status": "ok" if available else "unavailable", "worker": "running" if available else "stopped"},
            status_code=200 if available else 503,
        )

    @service.post("/jobs", status_code=202)
    async def submit_job(
        request: Request,
        file: UploadFile = File(...),
        focus: str = Form("", max_length=2000),
        cutoff_date: str | None = Form(None),
    ) -> JSONResponse:
        active_settings = request.app.state.settings
        store = request.app.state.store
        worker = request.app.state.worker
        if not worker.available:
            raise HTTPException(503, "Review worker is unavailable")
        if cutoff_date is None:
            cutoff = date.today().isoformat()
        else:
            try:
                cutoff = date.fromisoformat(cutoff_date).isoformat()
                if cutoff != cutoff_date:
                    raise ValueError()
            except ValueError:
                raise HTTPException(422, "cutoff_date must use YYYY-MM-DD") from None
        if not store.has_capacity(active_settings.max_pending_jobs):
            raise HTTPException(429, "Review queue is full", headers={"Retry-After": "5"})
        job_id = uuid.uuid4().hex
        job_dir = active_settings.data_dir / "jobs" / job_id
        staged_path: Path | None = None
        enqueued = False
        try:
            with tempfile.NamedTemporaryFile(dir=active_settings.data_dir / ".uploads", suffix=".pdf", delete=False) as staged:
                staged_path = Path(staged.name)
                size = 0
                signature = b""
                while chunk := await file.read(64 * 1024):
                    size += len(chunk)
                    if size > active_settings.max_upload_bytes:
                        raise HTTPException(413, "PDF exceeds size limit")
                    if len(signature) < 5:
                        signature += chunk[:5 - len(signature)]
                    staged.write(chunk)
                if signature != b"%PDF-":
                    raise HTTPException(415, "The uploaded file must have a PDF signature")
                staged.flush()
                os.fsync(staged.fileno())
            job_dir.mkdir(mode=0o700)
            os.replace(staged_path, job_dir / "input.pdf")
            filename = re.split(r"[/\\]", file.filename or "paper.pdf")[-1]
            filename = "".join(character for character in filename if character.isprintable())[:255] or "paper.pdf"
            job = {
                "id": job_id, "filename": filename, "focus": focus,
                "cutoff_date": cutoff, "created_at": utc_now(),
            }
            try:
                saved = store.enqueue(job, active_settings.max_pending_jobs)
            except QueueFull:
                raise HTTPException(429, "Review queue is full", headers={"Retry-After": "5"}) from None
            enqueued = True
            worker.wakeup.set()
            return JSONResponse(_public_job(saved), status_code=202, headers={"Location": f"/jobs/{job_id}"})
        finally:
            await file.close()
            if staged_path is not None:
                staged_path.unlink(missing_ok=True)
            if not enqueued and job_dir.exists():
                # These are unaccepted temporary uploads, never an existing job.
                shutil.rmtree(job_dir)

    @service.get("/jobs")
    def list_jobs(limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict[str, Any]:
        return {"jobs": [_public_job(job) for job in service.state.store.list(limit=limit, offset=offset)]}

    @service.get("/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        return _public_job(find_job(job_id))

    @service.post("/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, Any]:
        find_job(job_id)
        job = service.state.store.request_cancel(job_id)
        service.state.worker.wakeup.set()
        assert job is not None
        return _public_job(job)

    @service.get("/jobs/{job_id}/artifacts/{name}")
    def get_artifact(job_id: str, name: str) -> FileResponse:
        job = find_job(job_id)
        if name not in ARTIFACTS:
            raise HTTPException(404, "Artifact not found")
        if job["status"] not in TERMINAL_STATES or (name != "run.json" and job["status"] != "succeeded"):
            raise HTTPException(409, "Artifact is not available for this job state")
        jobs_root = service.state.settings.data_dir / "jobs"
        job_dir = jobs_root / job_id
        path = job_dir / name
        if (
            job_dir.is_symlink() or path.is_symlink()
            or job_dir.resolve().parent != jobs_root.resolve()
            or path.resolve().parent != job_dir.resolve()
            or not path.is_file()
        ):
            raise HTTPException(404, "Artifact not found")
        return FileResponse(path, media_type=ARTIFACTS[name], filename=name, headers={"X-Content-Type-Options": "nosniff"})

    return service


# Environment and credentials are checked on startup, not when tests import us.
app = create_app()
