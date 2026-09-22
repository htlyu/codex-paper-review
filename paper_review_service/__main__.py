"""Local command-line client: python -m paper_review_service submit paper.pdf --wait."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
import uuid

from dotenv import load_dotenv


def main() -> int:
    parser = argparse.ArgumentParser(description="本机 PDF 论文检查服务客户端")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--url", default="http://127.0.0.1:8787")
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit", help="上传 PDF")
    submit.add_argument("pdf", type=Path)
    submit.add_argument("--focus", default="")
    submit.add_argument("--cutoff-date")
    submit.add_argument("--wait", action="store_true", help="等待完成并下载结果")
    submit.add_argument("--output", type=Path)
    commands.add_parser("list", help="列出最近任务")
    commands.add_parser("health", help="查看服务状态")
    for action in ("status", "cancel", "download"):
        command = commands.add_parser(action)
        command.add_argument("id")
        if action == "download":
            command.add_argument("--output", type=Path)
    args = parser.parse_args()
    load_dotenv(args.env_file)
    token = os.environ.get("REVIEW_API_TOKEN", "")
    if args.command != "health" and not token:
        parser.error("请先配置 .env 中的 REVIEW_API_TOKEN")

    def request(path: str, data: bytes | None = None, content_type: str | None = None) -> bytes:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(args.url.rstrip("/") + path, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.read()

    def download(identifier: str) -> None:
        output = args.output or Path("var/downloads") / identifier
        output.mkdir(parents=True, exist_ok=True)
        for name in ("report.md", "result.json", "run.json"):
            # The service validates job state and only serves these three artifacts.
            content = request(f"/jobs/{identifier}/artifacts/{name}")
            target = output / name
            if target.exists():
                if target.read_bytes() == content:
                    continue
                raise ValueError(f"输出文件已存在且内容不同：{target}；请指定新的 --output 目录")
            with target.open("xb") as handle:
                handle.write(content)
        print(f"报告已保存：{(output / 'report.md').resolve()}")

    try:
        if args.command == "submit":
            boundary = "paper-review-" + uuid.uuid4().hex
            parts = []
            for name, value in (("focus", args.focus), ("cutoff_date", args.cutoff_date)):
                if value is not None:
                    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
            filename = args.pdf.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/pdf\r\n\r\n'.encode())
            parts.extend((args.pdf.read_bytes(), f"\r\n--{boundary}--\r\n".encode()))
            job = json.loads(request("/jobs", b"".join(parts), f"multipart/form-data; boundary={boundary}"))
            print(f"任务：{job['id']}", flush=True)
            previous = None
            while True:
                if job["status"] != previous:
                    print(f"状态：{job['status']}", flush=True)
                    previous = job["status"]
                if not args.wait:
                    return 0
                if job["status"] == "succeeded":
                    download(job["id"])
                    return 0
                if job["status"] in {"failed", "cancelled", "interrupted", "cleanup_failed"}:
                    print(json.dumps(job, ensure_ascii=False, indent=2))
                    return 1
                time.sleep(2)
                job = json.loads(request(f"/jobs/{job['id']}"))
        elif args.command == "download":
            download(args.id)
        else:
            path = {"health": "/health", "list": "/jobs"}.get(args.command)
            if path is None:
                path = f"/jobs/{args.id}" + ("/cancel" if args.command == "cancel" else "")
            result = request(path, b"" if args.command == "cancel" else None)
            print(json.dumps(json.loads(result), ensure_ascii=False, indent=2))
        return 0
    except urllib.error.HTTPError as error:
        print(f"服务返回 HTTP {error.code}：{error.read(4096).decode(errors='replace')}", file=sys.stderr)
        return 1
    except (OSError, ValueError, urllib.error.URLError) as error:
        print(f"操作失败：{error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已停止等待；后台任务仍会继续，可使用 cancel <任务 id> 取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
