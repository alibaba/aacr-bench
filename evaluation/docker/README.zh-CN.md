# Docker 评审

[English](README.md) | 简体中文

使用 `python -m pipeline run --docker`，为每个 PR 在独立容器中准备仓库并生成评审意见。指标评测在宿主执行。

## 构建镜像

按[评测指南](../README.zh-CN.md)安装宿主 Python 依赖并激活环境。Docker 评审需要 Docker CLI 和可用的 Docker 服务。

从仓库根目录执行：

```bash
cd evaluation
python docker/build_image.py --image aacr-review:latest
```

后续命令均在 `evaluation/` 中执行。镜像包含 Python、Git，以及 OCR、Claude Code 和 Codex CLI。Node.js 在构建时下载并安装，默认使用最新长期支持（LTS）版本。

构建助手默认读取 `evaluation/.env` 中以下可选版本字段。终端中非空的同名环境变量优先；未设置或为空时依次使用配置文件中的值和 `latest`。

| 变量 | 默认值 |
| --- | --- |
| `OCR_VERSION` | `latest` |
| `CLAUDE_CODE_VERSION` | `latest` |
| `CODEX_VERSION` | `latest` |

通过 `OCR_VERSION`、`CLAUDE_CODE_VERSION` 或 `CODEX_VERSION` 固定对应评审器的 CLI 版本。Docker 镜像安装所选的全部版本，并用所配置的模型运行对应评审器；其中 Codex 在容器内不使用自身沙箱，容器仍使用只读根文件系统、tmpfs 挂载和资源限制。

指定其他版本文件，或刷新安装缓存：

```bash
python docker/build_image.py --image aacr-review:latest \
  --env-file versions.env --no-cache
```

只有这些版本字段会从文件传给 Docker，凭据和文件本身不会进入镜像。构建缓存可能复用此前安装的 `latest`；需要获取当前版本时使用 `--no-cache`。

构建助手还支持用 `--build-arg` 配置 `NODE_VERSION`、`NPM_REGISTRY`、`APT_MIRROR`、`PIP_INDEX_URL` 和 `INSTALL_OCR` / `INSTALL_CLAUDE` / `INSTALL_CODEX` 等安装选项。将 `INSTALL_OCR`、`INSTALL_CLAUDE` 或 `INSTALL_CODEX` 设为 0 可不安装对应 CLI。关闭 Claude Code 和 Codex 后不安装 Node。按 Docker 主机的架构构建镜像。

修改评审器版本后须重新构建镜像。当前评审器显式指定的非 `latest` 版本必须与镜像中记录的版本一致，否则评审会停止并提示重建。需要复现结果时，使用具体版本并记录镜像 ID。

评审期间关闭评审器的自动更新。

## 配置模型

流水线自动读取 `evaluation/.env`，终端中已设置的同名环境变量优先。配置所选评审器及宿主裁判：

| 角色 | 变量 |
| --- | --- |
| OCR | `OCR_LLM_URL`、`OCR_LLM_TOKEN`、`OCR_LLM_MODEL`；可选 `OCR_USE_ANTHROPIC` |
| Claude Code | `ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_MODEL` |
| Codex | `CODEX_API_KEY`；可选 `CODEX_MODEL`、`CODEX_GATEWAY_URL` |
| 宿主裁判 | `JUDGE_BASE_URL`、`JUDGE_API_KEY`、`JUDGE_MODEL`；可选 `JUDGE_USE_MOCK`、`JUDGE_CONCURRENCY` |

宿主只把当前评审器的模型配置、凭据和可选网络设置（`HF_ENDPOINT`、`NETWORK_TIMEOUT_SECONDS`）传入容器。裁判凭据和参考评审意见留在宿主；环境文件不会挂载进容器。

每个容器自行下载并准备仓库，优先从 Hugging Face 下载快照，不可用时从 GitHub 克隆。容器须能访问仓库来源和评审模型端点，宿主无需准备仓库缓存或归档。

Linux 非 root 调用者会自动映射相同的 UID/GID，便于容器写入输出目录。Linux root 调用者使用镜像用户（UID 1000）；请确保结果目录及其父目录对该用户可写，或改用非 root 用户运行流水线。其他平台使用镜像用户。导出文件可由宿主读取；回拷到宿主的单 PR 结果文件权限为 0600。

## 运行

预览仓库准备，不调用模型：

```bash
python -m pipeline run --docker --stage review --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl --limit 2 \
  --run-id docker-preview --preview
```

仅生成评审意见：

```bash
python -m pipeline run --docker --stage review --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl --limit 2 --run-id docker-review
```

生成评审意见并继续评测：

```bash
python -m pipeline run --docker --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl --limit 2 --run-id docker-baseline
```

默认 `--stage all` 等所有评审任务结束后，在宿主统一评测；评审失败或取消时不自动评测。Docker `--preview` 会跳过评测。

评测已有结果时不带 `--docker`：

```bash
python -m pipeline run --stage eval --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl --limit 2 --run-id docker-review
```

`--docker` 与 `--stage eval` 不能同时使用。评审和评测使用相同的数据集、评审器、run-id 和样本数。`--limit` 按 JSONL 顺序取前 N 条；`--eval-rounds`、`--line-k` 和 `--use-deprecated-score` 保持原来的评测含义。

Docker 评审复用 run-id 时会打印警告，重新评审所有选中的 PR 并覆盖对应结果；不会跳过此前成功的评审，未选中的旧文件保留。重复前核对数据集和 run-id，不向同一结果目录并发运行多个任务。

## 参数与产物

| 参数 | 默认值或含义 |
| --- | --- |
| `--image` | `aacr-review:latest` 或 `AACR_DOCKER_IMAGE`；实际使用的镜像会记录在运行日志中。 |
| `--concurrency` | 默认同时运行 1 个 PR 容器。 |
| `--cpus` / `--memory` | 每容器 1 CPU / 2g；合并内存上限不得超过 Docker 服务总内存的 50%。 |
| `--timeout-minutes` | 评审器超时，默认 30 分钟。 |
| `--ocr-command` / `--max-tools` | 镜像内的 OCR 可执行文件，以及每文件工具调用上限。 |

容器以非 root 用户运行，根文件系统只读，并限制 CPU、内存、进程数和可写临时目录的容量。

默认输出目录：

```text
results/<dataset>/<reviewer>/<run-id>/
metrics/<dataset>/<reviewer>/<run-id>/
```

结果包括单 PR JSON、`summary_<reviewer>.json` 和 `docker_run.json`，各次尝试的日志、容器 ID 和元数据位于运行目录内的 `docker-logs/`。高级参数 `--results-dir` 模式下，日志写入 `<results-dir>/docker-logs/`。

评测已有结果前，先检查评审摘要中的状态。空意见列表可参与评测；预览结果不能用于比较模型表现。缺少 `JUDGE_API_KEY` 或启用 `JUDGE_USE_MOCK` 时使用本地相似度判定，比较模型表现应使用真实裁判。
