from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path("var").resolve())
    api_token: str = ""
    model: str = "gpt-6-astra"
    reasoning: str = "xhigh"
    max_upload_bytes: int = 20 * 1024 * 1024
    max_pages: int = 100
    max_pending_jobs: int = 20
    job_timeout_seconds: int = 3600
    max_subagents: int = 6
    codex_bin: str = "codex"
    codex_home: Path = Path("/tmp/codex-home")
    sandbox_mode: str = "danger-full-access"
    auth_file: Path = field(default_factory=lambda: Path.home() / ".codex" / "auth.json")
    docker_bin: str = "docker"
    docker_image: str = "paper-review-worker:local"
    proxy_url: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        def integer(name: str, default: int, minimum: int = 1) -> int:
            value = int(os.environ.get(name, default))
            if value < minimum:
                raise ValueError(f"{name} must be at least {minimum}")
            return value

        token = os.environ.get("REVIEW_API_TOKEN", "")
        if len(token) < 24:
            raise ValueError("Set REVIEW_API_TOKEN to a random token of at least 24 characters")
        return cls(
            data_dir=Path(os.environ.get("REVIEW_DATA_DIR", "var")).expanduser().resolve(),
            api_token=token,
            model=os.environ.get("REVIEW_MODEL", "gpt-6-astra"),
            reasoning=os.environ.get("REVIEW_REASONING", "xhigh"),
            max_upload_bytes=integer("REVIEW_MAX_UPLOAD_BYTES", 20 * 1024 * 1024),
            max_pages=integer("REVIEW_MAX_PAGES", 100),
            max_pending_jobs=integer("REVIEW_MAX_PENDING_JOBS", 20),
            job_timeout_seconds=integer("REVIEW_TIMEOUT_SECONDS", 3600),
            max_subagents=integer("REVIEW_MAX_SUBAGENTS", 6, 0),
            auth_file=Path(os.environ.get("REVIEW_AUTH_FILE", "~/.codex/auth.json")).expanduser().resolve(),
            docker_bin=os.environ.get("REVIEW_DOCKER_BIN", "docker"),
            docker_image=os.environ.get("REVIEW_DOCKER_IMAGE", "paper-review-worker:local"),
            proxy_url=os.environ.get("REVIEW_PROXY_URL", ""),
        )
