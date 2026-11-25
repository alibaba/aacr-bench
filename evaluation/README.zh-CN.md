# AACR-Bench 评测流水线

[English](README.md) | 简体中文

## 目录结构

```text
evaluation/
├── pipeline.py                # 命令入口，执行数据加载、评审和评测
├── config.py                  # 配置变量、默认值和保存路径
├── schema.py                  # JSONL 样本格式与数据检查
├── repo_utils.py              # 仓库获取、缓存和提交检查
├── evaluate.py                # 汇总评审结果，计算并保存指标
├── judge.py                   # 调用裁判进行语义匹配
├── reviewers/                 # 评审器调用与结果收集
│   ├── ocr.py                 # OpenCodeReview
│   ├── claude.py              # Claude Code
│   └── codex.py               # Codex
├── mcp_finding_server.py       # 接收 Claude Code 上报的评审意见
├── mcp_codex_finding_server.py # 接收 Codex 上报的评审意见
├── docker_runner.py           # 调度 Docker 评审任务并保存结果
├── hooks/
│   ├── on_stop_failure.py     # 记录 Claude Code 重试耗尽事件
│   └── collect_report_findings.py # 持久化 ReportFindings 工具调用
├── converters/
│   └── aacr_bench.py          # 将原始数据转换为 JSONL
├── docker/                    # Docker 镜像构建与容器内运行入口
├── benchmark/AACR-Bench/
│   ├── v1/                   # v1 原始数据缓存和下载信息
│   └── v2/                   # v2 原始数据缓存和下载信息
├── data/                     # 转换后的 JSONL 数据
├── repo/                     # 默认仓库缓存
├── snapshots/                # 默认仓库快照压缩包缓存
├── results/                  # 评审结果，按数据集、评审器和运行名称保存
├── metrics/                  # 评测指标，与评审结果使用相同的目录分组
├── .env.example              # 模型配置模板
└── requirements.txt          # Python 依赖及固定版本
```

数据、缓存和结果目录按需创建，图中列出默认位置。

## 流水线

本目录包含数据转换器和代码评审评测程序。转换器先生成 JSONL 文件（每行一个 JSON 对象），评测程序 `pipeline` 再执行以下阶段：

| 阶段 | 内容 | 模型调用 |
| --- | --- | --- |
| 数据加载 | 读取样本、提交范围和参考评审意见，校验格式 | 无 |
| 评审 | 由 OpenCodeReview（OCR）、Claude Code 或 Codex 生成评审意见 | 评审模型；预览时不调用 |
| 裁判评测 | 对照参考意见进行语义匹配，并计算指标 | 真实裁判模型；本地模拟模式不调用 |

评审前优先使用本地仓库或压缩包缓存；缓存不可用时，先下载 Hugging Face 仓库快照，快照不可用时再从 GitHub 克隆对应仓库。缓存目录由程序管理，不应指向开发中的仓库或共享只读目录。

`--docker` 只改变评审阶段，评测始终在宿主机执行。`--stage review` 只评审；`--stage eval` 在宿主机评测已有结果，不能与 `--docker` 同用；默认 `--stage all` 等全部选中样本的评审任务结束后，在宿主统一评测，包括评审失败的样本。取消或致命运行错误会停止流水线。

默认按文件匹配生成意见与参考意见，计算精确率（Precision）、召回率（Recall）和平衡分数（Balanced Score）。生成意见至少匹配一条此前尚未匹配的参考意见，才计入精确率分子。定义见[指标说明](../docs/zh/metrics.md)，数据背景见[项目 README](../README.zh-CN.md)。

## 标准数据格式

