# Docker 评审

[English](README.md) | 简体中文

使用 `python -m pipeline run --docker`，为每个 PR 启动独立容器，在容器中准备仓库并生成评审意见。OCR、Claude Code 和 Codex CLI 已安装在镜像中，宿主无需另行安装。结果和日志保存在宿主，指标评测也在宿主完成。

## 构建镜像

先按[评测指南](../README.zh-CN.md)准备并激活宿主 Python 环境，同时确认 Docker CLI 能够连接 Docker 服务。然后从仓库根目录构建镜像：

```bash
cd evaluation
python docker/build_image.py --image aacr-review:latest
```

后续命令都在 `evaluation/` 目录执行。镜像包含 Python 和 Git；Node.js 在构建时下载并安装，默认使用最新长期支持（LTS）版本。

通过以下可选变量指定评审器 CLI 版本。构建脚本先使用终端中非空的同名变量；未设置或为空时，再读取 `evaluation/.env`；两处都没有值则使用 `latest`：

| 变量 | 默认值 |
| --- | --- |
| `OCR_VERSION` | `latest` |
| `CLAUDE_CODE_VERSION` | `latest` |
| `CODEX_VERSION` | `latest` |

指定具体版本可以固定所用的 CLI。构建时只读取文件中的这三个版本字段，环境文件、凭据和参考评审意见不会进入镜像。

需要使用其他版本文件并刷新安装缓存时：

```bash
python docker/build_image.py --image aacr-review:latest \
  --env-file versions.env --no-cache
```

构建缓存可能保留此前安装的 `latest`，使用 `--no-cache` 可获取当前版本。修改评测代码或评审器版本后应重新构建镜像。如果为当前评审器指定具体版本，该版本必须与镜像中记录的一致，否则评审会停止并提示重建镜像。需要复现结果时，固定版本并记录镜像 ID。评审期间不会自动更新评审器。

构建脚本还支持通过 `--build-arg` 设置 `NODE_VERSION`、`NPM_REGISTRY`、`APT_MIRROR`、`PIP_INDEX_URL` 和 `INSTALL_OCR` / `INSTALL_CLAUDE` / `INSTALL_CODEX`。将相应的 `INSTALL_*` 选项设为 `0`，即可不安装该 CLI。Claude Code 和 Codex 都不安装时，也不会安装 Node.js。镜像应按 Docker 主机的架构构建。

## 配置模型

流水线会自动读取 `evaluation/.env`，终端中已设置的同名变量优先。配置所选评审器，以及评测时使用的宿主裁判：

| 角色 | 变量 |
| --- | --- |
| OCR | `OCR_LLM_URL`、`OCR_LLM_TOKEN`、`OCR_LLM_MODEL`；可选 `OCR_USE_ANTHROPIC` |
| Claude Code | `ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_MODEL`；可选 `CLAUDE_CODE_MAX_RETRIES` |
| Codex | `CODEX_API_KEY`；可选 `CODEX_MODEL`、`CODEX_GATEWAY_URL` |
| 宿主裁判 | `JUDGE_BASE_URL`、`JUDGE_API_KEY`、`JUDGE_MODEL`；可选 `JUDGE_USE_MOCK`、`JUDGE_CONCURRENCY` |

容器接收当前评审器的模型配置和凭据，以及可选网络设置（`HF_ENDPOINT`、`NETWORK_TIMEOUT_SECONDS`）。裁判凭据和参考评审意见留在宿主，环境文件也不会挂载进容器。

每个容器自行准备仓库，先尝试下载 Hugging Face 快照，不可用时再从 GitHub 获取。容器需要能访问这些来源和评审模型端点，宿主无需准备仓库缓存或归档。

## 运行

预览仓库准备，不调用模型：

```bash
python -m pipeline run --docker --stage review --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl --limit 2 \
  --run-id docker-preview --preview
```

仅生成评审意见，完成后停止：

```bash
python -m pipeline run --docker --stage review --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl --limit 2 --run-id docker-review
```

生成评审意见，然后在宿主评测：

```bash
python -m pipeline run --docker --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl --limit 2 --run-id docker-baseline
```

默认 `--stage all` 会等全部评审任务结束后，再评测选中的样本。评审失败时，失败前已经生成的意见仍会保留并进入评测。取消或致命运行错误会停止流水线。Docker `--preview` 会跳过评测。

只重试已有 run 中的失败样本时，在相同 `--run-id` 上增加 `--retry`：

```bash
python -m pipeline run --docker --stage review --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl --limit 2 --run-id docker-review --retry
```

