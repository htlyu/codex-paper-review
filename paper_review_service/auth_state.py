"""Persist Codex token refreshes without restoring an already superseded host seed."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import shutil
from contextlib import contextmanager


class AuthSyncFailed(RuntimeError):
    preserve_auth = True


@contextmanager
def auth_workspace(parent: Path, identifier: str):
    private = Path(tempfile.mkdtemp(prefix=f"job-{identifier}-", dir=parent))
    preserve = False
    try:
        yield private
    except BaseException as error:
        preserve = getattr(error, "preserve_auth", False)
        raise
    finally:
        if not preserve:
            shutil.rmtree(private)


def recover_auth_workspaces(parent: Path, identifier: str, host: Path) -> None:
    """Call only after confirming this job's Docker container is stopped."""
    for private in parent.glob(f"job-{identifier}-*"):
        if private.is_symlink() or not private.is_dir():
            raise AuthSyncFailed("Unsafe retained auth workspace")
        digest = (private / "initial-sha256").read_text().strip()
        sync_refreshed_auth(host, private / "auth.json", digest)
        shutil.rmtree(private)


def sync_refreshed_auth(host: Path, snapshot: Path, initial_sha256: str) -> str:
    """The service lock serializes its writes; other Codex clients do not share it."""
    refreshed = snapshot.read_bytes()
    if len(refreshed) > 1024 * 1024 or not isinstance(json.loads(refreshed), dict):
        raise ValueError("Codex produced an invalid auth document")
    if hashlib.sha256(refreshed).hexdigest() == initial_sha256:
        return "unchanged"
    lock = host.with_name(".paper-review-auth.lock")
    descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if hashlib.sha256(host.read_bytes()).hexdigest() != initial_sha256:
            return "host_changed"
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".paper-review-auth-", dir=host.parent, delete=False) as output:
                temporary = Path(output.name)
                output.write(refreshed)
                output.flush()
                os.fsync(output.fileno())
            # Check again after preparing the replacement, as a host login may have completed.
            if hashlib.sha256(host.read_bytes()).hexdigest() != initial_sha256:
                return "host_changed"
            os.replace(temporary, host)
            return "refreshed"
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