数据集采用 **JSONL**，每行一个样本（`ReviewInstance`）。以下示例取自 v2 中的 [browser-use/browser-use PR #1482](https://github.com/browser-use/browser-use/pull/1482)，为便于阅读展开显示：

```json
{
  "instance_id": "browser-use__browser-use@0b21e50",
  "repo": "browser-use/browser-use",
  "base_commit": "fa461585f1fc2d4a430e68e6c115a12f4936fe73",
  "head_commit": "0b21e50ca640581ecd46990475c8b223eaa05045",
  "clone_url": "https://github.com/browser-use/browser-use.git",
  "reference_comments": [
    {
      "path": "browser_use/browser/browser.py",
      "start_line": 290,
      "end_line": 292,
      "side": "right",
      "text": "`self.config.chrome_remote_debugging_port` is typed as `int | None`. If it is `None`, `socket.connect_ex` will raise a `TypeError` because it expects an integer port. Suggest adding a check for `None` or ensuring it is a valid integer before using it here."
    }
  ]
}
```

字段说明：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `instance_id` | 是 | 样本唯一标识，非空字符串；结果文件名为 `instance_id.replace("/", "__") + ".json"` |
| `repo` | 是 | `owner/name` 形式的仓库名称 |
| `base_commit` | 是 | 计算评审范围所用的基准提交 |
| `head_commit` | 是 | 被评审代码状态对应的目标提交 |
| `clone_url` | 否 | 克隆地址，缺省根据 `repo` 生成 GitHub 地址 |
| `reference_comments[]` | 否 | 已标注的参考评审意见，缺省为空数组 |
| `reference_comments[].path` | 是 | 参考意见对应的文件路径，非空字符串 |
| `reference_comments[].start_line` / `end_line` | 否 | 闭区间行号范围；正整数或 `null`，单行时两者相同 |
| `reference_comments[].side` | 否 | `left` 或 `right`，表示代码差异的旧侧或新侧，也可为 `null` |
| `reference_comments[].text` | 是 | 参考意见正文，非空字符串 |

行号只提供一个端点时会补齐另一个，起止顺序相反时会自动调整。默认文件级评分不按行号或 `side` 筛选，行号仅作为裁判上下文；旧逐条评分才按 `side`、行号区间和容差匹配。

加载时检查必填字段、行号和重复 `instance_id`，格式错误会提示所在行。使用 `--limit` 时，只检查已加载的样本；字段定义见 [schema.py](schema.py)。

## 可配置项

### 模型与网络

`pipeline` 启动时自动读取本目录的 `.env`；从终端传入的同名环境变量优先。填写所选评审器和裁判的配置：

| 用途 | 配置变量 |
| --- | --- |
| OCR 评审 | `OCR_LLM_URL`、`OCR_LLM_TOKEN`、`OCR_LLM_MODEL` |
| Claude Code 评审 | `ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_MODEL` |
| Codex 评审 | `CODEX_API_KEY`；可通过 `CODEX_MODEL` 指定模型 |
| 裁判评测 | `JUDGE_BASE_URL`、`JUDGE_API_KEY`、`JUDGE_MODEL` |

| 可选变量 | 含义 |
| --- | --- |
| `OCR_VERSION`、`CLAUDE_CODE_VERSION`、`CODEX_VERSION` | Docker 镜像中的评审器版本；未设置或留空时使用 `latest`，修改后须重建镜像 |
| `OCR_USE_ANTHROPIC` | `true` 使用 Anthropic 协议；`false` 使用 OpenAI 协议 |
| `CLAUDE_CODE_MAX_RETRIES` | Claude Code 重试次数，默认 10 |
| `CODEX_GATEWAY_URL` | 兼容 Codex 的模型网关地址 |
| `CODEX_HOME_BASE`、`CODEX_HOME_BACKUP` | Codex 临时配置目录的上级目录、运行后保存配置的位置 |
| `JUDGE_USE_MOCK` | `true` 时使用本地相似度判定 |
| `JUDGE_CONCURRENCY` | 文件级裁判请求并发数，默认 3 |
| `HF_ENDPOINT` | Hugging Face 服务或镜像地址 |
| `NETWORK_TIMEOUT_SECONDS` | 连接检查和部分下载请求的超时秒数，默认 60；不控制裁判请求或整次运行的总时长 |

未设置 `CODEX_MODEL` 时使用 Codex 默认模型；未设置 `CODEX_HOME_BACKUP` 时，生成的配置目录会在运行后清理。缺少 `JUDGE_API_KEY` 或启用 `JUDGE_USE_MOCK` 会进入本地模拟模式，仅适用于流程校验；评测日志会显示实际裁判模式。

### 运行参数

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `--reviewer` | 必填 | `ocr`、`claude` 或 `codex` |
| `--dataset` | 必填 | 转换后的 JSONL 文件 |
| `--stage` | `all` | `all` 连续评审和评测；`review` 仅评审；`eval` 仅评测 |
| `--docker` | 关闭 | 评审意见生成阶段改在 Docker 容器中执行，结果保存在宿主机；指标评测仍在宿主机完成；不可与 `--stage eval` 同时使用 |
| `--run-id` | 评审时自动生成 | 运行名称；使用默认保存目录且只执行评测时，需要已有的运行名称 |
| `--limit` | 全部 | 取 JSONL 前 N 条样本，使用正整数 |
| `--concurrency` | `1` | 宿主模式为并发评审的仓库组数，同一仓库串行；`--docker` 模式为并发 PR 容器数 |
| `--timeout-minutes` | `30` | 每条样本的评审超时，单位为分钟 |
| `--eval-rounds` | `1` | 裁判评测轮数，使用正整数 |
| `--repo-dir` | `repo/` | 宿主评审的仓库缓存目录，由程序管理；Docker 模式不使用该目录 |
| `--results-dir` | 自动选择 | 评审结果和指标直接保存到指定目录，不按运行名称生成目录 |
| `--ocr-command` | `ocr` | PATH 环境变量可找到的 OCR 命令名或二进制绝对路径；Docker 模式中的路径必须在镜像内可用 |
| `--max-tools` | `30` | 传给 OCR 的每文件工具调用限制；取值范围由 OCR 决定 |
| `--preview` | 关闭 | 检查仓库与差异，不调用评审模型；配合 `--stage review`。Docker 预览自动跳过评测 |
| `--use-deprecated-score` | 关闭 | 使用旧逐条语义及行号评分 |
| `--line-k` | `1` | 仅影响旧评分的行号容差 |

评审进程的超时时间为 `--timeout-minutes` 指定的分钟数，再加额外时间：OpenCodeReview 五分钟，Claude Code/Codex 一分钟。网络超时和评审超时分别设置；`--timeout-minutes` 不控制裁判请求。

### 数据转换

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `--version` | `v1` | 选择 `v1` 或 `v2` 正例数据 |
| `--input` | 版本对应路径 | 原始 JSON 文件 |
| `--output` | 版本对应路径 | JSONL 输出文件 |
| `--limit` | 全部 | 打乱原始数据后取前 N 条 |
| `--seed` | 每次随机 | 固定样本顺序 |
| `--validate` | 关闭 | 检查转换后的数据格式，不判断标注是否正确 |

v1 默认输出 `data/aacr_bench.jsonl`，v2 输出 `data/aacr_bench_v2.jsonl`。原始数据首次使用时可按元数据文件下载并校验；自定义 AACR-Bench 输出文件名应保留 `v1` 或 `v2`，供连接检查识别版本。

## 快速开始

以下示例使用 **v2 数据集和 OpenCodeReview**。获取本仓库源码后，从 `aacr-bench` 目录开始执行。

需要 Python 3.11 及以上，示例使用 Python 3.12。安装 [uv](https://docs.astral.sh/uv/) 和 Git；OpenCodeReview 需要 Git 2.41 及以上。通过 npm 安装评审器时，还需要 Node.js 和 npm。

### 1. 安装

```bash
cd evaluation
uv venv .venv --python 3.12
uv pip install -r requirements.txt
source .venv/bin/activate
npm install -g @alibaba-group/open-code-review
```

示例安装 OpenCodeReview。其他评审器的安装命令如下：

| 评审器 | 安装命令 |
| --- | --- |
| Claude Code | `npm install -g @anthropic-ai/claude-code` |
| Codex | `npm install -g @openai/codex` |

后续命令均在 `evaluation/` 目录中执行，使用已激活的虚拟环境。

### 2. 配置模型

```bash
if [ ! -e .env ] && [ ! -L .env ]; then
  cp -n .env.example .env
fi
chmod 600 .env
```

复制时不会覆盖已有 `.env`。编辑现有文件，按 [.env.example](.env.example) 填写评审模型和裁判模型配置：

| 用途 | 配置变量 |
| --- | --- |
| OpenCodeReview 评审 | `OCR_LLM_URL`、`OCR_LLM_TOKEN`、`OCR_LLM_MODEL`；可选 `OCR_USE_ANTHROPIC` |
| 裁判评测 | `JUDGE_BASE_URL`、`JUDGE_API_KEY`、`JUDGE_MODEL` |

程序会自动读取 `.env`；从终端传入的同名环境变量优先。缺少 `JUDGE_API_KEY` 或设置 `JUDGE_USE_MOCK=true` 时，使用本地相似度模拟判定，仅用于检查流程。比较模型表现时需配置真实裁判模型。

### 3. 转换与预览

```bash
python -m converters.aacr_bench --version v2 --seed 42 --validate

python -m pipeline run --stage review --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl \
  --limit 2 --preview --run-id v2-preview
```

转换时会随机排列样本；对同一份输入数据使用相同的随机种子（如 `--seed 42`），每次得到的样本顺序一致。`--limit 2` 只处理转换后 JSONL 文件（每行一个 JSON 对象）中的前两条样本。重新转换会覆盖输出文件；改变输入或抽样参数时，应该使用新的 `--output` 路径，并保留原实验的数据文件与顺序。预览检查所需提交和代码差异，不调用评审模型；`--stage review` 确保此时不执行裁判评测。

### 4. 评审与评测

```bash
python -m pipeline run --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl \
  --limit 2 --run-id v2-baseline
```

命令先执行评审阶段，调用评审模型生成意见；再执行裁判评测阶段。在真实裁判模式下，后者调用裁判模型进行语义匹配；本地模拟模式只计算相似度。程序随后计算指标，两个阶段均只处理上述前两条样本。

本示例的评审结果保存在 `results/aacr_bench_v2/ocr/v2-baseline/`，指标保存在 `metrics/aacr_bench_v2/ocr/v2-baseline/`。再次评审时使用不同的运行名称，以保留之前的结果。

使用 v1 时，将转换命令改为 `--version v1`，数据路径改为 `data/aacr_bench.jsonl`。配置相应 agent 和模型后，通过 `--reviewer claude` 或 `--reviewer codex` 选择其他评审器。


## 常用操作

### 分阶段运行

先评审，再对已有结果评测：

```bash
python -m pipeline run --stage review --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl --limit 2 --run-id v2-review

python -m pipeline run --stage eval --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl --limit 2 --run-id v2-review
```

两次命令应使用相同的数据文件及顺序、评审器、样本数和运行名称。新的评审实验使用新的名称；再次使用同一名称可能覆盖已有结果。建议名称以英文字母或数字开头和结尾，中间可使用点、下划线或连字符，例如 `v2-review`；评测时使用评审日志显示的运行目录名称。

### 并发与重复评测

评审命令可添加 `--concurrency 4`；实际并发数受所选仓库数限制，裁判并发由 `JUDGE_CONCURRENCY` 单独控制。

需要再次评测已有结果时，可执行：

```bash
python -m pipeline run --stage eval --reviewer ocr \
  --dataset data/aacr_bench_v2.jsonl \
  --limit 2 --run-id v2-review --eval-rounds 1
```

每次命令使用已有评审结果。需要比较不同评测结果时，先保留上一次的指标文件。

### 查看结果

默认输出路径中的 `<dataset>` 是 JSONL 文件名去掉扩展名后的部分：

| 位置 | 内容 |
| --- | --- |
| `results/<dataset>/<reviewer>/<run-name>/` | 单样本评审结果及 `summary_<reviewer>.json` |
| `metrics/<dataset>/<reviewer>/<run-name>/` | 指标汇总和每条样本的匹配结果 |

先核对评审摘要中的样本数和状态，再解读指标；退出码 0 不代表所有样本成功。结果缺失、不可读取、JSON 无法解析或顶层不是对象时计入 `missing_instances` 并排除指标分母；可解析对象中的空意见仍参与评测，已保存的失败或非结构化输出也可能被视为零意见；应根据摘要状态和结果字段识别。裁判错误可能产生空匹配，应核对日志中的裁判模式和错误；模型用量和耗时汇总只统计评审阶段。

命令用法可通过 `python -m pipeline run --help` 和 `python -m converters.aacr_bench --help` 查看。

### Docker 评审

使用 `--docker` 在容器中生成评审意见，指标仍在宿主机计算。镜像构建、版本、资源限制与日志见 [Docker 指南](docker/README.zh-CN.md)。

## FAQ（常见问题）

### 为什么 `missing_instances` 很大？

该字段统计未能加载对应结果的样本，包括文件缺失、无法读取、JSON 解析失败或顶层不是对象。先核对运行名称、数据文件、`--limit` 和评审摘要。已有结果但没有可解析意见的样本仍参与评测，不能仅用缺失数量判断评审是否成功。

### 为什么语义分数全为 0？

先查看日志中的裁判模式，确认 `JUDGE_API_KEY`、`JUDGE_MODEL` 和服务地址配置。使用真实裁判时，也应检查模型调用错误及结构化评审意见是否成功保存。指标文件生成不表示所有裁判调用成功；本地模拟分数也不用于模型质量比较。

### 仓库克隆在哪里？会重复克隆吗？

默认缓存位置为 `repo/<owner__repo>/`，可通过 `--repo-dir` 修改。可用缓存会直接使用；缓存不可用或缺少所需提交时，程序可能重新下载快照或克隆仓库。Docker 评审在每个容器内准备仓库，无需宿主缓存。