使用与原运行相同的数据集和 `--limit`。`--retry` 要求 run 目录、`--run-id` 和可读取的 `summary_<reviewer>.json` 已存在。它会先把失败样本的结果 JSON（以及可能存在的 MCP `.partial.json`）和 `docker-logs/instances/<attempt>/` 目录归档到 `retry-archive/<timestamp>/`，然后只重新运行失败样本；成功样本保持不变。归档失败时命令会在重跑前停止。`--stage all` 或 `--stage eval` 会在重试后继续评测，但仅当所有选中样本都成功时才进入评测；仍有失败时命令返回非零并跳过评测，修复后可再次执行 `--retry`。该流程允许 `--docker --stage eval --retry`；不带 `--retry` 的 `--docker --stage eval` 仍然无效。

评审过程中，每完成一个样本会输出一行本地时间、样本 ID、状态、收集到的意见数、耗时和总进度：

```text
[14:32:10] [docker] [37/151] owner__repo@abc123 ok findings=6 duration=412.8s
```

只评测已有结果、不重试时，不带 `--docker`：

```bash
python -m pipeline run --stage eval --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl --limit 2 --run-id docker-review
```

除 `--retry` 流程外，`--docker` 不能与 `--stage eval` 同用。评审和评测应使用相同的数据集、评审器、run-id 和样本数。`--limit` 按 JSONL 顺序取前 N 条；`--eval-rounds`、`--line-k` 和 `--use-deprecated-score` 保持原来的评测含义。

复用 run-id 且不带 `--retry` 时只提示警告，随后会重新评审所有选中的 PR，包括此前已成功的 PR，并覆盖相应结果。未选中的旧文件仍留在目录中。复用前请核对数据集和 run-id，不要向同一结果目录并发运行多个任务。

## 参数与产物

| 参数 | 默认值与行为 |
| --- | --- |
| `--image` | `aacr-review:latest` 或 `AACR_DOCKER_IMAGE`；实际使用的镜像会记录在运行日志中。 |
| `--retry` | 关闭。归档并只重跑已有 `--run-id` 中的失败样本，保留成功结果。 |
| `--concurrency` | 同时运行 1 个 PR 容器。 |
| `--cpus` / `--memory` | 每个容器 1 CPU / 2g；并发容器的内存上限总和不得超过 Docker 服务内存的 50%。 |
| `--timeout-minutes` | 评审器超时默认 30 分钟。容器运行时限会自动计入仓库准备、评审器额外时间和结束清理的余量，至少两小时。 |
| `--ocr-command` / `--max-tools` | 镜像内的 OCR 可执行文件，以及每文件工具调用上限。 |

容器以非 root 用户运行，根文件系统只读，并限制 CPU、内存、进程数和可写临时目录的容量。Codex 在容器内不使用自身沙箱，Docker 的限制仍然生效。自定义镜像必须设置非 root `USER`，且不能声明 `VOLUME`。

在 Linux 上，容器使用非 root 调用者的 UID/GID。root 调用者使用镜像用户（UID 1000）；输出目录及其父目录需要允许该用户写入，也可以改用非 root 用户运行流水线。其他平台使用镜像用户。容器导出的文件可在宿主读取，回拷到宿主的单 PR 结果文件权限为 0600。

默认输出目录：

```text
results/<dataset>/<reviewer>/<run-id>/
metrics/<dataset>/<reviewer>/<run-id>/
```

结果目录中包含单 PR JSON、`summary_<reviewer>.json` 和 `docker_run.json`。日志、容器 ID 和运行信息保存在其中的 `docker-logs/` 子目录。使用 `--results-dir` 时，日志写入 `<results-dir>/docker-logs/`。

评审失败时仍可能已经生成部分意见。运行器会把容器内的 `failed-results/<sample>.json` 复制为正式结果文件，写入 `review_status: failed`（有错误时同时写 `review_error`），并保留这些意见参与评测。如果 Claude Code 或 Codex 只留下 MCP `.partial.json`，运行器会用该列表构造结果。无法安全解析的单条意见会被丢弃，避免中断评测。完全没有结果的失败样本写成空意见。`--retry` 会先把失败结果文件、partial 文件和 attempt 目录归档到 `retry-archive/<timestamp>/`，再重新运行。

Claude 的事件流、stderr 日志和可用的 `.collector` hook 文件，在成功和失败时都会保留到 `docker-logs/instances/<attempt>/out/diagnostics/`。结果 JSON 中的路径指向这些文件。缺失或未能导出的诊断会单独记录，不覆盖原来的评审错误。

解读指标前，先检查评审摘要。空意见列表是有效结果，preview 不用于衡量模型表现。未配置 `JUDGE_API_KEY`，或启用 `JUDGE_USE_MOCK` 时，评测使用本地相似度，不调用真实裁判；比较模型表现时应配置真实裁判。
