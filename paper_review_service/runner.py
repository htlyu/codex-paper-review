"""Host runner: one disposable Codex container and one fresh auth snapshot per job."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Callable

from .config import Settings
from .auth_state import AuthSyncFailed, auth_workspace, sync_refreshed_auth


class ReviewFailed(RuntimeError):
    pass


class ReviewCancelled(RuntimeError):
    pass


class ReviewCleanupFailed(ReviewFailed):
    """Docker could not confirm that the job container has stopped."""
    preserve_auth = True


def container_name(identifier: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise ReviewFailed("Invalid job identifier")
    return "paper-review-" + identifier


def job_directory(settings: Settings, identifier: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise ReviewFailed("Invalid job identifier")
    root = (settings.data_dir / "jobs").resolve()
    path = (root / identifier).resolve()
    if path.parent != root:
        raise ReviewFailed("Job directory is outside the job store")
    return path


def docker_command(settings: Settings, work: Path, auth: Path, name: str) -> list[str]:
    command = [settings.docker_bin, "run", "--rm", "--init", "--name", name,
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "512", "--memory", "4g", "--cpus", "2",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=1073741824,mode=1777",
            "--mount", f"type=bind,source={work},target=/work",
            "--mount", f"type=bind,source={auth.parent},target=/codex-home",
            "--env", "CODEX_HOME=/codex-home", "--env", "HOME=/tmp",
            "--workdir", "/work"]
    if settings.proxy_url:
        command += ["--add-host", "host.docker.internal:host-gateway"]
        for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "NO_PROXY"):
            command += ["--env", key]
    return command + [settings.docker_image, "python", "-m", "paper_review_service.container_job"]


def stop_container(settings: Settings, name: str) -> None:
    try:
        removed = subprocess.run([settings.docker_bin, "rm", "--force", name],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
        if removed.returncode == 0:
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    # --rm may have removed a naturally exited container before our rm request.
    # A successful exact-name inventory distinguishes this from a daemon failure.
    try:
        remaining = subprocess.run(
            [settings.docker_bin, "container", "ls", "--all", "--filter", f"name=^/{name}$",
             "--format", "{{.Names}}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5, check=False,
        )
        if remaining.returncode == 0 and not remaining.stdout.strip():
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    raise ReviewCleanupFailed("Cannot confirm the job container stopped; restore Docker and restart the service")


def run_job(settings: Settings, job: dict, update: Callable[[str], None],
            should_cancel: Callable[[], bool]) -> dict:
    work = job_directory(settings, job["id"])
    if should_cancel():
        raise ReviewCancelled("Cancelled before start")
    update("preparing")
    original_hash = hashlib.sha256((work / "input.pdf").read_bytes()).hexdigest()
    try:
        # Read anew for every job; no long-lived bind to the host's replaceable inode.
        with settings.auth_file.open("rb") as handle:
            credentials = handle.read(1024 * 1024 + 1)
        if len(credentials) > 1024 * 1024 or not isinstance(json.loads(credentials), dict):
            raise ValueError("invalid auth document")
    except (OSError, ValueError) as error:
        raise ReviewFailed("Cannot read a valid Codex auth.json; run codex login on the host") from error
    payload = {"id": job["id"], "filename": job["filename"], "focus": job.get("focus", ""),
               "cutoff_date": job["cutoff_date"], "model": settings.model, "reasoning": settings.reasoning,
               "max_pages": settings.max_pages, "max_subagents": settings.max_subagents,
               "timeout_seconds": settings.job_timeout_seconds, "input_sha256": original_hash}
    (work / "job.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    secret_parent = settings.data_dir / ".auth"
    secret_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = container_name(job["id"])
    started = time.monotonic()
    initial_auth_hash = hashlib.sha256(credentials).hexdigest()
    metadata = None
    with auth_workspace(secret_parent, job["id"]) as private:
        auth = Path(private) / "auth.json"
        auth.write_bytes(credentials)
        auth.chmod(0o600)
        (Path(private) / "initial-sha256").write_text(initial_auth_hash)
        del credentials
        process = None
        try:
            with (work / "container.log").open("wb") as output:
                environment = os.environ.copy()
                if settings.proxy_url:
                    environment.update({key: settings.proxy_url for key in
                                        ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")})
                    environment["NO_PROXY"] = "localhost,127.0.0.1,::1"
                process = subprocess.Popen(docker_command(settings, work, auth, name),
                                           stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                                           env=environment)
                last_stage = "preparing"
                while process.poll() is None:
                    if should_cancel():
                        raise ReviewCancelled("Cancelled")
                    if time.monotonic() - started > settings.job_timeout_seconds:
                        raise ReviewFailed("Review exceeded its time limit")
                    try:
                        stage = json.loads((work / "progress.json").read_text()).get("stage")
                        if stage in {"preparing", "reviewing", "validating"} and stage != last_stage:
                            update(stage)
                            last_stage = stage
                    except (OSError, ValueError):
                        pass
                    time.sleep(0.2)
                if should_cancel():
                    raise ReviewCancelled("Cancelled")
                if process.returncode != 0:
                    raise ReviewFailed("Container review failed; inspect its local container.log and run.json")
            if hashlib.sha256((work / "input.pdf").read_bytes()).hexdigest() != original_hash:
                raise ReviewFailed("Input PDF changed during review")
            metadata = json.loads((work / "run.json").read_text(encoding="utf-8"))
            if metadata.get("status") != "succeeded":
                raise ReviewFailed("Container did not produce a validated report")
            for filename in ("result.json", "report.md", "run.json"):
                path = work / filename
                if path.is_symlink() or not path.is_file():
                    raise ReviewFailed("Missing or unsafe output artifact")
            return metadata
        except OSError as error:
            raise ReviewFailed("Docker or a required job file is unavailable") from error
        finally:
            # A terminated docker client does not terminate its container.
            # Reap the client first so it cannot keep issuing run/start requests.
            if process is not None:
                try:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                finally:
                    stop_container(settings, name)
            try:
                auth_sync = sync_refreshed_auth(settings.auth_file, auth, initial_auth_hash)
            except (OSError, ValueError) as error:
                raise AuthSyncFailed("Could not persist refreshed login; private auth workspace retained") from error
            if metadata is not None:
                metadata["auth_sync"] = auth_sync
                (work / "run.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
