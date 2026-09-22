# Codex Paper Review

上传一份 PDF 后，本机 Python API 为该任务启动一个独立 Docker 容器，调用 Codex 检查论文并生成中文问题清单。服务只分析上传的 PDF 和可公开检索的文献，不读取论文的 LaTeX 源码、私有实验数据或整个仓库。报告中的页码均为 **PDF 的物理页码，从 1 开始**，可能与论文印刷页码不同。

Docker 中运行的是 Codex 客户端和 PDF 工具，模型推理在云端进行，需要联网；论文内容会作为审阅输入发送给模型。默认使用 `gpt-6-astra`、`xhigh` 推理强度，依次运行 11 个独立的角色阶段。任务沿用当前 Codex 账户可用的模型访问和额度；配置模型名称不会增加账户权限。Codex 的非交互模式和账户认证方式见 [OpenAI Docs：非交互运行](https://learn.chatgpt.com/docs/non-interactive-mode)与[身份验证](https://learn.chatgpt.com/docs/auth)。

服务面向自己上传的可信 PDF，默认只监听 `127.0.0.1:8787`。它提供 HTTP API，没有上传网站；Swagger `/docs`、`/redoc` 和 OpenAPI JSON 均关闭。服务不用于多用户或公网服务。结果是作者自查材料；对于无法从 PDF 或公开来源确认的问题，仍需要作者检查原始证据。

## ScholarPeer 角色流程

提示词参考 [ScholarPeer v2 附录 G](https://arxiv.org/pdf/2601.22638v2)，按 CC BY 4.0 改编为作者自查用途。每个阶段都实际启动一次独立的 `codex exec`，读取公共约束、对应角色 prompt 和指定上游 JSON：

1. 中立摘要与主张提取。
2. 文献初查 → 文献补查 → 领域发展脉络。
3. 独立基线与数据集检查，仅接收摘要，避免被前面的文献结论引导。
4. 分别生成新颖性问题与技术问题。
5. 分别调查新颖性答案与技术答案。
6. 独立反证复核所有候选问题 → 综合最终报告。

共 11 次独立调用，按依赖顺序串行执行，禁用嵌套子代理。各阶段有明确的联网设置和固定权重的时间预算；这不是 PAT 的难度自适应预算算法。文本引文、页码、来源字段和反证处置经过程序校验；最终报告只能原样选取反证阶段保留的问题，不能重新加入已撤回的问题。语义判断和真实视觉查看仍依赖模型，字段校验不能证明判断正确。

保留了 ScholarPeer 的角色分工与先提问后调查流程，去掉固定问题/文献配额、会场白名单、“没搜到即高创新”和“不确定即拒稿”等规则；新增独立反证步骤，不输出录用分数。附录 H 的评估 prompts 依赖人工审稿或成对评审数据，不放入论文自查主流程。具体 prompt、来源与每项改动见[角色映射](docs/scholarpeer-prompts.md)和[第三方归属声明](THIRD_PARTY_NOTICES.md)。本项目不声称完整复现 ScholarPeer 或 Google PAT。

## 准备与启动

本机需要 Python 3.11 或更新版本、正在运行的 Docker，以及 Docker Compose 插件。在 macOS 上，可使用已经配置好的 Docker Desktop 或 Colima。先确认 `docker info` 和 `docker compose version` 能正常执行。

本机还需有可读写的 `~/.codex/auth.json`，其中包含当前 Codex 登录状态。服务会在**每次启动任务时**重新读取这个文件，并保留 Codex 自动刷新的登录状态。若登录仅存在于系统钥匙串而没有该文件，需要先按 [OpenAI 官方身份验证说明](https://learn.chatgpt.com/docs/auth)配置本机的文件凭据存储并完成登录。不要手动把凭据复制进服务代码目录或镜像。

克隆仓库并安装依赖：

```sh
git clone https://github.com/htlyu/codex-paper-review.git
cd codex-paper-review
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
docker compose build review-worker
```

镜像名为 `paper-review-worker:local`。Compose 只负责构建工作容器镜像；本机 API 会按任务创建容器，无需运行 `docker compose up`。

生成本机配置和随机 API token；以下命令不会覆盖已有 `.env`：

```sh
.venv/bin/python - <<'PY'
from pathlib import Path
import secrets

target = Path('.env')
if target.exists():
    raise SystemExit('.env 已存在，请保留或手动编辑。')
template = Path('.env.example').read_text(encoding='utf-8')
target.write_text(
    template.replace('REVIEW_API_TOKEN=', 'REVIEW_API_TOKEN=' + secrets.token_urlsafe(32), 1),
    encoding='utf-8',
)
target.chmod(0o600)
PY
```

`REVIEW_API_TOKEN` 是保护本机 HTTP API 的独立随机值，不是 OpenAI API key。该值必须至少 24 个字符。保留 `.env` 在本机，不提交到仓库。

启动 API 并保持这个终端运行：

```sh
.venv/bin/uvicorn paper_review_service.app:app \
  --env-file .env \
  --host 127.0.0.1 \
  --port 8787 \
  --workers 1
```

使用单 worker；不要加 `--reload`。在另一个终端检查服务：

```sh
curl --fail-with-body http://127.0.0.1:8787/health
```

健康检查仅说明 API 可响应。完整任务还需要 Docker 镜像、有效凭据、网络和所选模型的访问权限。

## 上传、查询与下载

最方便的方式是在本目录使用命令行客户端，它自动读取 `.env`：

```sh
.venv/bin/python -m paper_review_service submit /absolute/path/paper.pdf --wait
```

完成后，三份结果保存在 `var/downloads/<任务 id>/`；用 `--output /path/to/results` 可以指定目录。
`--focus '重点检查实验设计'` 和 `--cutoff-date 2026-09-22` 可指定审阅要求。
不加 `--wait` 会提交后立即返回任务 id；停止等待不会取消后台任务。

```sh
.venv/bin/python -m paper_review_service list
.venv/bin/python -m paper_review_service status <任务id>
.venv/bin/python -m paper_review_service cancel <任务id>
.venv/bin/python -m paper_review_service download <任务id>
```

也可以直接调用 HTTP API。

以下命令在仓库根目录执行。先从本机 `.env` 读取 API token：

```sh
review_token="$(.venv/bin/python -c 'from dotenv import dotenv_values; print(dotenv_values(".env")["REVIEW_API_TOKEN"])')"
```

上传 PDF，把路径换成自己的文件；`focus` 是可选的专项检查要求，`cutoff_date` 是可选的文献检索截止日期（`YYYY-MM-DD`，省略时使用本机当天日期）：

```sh
curl --fail-with-body \
  -H "Authorization: Bearer $review_token" \
  -F 'file=@/absolute/path/paper.pdf;type=application/pdf' \
  -F 'focus=重点检查主张与实验支持是否对应，以及关键引用是否准确' \
  -F 'cutoff_date=2026-09-22' \
  http://127.0.0.1:8787/jobs
```

响应包含任务 `id` 和状态。把返回的 `id` 填入以下变量，再查询进度：

```sh
review_id='填入任务 id'
curl --fail-with-body \
  -H "Authorization: Bearer $review_token" \
  "http://127.0.0.1:8787/jobs/$review_id"
```

等待状态变为 `succeeded` 后下载报告和结构化结果：

```sh
curl --fail-with-body \
  -H "Authorization: Bearer $review_token" \
  "http://127.0.0.1:8787/jobs/$review_id/artifacts/report.md" \
  -o report.md

curl --fail-with-body \
  -H "Authorization: Bearer $review_token" \
  "http://127.0.0.1:8787/jobs/$review_id/artifacts/result.json" \
  -o result.json

curl --fail-with-body \
  -H "Authorization: Bearer $review_token" \
  "http://127.0.0.1:8787/jobs/$review_id/artifacts/run.json" \
  -o run.json
```

`report.md` 用于阅读，`result.json` 保存结构化审阅结果，`run.json` 记录本次运行信息，以及 11 个阶段的输入依赖、prompt 摘要、耗时、CLI 用量和已校验的中间结果。报告和结构化结果仅在成功后可下载；失败任务若已生成 `run.json`，可下载该文件排查原因。取消尚未结束的任务：

```sh
curl --fail-with-body -X POST \
  -H "Authorization: Bearer $review_token" \
  "http://127.0.0.1:8787/jobs/$review_id/cancel"
```

| 接口 | 用途 |
| --- | --- |
| `GET /health` | 无需 token 的健康检查 |
| `POST /jobs` | multipart 上传 `file`，可带 `focus`、`cutoff_date` |
| `GET /jobs` | 查看任务列表，可带 `limit`、`offset` |
| `GET /jobs/{id}` | 查询任务状态及失败原因 |
| `POST /jobs/{id}/cancel` | 请求取消任务 |
| `GET /jobs/{id}/artifacts/{name}` | 下载 `report.md`、`result.json` 或 `run.json` |

除健康检查外，上述接口均需要 `Authorization: Bearer <REVIEW_API_TOKEN>`。

## 配置与运行边界

修改 `.env` 后重启本机 API。保持在服务目录启动，可使默认相对数据路径稳定。

| 环境变量 | 默认值／示例 | 含义 |
| --- | --- | --- |
| `REVIEW_API_TOKEN` | 必填随机值 | 本机 API 身份验证 |
| `REVIEW_DATA_DIR` | `./var` | 本机任务文件、报告及 SQLite 状态的目录 |
| `REVIEW_AUTH_FILE` | `~/.codex/auth.json` | 每次任务启动时读取的本机凭据文件 |
| `REVIEW_DOCKER_IMAGE` | `paper-review-worker:local` | 已构建的任务镜像 |
| `REVIEW_DOCKER_BIN` | `docker` | 本机 Docker CLI 路径 |
| `REVIEW_PROXY_URL` | 空（直接连接） | 可选的既有网络代理；宿主机代理可填 `http://host.docker.internal:7897` |
| `REVIEW_MODEL` | `gpt-6-astra` | 所有角色阶段使用的模型 |
| `REVIEW_REASONING` | `xhigh` | 推理强度 |
| `REVIEW_MAX_SUBAGENTS` | `6` | 保留的旧配置；当前角色流程禁用嵌套子代理，该值不影响阶段数量 |
| `REVIEW_TIMEOUT_SECONDS` | `3600` | 每任务最长运行时间（秒） |
| `REVIEW_MAX_UPLOAD_BYTES` | `20971520` | PDF 文件大小上限（20 MiB） |
| `REVIEW_MAX_PAGES` | `100` | PDF 页数上限 |
| `REVIEW_MAX_PENDING_JOBS` | `20` | 未结束的任务数量上限，包含当前执行中的任务 |

默认每份文件最多 20 MiB、100 页，不接受加密 PDF。一次只执行一个任务；每次任务依次启动 11 个独立 Codex 进程，同一时刻只运行一个角色。多阶段流程通常比单次调用耗时更多；总预算不足或中间产物未通过校验时，任务会失败并保存阶段状态。API 的任务状态保存在本机。超时或取消会停止任务容器；如果 Docker 无法确认停止，任务会进入 `cleanup_failed`，队列停止接新任务。重启时先确认本任务库的遗留容器已停止，再恢复排队任务；之前中断的任务不会自动重跑，需要重新上传。

每个任务的 Docker 容器采用以下边界：

- 挂载当前任务目录到 `/work`，可写；不挂载其他任务、整个仓库或用户主目录。
- 从本机 `~/.codex/auth.json` 读取快照，放入任务目录以外的私有临时目录，将该目录挂载为 `/codex-home`。初始只复制这个登录文件；不挂载本机的整个 `~/.codex`。凭据不包含在报告下载接口中。
- 设置 `CODEX_HOME=/codex-home`，允许 Codex 原子更新自己的临时登录文件。任务完成、取消或超时后，主机比较最初的文件摘要：本机内容未变时保存刷新的登录状态；已变时保留本机版本。正常结束后删除私有临时目录；异常退出时保留，重启确认容器停止后恢复。持久化失败时也保留私有目录并停止队列。
- 以本机用户的 UID/GID 运行，根文件系统只读，`/tmp` 使用 1 GiB tmpfs；移除 Linux capabilities，开启 `no-new-privileges` 与进程回收；限制为 2 CPU、4 GiB 内存、512 个进程。不挂载 Docker socket。

Codex 在这个隔离容器中使用 `danger-full-access` 和 `approval_policy=never`，以执行 PDF 工具及检索所需命令。该设置不应直接用于未经隔离的主机进程。相关字段以 [OpenAI 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)为准。镜像固定安装 `@openai/codex@0.155.1`，升级版本后应重新验证参数兼容性。

登录状态的持久化遵循 [OpenAI 的 CI/CD 认证说明](https://learn.chatgpt.com/docs/auth/ci-cd-auth)。服务会串行处理自己的任务，并在写回前两次核对本机内容；独立运行的其他 Codex 客户端并不参与服务的写入锁，因此同时刷新同一登录仍可能发生冲突。若出现 `host_changed` 或刷新失效，应以本机重新登录后的文件为准，避免把同一旧凭据复制给多台机器长期独立刷新。本机 API 应以自己的普通用户身份运行。

## 故障排查与验证

- **API 返回身份验证错误**：检查请求中的 token 是否与运行中 API 加载的 `.env` 一致；修改配置后需重启 API。
- **Docker 连接失败或找不到镜像**：确认 Docker/Colima 正在运行，并在本目录执行 `docker compose build review-worker`。
- **找不到登录文件或登录过期**：确认本机 `~/.codex/auth.json` 存在且当前用户可读，在本机更新登录后提交新任务。失败任务不会自动无限重试；服务会按上述规则保留正常的凭据刷新。
- **模型不可用、额度不足或网络错误**：查看任务状态中的失败原因，检查当前账户权限、用量及 Docker 网络。
- **容器直连返回 403、而本机通过代理可以访问**：将既有代理地址填入 `REVIEW_PROXY_URL` 后重启 API。容器里的 `127.0.0.1` 指向容器自身；宿主机代理使用 `host.docker.internal`。该设置不改变系统代理。
- **达到时间、内存或 PDF 限制**：查看失败原因，减少输入范围或调整任务配置后重新提交。

更多诊断信息保存在本机 `REVIEW_DATA_DIR/jobs/<任务 id>/` 下：`container.log` 记录容器启动与退出信息，`preparation.log` 记录 PDF 预处理，`stages/<阶段名>/codex-stderr.log` 记录各阶段 Codex 错误；同目录的 `prompt.md`、`schema.json`、`result.json`、`events.jsonl` 保留实际输入、输出约束、原始结果及工具事件。`workflow.json` 持续更新阶段进度，最终合并到可下载的 `run.json`。未到相应阶段时，部分文件可能尚未生成。这些内部日志不通过下载接口提供。

2026-09-22 已在 macOS / Apple Silicon / Colima 环境中完成真实云端联调：使用本机登录文件、`gpt-6-astra` / `xhigh`，上传一页合成 PDF，11 个独立阶段全部执行成功，耗时约 557 秒。技术回答与独立反证均确认“30 题答对 24 题，却报告 90%”的矛盾；实际应为 80%。三份结果均通过 HTTP 下载，`run.json` 包含 11 份阶段结果及 CLI 用量；任务容器和私有凭据副本正常清理。

这个合成样例将文献、基线和新颖性检查限定为不适用，验证的是阶段调用、数据传递、数字核查、复核和报告生成，不能证明真实文献搜索质量、复杂论文审查质量或与 PAT 等效。实测之后仅细化了反证阶段无法查看证据页时的暂撤说明，该降级分支另有回归测试。

44 项服务测试通过，覆盖队列、鉴权、上传限制、证据核验、取消、超时、异常恢复、凭据刷新、代理参数，以及 11 阶段的 prompt 注入、依赖传递、新增主张、反证与最终结果约束。复跑：

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
```
