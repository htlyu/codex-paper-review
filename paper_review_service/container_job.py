"""Runs only inside the disposable review container, never as an unsandboxed host agent."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .models import Report, render_markdown, validate_report


def write_json(path: Path, data: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def codex_command(work: Path, job: dict) -> list[str]:
    command = ["codex", "exec", "--ignore-user-config", "--ignore-rules", "--strict-config",
            "--skip-git-repo-check", "--ephemeral", "--color", "never",
            "--sandbox", "danger-full-access", "--model", job["model"],
            "-c", 'approval_policy="never"', "-c", 'cli_auth_credentials_store="file"',
            "-c", f'model_reasoning_effort={json.dumps(job["reasoning"])}',
            "-c", 'web_search="live"', "-c", "features.multi_agent=true",
            "-c", f'agents.enabled={str(job["max_subagents"] > 0).lower()}',
            "-c", f'agents.default_subagent_model={json.dumps(job["model"])}',
            "-c", f'agents.default_subagent_reasoning_effort={json.dumps(job["reasoning"])}',
            "-c", "features.apps=false", "-c", "features.plugins=false",
            "-c", "allow_login_shell=false", "--cd", str(work), "--json",
            "--output-schema", str(work / "report-schema.json"),
            "--output-last-message", str(work / "model-result.json"), "-"]
    if job["max_subagents"] > 0:
        command[2:2] = ["-c", f'agents.max_concurrent_threads_per_session={job["max_subagents"]}']
    return command


def run_process(command: list[str], work: Path, timeout: float, *, prompt: str | None = None,
                stdout_name: str = "preparation.log", stderr_name: str | None = None) -> None:
    with (work / stdout_name).open("ab") as stdout:
        with (work / (stderr_name or stdout_name)).open("ab") as stderr:
            process = subprocess.Popen(command, cwd=work, stdin=subprocess.PIPE if prompt else subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr, start_new_session=True)
            try:
                process.communicate(prompt.encode() if prompt else None, timeout=max(1, timeout))
            except BaseException:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise
            if process.returncode:
                raise RuntimeError(f"{Path(command[0]).name} exited with code {process.returncode}")


def read_usage(path: Path) -> dict:
    usage = {}
    if path.exists():
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                    if event.get("type") == "turn.completed":
                        usage = event.get("usage", {})
                except (ValueError, AttributeError):
                    continue
    return usage


def main() -> int:
    if not Path("/.dockerenv").exists():
        print("container_job must run inside Docker", file=sys.stderr)
        return 2
    work = Path("/work")
    job = json.loads((work / "job.json").read_text(encoding="utf-8"))
    started = time.monotonic()
    metadata = {"status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
                "model": job["model"], "reasoning": job["reasoning"],
                "model_selection": "Explicit Codex CLI configuration; not independent server attestation",
                "input_sha256": job["input_sha256"], "cutoff_date": job["cutoff_date"],
                "max_subagents": job["max_subagents"], "page_count": None}

    def remaining() -> float:
        return job["timeout_seconds"] - (time.monotonic() - started)

    try:
        auth_home = Path(os.environ.get("CODEX_HOME", "/codex-home"))
        auth_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not (auth_home / "auth.json").is_file():
            raise ValueError("Missing per-job Codex auth snapshot")
        (auth_home / "auth.json").chmod(0o600)
        metadata["codex_version"] = subprocess.check_output(["codex", "--version"], text=True, timeout=10).strip()
        write_json(work / "progress.json", {"stage": "preparing"})
        run_process([sys.executable, "-m", "paper_review_service.prepare_pdf", str(work), str(job["max_pages"])],
                    work, min(90, remaining()))
        pages = json.loads((work / "pages.json").read_text(encoding="utf-8"))
        metadata["page_count"] = len(pages)
        run_process(["pdftoppm", "-png", "-scale-to", "1600", str(work / "input.pdf"), str(work / "pages/page")],
                    work, min(120, remaining()))
        schema = Report.model_json_schema()
        write_json(work / "report-schema.json", schema)
        instructions = Path(__file__).with_name("review_prompt.md").read_text(encoding="utf-8")
        metadata["prompt_sha256"] = hashlib.sha256(instructions.encode()).hexdigest()
        request = "\n\n本次任务参数（作者提供）：\n" + json.dumps(
            {"focus": job.get("focus", ""), "literature_cutoff": job["cutoff_date"],
             "physical_page_count": len(pages), "model": job["model"], "reasoning": job["reasoning"],
             "max_subagents": job["max_subagents"]}, ensure_ascii=False)
        (work / "prompt.md").write_text(instructions + request, encoding="utf-8")
        write_json(work / "progress.json", {"stage": "reviewing"})
        run_process(codex_command(work, job), work, remaining(), prompt=instructions + request,
                    stdout_name="events.jsonl", stderr_name="codex-stderr.log")
        write_json(work / "progress.json", {"stage": "validating"})
        report = Report.model_validate_json((work / "model-result.json").read_text(encoding="utf-8"))
        errors = validate_report(report, pages)
        if hashlib.sha256((work / "input.pdf").read_bytes()).hexdigest() != job["input_sha256"]:
            errors.append("Input PDF changed during review")
        if errors:
            metadata["validation_errors"] = errors
            raise ValueError("Report failed evidence/coverage validation")
        write_json(work / "result.json", report.model_dump())
        (work / "report.md").write_text(render_markdown(report, metadata), encoding="utf-8")
        metadata["status"] = "succeeded"
    except Exception as error:
        metadata["status"] = "failed"
        # Do not serialize validation input or subprocess environment (could contain private data).
        metadata["error_type"] = type(error).__name__
        print(f"Review failed ({type(error).__name__}); inspect local preparation/Codex logs.", file=sys.stderr)
    finally:
        metadata["finished_at"] = datetime.now(timezone.utc).isoformat()
        metadata["elapsed_seconds"] = round(time.monotonic() - started, 2)
        metadata["parent_turn_usage"] = read_usage(work / "events.jsonl")
        metadata["usage_note"] = "CLI-reported parent turn usage; do not assume it includes all subagent consumption"
        write_json(work / "run.json", metadata)
    return 0 if metadata["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
